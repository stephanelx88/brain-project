"""Brain system configuration and paths.

Entity types are discovered dynamically from the filesystem. The default
seed types (people, projects, domains, ...) come from
`~/.brain/brain-config.yaml`'s `entity_types:` list when present (written
by `brain init`); otherwise we fall back to a developer-flavoured set so
the brain remains useful without any setup.

Extraction is never restricted to seed types — any folder under entities/
is a valid type at runtime, discovered via `_discover_entity_types`.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_BRAIN_DIR = Path.home() / ".brain"


def _resolve_brain_dir() -> Path:
    """Resolve the brain vault location.

    Resolution order:
      1. BRAIN_DIR environment variable (supports ~ and env expansion)
      2. ~/.brain (default)

    Users can point this at any folder — e.g. an existing Obsidian vault:
        export BRAIN_DIR="/Users/son/Documents/brain-2/stephane-brain/brain-stephane"
    """
    raw = os.environ.get("BRAIN_DIR")
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    return DEFAULT_BRAIN_DIR


BRAIN_DIR = _resolve_brain_dir()
IDENTITY_DIR = BRAIN_DIR / "identity"
ENTITIES_DIR = BRAIN_DIR / "entities"
TIMELINE_DIR = BRAIN_DIR / "timeline"
RAW_DIR = BRAIN_DIR / "raw"
GRAPHIFY_DIR = BRAIN_DIR / "graphify-out"

INDEX_FILE = BRAIN_DIR / "index.md"
LOG_FILE = BRAIN_DIR / "log.md"
CONFIG_FILE = BRAIN_DIR / "brain-config.yaml"

# Graph (ODB) layer
GRAPH_STORE_DIR = BRAIN_DIR / ".brain.rdf"
PENDING_TRIPLES_PATH = BRAIN_DIR / "pending_triples.jsonl"
TRIPLE_RULES_PATH = IDENTITY_DIR / "triple_rules.jsonl"
TRIPLE_RULES_MD_PATH = IDENTITY_DIR / "triple_rules.md"
PREDICATE_REGISTRY_PATH = IDENTITY_DIR / "predicates.jsonl"

# Hard-coded fallback when no preset has been picked yet. Kept tiny so a
# fresh install is still usable without `brain init`.
_DEFAULT_SEED_TYPES = ["people", "projects", "domains"]


def _read_seed_types_from_config() -> list[str] | None:
    """Return the user-configured entity_types list, or None on any error.

    PyYAML is an optional dep at the package level (it ships with the
    `init` extra). Importing it lazily keeps `import brain.config` cheap
    and avoids forcing the dep on read-only consumers like the MCP
    server's hot path."""
    if not CONFIG_FILE.exists():
        return None
    try:
        import yaml
        data = yaml.safe_load(CONFIG_FILE.read_text())
    except Exception:
        return None
    # safe_load returns whatever YAML parses to — could be None, str, list, dict.
    # Anything that isn't a dict means the file is unusable for our purposes.
    if not isinstance(data, dict):
        return None
    types = data.get("entity_types")
    if isinstance(types, list) and all(isinstance(t, str) and t for t in types):
        return types
    return None


SEED_TYPES: list[str] = _read_seed_types_from_config() or _DEFAULT_SEED_TYPES


def _discover_entity_types() -> dict[str, Path]:
    """Return {type_name: folder_path} for every subfolder of entities/."""
    found: dict[str, Path] = {}
    if ENTITIES_DIR.exists():
        for child in ENTITIES_DIR.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                found[child.name] = child
    for t in SEED_TYPES:
        found.setdefault(t, ENTITIES_DIR / t)
    return found


ENTITY_TYPES = _discover_entity_types()


def get_or_create_type_dir(type_name: str) -> Path:
    """Ensure a type folder exists and is registered. Returns its path."""
    if type_name in ENTITY_TYPES:
        ENTITY_TYPES[type_name].mkdir(parents=True, exist_ok=True)
        return ENTITY_TYPES[type_name]
    type_dir = ENTITIES_DIR / type_name
    type_dir.mkdir(parents=True, exist_ok=True)
    ENTITY_TYPES[type_name] = type_dir
    return type_dir


def ensure_dirs() -> None:
    """Create all brain directories if they don't exist."""
    for t in SEED_TYPES:
        (ENTITIES_DIR / t).mkdir(parents=True, exist_ok=True)
    ENTITY_TYPES.update(_discover_entity_types())
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    (TIMELINE_DIR / "weekly").mkdir(exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    GRAPHIFY_DIR.mkdir(parents=True, exist_ok=True)
