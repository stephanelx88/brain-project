"""Tests for runtime.poke — best-effort tmux wake-up."""
from __future__ import annotations

import pytest

from brain.runtime import names, poke


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_poke_skipped_when_no_entry(monkeypatch):
    """Unknown UUID → no-op, no subprocess call."""
    calls = []
    monkeypatch.setattr(poke.subprocess, "run",
                        lambda *a, **k: calls.append(a) or _R(0))
    assert poke.poke_session("nonexistent") is False
    assert calls == []


def test_poke_skipped_when_no_tmux_pane(monkeypatch):
    """Session registered without a tmux pane → no-op."""
    names.register("u1", "planner", "acme", "/tmp", 1, tmux_pane=None)
    calls = []
    monkeypatch.setattr(poke.subprocess, "run",
                        lambda *a, **k: calls.append(a) or _R(0))
    assert poke.poke_session("u1") is False
    assert calls == []


def test_poke_skipped_when_env_disables(monkeypatch):
    names.register("u1", "planner", "acme", "/tmp", 1, tmux_pane="%42")
    monkeypatch.setenv("BRAIN_TMUX_POKE", "0")
    calls = []
    monkeypatch.setattr(poke.subprocess, "run",
                        lambda *a, **k: calls.append(a) or _R(0))
    assert poke.poke_session("u1") is False
    assert calls == []


def test_poke_sends_enter_to_recorded_pane(monkeypatch):
    """Happy path: tmux send-keys is called with the right pane and Enter."""
    names.register("u1", "planner", "acme", "/tmp", 1, tmux_pane="%42")
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        # Stub display-message to "not in copy mode" then send-keys ok.
        if "display-message" in cmd:
            return _R(0, stdout="0\n")
        return _R(0)

    monkeypatch.setattr(poke.subprocess, "run", fake_run)
    assert poke.poke_session("u1") is True
    # Last call is the send-keys; verify shape.
    send = captured[-1]
    assert send[0] == "tmux"
    assert "send-keys" in send
    assert "%42" in send
    assert "Enter" in send


def test_poke_skipped_when_pane_in_copy_mode(monkeypatch):
    """User is reading scrollback (copy mode) → don't send keys; they'd
    hit copy-mode bindings instead of Claude Code."""
    names.register("u1", "planner", "acme", "/tmp", 1, tmux_pane="%42")
    sent = []

    def fake_run(cmd, **kwargs):
        if "display-message" in cmd:
            return _R(0, stdout="1\n")  # in copy mode
        sent.append(cmd)
        return _R(0)

    monkeypatch.setattr(poke.subprocess, "run", fake_run)
    assert poke.poke_session("u1") is False
    assert sent == []  # no send-keys


def test_poke_silent_on_subprocess_error(monkeypatch):
    """OSError / FileNotFoundError (tmux not installed) is silent."""
    names.register("u1", "planner", "acme", "/tmp", 1, tmux_pane="%42")

    def boom(*a, **k):
        raise FileNotFoundError("tmux missing")

    monkeypatch.setattr(poke.subprocess, "run", boom)
    # Should return False, not raise.
    assert poke.poke_session("u1") is False


def test_register_persists_tmux_pane():
    """Round-trip the tmux_pane field through the registry."""
    names.register("u1", "planner", "acme", "/tmp", 1, tmux_pane="%99")
    entry = names.get("u1")
    assert entry["tmux_pane"] == "%99"


def test_register_default_tmux_pane_is_none():
    """Backwards compat: existing callers omitting the kwarg get None."""
    names.register("u1", "planner", "acme", "/tmp", 1)
    entry = names.get("u1")
    assert entry["tmux_pane"] is None


# ─── helpers ───────────────────────────────────────────────────────


class _R:
    """Stand-in for subprocess.CompletedProcess."""
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
