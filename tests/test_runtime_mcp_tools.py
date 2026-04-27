"""End-to-end check of the three new MCP tool implementations."""
from __future__ import annotations

import json

import pytest

from brain import mcp_server
from brain.runtime import inbox, names


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_brain_set_name_writes_registry(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    out = json.loads(mcp_server.brain_set_name("planner"))
    assert out["ok"] and out["name"] == "planner"
    assert names.get("u1")["name"] == "planner"


def test_brain_set_name_validation_error(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    out = json.loads(mcp_server.brain_set_name("Planner"))
    assert not out["ok"] and out["error"] == "lowercase"


def test_brain_send_to_uuid_fire_and_forget(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    monkeypatch.setattr(mcp_server, "_live_uuids", lambda: set())
    target = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    out = json.loads(mcp_server.brain_send(to=target, body="GO"))
    assert out["ok"] and out["to_uuid"] == target
    pending = inbox.list_pending(target)
    assert len(pending) == 1 and pending[0]["body"] == "GO"


def test_brain_send_to_name_dead(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    monkeypatch.setattr(mcp_server, "_live_uuids", lambda: {"u1"})
    names.register("ghost", "executor", "acme", "/tmp/g", 99)
    out = json.loads(mcp_server.brain_send(to="executor", body="GO"))
    assert not out["ok"] and out["error"] == "recipient_dead"


def test_brain_inbox_returns_pending(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    inbox.send("u1", "snd", "planner", "executor", "hello")
    out = json.loads(mcp_server.brain_inbox())
    assert out["pending_count"] == 1
    assert out["messages"][0]["body"] == "hello"


def test_brain_inbox_mark_read_moves_files(monkeypatch):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    inbox.send("u1", "snd", "planner", "executor", "hello")
    out = json.loads(mcp_server.brain_inbox(mark_read=True))
    # `messages` reflects what the caller saw (1 message); after
    # mark_read, pending_count is recomputed to the post-mark state
    # (0 left in pending), delivered_count reflects the move.
    assert len(out["messages"]) == 1
    assert out["pending_count"] == 0
    assert out["delivered_count"] == 1
    assert inbox.list_pending("u1") == []
    assert len(inbox.list_delivered("u1")) == 1


def test_brain_inbox_mark_read_with_limit_does_not_overconsume(monkeypatch):
    """Regression for D-1: mark_read=True must only move the LISTED
    slice, not the full pending queue. Previously, with 5 pending and
    limit=2, all 5 silently moved to delivered/ even though the caller
    only saw 2.
    """
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    for i in range(5):
        inbox.send("u1", "snd", "planner", "executor", f"msg-{i}")
    assert len(inbox.list_pending("u1")) == 5
    out = json.loads(mcp_server.brain_inbox(mark_read=True, limit=2))
    assert len(out["messages"]) == 2
    assert out["pending_count"] == 3
    assert out["delivered_count"] == 2
    assert len(inbox.list_pending("u1")) == 3
    assert len(inbox.list_delivered("u1")) == 2


def test_ensure_self_registered_cursor_uses_uuid_prefix(monkeypatch):
    """Regression for D-3: a `cursor:<UUIDv4>` session must get a
    UUID-prefix short id, not the parent PID. Previously the code
    hardcoded source="claude" so Cursor sessions got PID-based names.
    """
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    cursor_uuid = "cursor:ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    mcp_server._ensure_self_registered(cursor_uuid)
    entry = names.get(cursor_uuid)
    assert entry is not None
    # Default-name format is "<project>-<short>"; tail = first 8 of UUID.
    tail = entry["name"].rsplit("-", 1)[-1]
    assert tail == "ab2b1fa6", f"expected UUID-prefix short, got {entry['name']!r}"
    # PID-derived names would be all-digit; confirm not.
    assert not tail.isdigit()


def test_ensure_self_registered_claude_falls_back_to_pid(monkeypatch):
    """D-3 regression-guard: non-cursor UUID still gets the PID-based
    short id (legacy fallback path).
    """
    monkeypatch.setattr(mcp_server, "_caller_project_for_uuid", lambda u: "acme")
    monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: "/tmp/acme")
    monkeypatch.setattr(mcp_server, "_detect_source_for_uuid", lambda u: "claude")
    plain_uuid = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    mcp_server._ensure_self_registered(plain_uuid)
    entry = names.get(plain_uuid)
    assert entry is not None
    tail = entry["name"].rsplit("-", 1)[-1]
    assert tail.isdigit(), f"expected PID short-id, got {entry['name']!r}"
