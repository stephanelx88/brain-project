"""Semantic dedupe pass — merge near-identical entities the lexical
reconciler in `brain.reconcile` is blind to.

Why a separate module: `reconcile.py` only catches slug-level dupes
(word overlap, compact-equality, Levenshtein ≤ 2 on slugs ≤16 chars).
That misses the common case — same idea, different wording — which
dominates the `insights/` folder once auto-extract has been running for
a while. This module uses the existing `.vec/` entity embeddings to
find semantic clusters, then asks Haiku to confirm each candidate pair
before any file is touched.

Pipeline:
  1. Load entity vectors from `.vec/entities.npy` (per-type filter).
  2. All-pairs cosine within each type. A pair is a *candidate* if
     cosine ≥ per-type candidate threshold AND the pair has not been
     judged before (ledger keyed by slugs + mtimes).
  3. Cap at `max_judgments` to bound LLM cost. Send each candidate to
     Haiku with both bodies. Parse strict JSON verdict.
  4. If `verdict == "merge"` AND cosine ≥ per-type auto threshold,
     apply the merge: append loser's facts to winner via existing
     `entities.append_to_entity` (fact-level dedup baked in), rewrite
     loser's frontmatter with `status: superseded` + `superseded_by:`
     pointing at the winner. Existing `clean.archive_stale_entities`
     will move the loser into `entities/_archive/<type>/` next pass.
  5. `merge` verdicts that fall between the candidate and auto
     thresholds are written as a proposal to
     `timeline/<date>-dedupe-<HHMM>.md` for human review.
  6. All verdicts (including `split`/`unrelated`/`unsure`) are recorded
     in `~/.brain/.dedupe.ledger.json` so the next run skips them
     unless either file's mtime changes.

Per-type thresholds reflect blast radius: merging two phrasings of an
insight is almost always good; merging two `decisions` or two `people`
is dangerous, so the bar is much higher.

Designed to run unattended from `auto-extract.sh` after `brain.reconcile`.
The active-session guard in that script already gates it correctly
(this module spawns Claude calls).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np

import brain.config as config
from brain import semantic
from brain.entities import append_to_entity_path
from brain.git_ops import commit
from brain.log import append_log

try:
    from brain.db import upsert_entity_from_file, delete_entity_by_path
except Exception:  # pragma: no cover
    upsert_entity_from_file = None
    delete_entity_by_path = None


# ---------------------------------------------------------------------------
# tunables
# ---------------------------------------------------------------------------

# (candidate_threshold, auto_apply_threshold) per entity type.
# - candidate: pair becomes a candidate above this cosine. We pay an LLM
#   call to judge it. Lower = more recall, more LLM calls.
# - auto_apply: even after LLM says "merge", we only write the merge if
#   cosine is at least this high. Below this, the merge becomes a
#   proposal in timeline/ for human review.
PER_TYPE_THRESHOLDS: dict[str, tuple[float, float]] = {
    # Insights: high duplication rate from auto-extract, low merge risk.
    # Empirical 2026-04-20: legitimate insight merges cluster 0.90-0.92,
    # so the auto bar sits at 0.90 — anything tighter routes them to
    # proposal files unnecessarily.
    "insights":    (0.85, 0.90),
    "domains":     (0.88, 0.93),
    "evolutions":  (0.88, 0.94),
    "feedback":    (0.88, 0.94),
    "issues":      (0.90, 0.94),
    "decisions":   (0.92, 0.97),
    "corrections": (0.95, 0.99),
    "projects":    (0.94, 0.99),
    "people":      (0.94, 0.99),
    "clients":     (0.94, 0.99),
}
DEFAULT_THRESHOLDS = (0.90, 0.96)

# Bound a single run to keep token cost and write blast radius small.
DEFAULT_MAX_JUDGMENTS = 20
DEFAULT_MAX_MERGES = 8

# Cosine value used as a hard floor to skip pairs we've judged before
# unless one of them was edited since.
LEDGER_PATH = config.BRAIN_DIR / ".dedupe.ledger.json"

# Truncate entity bodies for the judge prompt — anything past ~1.5KB is
# almost always boilerplate or fact accumulations rather than the
# defining text.
BODY_TRUNCATE_CHARS = 1500


# ---------------------------------------------------------------------------
# ledger — pairs judged before are skipped
# ---------------------------------------------------------------------------

def _ledger_load() -> dict:
    if not LEDGER_PATH.exists():
        return {}
    try:
        return json.loads(LEDGER_PATH.read_text())
    except Exception:
        return {}


def _ledger_save(led: dict) -> None:
    LEDGER_PATH.write_text(json.dumps(led, indent=2, sort_keys=True))


def _pair_key(slug_a: str, slug_b: str, type_: str) -> str:
    a, b = sorted([slug_a, slug_b])
    return f"{type_}|{a}|{b}"


def _file_mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except FileNotFoundError:
        return 0


def _ledger_skip(led: dict, key: str, mtime_a: int, mtime_b: int) -> bool:
    """Skip a pair only if the ledger entry was recorded against the same
    pair of mtimes. A real edit on either file invalidates the cache so
    the new wording gets re-judged."""
    rec = led.get(key)
    if not rec:
        return False
    return rec.get("mtime_a") == mtime_a and rec.get("mtime_b") == mtime_b


# ---------------------------------------------------------------------------
# candidate generation — semantic neighbours within the same type
# ---------------------------------------------------------------------------

def _abs_path(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else config.BRAIN_DIR / p


def _entity_alive(meta: dict) -> bool:
    """File still exists AND is not already marked superseded/archived."""
    p = _abs_path(meta["path"])
    if not p.exists():
        return False
    head = p.read_text(errors="replace")[:400]
    if "status: superseded" in head or "status: archived" in head:
        return False
    return True


def find_candidates(
    type_filter: str | None = None,
    threshold_override: float | None = None,
) -> list[dict]:
    """Return semantic dup candidates as
    `[{type, slug_a, slug_b, path_a, path_b, cosine}, ...]` sorted by
    descending cosine. Live entities only — superseded/archived skipped."""
    semantic.ensure_built()
    vecs, meta = semantic._load_entities()
    if vecs.shape[0] == 0:
        return []

    # Bucket row indices by type so the all-pairs loop stays per-type.
    by_type: dict[str, list[int]] = {}
    for i, m in enumerate(meta):
        if not _entity_alive(m):
            continue
        if type_filter and m["type"] != type_filter:
            continue
        by_type.setdefault(m["type"], []).append(i)

    candidates: list[dict] = []
    for type_, idxs in by_type.items():
        if len(idxs) < 2:
            continue
        cand_thresh = (threshold_override
                       if threshold_override is not None
                       else PER_TYPE_THRESHOLDS.get(type_, DEFAULT_THRESHOLDS)[0])
        sub = vecs[idxs]                       # (n, 384) L2-normalised
        sims = sub @ sub.T                     # cosine since rows are normalised
        # Mask the diagonal and the lower triangle so each pair appears once.
        n = len(idxs)
        iu = np.triu_indices(n, k=1)
        pair_sims = sims[iu]
        hits = np.where(pair_sims >= cand_thresh)[0]
        for h in hits:
            i_local, j_local = int(iu[0][h]), int(iu[1][h])
            mi, mj = meta[idxs[i_local]], meta[idxs[j_local]]
            candidates.append({
                "type": type_,
                "slug_a": mi["slug"],
                "slug_b": mj["slug"],
                "name_a": mi["name"],
                "name_b": mj["name"],
                "path_a": _abs_path(mi["path"]),
                "path_b": _abs_path(mj["path"]),
                "cosine": float(pair_sims[h]),
            })
    candidates.sort(key=lambda c: -c["cosine"])
    return candidates


# ---------------------------------------------------------------------------
# LLM judge — one Claude Haiku call per candidate
# ---------------------------------------------------------------------------

def _load_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "dedupe_judge.md").read_text()


def _read_body(path: Path) -> str:
    """Return the entity body trimmed to BODY_TRUNCATE_CHARS, frontmatter included."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return ""
    return text[:BODY_TRUNCATE_CHARS]


def _build_judge_prompt(cand: dict) -> str:
    template = _load_prompt()
    return (template
            .replace("{entity_type}", cand["type"])
            .replace("{cosine}", f"{cand['cosine']:.3f}")
            .replace("{slug_a}", cand["slug_a"])
            .replace("{slug_b}", cand["slug_b"])
            .replace("{body_a}", _read_body(cand["path_a"]))
            .replace("{body_b}", _read_body(cand["path_b"])))


def _parse_verdict(raw: str) -> dict | None:
    """Strict JSON expected. Tolerate code fences and surrounding text."""
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.startswith("```"))
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}") + 1
        if s < 0 or e <= s:
            return None
        try:
            obj = json.loads(text[s:e])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    if obj.get("verdict") not in {"merge", "split", "unrelated", "unsure"}:
        return None
    return obj


def judge_pair(cand: dict) -> dict | None:
    """One LLM call. Returns parsed verdict dict or None on failure."""
    # Imported lazily so a fresh checkout that hasn't installed the
    # extraction deps can still run `find_candidates`.
    from brain.auto_extract import call_claude
    prompt = _build_judge_prompt(cand)
    out = call_claude(prompt, timeout=120)
    if not out:
        return None
    return _parse_verdict(out)


# ---------------------------------------------------------------------------
# apply — write the merge
# ---------------------------------------------------------------------------

_FACT_LINE = re.compile(r"^\s*-\s")


def _parse_frontmatter(text: str) -> tuple[dict, int]:
    """Return ({key: raw_value_str}, body_start_index). Best-effort, line-based."""
    fm: dict[str, str] = {}
    if not text.startswith("---"):
        return fm, 0
    end = text.find("\n---", 3)
    if end == -1:
        return fm, 0
    block = text[3:end].strip("\n")
    for line in block.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    body_start = end + len("\n---\n")
    return fm, body_start


def _split_facts(body: str) -> list[str]:
    """Return all `- ...` bullet lines from the body (any section)."""
    return [l for l in body.split("\n") if _FACT_LINE.match(l)]


def _winner_loser(path_a: Path, path_b: Path,
                  llm_winner_slug: str | None) -> tuple[Path, Path]:
    """Resolve which file wins. Honor LLM hint if it matches one of the
    two slugs; otherwise pick by source_count desc, first_seen asc, then
    shorter slug. Ties broken by slug alphabetical so the choice is
    deterministic across runs."""
    if llm_winner_slug == path_a.stem:
        return path_a, path_b
    if llm_winner_slug == path_b.stem:
        return path_b, path_a

    def score(p: Path) -> tuple:
        fm, _ = _parse_frontmatter(p.read_text(errors="replace"))
        try:
            sc = int(fm.get("source_count", "1"))
        except ValueError:
            sc = 1
        first_seen = fm.get("first_seen", "9999-99-99")
        return (-sc, first_seen, len(p.stem), p.stem)

    return (path_a, path_b) if score(path_a) <= score(path_b) else (path_b, path_a)


def _facts_for_append(loser_body: str, loser_slug: str, now: str) -> str:
    """Return loser's fact lines, source-tagged so provenance is preserved
    and the existing fact-dedup logic in `entities.append_to_entity` can
    skip duplicates."""
    out = []
    for line in _split_facts(loser_body):
        s = line.lstrip()
        # Strip the leading bullet — append_to_entity adds it back.
        if s.startswith("- "):
            s = s[2:]
        if "(source:" not in s:
            s = f"{s} (source: dedupe-merge:{loser_slug}, {now})"
        out.append(f"- {s}")
    return "\n".join(out)


def _set_frontmatter_keys(text: str, updates: dict[str, str]) -> str:
    """Return `text` with each key in `updates` set to the new value.
    Adds the key just before the closing `---` if missing."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    head = text[3:end]
    body = text[end:]
    lines = head.split("\n")
    seen = set()
    for i, line in enumerate(lines):
        if ":" in line:
            k = line.split(":", 1)[0].strip()
            if k in updates:
                lines[i] = f"{k}: {updates[k]}"
                seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}: {v}")
    return "---" + "\n".join(lines) + body


def apply_merge(cand: dict, verdict: dict) -> dict:
    """Append loser facts into winner; mark loser superseded.

    Returns a small dict describing what happened so the caller can log
    and report. Idempotent-ish: if the loser is already superseded we
    no-op without touching the winner."""
    winner, loser = _winner_loser(cand["path_a"], cand["path_b"],
                                  verdict.get("winner_slug"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    loser_text = loser.read_text(errors="replace")
    loser_fm, loser_body_start = _parse_frontmatter(loser_text)
    if loser_fm.get("status") == "superseded":
        return {"action": "skip-already-superseded",
                "winner": winner.stem, "loser": loser.stem}
    loser_body = loser_text[loser_body_start:]

    facts_block = _facts_for_append(loser_body, loser.stem, now)
    if facts_block:
        try:
            # Path-addressed append: real entity slugs often carry date
            # prefixes (e.g. `2026-04-11-foo.md`) that `slugify(name)`
            # can't reconstruct. Going through `append_to_entity` by
            # name used to fail with `Entity not found` here, leaving
            # the merge half-done in the ledger.
            append_to_entity_path(winner, "Key Facts", facts_block)
        except Exception as exc:
            return {"action": "error",
                    "error": f"append failed: {exc}",
                    "winner": winner.stem, "loser": loser.stem}

    # Merge source_count so confidence is preserved when the loser is archived.
    winner_text = winner.read_text(errors="replace")
    winner_fm, _ = _parse_frontmatter(winner_text)
    try:
        new_count = int(winner_fm.get("source_count", "1")) + int(loser_fm.get("source_count", "1"))
    except ValueError:
        new_count = int(winner_fm.get("source_count", "1") or "1") + 1
    winner_text = _set_frontmatter_keys(winner_text, {
        "source_count": str(new_count),
        "last_updated": now,
    })
    winner.write_text(winner_text)

    new_loser = _set_frontmatter_keys(loser_text, {
        "status": "superseded",
        "superseded_by": winner.stem,
        "last_updated": now,
    })
    loser.write_text(new_loser)

    if upsert_entity_from_file is not None:
        # Surface db sync failures in the cron log — they used to be
        # swallowed silently here, which masked a contentless-FTS5 bug
        # that left the SQLite mirror stale after every dedupe merge.
        # The merge itself is durable on disk; a noisy db is recoverable
        # via `python -m brain.db rebuild`.
        for p in (winner, loser):
            try:
                upsert_entity_from_file(p)
            except Exception as exc:
                print(f"dedupe: db upsert failed for {p}: {exc}", file=sys.stderr)

    return {
        "action": "merged",
        "type": cand["type"],
        "winner": winner.stem,
        "loser": loser.stem,
        "cosine": cand["cosine"],
        "reason": verdict.get("reason", ""),
    }


# ---------------------------------------------------------------------------
# pending-merge drain — re-apply ledger entries when thresholds widen
# ---------------------------------------------------------------------------

def _entity_file_for(type_: str, slug: str) -> Path | None:
    """Resolve <slug>.md under entities/<type>/ if it exists."""
    type_dir = config.ENTITY_TYPES.get(type_) or (config.ENTITIES_DIR / type_)
    p = type_dir / f"{slug}.md"
    return p if p.exists() else None


def _is_alive_path(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    head = path.read_text(errors="replace")[:400]
    return "status: superseded" not in head and "status: archived" not in head


def drain_pending_ledger(
    led: dict,
    max_merges: int,
    force: bool = False,
) -> list[dict]:
    """Apply ledger entries marked `verdict: merge` that now meet the
    current per-type auto-apply threshold. Skips entries whose files
    have moved, been deleted, or already been superseded.

    `force=True` ignores the auto threshold and applies every pending
    `merge` verdict regardless of cosine. Intended for one-shot manual
    cleanups (`--force-apply-ledger-merges`) — the autonomous cron keeps
    the conservative thresholds.

    The ledger is mutated in place: applied entries get `applied: True`
    so they aren't re-tried. Returns the list of merge-result dicts."""
    merged: list[dict] = []
    for key, rec in list(led.items()):
        if len(merged) >= max_merges:
            break
        if rec.get("verdict") != "merge" or rec.get("applied"):
            continue

        type_, slug_a, slug_b = key.split("|", 2)
        auto_thresh = PER_TYPE_THRESHOLDS.get(type_, DEFAULT_THRESHOLDS)[1]
        cosine = float(rec.get("cosine", 0.0))
        if not force and cosine < auto_thresh:
            continue

        path_a = _entity_file_for(type_, slug_a)
        path_b = _entity_file_for(type_, slug_b)
        if not (_is_alive_path(path_a) and _is_alive_path(path_b)):
            # One side already gone — record it so we don't keep retrying.
            rec["applied"] = "skipped-missing-or-superseded"
            continue

        cand = {
            "type": type_,
            "slug_a": slug_a,
            "slug_b": slug_b,
            "path_a": path_a,
            "path_b": path_b,
            "cosine": cosine,
            "name_a": slug_a,
            "name_b": slug_b,
        }
        verdict = {
            "verdict": "merge",
            "winner_slug": rec.get("winner"),
            "reason": rec.get("reason", ""),
        }
        result = apply_merge(cand, verdict)
        if result.get("action") == "merged":
            merged.append(result)
            rec["applied"] = True
        elif result.get("action") == "skip-already-superseded":
            rec["applied"] = True
        else:
            rec["applied"] = f"error: {result.get('error', 'unknown')}"
    return merged


# ---------------------------------------------------------------------------
# proposal file — for borderline merges
# ---------------------------------------------------------------------------

def _write_proposals(proposals: list[dict]) -> Path | None:
    if not proposals:
        return None
    config.TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = config.TIMELINE_DIR / f"{now.strftime('%Y-%m-%d')}-dedupe-{now.strftime('%H%M')}.md"
    lines = [
        f"# Brain Dedupe Proposals — {now.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"_{len(proposals)} merge candidate(s) flagged by the LLM judge but below the auto-apply cosine bar. Review and merge by hand if you agree._",
        "",
    ]
    for p in proposals:
        cand, verdict = p["cand"], p["verdict"]
        lines.extend([
            f"## {cand['type']}: {cand['slug_a']}  ⇄  {cand['slug_b']}",
            f"- cosine: **{cand['cosine']:.3f}**",
            f"- LLM verdict: `{verdict.get('verdict')}`, winner: `{verdict.get('winner_slug', '?')}`",
            f"- reason: {verdict.get('reason', '')}",
            f"- files: `{cand['path_a'].relative_to(config.BRAIN_DIR)}` ⇄ `{cand['path_b'].relative_to(config.BRAIN_DIR)}`",
            "",
        ])
    path.write_text("\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

def run(
    apply: bool = False,
    max_judgments: int = DEFAULT_MAX_JUDGMENTS,
    max_merges: int = DEFAULT_MAX_MERGES,
    type_filter: str | None = None,
    threshold_override: float | None = None,
    quiet: bool = False,
    force_apply_ledger_merges: bool = False,
) -> dict:
    """One dedupe pass. Returns a summary dict."""
    config.ensure_dirs()
    led = _ledger_load()

    # Drain previously-judged merges that now meet the current auto-apply
    # bar (cheap: no LLM calls). Lets a threshold tweak — or simply a
    # later run with a bigger budget — pick up backlogged proposals
    # without re-spending tokens.
    pending_merged: list[dict] = []
    if apply:
        pending_merged = drain_pending_ledger(
            led, max_merges=max_merges, force=force_apply_ledger_merges,
        )

    remaining_merges = max(0, max_merges - len(pending_merged))

    candidates = find_candidates(type_filter, threshold_override)
    if not candidates and not pending_merged:
        if apply:
            _ledger_save(led)
        if not quiet:
            print("No semantic dedupe candidates.")
        return {"candidates": 0, "judged": 0, "merged": 0, "proposed": 0,
                "pending_merged": 0}

    judged = 0
    merged: list[dict] = list(pending_merged)
    proposals: list[dict] = []
    skipped_ledger = 0
    failures = 0

    for cand in candidates:
        if judged >= max_judgments:
            break

        mtime_a = _file_mtime(cand["path_a"])
        mtime_b = _file_mtime(cand["path_b"])
        key = _pair_key(cand["slug_a"], cand["slug_b"], cand["type"])
        if _ledger_skip(led, key, mtime_a, mtime_b):
            skipped_ledger += 1
            continue

        verdict = judge_pair(cand)
        judged += 1

        if verdict is None:
            failures += 1
            continue

        # Record every verdict so we don't pay for it twice.
        led[key] = {
            "verdict": verdict["verdict"],
            "winner": verdict.get("winner_slug"),
            "reason": verdict.get("reason", ""),
            "cosine": cand["cosine"],
            "mtime_a": mtime_a,
            "mtime_b": mtime_b,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        if verdict["verdict"] != "merge":
            continue

        auto_thresh = (PER_TYPE_THRESHOLDS.get(cand["type"], DEFAULT_THRESHOLDS)[1])
        if (cand["cosine"] >= auto_thresh
                and (len(merged) - len(pending_merged)) < remaining_merges
                and apply):
            result = apply_merge(cand, verdict)
            if result.get("action") == "merged":
                merged.append(result)
                # Bump the merged loser to "superseded" in the ledger so
                # future runs don't re-judge against it before the vec
                # index rebuilds.
                led[key]["applied"] = True
        else:
            proposals.append({"cand": cand, "verdict": verdict})

    proposal_path = None
    if apply and proposals:
        proposal_path = _write_proposals(proposals)

    if apply:
        _ledger_save(led)
        if merged:
            entity_lines = [f"{m['winner']} ← {m['loser']}" for m in merged]
            # Stage only what dedupe actually rewrote (winner + loser
            # entity files) plus log.md. The previous `git add -A`
            # behaviour committed unrelated user changes (e.g.
            # `where-is-son.md` deleted manually right before this
            # job ran) under the "merged N entities" message — see
            # git_ops.commit docstring for the postmortem.
            touched_paths: list[str] = ["log.md"]
            for m in merged:
                w = _entity_file_for(m["type"], m["winner"])
                l_ = _entity_file_for(m["type"], m["loser"])
                if w is not None:
                    touched_paths.append(str(w))
                if l_ is not None:
                    touched_paths.append(str(l_))
            commit(
                "brain: dedupe — merged "
                f"{len(merged)} entit{'y' if len(merged)==1 else 'ies'}\n\n"
                + "\n".join(entity_lines[:20]),
                paths=touched_paths,
            )
            append_log(
                "dedupe",
                f"merged {len(merged)} ({len(pending_merged)} from ledger), "
                f"proposed {len(proposals)}, judged {judged}",
            )

    summary = {
        "candidates": len(candidates),
        "judged": judged,
        "merged": len(merged),
        "pending_merged": len(pending_merged),
        "proposed": len(proposals),
        "skipped_ledger": skipped_ledger,
        "llm_failures": failures,
        "proposal_file": str(proposal_path) if proposal_path else None,
        "merges": merged,
    }

    if not quiet or merged or proposals:
        pending_note = (f" (+{len(pending_merged)} drained from ledger)"
                        if pending_merged else "")
        print(f"dedupe: {len(candidates)} candidate(s), "
              f"judged {judged} (skipped {skipped_ledger} from ledger), "
              f"{'merged' if apply else 'would merge'} {len(merged)}{pending_note}, "
              f"proposed {len(proposals)}, llm_fail {failures}")
        for m in merged:
            print(f"  merge {m['type']}: {m['winner']} ← {m['loser']}  "
                  f"(cos={m['cosine']:.3f})")
        if proposal_path:
            print(f"  proposals → {proposal_path}")

    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Semantic dedupe pass for the brain")
    p.add_argument("--apply", action="store_true",
                   help="Write the merges and ledger. Default: dry-run.")
    p.add_argument("--max-judgments", type=int, default=DEFAULT_MAX_JUDGMENTS)
    p.add_argument("--max-merges", type=int, default=DEFAULT_MAX_MERGES)
    p.add_argument("--type", default=None,
                   help="Restrict to one entity type (e.g. insights)")
    p.add_argument("--threshold", type=float, default=None,
                   help="Override candidate cosine threshold for this run")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--force-apply-ledger-merges", action="store_true",
                   help="One-shot: apply every pending `merge` verdict in "
                        "the ledger regardless of the auto threshold. "
                        "Useful for manual cleanups; the cron should not "
                        "set this.")
    args = p.parse_args(argv)

    summary = run(
        apply=args.apply,
        max_judgments=args.max_judgments,
        max_merges=args.max_merges,
        type_filter=args.type,
        threshold_override=args.threshold,
        quiet=args.quiet,
        force_apply_ledger_merges=args.force_apply_ledger_merges,
    )
    if not args.apply:
        # Make dry-run discoverable in the cron log.
        if summary["candidates"] > 0 and not args.quiet:
            print("Dry run. Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
