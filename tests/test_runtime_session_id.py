"""Detect the calling process's session UUID."""
from __future__ import annotations

import json

import pytest

from brain.runtime import session_id


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2")
    assert session_id.detect_own_uuid() == "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"


def test_env_var_invalid_uuid_falls_through(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "not-a-uuid")
    monkeypatch.setattr(session_id, "_claude_sessions_dir",
                        lambda: tmp_path / ".claude" / "sessions")
    monkeypatch.setattr(session_id, "_get_ppid", lambda: 99999)
    assert session_id.detect_own_uuid() is None


def test_ppid_lookup_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    sessions_dir = tmp_path / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "12345.json").write_text(json.dumps({
        "session_id": "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
        "cwd": "/tmp/acme",
    }))
    monkeypatch.setattr(session_id, "_claude_sessions_dir", lambda: sessions_dir)
    monkeypatch.setattr(session_id, "_get_ppid", lambda: 12345)
    assert session_id.detect_own_uuid() == "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"


def test_returns_none_when_nothing_works(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(session_id, "_claude_sessions_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr(session_id, "_get_ppid", lambda: 99999)
    assert session_id.detect_own_uuid() is None


def test_short_id_from_pid_when_known(monkeypatch):
    monkeypatch.setattr(session_id, "_get_ppid", lambda: 68293)
    assert session_id.short_id_for_default_name("uuid-doesnt-matter", source="claude") == "68293"


def test_short_id_from_uuid_for_cursor():
    out = session_id.short_id_for_default_name(
        "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2", source="cursor"
    )
    assert out == "ab2b1fa6"
