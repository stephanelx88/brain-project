"""Ensure the brain owner has a `people/` anchor entity.

Facts stated by or about the brain's owner ("Son ate bún riêu",
"Stephane manages the X project") need a landing entity — otherwise
the extractor either invents one, attaches them to the wrong person,
or silently drops them. This module writes a stub at
``entities/people/<owner-slug>.md`` on first run so every downstream
extraction pass has a canonical anchor to attach self-facts to.

Owner identity is resolved in priority order:
  1. ``brain-config.yaml`` ``identity.display_name`` (if set)
  2. ``brain-config.yaml`` ``identity.name``
  3. ``$USER`` environment variable

Idempotent: if the entity file already exists, this is a no-op.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import brain.config as config
from brain.io import atomic_write_text
from brain.slugify import slugify


def _resolve_owner() -> tuple[str, str] | None:
    """Return ``(slug, display_name)`` for the brain owner.

    Prefers ``identity.display_name`` from brain-config.yaml because
    config authors typically set ``identity.name`` to a GitHub handle
    (e.g. ``stephanelx88``) which is ugly as a people entity name.
    Falls back to ``identity.name`` and finally to ``$USER``.
    """
    display_name = ""
    if config.CONFIG_FILE.exists():
        try:
            import yaml
            data = yaml.safe_load(config.CONFIG_FILE.read_text()) or {}
            ident = data.get("identity") or {}
            display_name = (
                (ident.get("display_name") or ident.get("name") or "").strip()
            )
        except Exception:
            display_name = ""

    if not display_name:
        display_name = os.environ.get("USER", "").strip()

    if not display_name:
        return None

    slug = slugify(display_name)
    if not slug:
        return None
    return slug, display_name


def ensure_self_entity() -> Path | None:
    """Create ``entities/people/<owner-slug>.md`` if missing.

    Returns the path when a file was created, ``None`` on no-op
    (already exists, owner unknown, or disk error).
    """
    resolved = _resolve_owner()
    if resolved is None:
        return None
    slug, display_name = resolved

    people_dir = config.ENTITIES_DIR / "people"
    try:
        people_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    target = people_dir / f"{slug}.md"
    if target.exists():
        return None

    today = date.today().isoformat()
    content = (
        "---\n"
        "type: people\n"
        f"name: {display_name}\n"
        f"first_seen: {today}\n"
        "source_count: 1\n"
        "---\n\n"
        f"# {display_name}\n\n"
        f"Brain owner — anchor entity for facts stated by or about "
        f"{display_name} in sessions. Extraction pipeline attaches "
        "self-facts here.\n"
    )
    try:
        atomic_write_text(target, content)
    except OSError:
        return None
    return target


def owner_display_name() -> str | None:
    """Public helper — the display_name the extractor should reuse."""
    resolved = _resolve_owner()
    return resolved[1] if resolved else None
