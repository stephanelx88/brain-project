#!/usr/bin/env python3
"""Ingest arbitrary files into the brain.

Usage:
    python3 -m brain.ingest /path/to/file.md
    python3 -m brain.ingest /path/to/notes.txt
    python3 -m brain.ingest /path/to/data.csv

Reads the file, sends content to haiku for entity extraction,
and applies results to the brain via apply_extraction.
"""

import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

from brain.apply_extraction import apply_extraction
from brain.auto_extract import call_claude, get_existing_index, parse_extraction

SUPPORTED_EXTENSIONS = {".md", ".txt", ".csv", ".tsv", ".log", ".json", ".yaml", ".yml"}


def load_ingest_prompt() -> str:
    """Load the file ingestion prompt template."""
    prompt_path = Path(__file__).parent / "prompts" / "ingest_file.md"
    return prompt_path.read_text()


def read_file_content(file_path: Path) -> str:
    """Read file content, handling different formats."""
    suffix = file_path.suffix.lower()

    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        with open(file_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=delimiter)
            rows = list(reader)
        # Format as readable table
        if not rows:
            return "(empty file)"
        lines = []
        for row in rows:
            lines.append(" | ".join(row))
        return "\n".join(lines)

    # Default: read as text
    return file_path.read_text(encoding="utf-8")


def ingest_file(file_path: Path) -> dict:
    """Ingest a single file into the brain.

    Returns dict with created/updated entity counts.
    """
    if not file_path.exists():
        print(f"Error: {file_path} not found")
        sys.exit(1)

    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        print(f"Error: unsupported file type '{suffix}'")
        print(f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        sys.exit(1)

    content = read_file_content(file_path)
    if not content.strip():
        print(f"Error: {file_path} is empty")
        sys.exit(1)

    # Truncate very large files to stay within haiku context
    max_chars = 30_000
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n... [truncated at 30k chars]"

    existing_index = get_existing_index()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt_template = load_ingest_prompt()
    prompt = prompt_template.replace(
        "{existing_entities}", existing_index
    ).replace(
        "{filename}", file_path.name
    ).replace(
        "{file_type}", suffix.lstrip(".")
    ).replace(
        "{date}", today
    ).replace(
        "{content}", content
    )

    print(f"Extracting from {file_path.name}...")
    output = call_claude(prompt)
    if not output:
        print("Error: claude extraction failed")
        sys.exit(1)

    extraction = parse_extraction(output)
    if not extraction:
        print("Error: could not parse extraction output")
        sys.exit(1)

    source_label = f"ingest:{file_path.name}"
    result = apply_extraction(extraction, source_label)

    created = result.get("created", [])
    updated = result.get("updated", [])
    print(f"Done: {len(created)} created, {len(updated)} updated")
    if created:
        for entity in created:
            print(f"  + {entity}")
    if updated:
        for entity in updated:
            print(f"  ~ {entity}")

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 -m brain.ingest <file_path>")
        print(f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        sys.exit(1)

    file_path = Path(sys.argv[1]).resolve()
    ingest_file(file_path)


if __name__ == "__main__":
    main()
