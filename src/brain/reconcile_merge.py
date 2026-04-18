"""Merge duplicate entities detected by find_possible_duplicates.

Dry-run by default: shows proposed merges.
Pass --execute to actually merge files and rebuild index.
"""

import sys
import re
from pathlib import Path

import brain.config as config
from brain.config import ENTITY_TYPES
from brain.reconcile import find_possible_duplicates
from brain.index import rebuild_index


# Words that indicate different scope — skip merging when only one side has them
SPLITTING_WORDS = {
    "friday", "lunch", "dinner", "monday", "tuesday", "wednesday",
    "thursday", "saturday", "sunday", "weekly", "daily", "morning",
    "evening", "inventory", "analysis",
}


def parse_duplicate_line(line: str) -> tuple[str, str] | None:
    """Parse '- **type/slug1** and **type/slug2** share: ...' → (slug1, slug2)."""
    m = re.match(r"- \*\*([^*]+)\*\* and \*\*([^*]+)\*\* share:", line)
    if not m:
        return None
    return m.group(1), m.group(2)


def entity_path(type_slug: str) -> Path:
    """Convert 'domains/foo-bar' → absolute entity file path."""
    type_key, slug = type_slug.split("/", 1)
    return ENTITY_TYPES[type_key] / f"{slug}.md"


def is_high_confidence(pair: tuple[str, str]) -> bool:
    """Check if pair is safe to auto-merge."""
    k1, k2 = pair
    slug1 = k1.split("/", 1)[1]
    slug2 = k2.split("/", 1)[1]
    words1 = set(slug1.split("-"))
    words2 = set(slug2.split("-"))

    sym_diff = words1.symmetric_difference(words2)
    if sym_diff & SPLITTING_WORDS:
        return False

    diff_numbers = {w for w in sym_diff if w.isdigit() and len(w) >= 3}
    if len(diff_numbers) >= 2:
        return False

    return True


def pick_canonical(k1: str, k2: str) -> tuple[str, str]:
    """Return (canonical, duplicate). Shorter slug wins; tiebreak alphabetical."""
    s1, s2 = k1.split("/", 1)[1], k2.split("/", 1)[1]
    if len(s1) != len(s2):
        return (k1, k2) if len(s1) < len(s2) else (k2, k1)
    return (k1, k2) if s1 < s2 else (k2, k1)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split entity markdown into (frontmatter_dict, body)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 5:]
    fm = {}
    for line in fm_text.split("\n"):
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    return fm, body


def render_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def merge_frontmatter(fm1: dict, fm2: dict) -> dict:
    merged = dict(fm1)
    try:
        merged["source_count"] = str(int(fm1.get("source_count", 1)) + int(fm2.get("source_count", 1)))
    except ValueError:
        pass
    dates1, dates2 = fm1.get("first_seen", ""), fm2.get("first_seen", "")
    if dates1 and dates2:
        merged["first_seen"] = min(dates1, dates2)
    updates1, updates2 = fm1.get("last_updated", ""), fm2.get("last_updated", "")
    if updates1 and updates2:
        merged["last_updated"] = max(updates1, updates2)
    return merged


def merge_bodies(body1: str, body2: str) -> str:
    seen = set()
    lines = []
    for body in (body1, body2):
        for line in body.split("\n"):
            key = line.strip()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            lines.append(line)
    return "\n".join(lines)


def merge_pair(canonical: str, duplicate: str, execute: bool) -> str:
    """Returns status string."""
    canon_path = entity_path(canonical)
    dup_path = entity_path(duplicate)
    if not canon_path.exists() or not dup_path.exists():
        return "skipped (one file already gone)"

    canon_text = canon_path.read_text()
    dup_text = dup_path.read_text()
    fm1, body1 = split_frontmatter(canon_text)
    fm2, body2 = split_frontmatter(dup_text)

    new_fm = merge_frontmatter(fm1, fm2)
    new_body = merge_bodies(body1, body2)
    new_text = render_frontmatter(new_fm) + new_body

    if not execute:
        return "would merge"

    canon_path.write_text(new_text)
    dup_path.unlink()
    return "merged"


def main():
    execute = "--execute" in sys.argv

    raw = find_possible_duplicates()
    if raw == "None found.":
        print("No duplicates detected.")
        return

    pairs = []
    for line in raw.split("\n"):
        p = parse_duplicate_line(line)
        if p:
            pairs.append(p)

    auto_pairs = []
    skipped = []
    for k1, k2 in pairs:
        if is_high_confidence((k1, k2)):
            auto_pairs.append(pick_canonical(k1, k2))
        else:
            skipped.append((k1, k2))

    print(f"Detected: {len(pairs)} duplicate pairs")
    print(f"  Auto-merge candidates: {len(auto_pairs)}")
    print(f"  Skipped (low confidence): {len(skipped)}")
    print()

    if skipped:
        print("=== SKIPPED (review manually) ===")
        for k1, k2 in skipped:
            print(f"  {k1} <-> {k2}")
        print()

    print("=== MERGE PLAN ===")
    seen_deleted = set()
    results = {"merged": 0, "would merge": 0, "skipped (one file already gone)": 0}
    for canonical, duplicate in auto_pairs:
        if duplicate in seen_deleted or canonical in seen_deleted:
            continue
        status = merge_pair(canonical, duplicate, execute)
        results[status] = results.get(status, 0) + 1
        marker = "MERGE" if status == "merged" else ("DRY" if status == "would merge" else "SKIP")
        print(f"  [{marker}] keep={canonical}  delete={duplicate}")
        if status == "merged":
            seen_deleted.add(duplicate)

    print()
    print(f"Results: {results}")

    if execute and results.get("merged", 0) > 0:
        print("Rebuilding index...")
        rebuild_index()
        print("Done.")
    elif not execute:
        print("\nDry run only. Re-run with --execute to apply.")


if __name__ == "__main__":
    main()
