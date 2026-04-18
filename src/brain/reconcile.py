"""Reconciliation: scan brain for conflicts, low-confidence facts, duplicates."""

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import brain.config as config
from brain.config import BRAIN_DIR, ENTITIES_DIR, ENTITY_TYPES, TIMELINE_DIR


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


def find_possible_duplicates() -> str:
    """Find entities with similar names that might be the same."""
    all_names = {}
    for type_key, type_dir in ENTITY_TYPES.items():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            slug = f.stem
            words = set(slug.split("-"))
            all_names[f"{type_key}/{slug}"] = words

    duplicates = []
    keys = list(all_names.keys())
    for i, k1 in enumerate(keys):
        for k2 in keys[i + 1 :]:
            # Same type only
            if k1.split("/")[0] != k2.split("/")[0]:
                continue
            overlap = all_names[k1] & all_names[k2]
            union = all_names[k1] | all_names[k2]
            if len(overlap) / len(union) > 0.5 and len(overlap) >= 2:
                duplicates.append(f"- **{k1}** and **{k2}** share: {', '.join(overlap)}")

    return "\n".join(duplicates) if duplicates else "None found."


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
    path.write_text(content)
    return path
