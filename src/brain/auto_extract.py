#!/usr/bin/env python3
"""Auto-extract entities from raw session files.

Improvements over the original per-file loop:

  * batched: up to BATCH_SIZE sessions go to one LLM call
  * prefiltered: tool-noise stripped before the call (5–10× token savings)
  * cached entity-name list: no longer reads 216 KB index.md per call
  * write-through to SQLite/FTS5 via apply_extraction
  * single git commit per run instead of one per file

Falls back to per-file mode automatically if batch parsing fails, so
extraction never gets *worse* than the previous behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if os.environ.get("BRAIN_EXTRACTING"):
    sys.exit(0)

import brain.config as config
from brain.apply_extraction import apply_extraction
from brain.git_ops import commit
from brain.io import atomic_write_text
from brain.index import rebuild_index
from brain.log import append_log
from brain.prefilter import filter_session_text

# Per-run caps — large enough to drain a backlog quickly, small enough
# to keep wall-clock under launchd's timeout window.
MAX_FILES_PER_RUN = 30
BATCH_SIZE = 10
LLM_TIMEOUT_SEC = 300

CACHE_FILE = config.BRAIN_DIR / ".entity-names.cache"


# ---------------------------------------------------------------------------
# entity-name cache (replaces full-index parsing on every call)
# ---------------------------------------------------------------------------

def _build_entity_name_cache() -> str:
    """Walk entities/ once and produce a compact `## type\n- name` listing.

    Used as the `{existing_entities}` block sent to the extractor so it
    reuses canonical names. Cached on disk; refreshed when index.md is
    newer than the cache file (cheap heuristic — index.md is rebuilt
    whenever entities change).
    """
    config.ENTITY_TYPES.update(config._discover_entity_types())
    sections: list[str] = []
    for type_key in sorted(config.ENTITY_TYPES):
        type_dir = config.ENTITY_TYPES[type_key]
        if not type_dir.exists():
            continue
        names: list[str] = []
        for f in type_dir.glob("*.md"):
            if f.name.startswith("_"):
                continue
            name = f.stem.replace("-", " ").title()
            head = f.read_text(errors="replace")[:400]
            for line in head.split("\n"):
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
            names.append(name)
        if names:
            sections.append(f"## {type_key}")
            sections.extend(f"- {n}" for n in sorted(names))
    return "\n".join(sections) if sections else "(empty brain)"


def get_existing_index() -> str:
    """Return cached entity-name list; rebuild if stale.

    Guarantees the brain owner's ``people/`` anchor entity exists
    before the cache is built — without it the extractor has nowhere
    to attach self-facts like ``"Son ate bún riêu"``. Idempotent after
    first run.
    """
    try:
        from brain.self_entity import ensure_self_entity
        created = ensure_self_entity()
        if created is not None:
            # Invalidate the cache so the new owner entity shows up in
            # the list sent to the extractor on this very run.
            try:
                CACHE_FILE.unlink()
            except FileNotFoundError:
                pass
    except Exception:
        pass  # never let anchor-init failure block extraction

    try:
        cache_mtime = CACHE_FILE.stat().st_mtime
        index_mtime = config.INDEX_FILE.stat().st_mtime if config.INDEX_FILE.exists() else 0
        if cache_mtime >= index_mtime and CACHE_FILE.stat().st_size > 0:
            return CACHE_FILE.read_text()
    except FileNotFoundError:
        pass
    text = _build_entity_name_cache()
    atomic_write_text(CACHE_FILE, text)
    return text


# ---------------------------------------------------------------------------
# discovery + retry tracking
# ---------------------------------------------------------------------------

def get_pending_files() -> list[Path]:
    if not config.RAW_DIR.exists():
        return []
    files = sorted(config.RAW_DIR.glob("session-*.md"), key=lambda f: f.stat().st_mtime)
    return files[:MAX_FILES_PER_RUN]


def get_retry_count(raw_file: Path) -> int:
    rm = raw_file.with_suffix(".retries")
    if not rm.exists():
        return 0
    try:
        return int(rm.read_text().strip())
    except (ValueError, OSError):
        return 0


def increment_retry(raw_file: Path) -> None:
    rm = raw_file.with_suffix(".retries")
    atomic_write_text(rm, str(get_retry_count(raw_file) + 1))


def cleanup_file(raw_file: Path) -> None:
    raw_file.unlink(missing_ok=True)
    raw_file.with_suffix(".retries").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# LLM call — two backends:
#   1. Direct Anthropic SDK (when ANTHROPIC_API_KEY is set, ~3-5x faster
#      because no subprocess + auth dance every call). Opt-in to keep
#      offline/CLI-only setups working unchanged.
#   2. claude CLI in print mode (default fallback).
# ---------------------------------------------------------------------------

ANTHROPIC_MODEL = os.environ.get("BRAIN_ANTHROPIC_MODEL", "claude-haiku-4-5")
ANTHROPIC_MAX_TOKENS = int(os.environ.get("BRAIN_ANTHROPIC_MAX_TOKENS", "8192"))


def _call_anthropic_sdk(prompt: str, timeout: int) -> str | None:
    """Direct REST call via the anthropic SDK. Returns text or None."""
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(timeout=timeout)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        # Concatenate all text blocks (typical: a single block).
        out = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                out.append(text)
        return "".join(out).strip() if out else None
    except Exception as exc:
        print(f"anthropic SDK error: {exc}", file=sys.stderr)
        return None


def call_claude(prompt: str, timeout: int = LLM_TIMEOUT_SEC) -> str | None:
    # Prefer the SDK when an API key is present — much lower per-call latency.
    if os.environ.get("ANTHROPIC_API_KEY"):
        out = _call_anthropic_sdk(prompt, timeout)
        if out is not None:
            return out
        # SDK failed (network, model name, etc.) — fall through to CLI.

    try:
        env = {**os.environ, "BRAIN_EXTRACTING": "1"}
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--no-session-persistence"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        print(f"claude exit={result.returncode} stderr={result.stderr[:500]!r}", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"claude call timed out after {timeout}s", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("claude binary not found on PATH", file=sys.stderr)
        return None


def parse_extraction(raw_output: str) -> dict | None:
    text = raw_output.strip()
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}") + 1
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e])
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

def _load(name: str) -> str:
    return (Path(__file__).parent / "prompts" / name).read_text()


# ---------------------------------------------------------------------------
# batched processing
# ---------------------------------------------------------------------------

def _build_batch_prompt(files: list[Path], existing: str) -> tuple[str, dict[str, Path]]:
    """Return (prompt, session_id → file mapping)."""
    template = _load("extract_batch.md")
    blocks = []
    sid_map: dict[str, Path] = {}
    for f in files:
        sid = f.stem  # e.g. session-2026-04-19-070753-a221d11e
        sid_map[sid] = f
        body = filter_session_text(f.read_text(errors="replace"))
        blocks.append(f"### SESSION {sid}\n{body}\n--- END SESSION {sid} ---")
    sessions_block = "\n\n".join(blocks)
    prompt = template.replace("{existing_entities}", existing).replace(
        "{sessions_block}", sessions_block
    )
    return prompt, sid_map


def _process_batch(files: list[Path], existing: str) -> dict[str, str]:
    """Run one batched LLM call. Returns {file_stem: status}."""
    prompt, sid_map = _build_batch_prompt(files, existing)
    output = call_claude(prompt)
    statuses: dict[str, str] = {}
    if not output:
        for f in files:
            if get_retry_count(f) >= 2:
                cleanup_file(f)
                statuses[f.stem] = "llm_fail_dropped"
            else:
                increment_retry(f)
                statuses[f.stem] = "llm_fail_retry"
        return statuses

    parsed = parse_extraction(output)
    if not isinstance(parsed, dict) or "results" not in parsed:
        # Fall back to per-file mode rather than dropping the whole batch
        for f in files:
            statuses[f.stem] = _process_single(f, existing)
        return statuses

    seen_sids = set()
    for entry in parsed.get("results", []):
        sid = entry.get("session_id")
        if not sid or sid not in sid_map:
            continue
        seen_sids.add(sid)
        try:
            apply_extraction(
                {
                    "entities": entry.get("entities", []),
                    "corrections": entry.get("corrections", []),
                },
                source_label=sid,
                do_commit=False,
                do_rebuild_index=False,
            )
            cleanup_file(sid_map[sid])
            statuses[sid] = "ok"
        except Exception as e:
            print(f"apply failed for {sid}: {e}", file=sys.stderr)
            statuses[sid] = "apply_fail"

    # any session the LLM silently dropped → fall back to single-file
    for sid, f in sid_map.items():
        if sid not in seen_sids:
            statuses[sid] = _process_single(f, existing)
    return statuses


def _process_single(raw_file: Path, existing: str) -> str:
    """Per-file fallback (the original behaviour, minus the per-call commit)."""
    template = _load("extract_session.md")
    body = filter_session_text(raw_file.read_text(errors="replace"))
    try:
        from brain.triple_rules import rules_for_prompt as _rp
        triple_rules = _rp()
    except Exception:
        triple_rules = ""
    prompt = (template
        .replace("{existing_entities}", existing)
        .replace("{conversation_summary}", body)
        .replace("{triple_rules}", triple_rules or "(no learned rules yet)")
    )
    output = call_claude(prompt)
    if not output:
        if get_retry_count(raw_file) >= 2:
            cleanup_file(raw_file)
            return "llm_fail_dropped"
        increment_retry(raw_file)
        return "llm_fail_retry"
    extraction = parse_extraction(output)
    if not isinstance(extraction, dict):
        cleanup_file(raw_file)
        return "parse_fail_dropped"
    apply_extraction(
        extraction,
        source_label=raw_file.stem,
        do_commit=False,
        do_rebuild_index=False,
    )
    cleanup_file(raw_file)
    return "ok"


def main():
    # GC phantom entity rows BEFORE bailing on no-work. Without this, ghost
    # rows (e.g. from a leaked test fixture or a manual `rm` on an entity
    # file) accumulate indefinitely between successful extractions —
    # post_extraction_sync only runs when an extraction succeeds, so an
    # idle scheduler never reconciles. Cheap (one SELECT path + N stat()s).
    try:
        from brain.db import gc_orphaned_entities
        removed = gc_orphaned_entities()
        if removed:
            print(f"  gc: removed {len(removed)} phantom entity row(s)", flush=True)
    except Exception as exc:
        print(f"  gc skipped: {exc}", flush=True)

    pending = get_pending_files()
    if not pending:
        print("No pending files in ~/.brain/raw/")
        return

    existing = get_existing_index()
    total = len(pending)
    counts: dict[str, int] = {}
    print(f"Extracting {total} file(s) in batches of {BATCH_SIZE}...", flush=True)

    for start in range(0, total, BATCH_SIZE):
        chunk = pending[start:start + BATCH_SIZE]
        statuses = _process_batch(chunk, existing)
        for sid, st in statuses.items():
            counts[st] = counts.get(st, 0) + 1
            print(f"  {sid} → {st}", flush=True)

    # one rebuild + one commit for the entire run
    if counts.get("ok", 0) > 0:
        rebuild_index()
        # refresh the entity-name cache so the next run sees new entities
        try:
            CACHE_FILE.unlink()
        except FileNotFoundError:
            pass
        # Post-extraction sync: GC phantom/untracked index entries and
        # requeue any note-sourced facts whose source note changed.
        try:
            from brain.verify import post_extraction_sync
            sync = post_extraction_sync()
            parts = []
            if sync["gc_removed"] or sync["gc_added"]:
                parts.append(f"gc -/+{sync['gc_removed']}/{sync['gc_added']}")
            if sync["notes_requeued"]:
                parts.append(f"requeued {sync['notes_requeued']} note(s)")
            if sync.get("semantic_rebuilt"):
                parts.append("semantic drift → rebuilt")
            if parts:
                print(f"  verify: {', '.join(parts)}", flush=True)
        except Exception as exc:
            print(f"  verify sync skipped: {exc}", flush=True)
        # Refresh semantic index so MCP recall sees the new facts.
        # Failure here is non-fatal — semantic is a perf layer, not source of truth.
        #
        # Routes through the persistent semantic worker (warm sentence-
        # transformers model, ~100 ms incremental embed) instead of the
        # old in-process `semantic.build()` (~10 s cold-load + ~1.6 s
        # full rebuild = ~11.5 s wasted per batch). When the worker
        # socket is missing the helper falls back to the in-process
        # incremental path; only when *that* also fails do we re-raise
        # into the except below.
        try:
            from brain import semantic
            semantic.update_facts_entities_via_worker()
        except Exception as exc:
            print(f"  semantic refresh skipped: {exc}", flush=True)
        summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        append_log("extract-batch", summary)
        # Auto-managed allowlist (entities/, log.md, index.md, etc.) —
        # never `git add -A` from a scheduled job. See git_ops.commit.
        commit(f"brain: batch extract — {summary}")

    summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    print(f"Done. {summary}")


if __name__ == "__main__":
    main()
