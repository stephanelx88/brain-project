"""Top-N brain audit surface.

The reconcile pass produces a long-form report; this module reduces that
to the **3 most important things to look at right now** so the SessionStart
hook can show them as a single screenful at the top of every Claude/Cursor
session.

Ranking: contested facts first (you've already flagged them as wrong),
then pending dedupe merges from the ledger (LLM judge already said
"merge" but cosine fell below the auto-apply bar OR the prior auto-merge
errored), then the oldest single-source low-conf items (most likely to
have decayed). Capped at `limit` total.

Pending merges are sourced from the dedupe ledger (canonical state) and
cross-checked against on-disk file status — so an item disappears the
moment its merge lands or one side gets archived, instead of nagging
the user about already-resolved proposals.

Output is intentionally <10 lines and *empty* when there's nothing to
audit — empty stdout means the SessionStart hook adds zero context noise
to a clean brain.

Public API:
  top_n(limit=3) -> list[AuditItem]
  format_for_session(items) -> str   # the block injected into agent context
  main() -> int                       # CLI: `python -m brain.audit`
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import brain.config as config
from brain.config import BRAIN_DIR, ENTITY_TYPES, TIMELINE_DIR  # noqa: F401


@dataclass
class AuditItem:
    kind: str           # "contested" | "dedupe" | "low_confidence"
    label: str          # one-line human-readable
    detail: str = ""    # optional second line (path / reason)
    priority: int = 0   # higher = surface first


_FRONTMATTER_STATUS = re.compile(
    # Match `status: contested` only inside the leading `---` … `---` YAML
    # block. Anchored to start-of-line within the frontmatter so we don't
    # false-flag entities whose body text *describes* the contested feature
    # (e.g. the brain-reconciliation docs themselves).
    r"\A---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)


def _has_contested_frontmatter(text: str) -> bool:
    m = _FRONTMATTER_STATUS.match(text)
    if not m:
        return False
    for line in m.group(1).splitlines():
        if re.match(r"\s*status\s*:\s*contested\b", line):
            return True
    return False


def _contested_items() -> list[AuditItem]:
    """Entities whose frontmatter says `status: contested`. Highest priority
    because the user (or the LLM) already explicitly flagged the conflict."""
    out: list[AuditItem] = []
    for type_dir in ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            if f.name.startswith("_"):
                continue
            try:
                text = f.read_text()
            except OSError:
                continue
            if _has_contested_frontmatter(text):
                name = f.stem.replace("-", " ").title()
                rel = f.relative_to(BRAIN_DIR)
                out.append(AuditItem(
                    kind="contested",
                    label=f"Contested · {name} ({type_dir.name})",
                    detail=str(rel),
                    priority=100 + _brain_boost(text, name),
                ))
    return out


def _dedupe_ledger_path() -> Path:
    """Computed lazily so monkey-patching `audit.BRAIN_DIR` in tests
    actually redirects the lookup. A module-level constant captured at
    import time would silently keep pointing at the real `~/.brain`."""
    return BRAIN_DIR / ".dedupe.ledger.json"


def _entity_path_for(type_: str, slug: str) -> Path | None:
    """Resolve <slug>.md under the right type folder. None if missing."""
    type_dir = ENTITY_TYPES.get(type_)
    if type_dir is None:
        return None
    p = type_dir / f"{slug}.md"
    return p if p.exists() else None


def _is_pending_alive(path_a: Path | None, path_b: Path | None) -> bool:
    """Both files must exist AND neither marked superseded/archived. Used
    to filter out ledger entries whose merge already landed (loser file
    archived) or whose target was deleted manually."""
    for p in (path_a, path_b):
        if p is None or not p.exists():
            return False
        try:
            head = p.read_text(errors="replace")[:400]
        except OSError:
            return False
        if "status: superseded" in head or "status: archived" in head:
            return False
    return True


def _dedupe_items() -> list[AuditItem]:
    """Pending merge decisions read from the dedupe ledger.

    Source of truth is `~/.brain/.dedupe.ledger.json` rather than the
    timeline/*.md proposal files. Why: proposal files are append-only
    and quickly go stale — a candidate listed there gets auto-merged by
    `drain_pending_ledger` on the next run, but the markdown file still
    shows it as "needs decision". Reading the ledger + cross-checking
    file status guarantees we only surface items that genuinely still
    need the user's call.

    A pair is "pending" when:
      - ledger verdict == "merge"
      - ledger `applied` is not True (None, False, missing, or an
        error string from a failed prior attempt all qualify)
      - both entity files still exist on disk
      - neither file is marked `status: superseded` or `status: archived`
    """
    try:
        led = json.loads(_dedupe_ledger_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    out: list[AuditItem] = []
    for key, rec in led.items():
        if rec.get("verdict") != "merge":
            continue
        if rec.get("applied") is True:
            continue
        try:
            type_, slug_a, slug_b = key.split("|", 2)
        except ValueError:
            continue
        path_a = _entity_path_for(type_, slug_a)
        path_b = _entity_path_for(type_, slug_b)
        if not _is_pending_alive(path_a, path_b):
            continue
        boost = BRAIN_PRIORITY_BOOST if _BRAIN_RE.search(
            f"{slug_a} {slug_b}"
        ) else 0
        out.append(AuditItem(
            kind="dedupe",
            label=f"Merge? · {type_} · {slug_a} ⇄ {slug_b}",
            detail=f"cosine {float(rec.get('cosine', 0)):.3f} · "
                   f"`brain audit` to walk",
            priority=80 + boost,
        ))
    # Highest cosine first so the most obvious merges surface in top-N.
    out.sort(key=lambda it: it.detail, reverse=True)
    return out


_FRONTMATTER_DATE = re.compile(r"first_seen:\s*(\d{4}-\d{2}-\d{2})")
_SOURCE_COUNT = re.compile(r"source_count:\s*(\d+)")

# User preference (2026-04-20): prioritize brain-related items first because
# improving the brain tooling has compounding returns — fix the knowledge
# capture layer before fixing facts captured by it. We match `brain` as a
# whole word (not `brainstorm`, not `ebrain`) in the name or body.
_BRAIN_RE = re.compile(r"\bbrain\b", re.IGNORECASE)
BRAIN_PRIORITY_BOOST = 30


def _brain_boost(text: str, name: str = "") -> int:
    """+BRAIN_PRIORITY_BOOST if the entity is about the brain itself.

    Cheap substring check — we read the first 2 KB of the body because a
    fact-list entity that mentions 'brain' 10 paragraphs in is probably
    incidentally tagged; the signal lives up top where the name + first
    fact sit."""
    hay = name + "\n" + text[:2000]
    return BRAIN_PRIORITY_BOOST if _BRAIN_RE.search(hay) else 0


def _low_confidence_items(max_items: int = 20) -> list[AuditItem]:
    """Single-source entities, oldest first (with brain boost).

    Capped because in a busy brain there can be hundreds of these. The
    cap is intentionally wider than the SessionStart display limit so
    the final priority sort in `top_n` can surface brain-boosted items
    even when they aren't the N oldest. Insights & decisions weighted
    higher than misc types because their accuracy matters most.
    """
    HIGH_VALUE_TYPES = {"insights", "decisions", "people", "projects", "clients"}
    candidates: list[tuple[str, AuditItem]] = []  # (sort_key, item)
    for type_key, type_dir in ENTITY_TYPES.items():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            if f.name.startswith("_"):
                continue
            try:
                text = f.read_text()
            except OSError:
                continue
            sc = _SOURCE_COUNT.search(text)
            if not sc or int(sc.group(1)) != 1:
                continue
            # status: contested is already surfaced separately
            if _has_contested_frontmatter(text):
                continue
            date_m = _FRONTMATTER_DATE.search(text)
            sort_key = date_m.group(1) if date_m else "9999-99-99"
            name = f.stem.replace("-", " ").title()
            priority = 60 if type_key in HIGH_VALUE_TYPES else 40
            priority += _brain_boost(text, name)
            candidates.append((sort_key, AuditItem(
                kind="low_confidence",
                label=f"Confirm? · {name} ({type_key})",
                detail=f"single-source, {sort_key}",
                priority=priority,
            )))

    # Oldest single-source items first (most likely to be stale); the final
    # priority sort in top_n will then float brain-boosted items to the top.
    candidates.sort(key=lambda kv: kv[0])
    return [item for _, item in candidates[:max_items]]


def top_n(limit: int = 3) -> list[AuditItem]:
    """Return the top `limit` audit items across all signals.

    Order: priority desc, then `kind` to keep contested→dedupe→low_conf grouping.
    Returns [] if the brain is clean — caller should surface nothing.
    """
    pool: list[AuditItem] = []
    for fn in (_contested_items, _dedupe_items, _low_confidence_items):
        try:
            pool.extend(fn())
        except Exception:
            # Audit must never crash the SessionStart hook. A failure in
            # one signal just removes it from the pool.
            continue
    pool.sort(key=lambda it: (-it.priority, it.kind))
    return pool[:max(0, int(limit))]


def format_for_session(items: list[AuditItem]) -> str:
    """Render the audit block injected into the Claude/Cursor session context.

    Returns "" when there's nothing to audit so the SessionStart hook can
    stay silent. Format is intentionally compact — we're spending the
    user's context budget here.
    """
    if not items:
        return ""
    lines = [
        "🧠 Brain audit — top items needing a 5-second decision:",
    ]
    for i, it in enumerate(items, 1):
        lines.append(f"  {i}. {it.label}")
        if it.detail:
            lines.append(f"     {it.detail}")
    lines.append("")
    lines.append("Run `brain audit` to walk merges interactively, or "
                 "`python -m brain.reconcile` for the full report.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """`python -m brain.audit` — print the top-N audit block.

    Designed for the SessionStart hook: silent when clean, compact when not.
    Exit code is always 0 — a noisy hook would block agent startup, and a
    failed audit isn't a reason to kill the session.
    """
    import argparse
    p = argparse.ArgumentParser(description="Top-N brain audit summary")
    p.add_argument("--limit", "-n", type=int, default=3,
                   help="How many items to surface (default 3).")
    args = p.parse_args(argv)

    try:
        config.ensure_dirs()
        items = top_n(limit=args.limit)
        block = format_for_session(items)
        if block:
            print(block, end="")
    except Exception:
        # Belt and suspenders — `top_n` already swallows per-signal
        # errors, but if the *output formatting* itself blows up we still
        # don't want to fail the SessionStart hook.
        pass
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
