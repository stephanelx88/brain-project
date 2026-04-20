"""Rebuild the brain index.md from all entity files."""

import re
from pathlib import Path

import brain.config as config


def _extract_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter as a simple dict.

    Tolerant of malformed files: a missing closing `---` returns `{}`
    instead of raising ValueError (which previously crashed the entire
    rebuild_index() call mid-loop).
    """
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("---", 3)
    except ValueError:
        return {}
    fm_text = text[3:end].strip()
    result = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _first_sentence(text: str) -> str:
    """Extract first non-frontmatter, non-header sentence."""
    in_frontmatter = False
    for line in text.split("\n"):
        if line.strip() == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Return first real content line, truncated
        return line[:120] + ("..." if len(line) > 120 else "")
    return ""


def rebuild_index() -> None:
    """Rebuild index.md from all entity folders currently on disk."""
    sections = []

    # Refresh runtime dict so newly-created folders show up.
    config.ENTITY_TYPES.update(config._discover_entity_types())
    type_keys = sorted(config.ENTITY_TYPES.keys())

    for type_key in type_keys:
        type_dir = config.ENTITY_TYPES[type_key]
        label = type_key.replace("-", " ").title()
        if not type_dir.exists():
            sections.append(f"## {label}\n_No entities yet._\n")
            continue

        # Skip machine-managed files (`_MOC.md`, `_placeholder.md`, etc.) —
        # they're scaffolding, not entities. Other modules (auto_extract,
        # clean) already filter them out; without this filter index.md
        # ends up listing every type's MOC as if it were an entity.
        files = sorted(p for p in type_dir.glob("*.md") if not p.name.startswith("_"))
        if not files:
            sections.append(f"## {label}\n_No entities yet._\n")
            continue

        lines = [f"## {label}"]
        for f in files:
            text = f.read_text()
            fm = _extract_frontmatter(text)
            name = fm.get("name", f.stem.replace("-", " ").title())
            status = fm.get("status", "current")
            summary = _first_sentence(text)
            rel_path = f.relative_to(config.BRAIN_DIR)
            status_marker = ""
            if status == "contested":
                status_marker = " ⚡"
            elif status == "superseded":
                status_marker = " ~~"
            lines.append(f"- [[{rel_path}|{name}]]{status_marker} — {summary}")
        sections.append("\n".join(lines) + "\n")

    content = "# Brain Index\n\nEntity catalog for fast lookup. Updated automatically.\n\n"
    content += "\n".join(sections)

    config.INDEX_FILE.write_text(content)
