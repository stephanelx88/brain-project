"""Hook entry point — pulls pending, surfaces, marks delivered."""
from __future__ import annotations

import pytest

from brain.runtime import hook, inbox, paths


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_no_uuid_silent_exit(monkeypatch, capsys):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: None)
    rc = hook.run()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_no_pending_silent_exit(monkeypatch, capsys):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    rc = hook.run()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_pending_messages_surfaced_and_marked(monkeypatch, capsys):
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    inbox.send("u1", "snd", "planner", "executor", "GO")
    rc = hook.run()
    assert rc == 0
    captured = capsys.readouterr()
    assert "<system-reminder>" in captured.out
    assert "GO" in captured.out
    assert list(paths.inbox_pending_dir("u1").iterdir()) == []
    delivered = list(paths.inbox_delivered_dir("u1").iterdir())
    assert len(delivered) == 1


def test_exception_logged_not_raised(monkeypatch):
    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr("brain.runtime.hook.session_id.detect_own_uuid", _boom)
    rc = hook.run()
    assert rc == 0  # never raises to caller
    log = paths.hook_log_path()
    assert log.exists()
    assert "boom" in log.read_text()


def test_main_returns_zero_when_no_session(monkeypatch, capsys):
    """CLI entry point: no detectable UUID -> rc=0, no stdout."""
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: None)
    rc = hook.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_returns_zero_when_pending(monkeypatch, capsys):
    """CLI entry point: pending message present -> rc=0, surfaced to stdout."""
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    inbox.send("u1", "snd", "planner", "executor", "GO-via-main")
    rc = hook.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "<system-reminder>" in captured.out
    assert "GO-via-main" in captured.out
    # Side effect: pending drained
    assert list(paths.inbox_pending_dir("u1").iterdir()) == []
