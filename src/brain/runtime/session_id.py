"""Detect own session UUID for tools and hooks running inside a session.

Resolution chain:
  1. CLAUDE_SESSION_ID env var (if Claude Code exposes it)
  2. Parent PID lookup against ~/.claude/sessions/<pid>.json (matches
     brain.live_sessions' liveness check)
  3. None — caller decides how to surface the failure
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _claude_sessions_dir() -> Path:
    return Path.home() / ".claude" / "sessions"


def _get_ppid() -> int:
    return os.getppid()


def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s or ""))


def detect_own_uuid() -> Optional[str]:
    """Return calling process's session UUID, or None if undetectable.

    Tries CLAUDE_SESSION_ID env first; falls back to PPID lookup.
    """
    env = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if env and _is_uuid(env):
        return env
    # If env is set but malformed, don't trust it — fall through.

    sdir = _claude_sessions_dir()
    if sdir.is_dir():
        ppid = _get_ppid()
        cand = sdir / f"{ppid}.json"
        if cand.exists():
            try:
                data = json.loads(cand.read_text())
            except (OSError, json.JSONDecodeError):
                return None
            # Claude Code writes the key as `sessionId` (camelCase) in
            # ~/.claude/sessions/<pid>.json — verified against real
            # files on 2026-04-26 + matches brain.harvest_session
            # convention. The legacy `session_id` snake_case is kept as
            # fallback for tests + forward-compat if Claude renames.
            sid = (data.get("sessionId")
                   or data.get("session_id")
                   or "").strip()
            if _is_uuid(sid):
                return sid

    return None


def short_id_for_default_name(uuid: str, *, source: str) -> str:
    """Choose the per-session short id used in the default name.

    Claude:  parent PID (5 digits typically, matches `ps` output)
    Cursor:  first 8 chars of UUID (no PID mapping available)
    """
    if source == "claude":
        return str(_get_ppid())
    return (uuid or "").split(":", 1)[-1][:8]
