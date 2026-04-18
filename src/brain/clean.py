"""Clean stale data from the brain. Pure Python, no LLM calls.

- Orphan .retries markers (no paired raw .md file)
- Empty or broken entity files (source_count: 0 or missing body)
- Stale .harvested entries pointing to JSONLs that no longer exist

Safe to run repeatedly.
"""

import sys
from pathlib import Path

import brain.config as config
from brain.config import ENTITY_TYPES


def clean_orphan_retries(execute: bool) -> int:
    """Remove .retries files that have no matching session-*.md."""
    raw_dir = config.RAW_DIR
    if not raw_dir.exists():
        return 0
    removed = 0
    for retry_file in raw_dir.glob("*.retries"):
        paired_md = retry_file.with_suffix(".md")
        if not paired_md.exists():
            if execute:
                retry_file.unlink()
            removed += 1
    return removed


def clean_empty_entities(execute: bool) -> int:
    """Remove entity files that are empty or have source_count: 0."""
    removed = 0
    for type_dir in ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            if f.name.startswith("_"):
                continue
            text = f.read_text()
            if not text.strip():
                if execute:
                    f.unlink()
                removed += 1
                continue
            if "source_count: 0" in text:
                body_start = text.find("\n---\n")
                if body_start != -1:
                    body = text[body_start + 5:].strip()
                    if not body or body.count("\n") < 2:
                        if execute:
                            f.unlink()
                        removed += 1
    return removed


def clean_stale_harvested(execute: bool) -> int:
    """Remove .harvested entries whose JSONL no longer exists."""
    harvested_file = config.BRAIN_DIR / ".harvested"
    if not harvested_file.exists():
        return 0

    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return 0

    existing_ids = {p.stem for p in claude_projects.rglob("*.jsonl")}
    lines = harvested_file.read_text().strip().splitlines()
    kept = [sid for sid in lines if sid in existing_ids]
    removed = len(lines) - len(kept)

    if execute and removed > 0:
        harvested_file.write_text("\n".join(kept) + "\n")
    return removed


def main():
    execute = "--execute" in sys.argv

    orphan = clean_orphan_retries(execute)
    empty = clean_empty_entities(execute)
    stale = clean_stale_harvested(execute)

    verb = "Removed" if execute else "Would remove"
    print(f"{verb}:")
    print(f"  orphan .retries: {orphan}")
    print(f"  empty entities: {empty}")
    print(f"  stale .harvested entries: {stale}")

    if not execute:
        print("\nDry run. Re-run with --execute to apply.")


if __name__ == "__main__":
    main()
