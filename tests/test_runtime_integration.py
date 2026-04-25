"""End-to-end same-process integration smoke test.

This stops short of spawning two real Claude Code sessions (which
needs a Claude binary on PATH and is environment-dependent). It does
exercise: name registry → resolve → send → list pending → mark
delivered, with a stubbed live_uuids set.
"""
from __future__ import annotations

import json

import pytest

from brain import mcp_server


pytestmark = pytest.mark.integration


@pytest.fixture
def two_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))

    def make_caller(uuid: str, project: str = "acme", cwd: str = "/tmp/acme"):
        return {
            "uuid": uuid,
            "project": project,
            "cwd": cwd,
        }

    return [
        make_caller("11111111-2222-3333-4444-555555555555"),
        make_caller("66666666-7777-8888-9999-000000000000"),
    ]


def test_two_sessions_round_trip(two_sessions, monkeypatch):
    a, b = two_sessions

    def install_caller(caller):
        monkeypatch.setattr(
            "brain.runtime.session_id.detect_own_uuid", lambda: caller["uuid"]
        )
        monkeypatch.setattr(mcp_server, "_caller_project_for_uuid",
                            lambda u: caller["project"])
        monkeypatch.setattr(mcp_server, "_caller_cwd", lambda: caller["cwd"])
        monkeypatch.setattr(mcp_server, "_live_uuids", lambda: {a["uuid"], b["uuid"]})

    install_caller(a)
    out_a = json.loads(mcp_server.brain_set_name("planner"))
    assert out_a["ok"]

    install_caller(b)
    out_b = json.loads(mcp_server.brain_set_name("executor"))
    assert out_b["ok"]

    install_caller(a)
    out_send = json.loads(mcp_server.brain_send(to="executor", body="GO"))
    assert out_send["ok"] and out_send["to_uuid"] == b["uuid"]

    install_caller(b)
    out_inbox = json.loads(mcp_server.brain_inbox())
    assert out_inbox["pending_count"] == 1
    assert out_inbox["messages"][0]["body"] == "GO"
    assert out_inbox["messages"][0]["from_name_at_send"] == "planner"
