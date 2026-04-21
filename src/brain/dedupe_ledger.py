"""Ledger persistence for brain.dedupe — extracted from dedupe.py.

The ledger records which entity pairs have already been judged so a
subsequent run skips them unless one of the files changed.
"""

from __future__ import annotations

from pathlib import Path

import brain.config as config
from brain.io import atomic_write_text

try:
    import json as _json
except ImportError:  # pragma: no cover
    raise

LEDGER_PATH: Path = config.BRAIN_DIR / ".dedupe.ledger.json"


def load() -> dict:
    if not LEDGER_PATH.exists():
        return {}
    try:
        return _json.loads(LEDGER_PATH.read_text())
    except Exception:
        return {}


def save(led: dict) -> None:
    atomic_write_text(LEDGER_PATH, _json.dumps(led, indent=2, sort_keys=True))


def pair_key(slug_a: str, slug_b: str, type_: str) -> str:
    a, b = sorted([slug_a, slug_b])
    return f"{type_}|{a}|{b}"


def file_mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except FileNotFoundError:
        return 0


def should_skip(led: dict, key: str, mtime_a: int, mtime_b: int) -> bool:
    """Skip a pair only when the ledger entry was recorded against the same
    pair of mtimes. A real edit on either file invalidates the cache."""
    rec = led.get(key)
    if not rec:
        return False
    return rec.get("mtime_a") == mtime_a and rec.get("mtime_b") == mtime_b
