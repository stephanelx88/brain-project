#!/usr/bin/env python3
"""Extract entity facts from user-authored vault notes.

Companion to `auto_extract.py` (which handles session transcripts).
Where session extraction asks "what did Claude+user discuss?", note
extraction asks "what fact did the user explicitly write down?".

The crucial difference: every fact this module produces is recorded
in `fact_provenance` linked to the source note path. When the user
later deletes the note, `ingest_notes.invalidate_facts_for_note`
strikethroughs the matching facts in entity files — so the brain
forgets what the user forgot, instead of holding stale knowledge
forever.

Gating is by `notes.extracted_sha`: a note is "pending" when its
current sha differs from the sha we last processed. New notes have
extracted_sha=NULL → always pending. Edits bump sha → re-extract.
Idempotent: calling `process_pending` twice with no vault changes
does zero LLM work.

Run via `python -m brain.note_extract` (CLI) or imported by the
auto-extract launchd tick.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if os.environ.get("BRAIN_EXTRACTING"):
    sys.exit(0)

import brain.config as config
from brain import db
from brain.apply_extraction import apply_extraction
from brain.auto_extract import (
    call_claude,
    get_existing_index,
    parse_extraction,
)
from brain.git_ops import commit
from brain.index import rebuild_index
from brain.ingest_notes import invalidate_facts_for_note
from brain.log import append_log

PROMPT_FILE = Path(__file__).parent / "prompts" / "extract_note.md"

# Per-tick caps. Note bodies are short (~hundreds of bytes), so we
# can do more per run than session extraction without blowing the
# launchd 5-minute window.
MAX_NOTES_PER_RUN = 30
MIN_BODY_CHARS = 0  # 0 = also extract from title-only notes (often the highest-signal ones)
MAX_BODY_CHARS = 60_000  # truncate giant user dumps (e.g. pasted spec PDFs) before LLM call
LLM_TIMEOUT_SEC = 120

# Note extraction targets *user-authored* notes only. The brain itself
# writes into playground/, timeline/, identity/corrections.md, log.md,
# research-log.md — those are derivatives, never sources. Sending them
# back through extraction would produce echo facts and hide the actual
# user signal. Keep this list tight; new auto-managed dirs go here.
EXCLUDED_DIR_PREFIXES: tuple[str, ...] = (
    "playground",
    "timeline",
    "identity",
    "chats",
    "logs",
    "_archive",
)
EXCLUDED_PATHS: tuple[str, ...] = (
    "log.md",
    "index.md",
    "research-log.md",
    "recall-ledger.jsonl",
    "README.md",
    # System-managed files rendered by bin/install.sh into the vault root.
    # These are documentation/config, NOT user-typed facts. Sending them
    # to the LLM produces hallucinated facts: e.g. cursor-user-rules.md
    # contains the example "đôi dép tôi đâu?" → the LLM extracted that as
    # a real fact "Son's slippers are in the bedroom" (incident 2026-04-21).
    # If install.sh starts rendering more files into the vault root, add
    # them here — the rule is "if a script writes it, exclude it".
    "cursor-user-rules.md",
    "program.md",
    "eval-queries.md",
)


def _build_prompt(note: dict, existing_entities: str) -> str:
    template = PROMPT_FILE.read_text()
    body = note.get("body") or "(empty body — the title IS the fact)"
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"\n\n…[truncated, original {len(body)} chars]"
    date = datetime.fromtimestamp(note.get("mtime") or 0, tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        from brain.triple_rules import rules_for_prompt
        triple_rules = rules_for_prompt()
    except Exception:
        triple_rules = ""
    return (
        template
        .replace("{existing_entities}", existing_entities)
        .replace("{note_path}", note["path"])
        .replace("{title}", note.get("title") or note["path"])
        .replace("{date}", date)
        .replace("{body}", body)
        .replace("{triple_rules}", triple_rules or "(no learned rules yet)")
    )


def _source_label(note: dict) -> str:
    # Mirrors session-extraction labels: "note:<rel-path>" so log.md
    # entries are unambiguous about who produced the fact.
    return f"note:{note['path']}"


def process_pending(
    max_notes: int = MAX_NOTES_PER_RUN,
    *,
    verbose: bool = False,
    dry_run: bool = False,
) -> dict:
    """Drain pending note extractions, apply each to the brain.

    Returns a summary dict suitable for log lines:
      {processed, empty, errors, entities_created, entities_updated,
       touched_paths}
    """
    pending = db.pending_note_extractions(
        limit=max_notes,
        min_body_chars=MIN_BODY_CHARS,
        exclude_prefixes=EXCLUDED_DIR_PREFIXES,
        exclude_paths=EXCLUDED_PATHS,
    )
    summary = {
        "processed": 0, "empty": 0, "errors": 0,
        "entities_created": 0, "entities_updated": 0,
        "touched_paths": set(),
    }
    if not pending:
        if verbose:
            print("note_extract: no pending notes")
        return summary

    if verbose:
        print(f"note_extract: {len(pending)} pending note(s)", flush=True)
    if dry_run:
        for n in pending:
            print(f"  would extract: {n['path']} ({len(n['body'])} chars)")
        return summary

    existing = get_existing_index()

    for note in pending:
        prompt = _build_prompt(note, existing)
        if verbose:
            print(f"  → {note['path']} ({len(note['body'])} chars)", flush=True)

        raw = call_claude(prompt, timeout=LLM_TIMEOUT_SEC)
        if raw is None:
            summary["errors"] += 1
            if verbose:
                print(f"    llm_fail (will retry next tick)", flush=True)
            continue

        parsed = parse_extraction(raw)
        if not isinstance(parsed, dict):
            summary["errors"] += 1
            if verbose:
                print(f"    parse_fail (will retry next tick)", flush=True)
            continue

        entities = parsed.get("entities") or []
        if not entities and not parsed.get("corrections"):
            # Empty extraction is a valid outcome — mark sha so we
            # don't re-LLM the same empty note every tick.
            db.mark_note_extracted(note["path"], note["sha"])
            summary["empty"] += 1
            summary["processed"] += 1
            if verbose:
                print(f"    empty (marked extracted)", flush=True)
            continue

        # If this is an EDIT (we extracted a previous version of this note),
        # retract everything that previous version contributed BEFORE adding
        # the new facts. Otherwise old + new pile up and contradict each
        # other — the user-reported case where editing
        # "ho dang o can tho" → "ho dang o con dao" left both facts live in
        # entities/people/{thuha,trinh}.md.
        #
        # Order matters: invalidate FIRST (clears old provenance rows for
        # this note), then apply (writes fresh provenance rows). Doing it
        # after apply would also strikethrough the just-added new facts.
        is_edit = note["extracted_sha"] is not None
        if is_edit:
            try:
                inv = invalidate_facts_for_note(note["path"], verbose=False, reason="edited")
                if verbose and inv["facts_invalidated"]:
                    print(
                        f"    edit detected — invalidated "
                        f"{inv['facts_invalidated']} prior fact(s) "
                        f"in {inv['entities_touched']} entity file(s)",
                        flush=True,
                    )
                for ep in inv.get("entity_paths") or []:
                    summary["touched_paths"].add(str(ep))
            except Exception as exc:
                if verbose:
                    print(f"    edit-invalidate failed (non-fatal): {exc}", flush=True)

        try:
            result = apply_extraction(
                parsed,
                _source_label(note),
                do_commit=False,        # batch one commit at the end
                do_rebuild_index=False, # ditto
                source_note_paths=[note["path"]],
                source_sha=note["sha"],
            )
        except Exception as exc:
            summary["errors"] += 1
            if verbose:
                print(f"    apply_fail: {exc}", flush=True)
            continue

        db.mark_note_extracted(note["path"], note["sha"])
        summary["processed"] += 1
        summary["entities_created"] += len(result.get("created") or [])
        summary["entities_updated"] += len(result.get("updated") or [])
        for p in result.get("touched_paths") or []:
            summary["touched_paths"].add(str(p))
        if verbose:
            c = len(result.get("created") or [])
            u = len(result.get("updated") or [])
            print(f"    ok: +{c} new, ~{u} updated", flush=True)

    if summary["touched_paths"]:
        rebuild_index()
        # Post-extraction sync: GC phantom/untracked entities and requeue
        # stale note provenance for re-extraction next cycle.
        try:
            from brain.verify import post_extraction_sync
            sync = post_extraction_sync()
            if verbose:
                parts = []
                if sync["gc_removed"] or sync["gc_added"]:
                    parts.append(f"gc -/+{sync['gc_removed']}/{sync['gc_added']}")
                if sync["notes_requeued"]:
                    parts.append(f"requeued {sync['notes_requeued']} note(s)")
                if parts:
                    print(f"  verify: {', '.join(parts)}", flush=True)
        except Exception as exc:
            if verbose:
                print(f"  verify sync skipped: {exc}", flush=True)
        paths = sorted(summary["touched_paths"]) + [
            "log.md", "index.md", "identity/corrections.md"
        ]
        msg = (
            f"note_extract: +{summary['entities_created']} entities, "
            f"~{summary['entities_updated']} updates "
            f"from {summary['processed']} note(s)"
        )
        try:
            commit(msg, paths=paths)
        except Exception as exc:
            if verbose:
                print(f"  commit failed (non-fatal): {exc}", flush=True)

    append_log(
        "note_extract",
        f"processed={summary['processed']} "
        f"empty={summary['empty']} errors={summary['errors']} "
        f"+{summary['entities_created']} new ~{summary['entities_updated']} updated",
    )
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract entity facts from user vault notes.")
    ap.add_argument("--max", type=int, default=MAX_NOTES_PER_RUN,
                    help="Max notes to process this run (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="List pending notes without LLM-calling them")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    summary = process_pending(max_notes=args.max, verbose=args.verbose, dry_run=args.dry_run)
    if not args.verbose:
        print(
            f"note_extract: processed={summary['processed']} "
            f"empty={summary['empty']} errors={summary['errors']} "
            f"+{summary['entities_created']} new "
            f"~{summary['entities_updated']} updated"
        )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
