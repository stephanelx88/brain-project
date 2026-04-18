"""Brain system configuration and paths."""

from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
IDENTITY_DIR = BRAIN_DIR / "identity"
ENTITIES_DIR = BRAIN_DIR / "entities"
TIMELINE_DIR = BRAIN_DIR / "timeline"
RAW_DIR = BRAIN_DIR / "raw"
GRAPHIFY_DIR = BRAIN_DIR / "graphify-out"

INDEX_FILE = BRAIN_DIR / "index.md"
LOG_FILE = BRAIN_DIR / "log.md"

ENTITY_TYPES = {
    "people": ENTITIES_DIR / "people",
    "clients": ENTITIES_DIR / "clients",
    "projects": ENTITIES_DIR / "projects",
    "domains": ENTITIES_DIR / "domains",
    "decisions": ENTITIES_DIR / "decisions",
    "issues": ENTITIES_DIR / "issues",
    "insights": ENTITIES_DIR / "insights",
    "evolutions": ENTITIES_DIR / "evolutions",
}


def ensure_dirs():
    """Create all brain directories if they don't exist."""
    for d in ENTITY_TYPES.values():
        d.mkdir(parents=True, exist_ok=True)
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    (TIMELINE_DIR / "weekly").mkdir(exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    GRAPHIFY_DIR.mkdir(parents=True, exist_ok=True)
