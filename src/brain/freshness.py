"""Per-source watermarks for `mcp_server._ensure_fresh`.

Today every read-tool call triggers three unconditional sweeps:
  1. sync_mutated_entities (stat every entity file)
  2. ingest_notes.ingest_all (walk the vault, diff against db)
  3. semantic.ensure_built (incremental embed probe)

Even when nothing changed, the walks cost ~10-40 ms each. This module
records, per source-dir, the wall-clock epoch at which we last
completed a successful sweep. A sweep is only re-run when the dir's
newest file mtime advanced past the watermark — so an idle vault pays
a single recursive `stat` on the root, not three walks.

Storage: `<BRAIN_DIR>/.freshness.json`, format:

    {"entities": 1714200000.0, "notes": 1714200000.0, "raw": 1714200000.0}

`_path()` is resolved per-call so tests that monkeypatch
`config.BRAIN_DIR` see the override (mirrors predicate_registry._path).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import brain.config as config
from brain.io import atomic_write_text


WATERMARK_KEYS: tuple[str, ...] = ("entities", "notes", "raw")

# Dirs the notes walker already skips — keep in sync with
# ingest_notes.EXCLUDE_DIR_NAMES + a few machine-only paths a watermark
# scan should not bother descending into.
_SKIP_DIR_NAMES: frozenset[str] = frozenset({
    "entities", "raw", "_archive", "logs",
    ".obsidian", ".git", ".vec", ".extract.lock.d",
    "node_modules", ".trash", ".brain.rdf",
})


def _path() -> Path:
    return config.BRAIN_DIR / ".freshness.json"


def load() -> dict[str, float]:
    """Return the watermark dict, with all known keys populated (0.0 default)."""
    out: dict[str, float] = {k: 0.0 for k in WATERMARK_KEYS}
    p = _path()
    if not p.exists():
        return out
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    for k in WATERMARK_KEYS:
        v = data.get(k)
        if isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def save(updates: dict[str, float]) -> None:
    """Merge `updates` into the on-disk watermark. Silent-fail on OSError
    (mirrors predicate_registry.save — a watermark write that fails just
    means the next sweep will re-run; never bubble up to the hot path).
    """
    current = load()
    current.update({k: float(v) for k, v in updates.items() if k in WATERMARK_KEYS})
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_path(), json.dumps(current, indent=2) + "\n")
    except OSError:
        pass


def bump(key: str, when: float | None = None) -> None:
    """Advance watermark[key] to `when` (default: now). No-op if key unknown."""
    if key not in WATERMARK_KEYS:
        return
    save({key: when if when is not None else time.time()})


def entities_dir_mtime() -> float:
    """Max mtime across every `*.md` under entities/. Zero on missing dir.

    Cost: O(N files) — same as today's `sync_mutated_entities`' first
    pass but without the per-file DB lookup. On a vault with ~500
    entities this is ~5 ms on a warm page cache.
    """
    root = config.ENTITIES_DIR
    if not root.exists():
        return 0.0
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if not f.endswith(".md"):
                continue
            try:
                m = (Path(dirpath) / f).stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest = m
    return newest


def notes_dir_mtime() -> float:
    """Max mtime across every `*.md` under BRAIN_DIR that `ingest_notes`
    would ingest — i.e. the vault root minus machine-managed subtrees.
    Zero when the vault is absent."""
    root = config.BRAIN_DIR
    if not root.exists():
        return 0.0
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dpath = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIR_NAMES and not d.startswith(".")
        ]
        for f in filenames:
            if not f.endswith(".md") or f.startswith("_"):
                continue
            try:
                m = (dpath / f).stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest = m
    return newest


def needs_sweep(key: str, probe_mtime: float | None = None) -> bool:
    """Return True when the source dir has advanced past its watermark.

    `probe_mtime` can be supplied by the caller when it already knows
    the dir's newest mtime (avoids a redundant walk). When None, this
    function walks the dir itself.
    """
    if key not in WATERMARK_KEYS:
        return True
    watermark = load().get(key, 0.0)
    if probe_mtime is None:
        if key == "entities":
            probe_mtime = entities_dir_mtime()
        elif key == "notes":
            probe_mtime = notes_dir_mtime()
        else:
            probe_mtime = 0.0
    # Slack of 1 ms guards against filesystems whose mtime resolution
    # tops out at the second (ext4 noatime/relatime + a watermark
    # written in the same second as the file write).
    return probe_mtime > watermark + 1e-3
