#!/usr/bin/env python3
"""Harvest Claude Code session transcripts into ~/.brain/raw/.

Scans all project JSONL files in ~/.claude/projects/, skips already-harvested
sessions, and writes structured summaries to ~/.brain/raw/ for extraction.

Called by the SessionStart hook in ~/.claude/settings.json.
Runs on every new session — harvests whatever ended since last time.

Incremental mode (the default now): a SQLite ledger at
`~/.brain/.harvest.db` records `(session_id → (path, last_byte_offset,
last_ingested_at))`. On each run we `seek()` to the recorded offset and
process only new bytes — no more re-parsing 2000 finished sessions.

The legacy `.harvested` file is still maintained (so the cleanup
script keeps working), but the ledger is authoritative.
"""

import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import brain.config as config

BRAIN_RAW = config.RAW_DIR
HARVESTED_FILE = config.BRAIN_DIR / ".harvested"
LEDGER_DB = config.BRAIN_DIR / ".harvest.db"
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Cursor stores its agent transcripts at
#   ~/.cursor/projects/<workspace-slug>/agent-transcripts/<uuid>/<uuid>.jsonl
# Schema is similar to Claude's but uses `role` instead of `type` and has
# no top-level `timestamp`. We harvest them alongside Claude sessions so
# Cursor work shows up in the brain too.
CURSOR_DIR = Path.home() / ".cursor"
CURSOR_PROJECTS_DIR = CURSOR_DIR / "projects"

# Cursor session IDs are namespaced (`cursor:<uuid>`) so they can't collide
# with Claude UUIDs in the harvest ledger. Existing Claude entries stay
# unprefixed for backward compatibility with the ledger written so far.
CURSOR_PREFIX = "cursor:"

# How recent a Cursor jsonl can be before we treat the session as still
# active and skip it. Cursor has no per-session PID file like Claude, so
# we fall back to a conservative mtime check.
CURSOR_ACTIVE_WINDOW_SEC = 60

MIN_MESSAGES = 4
MAX_AGE_SECONDS = 86400
MAX_MSG_CHARS = 3000
TOOL_INPUT_PREVIEW = 200


@contextmanager
def _ledger():
    LEDGER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER_DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS harvest_state (
              session_id   TEXT PRIMARY KEY,
              path         TEXT NOT NULL,
              byte_offset  INTEGER NOT NULL DEFAULT 0,
              last_seen    REAL NOT NULL
           )"""
    )
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_offset(session_id: str) -> int:
    with _ledger() as conn:
        row = conn.execute(
            "SELECT byte_offset FROM harvest_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def set_offset(session_id: str, path: Path, offset: int) -> None:
    with _ledger() as conn:
        conn.execute(
            """INSERT INTO harvest_state(session_id, path, byte_offset, last_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 path=excluded.path,
                 byte_offset=excluded.byte_offset,
                 last_seen=excluded.last_seen""",
            (session_id, str(path), offset, time.time()),
        )


def load_harvested() -> list[str]:
    """Load list of already-harvested session IDs in insertion order."""
    if not HARVESTED_FILE.exists():
        return []
    return HARVESTED_FILE.read_text().strip().splitlines()


def save_harvested(harvested: list[str]) -> None:
    """Persist harvested session IDs preserving insertion order."""
    HARVESTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    HARVESTED_FILE.write_text("\n".join(harvested) + "\n")


def rotate_harvested(max_entries: int = 2000) -> int:
    """Trim .harvested to at most max_entries, keeping the most recently added."""
    if not HARVESTED_FILE.exists():
        return 0
    lines = HARVESTED_FILE.read_text().strip().splitlines()
    if len(lines) <= max_entries:
        return 0
    trimmed = lines[-max_entries:]
    HARVESTED_FILE.write_text("\n".join(trimmed) + "\n")
    return len(lines) - len(trimmed)


def find_all_session_jsonls() -> list[Path]:
    """Find all Claude session JSONL files across all projects.

    Kept for backward compatibility; the harvester proper iterates the
    union of Claude + Cursor via `_iter_all_sources()`.
    """
    if not PROJECTS_DIR.exists():
        return []
    results = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        results.extend(project_dir.glob("*.jsonl"))
    return results


def find_cursor_session_jsonls() -> list[Path]:
    """Find all Cursor agent transcripts.

    Layout: `~/.cursor/projects/<workspace>/agent-transcripts/<uuid>/<uuid>.jsonl`.
    The `subagents/` subdir under each transcript holds task-subagent
    transcripts; we skip those for now (they're noisy and the parent
    transcript already references the spawn).
    """
    if not CURSOR_PROJECTS_DIR.exists():
        return []
    results: list[Path] = []
    for project_dir in CURSOR_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        transcripts_root = project_dir / "agent-transcripts"
        if not transcripts_root.is_dir():
            continue
        for session_dir in transcripts_root.iterdir():
            if not session_dir.is_dir() or session_dir.name == "subagents":
                continue
            # Each session dir holds one `<uuid>.jsonl` file (and maybe a
            # nested `subagents/` dir we ignore).
            for jsonl in session_dir.glob("*.jsonl"):
                results.append(jsonl)
    return results


def is_cursor_path(jsonl_path: Path) -> bool:
    try:
        jsonl_path.relative_to(CURSOR_PROJECTS_DIR)
        return True
    except ValueError:
        return False


def get_session_id(jsonl_path: Path) -> str:
    """Return the namespaced session ID for a transcript path.

    Claude transcripts return their bare UUID (unchanged — keeps the
    existing ledger valid). Cursor transcripts get a `cursor:` prefix so
    they share the same harvest_state table without UUID collisions.
    """
    if is_cursor_path(jsonl_path):
        return f"{CURSOR_PREFIX}{jsonl_path.stem}"
    return jsonl_path.stem


def extract_messages(jsonl_path: Path, start_offset: int = 0) -> tuple[list[dict], int]:
    """Extract user/assistant messages from a session JSONL.

    Returns (messages, new_byte_offset). If `start_offset` is non-zero we
    `seek()` there first and only parse newly-appended bytes — so a long
    session that's been harvested before only re-parses the latest turns.

    A best-effort sanity check: if the file shrank below `start_offset`
    (e.g. user truncated it manually), we restart from 0.
    """
    messages: list[dict] = []
    file_size = jsonl_path.stat().st_size
    if start_offset > file_size:
        start_offset = 0
    with open(jsonl_path, "rb") as fb:
        # `start_offset` is always set by a previous tell() that landed on
        # a newline boundary, so seeking there is safe — no need to drop
        # a "partial" line. (If somebody edits the file by hand we may
        # parse one slightly-mangled line; json.loads handles that.)
        fb.seek(start_offset)
        new_offset = fb.tell()
        for raw_bytes in fb:
            try:
                line = raw_bytes.decode("utf-8", errors="replace").strip()
            except Exception:
                new_offset += len(raw_bytes)
                continue
            new_offset += len(raw_bytes)
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Claude uses `type`, Cursor uses `role` — accept either.
            # If both are present (unlikely), `type` wins to preserve the
            # historical semantics (Claude harvests are the established
            # baseline).
            entry_type = entry.get("type") or entry.get("role")
            if entry_type not in ("user", "assistant"):
                continue

            msg = entry.get("message", {})
            text = ""

            if isinstance(msg, str):
                text = msg
            elif isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                tool = block.get("name", "unknown")
                                inp = block.get("input", {})
                                # For Write/Edit tools, capture the key semantic fields
                                if tool == "Write":
                                    fp = inp.get("file_path", "")
                                    file_content = inp.get("content", "")[:1000]
                                    parts.append(f"[tool: {tool} → file_path={fp}]\n  content: {file_content[:800]}")
                                elif tool == "Edit":
                                    fp = inp.get("file_path", "")
                                    old = inp.get("old_string", "")[:200]
                                    new = inp.get("new_string", "")[:200]
                                    parts.append(f"[tool: {tool} → {fp}]\n  old: {old}\n  new: {new}")
                                else:
                                    inp_json = json.dumps(inp)
                                    parts.append(f"[tool: {tool} → {inp_json[:TOOL_INPUT_PREVIEW]}]")
                    text = "\n".join(parts)

            text = text.strip()
            if not text:
                continue

            if len(text) > MAX_MSG_CHARS:
                text = text[:MAX_MSG_CHARS] + "\n... [truncated]"

            messages.append({
                "role": entry_type,
                "text": text,
                "timestamp": entry.get("timestamp"),
            })

    return messages, new_offset


def derive_project_name(jsonl_path: Path) -> str:
    """Derive a human-readable project name from the JSONL path.

    Claude lays out `~/.claude/projects/<dir-with-path-encoded>/<uuid>.jsonl`,
    so the project name is `jsonl_path.parent.name`. Cursor lays out
    `~/.cursor/projects/<workspace>/agent-transcripts/<uuid>/<uuid>.jsonl`,
    so we walk one extra level up to reach the workspace dir. Either
    way the directory is `Users-<user>-some-path` style; we strip the
    leading filesystem-path prefix (`Users`, `home`, the current user,
    common desktop folders) so the readable name is just `code/myproj`.

    Username is resolved from `$USER` so this works for anyone — not
    only the original author. (Previously hardcoded as "son".)

    Cursor names get a `cursor/` prefix so it's obvious in the captured
    `session-*.md` whether a turn came from Cursor or Claude Code.
    """
    if is_cursor_path(jsonl_path):
        # …/agent-transcripts/<uuid>/<uuid>.jsonl → workspace is 3 up.
        try:
            workspace_dir_name = jsonl_path.parents[2].name
        except IndexError:
            workspace_dir_name = jsonl_path.parent.name
        prefix = "cursor/"
    else:
        workspace_dir_name = jsonl_path.parent.name
        prefix = ""

    parts = workspace_dir_name.split("-")
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    skip_prefixes = {"", "Users", "home", "Desktop", "Documents", "Downloads"}
    if user:
        skip_prefixes.add(user)
    meaningful: list[str] = []
    for part in parts:
        if part in skip_prefixes and not meaningful:
            continue
        meaningful.append(part)
    base = "/".join(meaningful) if meaningful else workspace_dir_name
    return f"{prefix}{base}"


def format_session_summary(messages: list[dict], project_name: str, session_id: str) -> str:
    """Format extracted messages into a structured session summary."""
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# Session Summary",
        f"- **Project**: {project_name}",
        f"- **Captured**: {timestamp}",
        f"- **Session ID**: {session_id}",
        "",
        "## Conversation",
        "",
    ]

    for msg in messages:
        role = "User" if msg["role"] == "user" else "Claude"
        lines.append(f"### {role}")
        lines.append(msg["text"])
        lines.append("")

    return "\n".join(lines)


def claude_active_sessions() -> list[dict]:
    """Return all Claude Code sessions whose registered PID is still alive.

    Walks `CLAUDE_DIR / "sessions" / *.json` once and returns, for every entry
    whose recorded PID responds to `os.kill(pid, 0)`, a dict of:

        {"session_id": str, "pid": int, "cwd": str}

    JSON decode errors, missing fields, and dead PIDs are silently skipped.
    Cursor sessions are not included (Cursor exposes no PID file).
    """
    sessions_dir = CLAUDE_DIR / "sessions"
    if not sessions_dir.exists():
        return []
    out: list[dict] = []
    for f in sessions_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        sid = data.get("sessionId")
        pid = data.get("pid")
        cwd = data.get("cwd", "") or ""
        if not sid or not pid:
            continue
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError, TypeError):
            continue
        out.append({"session_id": sid, "pid": int(pid), "cwd": cwd})
    return out


def is_active_session(session_id: str) -> bool:
    """Check if a Claude Code session is currently active (PID still alive).

    Cursor doesn't expose a per-session PID file, so for `cursor:` IDs
    this returns False — the harvester instead uses an mtime guard
    (`_cursor_recently_active`) to decide whether to skip a Cursor file.
    """
    if session_id.startswith(CURSOR_PREFIX):
        return False
    return any(cs["session_id"] == session_id for cs in claude_active_sessions())


def _cursor_recently_active(jsonl_path: Path, mtime: float) -> bool:
    """Cursor-specific 'session is live' guard.

    Cursor has no PID file, so we treat any transcript whose mtime is
    within the last `CURSOR_ACTIVE_WINDOW_SEC` seconds as active and
    skip it. The next harvest cycle will pick it up once it's quiet.

    This is the equivalent of the active-session guard that prevents the
    'dual-instance Mac freeze' from the Claude side — never spawn LLM
    work on a session that's still being written.
    """
    return (time.time() - mtime) < CURSOR_ACTIVE_WINDOW_SEC


def harvest_all() -> int:
    """Harvest all unprocessed, non-active sessions. Returns count harvested.

    Sources scanned (union):
      - `~/.claude/projects/*/*.jsonl`             (Claude Code)
      - `~/.cursor/projects/*/agent-transcripts/*/*.jsonl`  (Cursor)

    Two-tier dedup, shared across both sources:
      1. SQLite ledger (`get_offset` / `set_offset`) — authoritative,
         records *byte offset* so we resume long sessions incrementally.
         Cursor IDs are namespaced (`cursor:<uuid>`) so they can't collide.
      2. Legacy `.harvested` text file — kept in sync so other tooling
         (e.g. `brain.clean.clean_stale_harvested`) still works.

    A session is harvested when:
      - it's not currently active (Claude PID alive, or Cursor file
        touched in the last CURSOR_ACTIVE_WINDOW_SEC seconds),
      - its file mtime is within MAX_AGE_SECONDS, AND
      - either it has no offset yet OR new bytes appeared since last run.

    Failures on one source never abort the other — Cursor is best-effort.
    """
    harvested = load_harvested()
    seen = set(harvested)
    all_jsonls = list(find_all_session_jsonls())
    try:
        all_jsonls.extend(find_cursor_session_jsonls())
    except Exception:
        # Cursor's directory layout could change without warning; we don't
        # want a Cursor failure to break the (proven) Claude harvest path.
        pass
    cutoff_time = time.time() - MAX_AGE_SECONDS
    count = 0

    def mark(session_id: str) -> None:
        if session_id not in seen:
            harvested.append(session_id)
            seen.add(session_id)

    for jsonl_path in all_jsonls:
        session_id = get_session_id(jsonl_path)

        try:
            stat = jsonl_path.stat()
        except OSError:
            continue
        mtime = stat.st_mtime
        size = stat.st_size

        if mtime < cutoff_time:
            mark(session_id)
            continue

        # Active-session guard — different mechanism per source.
        if is_cursor_path(jsonl_path):
            if _cursor_recently_active(jsonl_path, mtime):
                continue
        else:
            if is_active_session(session_id):
                continue

        prior_offset = get_offset(session_id)
        if prior_offset >= size:
            # already fully ingested; nothing new
            mark(session_id)
            continue

        try:
            messages, new_offset = extract_messages(jsonl_path, start_offset=prior_offset)
        except Exception:
            # A malformed Cursor transcript shouldn't block Claude harvest.
            continue
        # Always advance the offset — even if this slice was too small —
        # so we don't reread the same prefix forever.
        set_offset(session_id, jsonl_path, new_offset)

        if len(messages) < MIN_MESSAGES:
            # Not enough new content this round; will try again when more
            # bytes arrive. Mark as seen only if file is "complete enough"
            # (no new content for a long time → mark to silence re-checks).
            if prior_offset == 0 and (time.time() - mtime) > 3600:
                mark(session_id)
            continue

        project_name = derive_project_name(jsonl_path)
        summary = format_session_summary(messages, project_name, session_id)

        BRAIN_RAW.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        # Strip the `cursor:` prefix for the filename slug (filesystem-safe)
        # but keep it in the session-summary body (so the source is recorded).
        slug = session_id.split(":", 1)[-1][:8]
        source_tag = "cursor" if is_cursor_path(jsonl_path) else "claude"
        filename = f"session-{now.strftime('%Y-%m-%d-%H%M%S')}-{source_tag}-{slug}.md"
        output_path = BRAIN_RAW / filename
        output_path.write_text(summary)

        mark(session_id)
        count += 1

    save_harvested(harvested)
    rotate_harvested()
    return count


def main():
    count = harvest_all()
    if count:
        print(f"Harvested {count} session(s) to ~/.brain/raw/")


if __name__ == "__main__":
    main()
