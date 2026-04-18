"""Entity page CRUD operations for the brain."""

from datetime import datetime, timezone
from pathlib import Path

from brain.slugify import slugify, validate_slug
import brain.config as config

# Map plural type names (keys) to singular type names (frontmatter values)
_TYPE_TO_FRONTMATTER_TYPE = {
    "people": "person",
    "clients": "client",
    "projects": "project",
    "domains": "domain",
    "decisions": "decision",
    "issues": "issue",
    "insights": "insight",
    "evolutions": "evolution",
}


def entity_path(entity_type: str, name: str) -> Path:
    """Get the file path for an entity. Creates parent dir if needed."""
    type_dir = config.ENTITY_TYPES.get(entity_type)
    if type_dir is None:
        raise ValueError(f"Unknown entity type: {entity_type}")
    type_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(name)
    validate_slug(slug)
    return type_dir / f"{slug}.md"


def entity_exists(entity_type: str, name: str) -> bool:
    """Check if an entity page already exists."""
    return entity_path(entity_type, name).exists()


def read_entity(entity_type: str, name: str) -> str | None:
    """Read an entity page. Returns None if not found."""
    path = entity_path(entity_type, name)
    if not path.exists():
        return None
    return path.read_text()


def create_entity(
    entity_type: str,
    name: str,
    *,
    frontmatter: dict | None = None,
    body: str = "",
) -> Path:
    """Create a new entity page from template."""
    path = entity_path(entity_type, name)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    fm = {
        "type": _TYPE_TO_FRONTMATTER_TYPE.get(entity_type, entity_type),
        "name": name,
        "status": "current",
        "first_seen": now,
        "last_updated": now,
        "source_count": 1,
        "tags": [],
    }
    if frontmatter:
        fm.update(frontmatter)

    fm_lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            fm_lines.append(f"{k}: {v}")
        elif isinstance(v, str) and any(c in v for c in ":#[]{}"):
            fm_lines.append(f'{k}: "{v}"')
        else:
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")

    content = "\n".join(fm_lines) + f"\n\n# {name}\n\n{body}\n"
    path.write_text(content)
    return path


def append_to_entity(entity_type: str, name: str, section: str, content: str) -> Path:
    """Append content to a section of an existing entity page.

    If the section doesn't exist, creates it.
    Also updates last_updated in frontmatter.
    """
    path = entity_path(entity_type, name)
    if not path.exists():
        raise FileNotFoundError(f"Entity not found: {entity_type}/{name}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = path.read_text()

    # Update last_updated in frontmatter
    lines = text.split("\n")
    updated_lines = []
    for line in lines:
        if line.startswith("last_updated:"):
            updated_lines.append(f"last_updated: {now}")
        elif line.startswith("source_count:"):
            try:
                count = int(line.split(":")[1].strip()) + 1
            except (ValueError, IndexError):
                count = 2
            updated_lines.append(f"source_count: {count}")
        else:
            updated_lines.append(line)
    text = "\n".join(updated_lines)

    # Find or create section
    section_header = f"## {section}"
    if section_header in text:
        # Append after section header
        idx = text.index(section_header) + len(section_header)
        # Find the end of the line
        next_newline = text.index("\n", idx)
        text = text[:next_newline] + f"\n{content}" + text[next_newline:]
    else:
        # Add section at the end
        text = text.rstrip() + f"\n\n{section_header}\n{content}\n"

    path.write_text(text)
    return path


def list_entities(entity_type: str) -> list[str]:
    """List all entity names of a given type."""
    type_dir = config.ENTITY_TYPES.get(entity_type)
    if type_dir is None or not type_dir.exists():
        return []
    return [p.stem for p in type_dir.glob("*.md")]
