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
    assert out["pending_count"] == 1
    assert inbox.list_pending("u1") == []
    assert len(inbox.list_delivered("u1")) == 1
