"""Live session discovery and tail — extracted from mcp_server.py.

All functions return Python objects; JSON serialisation is the caller's
responsibility so this module can be used and tested without MCP.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from brain import harvest_session


def find_session_jsonl(session_id: str) -> Path | None:
    """Resolve a session_id to its on-disk transcript jsonl. None if not found.

    Accepts: Claude UUID, `cursor:<uuid>`, or bare Cursor UUID.
    """
    want_cursor_only = session_id.startswith(harvest_session.CURSOR_PREFIX)
    bare = session_id.split(":", 1)[-1]
    candidates: list[Path] = []
    if not want_cursor_only:
        candidates.extend(harvest_session.find_all_session_jsonls())
    try:
        candidates.extend(harvest_session.find_cursor_session_jsonls())
    except Exception:
        pass
    for p in candidates:
        if p.stem == bare:
            return p
    return None


def list_live_sessions(
    active_within_sec: int = 300,
    include_self: bool = False,
) -> list[dict]:
    """Return live Claude + Cursor sessions sorted by age_sec asc.

    activity rule:
      - Claude:  PID in ~/.claude/sessions/<pid>.json is alive
      - Cursor:  transcript jsonl mtime within `active_within_sec` s

    Each row: {source, session_id, project, cwd, pid, last_write, age_sec, path}.
    `active_within_sec` is clamped to [1, 86400].
    """
    window = max(1, min(int(active_within_sec), 86400))
    now = datetime.now(timezone.utc)
    out: list[dict] = []

    self_sid: str | None = None
    if not include_self:
        ppid = os.getppid()
        for cs in harvest_session.claude_active_sessions():
            if cs["pid"] == ppid:
                self_sid = cs["session_id"]
                break

    for cs in harvest_session.claude_active_sessions():
        if cs["session_id"] == self_sid:
            continue
        jsonl = find_session_jsonl(cs["session_id"])
        last_write_iso = None
        age = None
        path_str = None
        project = ""
        if jsonl is not None:
            try:
                mtime = jsonl.stat().st_mtime
                last_write_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                age = int(now.timestamp() - mtime)
                path_str = str(jsonl)
                project = harvest_session.derive_project_name(jsonl)
            except OSError:
                pass
        out.append({
            "source": "claude",
            "session_id": cs["session_id"],
            "project": project,
            "cwd": cs["cwd"],
            "pid": cs["pid"],
            "last_write": last_write_iso,
            "age_sec": age,
            "path": path_str,
        })

    cutoff = now.timestamp() - window
    try:
        cursor_jsonls = harvest_session.find_cursor_session_jsonls()
    except Exception:
        cursor_jsonls = []
    for jsonl in cursor_jsonls:
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        out.append({
            "source": "cursor",
            "session_id": f"{harvest_session.CURSOR_PREFIX}{jsonl.stem}",
            "project": harvest_session.derive_project_name(jsonl),
            "cwd": None,
            "pid": None,
            "last_write": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "age_sec": int(now.timestamp() - mtime),
            "path": str(jsonl),
        })

    out.sort(key=lambda r: r.get("age_sec") if r.get("age_sec") is not None else 10**9)
    return out


def tail_live_session(session_id: str, n: int = 20) -> dict:
    """Return last n turns of one live session as a dict.

    Returns {source, session_id, project, last_write, turns, total_turns}
    or {error: ...} on failure.
    """
    n = max(1, min(int(n), 200))
    session_id = (session_id or "").strip()
    if not session_id:
        return {"error": "session_id is required"}

    jsonl = find_session_jsonl(session_id)
    if jsonl is None:
        return {"error": f"session not found: {session_id}"}

    try:
        messages, _ = harvest_session.extract_messages(jsonl, start_offset=0)
    except Exception as e:
        return {"error": f"failed to read transcript: {e}"}

    try:
        mtime = jsonl.stat().st_mtime
        last_write = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except OSError:
        last_write = None

    source = "cursor" if harvest_session.is_cursor_path(jsonl) else "claude"
    return {
        "source": source,
        "session_id": session_id,
        "project": harvest_session.derive_project_name(jsonl),
        "last_write": last_write,
        "turns": messages[-n:],
        "total_turns": len(messages),
    }
