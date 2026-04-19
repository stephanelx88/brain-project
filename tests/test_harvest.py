"""Tests for harvest_session message extraction."""

import json
from pathlib import Path

from brain.harvest_session import extract_messages


def test_extract_messages_with_write_tool_preserves_subsequent_text(tmp_path):
    """Write tool_use followed by text block should not lose the text.

    Bug: line 104 shadows outer 'content' variable when processing Write tool input.
    """
    jsonl_path = tmp_path / "session.jsonl"
    entry = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Creating file."},
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {
                        "file_path": "/tmp/test.txt",
                        "content": "file content here"
                    }
                },
                {"type": "text", "text": "File created successfully."}
            ]
        }
    }
    jsonl_path.write_text(json.dumps(entry) + "\n")
    messages, _offset = extract_messages(jsonl_path)
    assert len(messages) == 1
    # The text "File created successfully." must appear in output
    assert "File created successfully." in messages[0]["text"]
    # The Write tool reference must also appear
    assert "Write" in messages[0]["text"]


def test_extract_messages_simple_text(tmp_path):
    jsonl_path = tmp_path / "session.jsonl"
    entries = [
        {"type": "user", "message": {"content": "hello"}, "timestamp": "2026-04-11T10:00:00Z"},
        {"type": "assistant", "message": {"content": "hi there"}, "timestamp": "2026-04-11T10:00:05Z"},
    ]
    jsonl_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    messages, offset = extract_messages(jsonl_path)
    assert len(messages) == 2
    assert messages[0]["text"] == "hello"
    assert messages[1]["text"] == "hi there"
    assert offset == jsonl_path.stat().st_size


def test_extract_messages_skips_non_user_assistant(tmp_path):
    jsonl_path = tmp_path / "session.jsonl"
    entries = [
        {"type": "system", "message": {"content": "system prompt"}},
        {"type": "user", "message": {"content": "hello"}},
    ]
    jsonl_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    messages, _offset = extract_messages(jsonl_path)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


def test_extract_messages_incremental_resume(tmp_path):
    """Resuming with start_offset only returns the newly-appended turns."""
    jsonl_path = tmp_path / "session.jsonl"

    first_batch = [
        {"type": "user", "message": {"content": "first"}},
        {"type": "assistant", "message": {"content": "ok"}},
    ]
    jsonl_path.write_text("\n".join(json.dumps(e) for e in first_batch) + "\n")

    msgs1, off1 = extract_messages(jsonl_path)
    assert len(msgs1) == 2
    assert off1 == jsonl_path.stat().st_size

    # Append more turns
    with open(jsonl_path, "a") as f:
        for e in [
            {"type": "user", "message": {"content": "second"}},
            {"type": "assistant", "message": {"content": "still here"}},
        ]:
            f.write(json.dumps(e) + "\n")

    msgs2, off2 = extract_messages(jsonl_path, start_offset=off1)
    assert len(msgs2) == 2
    assert msgs2[0]["text"] == "second"
    assert msgs2[1]["text"] == "still here"
    assert off2 == jsonl_path.stat().st_size


import brain.harvest_session as hs
from brain.harvest_session import rotate_harvested, load_harvested, save_harvested


def test_rotate_harvested_keeps_recent_removes_old(tmp_path, monkeypatch):
    """Rotation should trim to max_entries, keeping most recently added."""
    harvested_file = tmp_path / ".harvested"
    monkeypatch.setattr(hs, "HARVESTED_FILE", harvested_file)

    # Write 5000 session IDs
    ids = {f"session-{i}" for i in range(5000)}
    save_harvested(ids)
    assert harvested_file.exists()

    lines_before = len(harvested_file.read_text().strip().splitlines())
    assert lines_before == 5000

    # Rotate with max_entries=2000
    removed = rotate_harvested(max_entries=2000)
    assert removed == 3000

    lines_after = len(harvested_file.read_text().strip().splitlines())
    assert lines_after == 2000


def test_rotate_harvested_noop_when_small(tmp_path, monkeypatch):
    """No rotation needed when under the limit."""
    harvested_file = tmp_path / ".harvested"
    monkeypatch.setattr(hs, "HARVESTED_FILE", harvested_file)

    ids = {f"session-{i}" for i in range(100)}
    save_harvested(ids)

    removed = rotate_harvested(max_entries=2000)
    assert removed == 0
