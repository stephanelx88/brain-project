"""Reconciliation: scan brain for conflicts, low-confidence facts, duplicates."""

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import brain.config as config
from brain.config import BRAIN_DIR, ENTITIES_DIR, ENTITY_TYPES, TIMELINE_DIR
from brain.io import atomic_write_text


def get_recent_log(hours: int = 2) -> str:
    """Get log entries from the last N hours."""
    log_file = config.LOG_FILE
    if not log_file.exists():
        return "No log entries."
    text = log_file.read_text().strip()
    if not text:
        return "No log entries."

    lines = text.split("\n")
    entries = [l for l in lines if l.startswith("## [")]
    if not entries:
        return "No log entries."

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    for entry in entries:
        match = re.match(r"## \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]", entry)
        if match:
            try:
                entry_time = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                if entry_time >= cutoff:
                    recent.append(entry)
            except ValueError:
                recent.append(entry)
        else:
            recent.append(entry)

    return "\n".join(recent) if recent else "No log entries."


def find_contested_facts() -> str:
    """Scan all entity files for status: contested."""
    contested = []
    for type_dir in ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            text = f.read_text()
            if "status: contested" in text:
                name = f.stem.replace("-", " ").title()
                entity_type = type_dir.name
                contested.append(f"- **{name}** ({entity_type}): {f.relative_to(BRAIN_DIR)}")
    return "\n".join(contested) if contested else "None found."


def find_low_confidence_facts() -> str:
    """Scan for entities with source_count: 1."""
    low_conf = []
    for type_dir in ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            text = f.read_text()
            match = re.search(r"source_count:\s*(\d+)", text)
            if match and int(match.group(1)) == 1:
                name = f.stem.replace("-", " ").title()
                entity_type = type_dir.name
                # Get first fact line for context
                fact_line = ""
                for line in text.split("\n"):
                    if line.startswith("- ") and "source:" in line:
                        fact_line = line[:100]
                        break
                low_conf.append(f"- **{name}** ({entity_type}): {fact_line}")
    return "\n".join(low_conf[:10]) if low_conf else "None — all facts have multiple sources."


def _compact(slug: str) -> str:
    """Collapse to lowercase alphanumerics — catches `mover-os` ≡ `moveros`."""
    return "".join(c for c in slug.lower() if c.isalnum())


def _lev(a: str, b: str) -> int:
    """Bounded Levenshtein. Cheap O(len(a)*len(b))."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 4:
        return 5
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j, cb in enumerate(b, 1):
        cur = [j] + [0] * len(a)
        for i, ca in enumerate(a, 1):
            cur[i] = min(
                prev[i] + 1,
                cur[i - 1] + 1,
                prev[i - 1] + (0 if ca == cb else 1),
            )
        prev = cur
    return prev[-1]


def find_possible_duplicates() -> str:
    """Find entities with similar names that might be the same.

    Three independent signals (any one fires → candidate):
      1. Word-token overlap ≥ 50% (>=2 shared) or strict subset
      2. Compact-string equality (`mover-os` == `moveros`)
      3. Levenshtein ≤ 2 on slugs shorter than 16 chars (typo-class)
    """
    entries = {}
    for type_key, type_dir in ENTITY_TYPES.items():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            slug = f.stem
            entries[f"{type_key}/{slug}"] = {
                "slug": slug,
                "words": set(slug.split("-")),
                "compact": _compact(slug),
            }

    duplicates: list[tuple[str, str, str]] = []
    keys = list(entries.keys())
    for i, k1 in enumerate(keys):
        e1 = entries[k1]
        t1 = k1.split("/")[0]
        for k2 in keys[i + 1:]:
            if k1.split("/")[0] != k2.split("/")[0]:
                continue
            e2 = entries[k2]

            w1, w2 = e1["words"], e2["words"]
            overlap = w1 & w2
            union = w1 | w2
            is_subset = w1 < w2 or w2 < w1
            reason = None
            if e1["compact"] == e2["compact"] and e1["slug"] != e2["slug"]:
                reason = "compact-equal"
            elif (overlap and len(overlap) / len(union) > 0.5 and len(overlap) >= 2) \
                    or (is_subset and len(overlap) >= 1):
                reason = "word-overlap"
            elif (
                len(e1["slug"]) <= 16
                and len(e2["slug"]) <= 16
                and _lev(e1["slug"], e2["slug"]) <= 2
            ):
                reason = "levenshtein"
            if reason:
                shared = ", ".join(overlap) if overlap else reason
                duplicates.append((k1, k2, f"{reason}: {shared}"))

    if not duplicates:
        return "None found."
    return "\n".join(f"- **{k1}** and **{k2}** share: {why}" for k1, k2, why in duplicates)


def prepare_reconciliation() -> dict:
    """Gather all reconciliation data."""
    return {
        "recent_log": get_recent_log(),
        "contested_facts": find_contested_facts(),
        "low_confidence_facts": find_low_confidence_facts(),
        "possible_duplicates": find_possible_duplicates(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }


def has_items_to_reconcile() -> bool:
    """Quick check: is there anything worth reconciling?"""
    data = prepare_reconciliation()
    return not all(
        v in ("None found.", "None — all facts have multiple sources.", "No log entries.")
        for k, v in data.items()
        if k != "date" and k != "recent_log"
    )


def write_reconciliation_file(content: str) -> Path:
    """Write reconciliation output to timeline."""
    now = datetime.now(timezone.utc)
    filename = f"{now.strftime('%Y-%m-%d')}-reconcile-{now.strftime('%H%M')}.md"
    path = TIMELINE_DIR / filename
    atomic_write_text(path, content)
    return path


def _format_report(data: dict) -> str:
    return (
        f"# Reconciliation — {data['date']}\n\n"
        f"## Recent log\n{data['recent_log']}\n\n"
        f"## Contested facts\n{data['contested_facts']}\n\n"
        f"## Low-confidence facts\n{data['low_confidence_facts']}\n\n"
        f"## Possible duplicates\n{data['possible_duplicates']}\n"
    )


def main(argv: list[str] | None = None) -> int:
    """`python -m brain.reconcile` — print the reconciliation report and,
    when `--write` is passed, persist it under `timeline/`.

    Without a main(), the launchd `auto-extract.sh` invocation
    (`python -m brain.reconcile`) was a silent no-op: the module loaded,
    no top-level code ran, and the shell got exit 0. Now the module
    actually does the reconciliation work it advertises.
    """
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Reconcile contested / duplicate / low-conf facts")
    p.add_argument("--write", action="store_true",
                   help="Persist the report under ~/.brain/timeline/")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress stdout when there's nothing to reconcile")
    args = p.parse_args(argv)

    config.ensure_dirs()
    if not has_items_to_reconcile():
        if not args.quiet:
            print("Nothing to reconcile.")
        return 0

    data = prepare_reconciliation()
    report = _format_report(data)
    print(report)
    if args.write:
        path = write_reconciliation_file(report)
        print(f"\nWrote: {path}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
