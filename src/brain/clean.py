"""Clean stale data and structural debt from the brain. Pure Python, no LLM calls.

Passes (each idempotent, all gated by --execute):

  * orphan .retries markers (no paired raw .md file)
  * empty entity files / source_count: 0 with empty body
  * stale .harvested entries pointing to JSONLs that no longer exist
  * `_placeholder.md` stub files (legacy seed; pollutes the index)
  * collapse repeated `(source: …)` annotations on fact lines
  * regenerate per-type `_MOC.md` (Map Of Content) for Obsidian browse
  * archive entities whose status is `archived`/`superseded` into
    `entities/_archive/<type>/`

Safe to run repeatedly. Used by the launchd cron after extraction.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import brain.config as config
from brain.config import ENTITY_TYPES

_SOURCE_RE = re.compile(r"\s*\(source:[^)]*\)")


def clean_orphan_retries(execute: bool) -> int:
    raw_dir = config.RAW_DIR
    if not raw_dir.exists():
        return 0
    removed = 0
    for retry_file in raw_dir.glob("*.retries"):
        if not retry_file.with_suffix(".md").exists():
            if execute:
                retry_file.unlink()
            removed += 1
    return removed


def clean_empty_entities(execute: bool) -> int:
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


def clean_placeholder_files(execute: bool) -> int:
    """Remove `_placeholder.md` stubs left over from the seed scaffolding.

    They weren't entities; they were docs. The current index treats every
    `*.md` under entities/<type>/ as an entity, so they show up as fake rows.
    """
    removed = 0
    for type_dir in ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("_placeholder.md"):
            if execute:
                f.unlink()
            removed += 1
    return removed


def collapse_double_sources(execute: bool) -> int:
    """Fact lines sometimes get `(source: A) (source: B)` from append paths.
    Keep only the LAST `(source: …)` annotation per line; that one carries
    the most recent provenance + date. Returns number of lines edited.
    """
    edited = 0
    for type_dir in ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            text = f.read_text()
            new_lines = []
            file_changed = False
            for line in text.split("\n"):
                if not line.lstrip().startswith("- "):
                    new_lines.append(line)
                    continue
                matches = list(_SOURCE_RE.finditer(line))
                if len(matches) <= 1:
                    new_lines.append(line)
                    continue
                # keep last match, strip the rest
                last = matches[-1]
                kept = _SOURCE_RE.sub("", line[: last.start()]) + line[last.start():]
                kept = re.sub(r"\s{2,}", " ", kept).rstrip()
                new_lines.append(kept)
                file_changed = True
                edited += 1
            if file_changed and execute:
                f.write_text("\n".join(new_lines))
    return edited


def archive_stale_entities(execute: bool) -> int:
    """Move entities marked archived/superseded out of the live folders.

    Keeps the markdown around (under entities/_archive/<type>/) so
    history is preserved but the live index isn't polluted.
    """
    moved = 0
    archive_root = config.ENTITIES_DIR / "_archive"
    for type_dir in ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        if type_dir.name.startswith("_"):
            continue
        for f in type_dir.glob("*.md"):
            head = f.read_text(errors="replace")[:400]
            if "status: archived" not in head and "status: superseded" not in head:
                continue
            target_dir = archive_root / type_dir.name
            if execute:
                target_dir.mkdir(parents=True, exist_ok=True)
                f.rename(target_dir / f.name)
            moved += 1
    return moved


def generate_mocs(execute: bool) -> int:
    """Regenerate `entities/<type>/_MOC.md` Map Of Content per type.

    Obsidian shows MOCs prominently; users can browse a type at a glance
    without opening the giant root-level `index.md`.
    """
    written = 0
    for type_key, type_dir in ENTITY_TYPES.items():
        if not type_dir.exists() or type_dir.name.startswith("_"):
            continue
        files = sorted(p for p in type_dir.glob("*.md") if not p.name.startswith("_"))
        if not files:
            continue
        lines = [
            f"# {type_key.title()} — Map Of Content",
            "",
            f"_{len(files)} entities. Auto-generated by `brain.clean`._",
            "",
        ]
        for f in files:
            head = f.read_text(errors="replace")[:400]
            name = f.stem.replace("-", " ").title()
            for line in head.split("\n"):
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
            lines.append(f"- [[{f.stem}|{name}]]")
        if execute:
            (type_dir / "_MOC.md").write_text("\n".join(lines) + "\n")
        written += 1
    return written


def main():
    execute = "--execute" in sys.argv
    config.ensure_dirs()
    config.ENTITY_TYPES.update(config._discover_entity_types())

    results = {
        "orphan .retries":           clean_orphan_retries(execute),
        "empty entities":            clean_empty_entities(execute),
        "stale .harvested entries":  clean_stale_harvested(execute),
        "_placeholder.md stubs":     clean_placeholder_files(execute),
        "double-source fact lines":  collapse_double_sources(execute),
        "stale entities archived":   archive_stale_entities(execute),
        "MOC files written":         generate_mocs(execute),
    }

    verb = "Did" if execute else "Would do"
    print(f"{verb}:")
    for k, v in results.items():
        print(f"  {k:32s} {v}")

    if not execute:
        print("\nDry run. Re-run with --execute to apply.")


if __name__ == "__main__":
    main()
