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


def test_stop_mode_emits_block_decision_json(monkeypatch, capsys):
    """Stop mode wraps the surface block in Claude Code's Stop-hook
    decision JSON, so the agent auto-continues with peer messages
    rather than going idle."""
    import json as _json
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setenv("BRAIN_STOP_POLL_SEC", "0")  # no poll, deterministic
    inbox.send("u1", "snd", "planner", "executor", "PEER-REPLY")
    rc = hook.run(stop_mode=True)
    assert rc == 0
    captured = capsys.readouterr()
    payload = _json.loads(captured.out)
    assert payload["decision"] == "block"
    assert "<system-reminder>" in payload["reason"]
    assert "PEER-REPLY" in payload["reason"]
    assert list(paths.inbox_pending_dir("u1").iterdir()) == []


def test_stop_mode_silent_when_no_pending(monkeypatch, capsys):
    """No pending → no JSON → assistant stops normally."""
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setenv("BRAIN_STOP_POLL_SEC", "0")
    rc = hook.run(stop_mode=True)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_stop_mode_polls_briefly_for_late_arriving_reply(monkeypatch, capsys):
    """When a peer reply lands during the poll window, Stop hook
    catches it instead of letting the assistant stop."""
    import threading
    import json as _json
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setenv("BRAIN_STOP_POLL_SEC", "1.5")

    def _late_send():
        import time as _t
        _t.sleep(0.3)
        inbox.send("u1", "snd", "planner", "executor", "LATE-PEER-REPLY")

    t = threading.Thread(target=_late_send)
    t.start()
    rc = hook.run(stop_mode=True)
    t.join()
    assert rc == 0
    out = capsys.readouterr().out
    payload = _json.loads(out)
    assert payload["decision"] == "block"
    assert "LATE-PEER-REPLY" in payload["reason"]


def test_main_dispatches_stop_flag(monkeypatch, capsys):
    """`--stop` on argv routes to stop_mode."""
    import json as _json
    monkeypatch.setattr("brain.runtime.session_id.detect_own_uuid", lambda: "u1")
    monkeypatch.setenv("BRAIN_STOP_POLL_SEC", "0")
    monkeypatch.setattr("sys.argv", ["hook", "--stop"])
    inbox.send("u1", "snd", "planner", "executor", "VIA-MAIN-STOP")
    rc = hook.main()
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert "VIA-MAIN-STOP" in payload["reason"]
