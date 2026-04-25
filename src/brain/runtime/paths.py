"""Filesystem layout for the runtime transport layer.

All paths live under BRAIN_RUNTIME_DIR (default ~/.brain-runtime),
which is intentionally OUTSIDE BRAIN_DIR so transport never touches
the curated-knowledge pipeline.
"""
from __future__ import annotations

import os
from pathlib import Path


def runtime_root() -> Path:
    """Resolve the runtime root from BRAIN_RUNTIME_DIR, default ~/.brain-runtime."""
    raw = os.environ.get("BRAIN_RUNTIME_DIR")
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    return Path.home() / ".brain-runtime"


def inbox_dir() -> Path:
    return runtime_root() / "inbox"


def inbox_pending_dir(session_uuid: str) -> Path:
    return inbox_dir() / session_uuid / "pending"


def inbox_delivered_dir(session_uuid: str) -> Path:
    return inbox_dir() / session_uuid / "delivered"


def names_dir() -> Path:
    return runtime_root() / "names"


def name_file(session_uuid: str) -> Path:
    return names_dir() / f"{session_uuid}.json"


def hook_log_path() -> Path:
    return runtime_root() / "log" / "inbox-hook.log"
