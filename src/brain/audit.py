"""Top-N brain audit surface.

The reconcile pass produces a long-form report; this module reduces that
to the **3 most important things to look at right now** so the SessionStart
hook can show them as a single screenful at the top of every Claude/Cursor
session.

Ranking: contested facts first (you've already flagged them as wrong),
then high-confidence dedupe candidates from the most recent dedupe report
(LLM judge already said "merge"), then the oldest single-source low-conf
items (most likely to have decayed). Capped at `limit` total.

Output is intentionally <10 lines and *empty* when there's nothing to
audit — empty stdout means the SessionStart hook adds zero context noise
to a clean brain.

Public API:
  top_n(limit=3) -> list[AuditItem]
  format_for_session(items) -> str   # the block injected into agent context
  main() -> int                       # CLI: `python -m brain.audit`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import brain.config as config
from brain.config import BRAIN_DIR, ENTITY_TYPES, TIMELINE_DIR


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
                    priority=100,
                ))
    return out


_DEDUPE_HEADER = re.compile(
    # Matches the section headers in the dedupe report:
    #   "## insights: slug-a  ⇄  slug-b"
    r"^##\s+([^:]+):\s*(\S+)\s+⇄\s+(\S+)\s*$",
    re.MULTILINE,
)
_DEDUPE_VERDICT = re.compile(r"LLM verdict:\s*`?(\w+)`?", re.IGNORECASE)


def _latest_dedupe_path() -> Path | None:
    """Newest `~/.brain/timeline/YYYY-MM-DD-dedupe-HHMM.md` file, or None."""
    if not TIMELINE_DIR.exists():
        return None
    candidates = sorted(TIMELINE_DIR.glob("*-dedupe-*.md"))
    return candidates[-1] if candidates else None


def _dedupe_items() -> list[AuditItem]:
    """High-confidence merge candidates from the latest dedupe report.

    We rely on the dedupe pass having already filtered to
    `verdict == merge`; we just surface the headers. Skipping reports
    older than 7 days so we don't keep nagging about resolved items the
    user already merged manually (the file stays around but is stale).
    """
    path = _latest_dedupe_path()
    if path is None:
        return []
    try:
        age_days = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 86400
        if age_days > 7:
            return []
        text = path.read_text()
    except OSError:
        return []

    out: list[AuditItem] = []
    # Walk header → next-header chunks so we can read the verdict per pair.
    headers = list(_DEDUPE_HEADER.finditer(text))
    for i, m in enumerate(headers):
        ent_type, slug_a, slug_b = m.group(1).strip(), m.group(2), m.group(3)
        chunk_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        chunk = text[m.end():chunk_end]
        verdict_match = _DEDUPE_VERDICT.search(chunk)
        verdict = verdict_match.group(1).lower() if verdict_match else ""
        if verdict != "merge":
            continue
        out.append(AuditItem(
            kind="dedupe",
            label=f"Merge? · {ent_type} · {slug_a} ⇄ {slug_b}",
            detail=f"see {path.relative_to(BRAIN_DIR)}",
            priority=80,
        ))
    return out


_FRONTMATTER_DATE = re.compile(r"first_seen:\s*(\d{4}-\d{2}-\d{2})")
_SOURCE_COUNT = re.compile(r"source_count:\s*(\d+)")


def _low_confidence_items(max_items: int = 5) -> list[AuditItem]:
    """Single-source entities, oldest first.

    Capped because in a busy brain there can be hundreds of these — we
    only need the most decayed ones to nudge the user into a quick
    confirm/delete pass. Insights & decisions are weighted higher
    than misc types because they're the ones whose accuracy matters most.
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
            candidates.append((sort_key, AuditItem(
                kind="low_confidence",
                label=f"Confirm? · {name} ({type_key})",
                detail=f"single-source, {sort_key}",
                priority=priority,
            )))

    # Oldest single-source items first (most likely to be stale).
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
    lines.append("Run `python -m brain.reconcile` for the full report, or "
                 "ask the brain MCP tool `brain_audit` anytime.")
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
