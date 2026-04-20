"""Tests for `brain.status`.

We patch the module-level paths/labels to point at a fake vault so
none of these tests touch the real `~/.brain/` or call `launchctl`
against a real job. The launchd / ps subprocess calls are stubbed out
entirely — testing the parsers, not the OS integration."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import brain.status as status


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    brain_dir = tmp_path / ".brain"
    (brain_dir / "logs").mkdir(parents=True)
    (brain_dir / "entities").mkdir()
    (brain_dir / "raw").mkdir()
    monkeypatch.setattr(status.config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(status.config, "RAW_DIR", brain_dir / "raw")
    monkeypatch.setattr(status, "EXTRACT_LOCK_DIR", brain_dir / ".extract.lock.d")
    monkeypatch.setattr(status, "AUTO_EXTRACT_LOG", brain_dir / "logs" / "auto-extract.log")
    monkeypatch.setattr(status, "HARVEST_LEDGER", brain_dir / ".harvested")
    monkeypatch.setattr(status, "DEDUPE_LEDGER", brain_dir / ".dedupe.ledger.json")
    monkeypatch.setattr(status, "RECALL_LEDGER", brain_dir / "recall-ledger.jsonl")
    monkeypatch.setattr(status, "LAUNCHD_PLIST", tmp_path / "missing.plist")
    return brain_dir


def _stub_launchctl_loaded(monkeypatch, pid: int | None, last_exit: int = 0):
    """Stub launchctl to look like the job is loaded with given PID."""
    body = "{\n"
    if pid is not None:
        body += '\t"PID" = ' + str(pid) + ";\n"
    body += '\t"LastExitStatus" = ' + str(last_exit) + ";\n"
    body += '\t"Label" = "' + status.LAUNCHD_LABEL + '";\n};\n'
    class _CP:
        returncode = 0
        stdout = body
    def fake_run(cmd, **kw):
        if cmd[:2] == ["launchctl", "list"]:
            return _CP()
        if cmd[:2] == ["ps", "-A"]:
            class _CP2: returncode = 0; stdout = ""
            return _CP2()
        raise AssertionError(f"unexpected subprocess: {cmd}")
    monkeypatch.setattr(status.subprocess, "run", fake_run)


def _stub_launchctl_not_loaded(monkeypatch):
    class _CP:
        returncode = 113
        stdout = ""
    def fake_run(cmd, **kw):
        if cmd[:2] == ["launchctl", "list"]:
            return _CP()
        if cmd[:2] == ["ps", "-A"]:
            class _CP2: returncode = 0; stdout = ""
            return _CP2()
        raise AssertionError(f"unexpected subprocess: {cmd}")
    monkeypatch.setattr(status.subprocess, "run", fake_run)


def test_launchd_not_loaded(fake_vault, monkeypatch):
    _stub_launchctl_not_loaded(monkeypatch)
    rep = status.gather()
    assert rep.launchd["loaded"] is False
    assert rep.launchd["pid"] is None
    text = status.format_text(rep)
    assert "NOT LOADED" in text


def test_launchd_loaded_idle(fake_vault, monkeypatch):
    _stub_launchctl_loaded(monkeypatch, pid=None)
    rep = status.gather()
    assert rep.launchd["loaded"] is True
    assert rep.launchd["pid"] is None
    assert rep.launchd["last_exit"] == 0


def test_in_flight_with_alive_pid(fake_vault, monkeypatch):
    _stub_launchctl_loaded(monkeypatch, pid=os.getpid())
    lock = fake_vault / ".extract.lock.d"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    rep = status.gather()
    assert rep.in_flight["running"] is True
    assert rep.in_flight["pid"] == os.getpid()


def test_in_flight_stale_lock(fake_vault, monkeypatch):
    _stub_launchctl_not_loaded(monkeypatch)
    lock = fake_vault / ".extract.lock.d"
    lock.mkdir()
    # PID 999999 ~ guaranteed-not-running on dev machines.
    (lock / "pid").write_text("999999")
    rep = status.gather()
    assert rep.in_flight["running"] is False
    assert rep.in_flight["stale"] is True


def test_last_run_parsed_from_log(fake_vault, monkeypatch):
    _stub_launchctl_not_loaded(monkeypatch)
    log = fake_vault / "logs" / "auto-extract.log"
    log.write_text(
        "=== 2026-04-20T13:00:00Z auto-extract run (active_session=0) ===\n"
        "did stuff\n"
        "=== 2026-04-20T13:05:00Z auto-extract run (active_session=1) ===\n"
        "skip auto_extract+reconcile+dedupe: active session (last write 2s ago)\n"
        "=== 2026-04-20T13:10:00Z auto-extract run (active_session=1) ===\n"
        "skip auto_extract+reconcile+dedupe: active session (last write 1s ago)\n"
    )
    rep = status.gather()
    assert rep.last_run["ts"] == "2026-04-20T13:10:00Z"
    assert rep.last_run["active_session"] is True
    assert rep.last_run["skipped_streak"] >= 2


def test_no_log_file(fake_vault, monkeypatch):
    _stub_launchctl_not_loaded(monkeypatch)
    rep = status.gather()
    assert rep.last_run["ts"] is None
    assert rep.last_run["age_s"] is None


def test_ledger_counts(fake_vault, monkeypatch):
    _stub_launchctl_not_loaded(monkeypatch)
    (fake_vault / ".harvested").write_text("a\nb\nc\n")
    (fake_vault / ".dedupe.ledger.json").write_text(json.dumps({"k1": 1, "k2": 2}))
    rep = status.gather()
    assert rep.ledgers["harvested"] == 3
    assert rep.ledgers["dedupe_verdicts"] == 2


def test_format_text_smoke(fake_vault, monkeypatch):
    """End-to-end: gather + format does not raise on a near-empty vault."""
    _stub_launchctl_not_loaded(monkeypatch)
    out = status.format_text(status.gather())
    # The header is the user's contract — anchor on it.
    assert out.startswith("🧠 Brain status")
    for key in ("vault", "launchd", "last run", "next run", "in flight",
                "procs", "ledgers", "coverage", "audit", "vault stats"):
        assert key in out


def test_to_json_roundtrip(fake_vault, monkeypatch):
    _stub_launchctl_not_loaded(monkeypatch)
    rep = status.gather()
    parsed = json.loads(status.to_json(rep))
    # Every public dataclass field should round-trip through JSON.
    for k in ("brain_dir", "launchd", "in_flight", "last_run", "next_run",
              "spawned_procs", "ledgers", "pending_audit", "vault", "coverage"):
        assert k in parsed


def test_coverage_absent_when_no_ledger(fake_vault, monkeypatch):
    """Fresh install with no autoresearch runs — coverage surfaces as
    `available: False` rather than synthesising a bogus score."""
    _stub_launchctl_not_loaded(monkeypatch)
    rep = status.gather()
    assert rep.coverage["available"] is False
    assert rep.coverage["latest_score"] is None
    assert rep.coverage["runs_logged"] == 0


def test_coverage_parsed_and_delta(fake_vault, monkeypatch):
    """Two eval rows in the ledger → latest_score + delta computed."""
    _stub_launchctl_not_loaded(monkeypatch)
    ledger = fake_vault / "recall-ledger.jsonl"
    ledger.write_text(
        '{"ts":"2026-04-20T14:00:00Z","kind":"eval","score":0.2,"avg_top":0.60,'
        '"misses":3,"total":15,"threshold":0.6}\n'
        '{"ts":"2026-04-20T14:30:00Z","kind":"eval","score":0.0667,'
        '"avg_top":0.69,"misses":1,"total":15,"threshold":0.6}\n'
    )
    rep = status.gather()
    assert rep.coverage["available"] is True
    assert rep.coverage["runs_logged"] == 2
    assert abs(rep.coverage["latest_score"] - 0.0667) < 1e-6
    assert abs(rep.coverage["prev_score"] - 0.2) < 1e-6
    assert rep.coverage["delta_score"] < 0  # improved (fewer misses)
    assert rep.coverage["latest_avg_top"] == 0.69
    out = status.format_text(rep)
    assert "coverage" in out
    assert "6.7%" in out or "6.67%" in out


def test_coverage_tolerates_corrupt_lines(fake_vault, monkeypatch):
    """Partial writes + hand-editing can leave junk lines; we skip them
    silently rather than erroring out the whole dashboard."""
    _stub_launchctl_not_loaded(monkeypatch)
    (fake_vault / "recall-ledger.jsonl").write_text(
        '{"ts":"x","kind":"eval","score":0.1,"total":10,"misses":1}\n'
        'not json at all\n'
        '{"ts":"y","kind":"something-else","score":"ignored"}\n'
        '{"ts":"z","kind":"eval","score":0.05,"total":20,"misses":1}\n'
    )
    rep = status.gather()
    assert rep.coverage["runs_logged"] == 2
    assert rep.coverage["latest_score"] == 0.05


def test_delta_str_formats():
    assert status._delta_str(None) is None
    assert status._delta_str(5) == "5s"
    assert status._delta_str(125) == "2m05s"
    assert status._delta_str(3725) == "1h02m"
