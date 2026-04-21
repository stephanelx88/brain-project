"""Entity page CRUD operations for the brain."""

from datetime import datetime, timezone
from pathlib import Path

from brain.io import atomic_write_text
from brain.slugify import slugify, validate_slug
import brain.config as config

# Irregular plurals — used as frontmatter `type` values. For any other type
# name, we fall back to stripping a trailing "s" if present, else use the
# name as-is. Extraction picks these names freely, so this map only captures
# the handful of irregular English plurals we seed the brain with.
_IRREGULAR_SINGULAR = {
    "people": "person",
}


def _singular_type(entity_type: str) -> str:
    if entity_type in _IRREGULAR_SINGULAR:
        return _IRREGULAR_SINGULAR[entity_type]
    if entity_type.endswith("s") and len(entity_type) > 1:
        return entity_type[:-1]
    return entity_type


def entity_path(entity_type: str, name: str) -> Path:
    """Get the file path for an entity. Creates the type folder on demand."""
    type_dir = config.get_or_create_type_dir(entity_type)
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
        "type": _singular_type(entity_type),
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
    atomic_write_text(path, content)
    return path


_FACT_PREFIX = "- "


def _normalize_fact(line: str) -> str:
    """Strip leading bullet, source-suffix annotations, and whitespace.

    Used as the dedup key so the same fact captured from two sessions
    is recognised even when its `(source: …, date)` suffixes differ.
    """
    s = line.strip()
    if s.startswith(_FACT_PREFIX):
        s = s[len(_FACT_PREFIX):]
    # Drop any number of trailing "(source: …)" annotations
    while True:
        i = s.rfind("(source:")
        if i == -1:
            break
        j = s.find(")", i)
        if j == -1:
            break
        s = (s[:i] + s[j + 1:]).strip()
    return s.lower()


def append_to_entity_path(path: Path, section: str, content: str) -> Path:
    """Append content to a section of an entity file *addressed by path*.

    Why a path-based variant: callers like `brain.dedupe.apply_merge`
    already know the exact winner file (e.g. `2026-04-11-foo.md`) and
    must NOT re-derive it from the frontmatter `name:` via slugify —
    real slugs often carry date prefixes / disambiguators that the
    plain `slugify(name)` round-trip can't reconstruct, which used to
    surface as `Entity not found` errors mid-merge.

    Same dedup-by-normalised-fact semantics as `append_to_entity`.
    """
    if not path.exists():
        raise FileNotFoundError(f"Entity not found: {path}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = path.read_text()

    existing_facts = {
        _normalize_fact(line)
        for line in text.split("\n")
        if line.lstrip().startswith(_FACT_PREFIX)
    }
    new_lines = []
    for line in content.split("\n"):
        if not line.strip():
            continue
        key = _normalize_fact(line) if line.lstrip().startswith(_FACT_PREFIX) else None
        if key and key in existing_facts:
            continue
        if key:
            existing_facts.add(key)
        new_lines.append(line)
    if not new_lines:
        return path
    new_content = "\n".join(new_lines)

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

    section_header = f"## {section}"
    if section_header in text:
        idx = text.index(section_header) + len(section_header)
        next_newline = text.index("\n", idx)
        text = text[:next_newline] + f"\n{new_content}" + text[next_newline:]
    else:
        text = text.rstrip() + f"\n\n{section_header}\n{new_content}\n"

    atomic_write_text(path, text)
    return path


def append_to_entity(entity_type: str, name: str, section: str, content: str) -> Path:
    """Append content to a section of an existing entity page, addressed
    by `(entity_type, name)`. Thin wrapper over `append_to_entity_path`
    that resolves the path via slugify — only safe when the caller
    knows the entity was created from this exact name (no date prefix
    or other disambiguator)."""
    path = entity_path(entity_type, name)
    if not path.exists():
        raise FileNotFoundError(f"Entity not found: {entity_type}/{name}")
    return append_to_entity_path(path, section, content)


def list_entities(entity_type: str) -> list[str]:
    """List all entity names of a given type."""
    type_dir = config.ENTITY_TYPES.get(entity_type)
    if type_dir is None or not type_dir.exists():
        return []
    return [p.stem for p in type_dir.glob("*.md")]
