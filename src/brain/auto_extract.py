#!/usr/bin/env python3
"""Auto-extract entities from raw session files using claude CLI.

Reads each ~/.brain/raw/session-*.md file, sends it to claude -p with an
extraction prompt, applies the resulting entities to the brain, and cleans up.

Called by the SessionStart hook after harvest_session.py.
Uses haiku for cost efficiency.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Prevent recursive invocation: if this env var is set, we're already inside
# an auto_extract → claude -p → SessionStart hook chain. Exit immediately.
if os.environ.get("BRAIN_EXTRACTING"):
    sys.exit(0)

from brain.apply_extraction import apply_extraction
import brain.config as config

# Max raw files to process per hook invocation (stay under timeout)
MAX_FILES_PER_RUN = 5


def get_existing_index() -> str:
    """Read the current brain index, truncated to save tokens.

    Only sends entity names grouped by type — not full descriptions.
    """
    if not config.INDEX_FILE.exists():
        return "(empty brain)"
    text = config.INDEX_FILE.read_text()
    lines = []
    for line in text.split("\n"):
        if line.startswith("- [["):
            # Extract just the entity name from "- [[path|Name]] — description"
            name_start = line.find("|")
            name_end = line.find("]]")
            if name_start > 0 and name_end > name_start:
                entity_name = line[name_start + 1:name_end]
                lines.append(f"- {entity_name}")
        elif line.startswith("## "):
            lines.append(line)
    return "\n".join(lines) if lines else "(empty brain)"


def get_pending_files() -> list[Path]:
    """Get raw session files, oldest first, capped at MAX_FILES_PER_RUN."""
    if not config.RAW_DIR.exists():
        return []
    files = sorted(config.RAW_DIR.glob("session-*.md"), key=lambda f: f.stat().st_mtime)
    return files[:MAX_FILES_PER_RUN]


def load_extract_prompt() -> str:
    """Load the extraction prompt template from prompts/extract_session.md."""
    prompt_path = Path(__file__).parent / "prompts" / "extract_session.md"
    return prompt_path.read_text()


def call_claude(prompt: str) -> str | None:
    """Call claude CLI in print mode with haiku."""
    try:
        env = {**os.environ, "BRAIN_EXTRACTING": "1"}
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--no-session-persistence"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        print(f"claude exit={result.returncode} stderr={result.stderr[:500]!r}", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("claude call timed out after 180s", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("claude binary not found on PATH", file=sys.stderr)
        return None


def parse_extraction(raw_output: str) -> dict | None:
    """Parse JSON from claude output, handling markdown fences."""
    text = raw_output.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in the output
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return None
    return None


def get_retry_count(raw_file: Path) -> int:
    """Get the retry count for a raw file."""
    retry_marker = raw_file.with_suffix(".retries")
    if retry_marker.exists():
        try:
            return int(retry_marker.read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def increment_retry(raw_file: Path) -> None:
    """Increment the retry counter for a raw file."""
    retry_marker = raw_file.with_suffix(".retries")
    count = get_retry_count(raw_file) + 1
    retry_marker.write_text(str(count))


def cleanup_file(raw_file: Path) -> None:
    """Remove a raw file and its retry marker."""
    raw_file.unlink(missing_ok=True)
    raw_file.with_suffix(".retries").unlink(missing_ok=True)


def process_file(raw_file: Path) -> str:
    """Process a single raw session file end-to-end. Returns status string."""
    session_content = raw_file.read_text()
    existing_index = get_existing_index()

    prompt_template = load_extract_prompt()
    prompt = prompt_template.replace(
        "{existing_entities}", existing_index
    ).replace(
        "{conversation_summary}", session_content
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

    source_label = raw_file.stem
    apply_extraction(extraction, source_label)
    cleanup_file(raw_file)
    return "ok"


def main():
    pending = get_pending_files()
    if not pending:
        print("No pending files in ~/.brain/raw/")
        return

    total = len(pending)
    counts = {"ok": 0, "llm_fail_retry": 0, "llm_fail_dropped": 0, "parse_fail_dropped": 0}
    print(f"Extracting {total} file(s)...", flush=True)

    for i, raw_file in enumerate(pending, 1):
        status = process_file(raw_file)
        counts[status] = counts.get(status, 0) + 1
        print(f"[{i}/{total}] {raw_file.name} → {status}", flush=True)

    summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    print(f"Done. {summary}")


if __name__ == "__main__":
    main()
