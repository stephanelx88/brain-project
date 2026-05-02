"""Tests for the cross-platform scheduler backend.

The three code paths — launchd, systemd, null — are exercised
independently by forcing `current_backend()` to each value, so the
same CI can cover macOS and Linux behaviour without actually running
on both kernels.

subprocess is stubbed in every test so we never shell out to the real
launchctl/systemctl during CI.
"""

from __future__ import annotations

import pytest

from brain import scheduler


# ---------- backend dispatcher ------------------------------------------


def test_current_backend_darwin(monkeypatch):
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    assert scheduler.current_backend() == "launchd"


def test_current_backend_linux(monkeypatch):
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    assert scheduler.current_backend() == "systemd"


def test_current_backend_unknown_platform(monkeypatch):
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Windows")
    assert scheduler.current_backend() == "none"


# ---------- launchd label resolution ------------------------------------


def test_launchd_label_uses_current_user(monkeypatch):
    """The plist installed by bin/install.sh is rendered with
    `com.{{USERNAME}}.brain-auto-extract`, so the runtime probe must
    compose the same label per user. Hardcoding "son" (the original
    author's username) made `brain status` report "scheduler not loaded"
    for any user other than son even when the job WAS loaded.
    """
    monkeypatch.setenv("USER", "alice")
    monkeypatch.delenv("USERNAME", raising=False)
    assert scheduler._launchd_label() == "com.alice.brain-auto-extract"


def test_launchd_label_falls_back_to_username_env(monkeypatch):
    """On Windows-style hosts $USER is unset; $USERNAME is the standard.
    Defensive even though Windows hits the null backend path — the
    helper is platform-neutral."""
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("USERNAME", "bob")
    assert scheduler._launchd_label() == "com.bob.brain-auto-extract"


def test_launchd_label_safe_default_when_no_user_env(monkeypatch):
    """Both env vars unset → "user" placeholder. Never crash, never
    return a bare/empty label."""
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    assert scheduler._launchd_label() == "com.user.brain-auto-extract"


# ---------- launchd backend ---------------------------------------------


@pytest.fixture
def force_launchd(monkeypatch):
    monkeypatch.setattr(scheduler, "current_backend", lambda: "launchd")


def _fake_launchctl(stdout: str, returncode: int = 0):
    class _CP:
        pass
    cp = _CP()
    cp.returncode = returncode
    cp.stdout = stdout
    return cp


def test_launchd_not_loaded_returns_false(force_launchd, monkeypatch):
    monkeypatch.setattr(
        scheduler.subprocess, "run",
        lambda *a, **kw: _fake_launchctl("", returncode=113),
    )
    out = scheduler.get_status()
    assert out["backend"] == "launchd"
    assert out["loaded"] is False
    assert out["pid"] is None


def test_launchd_loaded_idle(force_launchd, monkeypatch):
    body = (
        "{\n"
        '\t"LastExitStatus" = 0;\n'
        '\t"Label" = "com.son.brain-auto-extract";\n'
        "};\n"
    )
    monkeypatch.setattr(
        scheduler.subprocess, "run",
        lambda *a, **kw: _fake_launchctl(body),
    )
    out = scheduler.get_status()
    assert out["loaded"] is True
    assert out["pid"] is None
    assert out["last_exit"] == 0


def test_launchd_loaded_running_reports_pid(force_launchd, monkeypatch):
    body = (
        "{\n"
        '\t"PID" = 54321;\n'
        '\t"LastExitStatus" = 0;\n'
        '\t"Label" = "com.son.brain-auto-extract";\n'
        "};\n"
    )
    monkeypatch.setattr(
        scheduler.subprocess, "run",
        lambda *a, **kw: _fake_launchctl(body),
    )
    out = scheduler.get_status()
    assert out["pid"] == 54321


def test_launchd_reports_nonzero_exit(force_launchd, monkeypatch):
    body = '{\n\t"LastExitStatus" = -9;\n};\n'
    monkeypatch.setattr(
        scheduler.subprocess, "run",
        lambda *a, **kw: _fake_launchctl(body),
    )
    out = scheduler.get_status()
    assert out["last_exit"] == -9


def test_launchd_missing_binary_reports_not_loaded(force_launchd, monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("launchctl not on PATH")
    monkeypatch.setattr(scheduler.subprocess, "run", boom)
    out = scheduler.get_status()
    assert out["loaded"] is False
    assert out["backend"] == "launchd"


# ---------- systemd backend ---------------------------------------------


@pytest.fixture
def force_systemd(monkeypatch):
    monkeypatch.setattr(scheduler, "current_backend", lambda: "systemd")


def _systemctl_stub(replies: dict[str, str]):
    """Return a subprocess.run stub that answers by matching the unit
    argument. replies = {unit_name: stdout_text}."""
    def fake(cmd, **kw):
        class _CP:
            returncode = 0
            stdout = ""
        cp = _CP()
        # cmd shape: ["systemctl", "--user", "show", UNIT, "--property=..."]
        unit = cmd[3] if len(cmd) > 3 else ""
        cp.stdout = replies.get(unit, "")
        if not cp.stdout:
            cp.returncode = 1
        return cp
    return fake


def test_systemd_not_installed(force_systemd, monkeypatch):
    """Neither the timer nor the service exist → loaded=False, all
    optional fields None."""
    monkeypatch.setattr(
        scheduler.subprocess, "run", _systemctl_stub({})
    )
    out = scheduler.get_status()
    assert out["backend"] == "systemd"
    assert out["loaded"] is False
    assert out["pid"] is None
    assert out["interval_s"] is None


def test_systemd_timer_active_service_idle(force_systemd, monkeypatch):
    """Common steady state: timer primed, service slot exists but idle
    (MainPID=0, last exit 0)."""
    monkeypatch.setattr(scheduler.subprocess, "run", _systemctl_stub({
        scheduler.SYSTEMD_UNIT: (
            "ActiveState=active\n"
            "LoadState=loaded\n"
            "TimersMonotonic={ OnUnitActiveSec 15min }\n"
            "TimersCalendar=\n"
            "NextElapseUSecMonotonic=123\n"
        ),
        f"{scheduler.LABEL_BASE}.service": (
            "MainPID=0\n"
            "ExecMainStatus=0\n"
            "ExecMainCode=1\n"
        ),
    }))
    out = scheduler.get_status()
    assert out["loaded"] is True
    assert out["pid"] is None                           # MainPID=0 → idle
    assert out["last_exit"] == 0
    assert out["interval_s"] == 15 * 60                 # 15min → 900s


def test_systemd_timer_and_service_running(force_systemd, monkeypatch):
    """Mid-execution: service MainPID populated → scheduler reports pid."""
    monkeypatch.setattr(scheduler.subprocess, "run", _systemctl_stub({
        scheduler.SYSTEMD_UNIT: (
            "ActiveState=active\nLoadState=loaded\n"
            "TimersMonotonic={ OnUnitActiveSec 300s }\n"
        ),
        f"{scheduler.LABEL_BASE}.service": (
            "MainPID=4242\nExecMainStatus=0\n"
        ),
    }))
    out = scheduler.get_status()
    assert out["pid"] == 4242
    assert out["interval_s"] == 300


def test_systemd_timer_installed_but_disabled(force_systemd, monkeypatch):
    """`systemctl --user disable` leaves LoadState=loaded but
    ActiveState=inactive — treat as not loaded."""
    monkeypatch.setattr(scheduler.subprocess, "run", _systemctl_stub({
        scheduler.SYSTEMD_UNIT: (
            "ActiveState=inactive\nLoadState=loaded\n"
            "TimersMonotonic={ OnUnitActiveSec 300s }\n"
        ),
    }))
    out = scheduler.get_status()
    assert out["loaded"] is False


def test_systemd_calendar_based_timer_interval_none(force_systemd, monkeypatch):
    """A TimersCalendar-only timer (no monotonic trigger) has no
    knowable interval — the dashboard should render 'unknown'."""
    monkeypatch.setattr(scheduler.subprocess, "run", _systemctl_stub({
        scheduler.SYSTEMD_UNIT: (
            "ActiveState=active\nLoadState=loaded\n"
            "TimersMonotonic=\n"
            "TimersCalendar={ OnCalendar hourly }\n"
        ),
    }))
    out = scheduler.get_status()
    assert out["loaded"] is True
    assert out["interval_s"] is None


def test_systemd_missing_binary_returns_not_loaded(force_systemd, monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("systemctl not on PATH")
    monkeypatch.setattr(scheduler.subprocess, "run", boom)
    out = scheduler.get_status()
    assert out["loaded"] is False
    assert out["backend"] == "systemd"


# ---------- interval parser edge cases ----------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("{ OnUnitActiveSec 15min }",              15 * 60),
    ("{ OnActiveSec 5s } { OnUnitActiveSec 300s }", 5),   # smallest wins
    ("{ OnBootSec 2min } { OnUnitActiveSec 30s }",  30),
    ("{ OnBootSec 100ms }",                     0),        # 100ms rounds to 0 → filtered
    ("{ OnBootSec 2h }",                        2 * 3600),
    ("",                                        None),
    ("calendar-only",                           None),
])
def test_parse_timer_interval_shapes(raw, expected):
    if expected == 0:
        # Rounds to 0 which we filter out — returns None.
        assert scheduler._parse_timer_interval(raw) is None
    else:
        assert scheduler._parse_timer_interval(raw) == expected


# ---------- null fallback -----------------------------------------------


def test_null_backend_shape(monkeypatch):
    monkeypatch.setattr(scheduler, "current_backend", lambda: "none")
    out = scheduler.get_status()
    assert out == {
        "backend": "none",
        "loaded": False,
        "pid": None,
        "last_exit": None,
        "interval_s": None,
        "label": scheduler.LABEL_BASE,
    }


def test_get_status_swallows_backend_exceptions(monkeypatch):
    """A probe that blows up must not crash `brain_status`. The
    fallback is a null-shaped dict so the dashboard still renders."""
    monkeypatch.setattr(scheduler, "current_backend", lambda: "launchd")
    def boom(*a, **kw):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(scheduler, "_launchd_status", boom)
    out = scheduler.get_status()
    assert out["backend"] == "none"
    assert out["loaded"] is False
