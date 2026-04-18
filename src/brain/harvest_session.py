#!/usr/bin/env python3
"""Harvest Claude Code session transcripts into ~/.brain/raw/.

Scans all project JSONL files in ~/.claude/projects/, skips already-harvested
sessions, and writes structured summaries to ~/.brain/raw/ for extraction.

Called by the SessionStart hook in ~/.claude/settings.json.
Runs on every new session — harvests whatever ended since last time.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import brain.config as config

BRAIN_RAW = config.RAW_DIR
HARVESTED_FILE = config.BRAIN_DIR / ".harvested"
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Sessions shorter than this many user+assistant messages aren't worth capturing
MIN_MESSAGES = 4

# Only harvest sessions modified in the last N seconds (24 hours)
MAX_AGE_SECONDS = 86400

# Max chars per message to include
MAX_MSG_CHARS = 3000

# When truncating a tool_use input, how much of the JSON to show
TOOL_INPUT_PREVIEW = 200


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
    """Trim .harvested to at most max_entries, keeping the most recently added.

    Returns the number of entries removed.
    """
    if not HARVESTED_FILE.exists():
        return 0
    lines = HARVESTED_FILE.read_text().strip().splitlines()
    if len(lines) <= max_entries:
        return 0
    trimmed = lines[-max_entries:]
    HARVESTED_FILE.write_text("\n".join(trimmed) + "\n")
    return len(lines) - len(trimmed)


def find_all_session_jsonls() -> list[Path]:
    """Find all session JSONL files across all projects."""
    if not PROJECTS_DIR.exists():
        return []
    results = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        results.extend(project_dir.glob("*.jsonl"))
    return results


def get_session_id(jsonl_path: Path) -> str:
    """Extract session ID from JSONL filename (filename minus .jsonl)."""
    return jsonl_path.stem


def extract_messages(jsonl_path: Path) -> list[dict]:
    """Extract user and assistant messages from a session JSONL."""
    messages = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
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

    return messages


def derive_project_name(jsonl_path: Path) -> str:
    """Derive a human-readable project name from the JSONL path."""
    project_dir_name = jsonl_path.parent.name
    parts = project_dir_name.split("-")
    meaningful = []
    skip_prefixes = {"", "Users", "son", "Desktop", "Documents", "home"}
    for part in parts:
        if part in skip_prefixes and not meaningful:
            continue
        meaningful.append(part)
    return "/".join(meaningful) if meaningful else project_dir_name


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


def is_active_session(session_id: str) -> bool:
    """Check if a session is currently active (has a PID file in sessions/)."""
    sessions_dir = CLAUDE_DIR / "sessions"
    if not sessions_dir.exists():
        return False
    for session_file in sessions_dir.glob("*.json"):
        try:
            data = json.loads(session_file.read_text())
            if data.get("sessionId") == session_id:
                # Check if the process is still running
                pid = data.get("pid")
                if pid:
                    try:
                        os.kill(pid, 0)  # signal 0 = check if alive
                        return True
                    except OSError:
                        return False
        except (json.JSONDecodeError, OSError):
            continue
    return False


def harvest_all() -> int:
    """Harvest all unprocessed, non-active sessions. Returns count harvested."""
    harvested = load_harvested()
    seen = set(harvested)
    all_jsonls = find_all_session_jsonls()
    cutoff_time = time.time() - MAX_AGE_SECONDS
    count = 0

    def mark(session_id: str) -> None:
        if session_id not in seen:
            harvested.append(session_id)
            seen.add(session_id)

    for jsonl_path in all_jsonls:
        session_id = get_session_id(jsonl_path)

        # Skip already harvested
        if session_id in seen:
            continue

        # Skip old sessions — only harvest recent ones
        try:
            mtime = jsonl_path.stat().st_mtime
            if mtime < cutoff_time:
                mark(session_id)  # mark old ones so we never re-check
                continue
        except OSError:
            continue

        # Skip currently active sessions (don't harvest our own session)
        if is_active_session(session_id):
            continue

        messages = extract_messages(jsonl_path)
        if len(messages) < MIN_MESSAGES:
            # Not enough content — skip silently
            mark(session_id)
            continue

        project_name = derive_project_name(jsonl_path)
        summary = format_session_summary(messages, project_name, session_id)

        BRAIN_RAW.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        filename = f"session-{now.strftime('%Y-%m-%d-%H%M%S')}-{session_id[:8]}.md"
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
