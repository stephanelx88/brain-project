"""Brain system configuration and paths.

Entity types are discovered dynamically from the filesystem. The default
seed types (people, projects, domains, etc.) create empty directories the
first time `ensure_dirs()` runs so the brain starts with a useful skeleton.
Extraction is free to introduce new types; any folder under entities/ is a
valid type at runtime.
"""

from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
IDENTITY_DIR = BRAIN_DIR / "identity"
ENTITIES_DIR = BRAIN_DIR / "entities"
TIMELINE_DIR = BRAIN_DIR / "timeline"
RAW_DIR = BRAIN_DIR / "raw"
GRAPHIFY_DIR = BRAIN_DIR / "graphify-out"

INDEX_FILE = BRAIN_DIR / "index.md"
LOG_FILE = BRAIN_DIR / "log.md"

# Seed types used when a fresh brain is initialized. Extraction is not
# limited to these — any folder under entities/ is a valid type.
SEED_TYPES = ["people", "projects", "domains"]


def _discover_entity_types() -> dict[str, Path]:
    """Return {type_name: folder_path} for every subfolder of entities/."""
    found = {}
    if ENTITIES_DIR.exists():
        for child in ENTITIES_DIR.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                found[child.name] = child
    for t in SEED_TYPES:
        found.setdefault(t, ENTITIES_DIR / t)
    return found


# Populated at import time; extend at runtime via get_or_create_type_dir().
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


def ensure_dirs():
    """Create all brain directories if they don't exist."""
    for t in SEED_TYPES:
        (ENTITIES_DIR / t).mkdir(parents=True, exist_ok=True)
    # Refresh the runtime dict to include the seed dirs
    ENTITY_TYPES.update(_discover_entity_types())
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    (TIMELINE_DIR / "weekly").mkdir(exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    GRAPHIFY_DIR.mkdir(parents=True, exist_ok=True)
