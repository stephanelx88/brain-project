"""Preset profiles for `brain init`.

Each YAML file in this package describes a persona — the entity types that
folder will be seeded with, plus identity hints used to render
`identity/who-i-am.md`. Presets are pure metadata; runtime code never
restricts entity types to the preset (folders under entities/ are
auto-discovered by `config._discover_entity_types`).

Add a new preset by dropping `<name>.yaml` here. The wizard picks them up
automatically via `list_presets()`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PRESETS_DIR = Path(__file__).parent


def list_presets() -> list[dict[str, Any]]:
    """Return all preset definitions, sorted by display order."""
    out: list[dict[str, Any]] = []
    for path in sorted(PRESETS_DIR.glob("*.yaml")):
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        data["_slug"] = path.stem
        out.append(data)
    out.sort(key=lambda d: d.get("order", 999))
    return out


def load_preset(slug: str) -> dict[str, Any]:
    """Load one preset by slug (filename without .yaml)."""
    path = PRESETS_DIR / f"{slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No preset named {slug!r} in {PRESETS_DIR}")
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    data["_slug"] = slug
    return data
