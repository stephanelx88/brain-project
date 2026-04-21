"""Validation guard for extraction output.

Rejects entities whose type is not in the configured allowed set, stopping
the LLM from inventing categories and keeping the entity graph predictable.

The allowed-type list is read from config.SEED_TYPES (which mirrors
brain-config.yaml's `entity_types:`), plus any types already on disk.
"""
from __future__ import annotations

from brain import config


def _allowed_types() -> frozenset[str]:
    types = set(config.SEED_TYPES)
    types.update(config.ENTITY_TYPES.keys())
    return frozenset(types)


def validate_entity(item: dict) -> tuple[bool, str]:
    """Return (is_valid, rejection_reason).

    Checks:
    - entity_type is non-empty and in the allowed set
    - name is non-empty
    """
    entity_type = (item.get("type") or "").strip().lower()
    name = (item.get("name") or "").strip()

    if not entity_type:
        return False, "missing entity type"
    if not name:
        return False, "missing entity name"

    allowed = _allowed_types()
    if entity_type not in allowed:
        return False, f"type '{entity_type}' not in allowed set: {sorted(allowed)}"

    return True, ""
