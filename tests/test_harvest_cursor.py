"""Cursor-source tests for `brain.harvest_session`.

Covers the integration added when Cursor was added as a second harvest
source alongside Claude Code:
  * `extract_messages` accepts Cursor's `role` schema as a synonym for `type`.
  * `is_cursor_path` correctly identifies cursor-rooted transcripts.
  * `get_session_id` namespaces cursor IDs with `cursor:` so they can't
    collide with Claude UUIDs.
  * `derive_project_name` walks one level higher for cursor's nested
    `agent-transcripts/<uuid>/<uuid>.jsonl` layout and tags the result
    with a `cursor/` prefix.
  * `_cursor_recently_active` skips files touched within the active window.
  * `find_cursor_session_jsonls` finds cursor sessions but ignores the
    `subagents/` subdirectory and missing-dir cases.
  * `harvest_all` survives Cursor errors without aborting Claude harvest.
"""

import json
import os
import time
from pathlib import Path

import pytest

import brain.harvest_session as hs


def _write_cursor_session(root: Path, workspace: str, session_id: str,
                          entries: list[dict]) -> Path:
    """Build a `~/.cursor/projects/<workspace>/agent-transcripts/<id>/<id>.jsonl`."""
    session_dir = root / "projects" / workspace / "agent-transcripts" / session_id
    session_dir.mkdir(parents=True)
    jsonl = session_dir / f"{session_id}.jsonl"
    jsonl.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return jsonl


def test_extract_messages_accepts_cursor_role_schema(tmp_path):
    """Cursor jsonl uses `role` instead of `type`. The parser must handle both."""
    jsonl = tmp_path / "cursor.jsonl"
    entries = [
        {"role": "user", "message": {"content": [{"type": "text", "text": "hi from cursor"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "hello back"}]}},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    messages, offset = hs.extract_messages(jsonl)
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert "hi from cursor" in messages[0]["text"]
    assert messages[1]["role"] == "assistant"
    assert "hello back" in messages[1]["text"]
    assert offset == jsonl.stat().st_size


def test_extract_messages_type_wins_over_role_when_both_present(tmp_path):
    """Defensive: if a malformed entry has both, prefer `type` (Claude semantics)."""
    jsonl = tmp_path / "mixed.jsonl"
    entry = {"type": "user", "role": "system",
             "message": {"content": "hello"}}
    jsonl.write_text(json.dumps(entry) + "\n")

    messages, _ = hs.extract_messages(jsonl)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


def test_is_cursor_path(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "CURSOR_PROJECTS_DIR", tmp_path / "projects")
    cursor_jsonl = _write_cursor_session(
        tmp_path, "Users-x-foo", "abcd1234",
        [{"role": "user", "message": {"content": "x"}}],
    )
    assert hs.is_cursor_path(cursor_jsonl) is True

    claude_jsonl = tmp_path / "claude" / "abcd1234.jsonl"
    claude_jsonl.parent.mkdir(parents=True)
    claude_jsonl.write_text("{}\n")
    assert hs.is_cursor_path(claude_jsonl) is False


def test_get_session_id_namespaces_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "CURSOR_PROJECTS_DIR", tmp_path / "projects")
    cursor_jsonl = _write_cursor_session(
        tmp_path, "Users-x-foo", "uuid-abc",
        [{"role": "user", "message": {"content": "x"}}],
    )
    assert hs.get_session_id(cursor_jsonl) == "cursor:uuid-abc"

    claude_jsonl = tmp_path / "p1" / "uuid-abc.jsonl"
    claude_jsonl.parent.mkdir(parents=True)
    claude_jsonl.write_text("{}\n")
    assert hs.get_session_id(claude_jsonl) == "uuid-abc"


def test_derive_project_name_for_cursor_walks_to_workspace(tmp_path, monkeypatch):
    """Cursor path is one level deeper than Claude — walk up one extra dir."""
    monkeypatch.setattr(hs, "CURSOR_PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("USER", "son")
    cursor_jsonl = _write_cursor_session(
        tmp_path, "Users-son-code-myproj", "deadbeef",
        [{"role": "user", "message": {"content": "x"}}],
    )
    name = hs.derive_project_name(cursor_jsonl)
    assert name == "cursor/code/myproj"


def test_derive_project_name_claude_unchanged(tmp_path, monkeypatch):
    """Make sure the Cursor branch didn't break the Claude path."""
    monkeypatch.setenv("USER", "son")
    claude_jsonl = tmp_path / "Users-son-code-myproj" / "abcd.jsonl"
    claude_jsonl.parent.mkdir(parents=True)
    claude_jsonl.write_text("{}\n")
    assert hs.derive_project_name(claude_jsonl) == "code/myproj"


def test_derive_project_name_uses_cwd_to_preserve_hyphens(tmp_path, monkeypatch):
    """When cwd is supplied, derive_project_name must use it directly
    instead of decoding the encoded directory name. Claude's encoding
    replaces `/` with `-` with no escape for hyphens that were in the
    original path, so an encoded name like `Users-son-code-brain-project`
    cannot be unambiguously decoded — it could mean `code/brain-project`
    (one dir) or `code/brain/project` (two dirs). The fallback decoder
    picks the lossy second option; the cwd path picks the truth.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("USER", "son")
    # The encoded dir name on disk is the lossy form...
    claude_jsonl = tmp_path / "claude" / "projects" / "-Users-son-code-brain-project" / "uuid.jsonl"
    claude_jsonl.parent.mkdir(parents=True)
    claude_jsonl.write_text("{}\n")
    # ...but cwd preserves the truth — `brain-project` is one directory.
    cwd = str(home / "code" / "brain-project")
    assert hs.derive_project_name(claude_jsonl, cwd=cwd) == "code/brain-project"
    # Without cwd, we get the existing lossy decoding (regression guard
    # — flagging if someone breaks the fallback path).
    assert hs.derive_project_name(claude_jsonl) == "code/brain/project"


def test_derive_project_name_with_cwd_outside_home_returns_full_path(
    tmp_path, monkeypatch
):
    """A session running from outside `$HOME` (e.g. `/tmp/work`) should
    display its full path, not silently fall back to the lossy decoder.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    claude_jsonl = tmp_path / "projects" / "-tmp-work" / "uuid.jsonl"
    claude_jsonl.parent.mkdir(parents=True)
    claude_jsonl.write_text("{}\n")
    assert hs.derive_project_name(claude_jsonl, cwd="/tmp/work") == "/tmp/work"


def test_derive_project_name_with_empty_cwd_falls_back(tmp_path, monkeypatch):
    """An empty/None cwd must NOT silently produce an empty project —
    we fall back to the encoded-dir decoder path so callers without
    cwd still get a label.
    """
    monkeypatch.setenv("USER", "son")
    claude_jsonl = tmp_path / "Users-son-code-myproj" / "abcd.jsonl"
    claude_jsonl.parent.mkdir(parents=True)
    claude_jsonl.write_text("{}\n")
    assert hs.derive_project_name(claude_jsonl, cwd="") == "code/myproj"
    assert hs.derive_project_name(claude_jsonl, cwd=None) == "code/myproj"


def test_derive_project_name_cursor_path_with_cwd_keeps_cursor_prefix(
    tmp_path, monkeypatch
):
    """Cursor sessions normally don't have a cwd, but if a future
    Cursor version exposes one, the cwd path must still apply the
    `cursor/` prefix so peer-sessions display can tell tools apart.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(hs, "CURSOR_PROJECTS_DIR", tmp_path / "projects")
    cursor_jsonl = _write_cursor_session(
        tmp_path, "Users-son-code-myproj", "deadbeef",
        [{"role": "user", "message": {"content": "x"}}],
    )
    cwd = str(home / "code" / "my-proj-with-hyphen")
    assert hs.derive_project_name(cursor_jsonl, cwd=cwd) == "cursor/code/my-proj-with-hyphen"


def test_cursor_recently_active(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("{}\n")
    now = time.time()
    assert hs._cursor_recently_active(p, mtime=now) is True
    assert hs._cursor_recently_active(p, mtime=now - hs.CURSOR_ACTIVE_WINDOW_SEC - 5) is False


def test_find_cursor_session_jsonls_skips_subagents_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "CURSOR_PROJECTS_DIR", tmp_path / "projects")

    # Real session
    session_dir = tmp_path / "projects" / "ws1" / "agent-transcripts" / "uuid-1"
    session_dir.mkdir(parents=True)
    real = session_dir / "uuid-1.jsonl"
    real.write_text("{}\n")

    # subagents subdir at the agent-transcripts level — must be skipped
    sub = tmp_path / "projects" / "ws1" / "agent-transcripts" / "subagents"
    sub.mkdir()
    fake = sub / "should-not-show.jsonl"
    fake.write_text("{}\n")

    # Project dir without agent-transcripts at all (timestamp-only Cursor proj)
    (tmp_path / "projects" / "1775811432143").mkdir(parents=True)

    # Empty cursor home shouldn't crash
    found = hs.find_cursor_session_jsonls()
    assert real in found
    assert fake not in found


def test_find_cursor_session_jsonls_no_cursor_dir(tmp_path, monkeypatch):
    """Cursor not installed → empty list, no error."""
    monkeypatch.setattr(hs, "CURSOR_PROJECTS_DIR", tmp_path / "does-not-exist")
    assert hs.find_cursor_session_jsonls() == []


def test_is_active_session_false_for_cursor_prefix():
    """Cursor IDs never match Claude PID files; the prefix short-circuits."""
    assert hs.is_active_session("cursor:abcd1234") is False


def test_harvest_all_survives_cursor_failure(tmp_path, monkeypatch):
    """A blow-up in `find_cursor_session_jsonls` must not abort Claude harvest."""

    monkeypatch.setattr(hs, "BRAIN_RAW", tmp_path / "raw")
    monkeypatch.setattr(hs, "HARVESTED_FILE", tmp_path / ".harvested")
    monkeypatch.setattr(hs, "LEDGER_DB", tmp_path / ".harvest.db")
    monkeypatch.setattr(hs, "PROJECTS_DIR", tmp_path / "claude-projects")

    def boom():
        raise RuntimeError("simulated cursor failure")

    monkeypatch.setattr(hs, "find_cursor_session_jsonls", boom)

    # Should return cleanly with 0 (no Claude sessions either) instead of raising.
    count = hs.harvest_all()
    assert count == 0
