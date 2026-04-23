"""Cross-platform scheduler backend.

Abstracts "is the auto-extract job loaded, running, and when did it
last fire?" across macOS (`launchd`) and Linux (`systemd --user`).
Callers (brain.status, bin/install.sh via Python wrapper) see a
uniform dict — the backend is chosen by platform.

Dict shape returned by `get_status()`:

    {
        "backend": "launchd" | "systemd" | "none",
        "loaded": bool,
        "pid": int | None,
        "last_exit": int | None,
        "interval_s": int | None,
        "label": str,
    }

Field semantics:
  - ``loaded``     — job is known to the scheduler (may be idle between ticks)
  - ``pid``        — present only while the job is actively executing
  - ``last_exit``  — most recent exit code; 0 is healthy, non-zero is worth surfacing
  - ``interval_s`` — seconds between scheduled ticks; None if unknown / event-driven
  - ``label``      — scheduler-level job name for debugging (plist filename
                     on macOS, systemd unit name on Linux)

`brain_status` MCP tool surfaces this dict verbatim under the
``scheduler`` key so agents can reason about scheduling state without
platform-sniffing. The launchd-specific key name is retired — the
project now runs on Ubuntu Server headless as well as developer Macs,
and one canonical key keeps downstream callers simple.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path


# Backend-neutral job label. macOS uses this verbatim as the launchd
# label (``com.son.brain-auto-extract``); Linux uses it as the systemd
# unit stem (``brain-auto-extract.timer``). Callers never need to
# translate between the two — pass this string into any backend and
# the backend knows what to do with it.
LABEL_BASE = "brain-auto-extract"
LAUNCHD_LABEL = "com.son.brain-auto-extract"
SYSTEMD_UNIT = f"{LABEL_BASE}.timer"


def _default_interval() -> int | None:
    """Nothing to probe when no backend is available — callers render
    'unknown' in the UI."""
    return None


# ---------------------------------------------------------------------------
# launchd (macOS)
# ---------------------------------------------------------------------------

_LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchd_read_interval() -> int | None:
    """Pull StartInterval out of the plist with a regex.

    `plistlib` would be more correct, but we only need "roughly how
    often" for the dashboard; plists using StartCalendarInterval instead
    return None (the dashboard then says "unknown"). Kept lightweight so
    `brain_status` stays sub-50ms on every call."""
    try:
        text = _LAUNCHD_PLIST.read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", text)
    return int(m.group(1)) if m else None


def _launchd_status() -> dict:
    out: dict = {
        "backend": "launchd",
        "loaded": False,
        "pid": None,
        "last_exit": None,
        "interval_s": None,
        "label": LAUNCHD_LABEL,
    }
    try:
        cp = subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return out
    if cp.returncode != 0:
        return out
    out["loaded"] = True
    # `launchctl list LABEL` emits a plist-ish text blob:
    #   { "PID" = 12345; "LastExitStatus" = 0; "Label" = "..."; };
    pid_m = re.search(r'"PID"\s*=\s*(\d+);', cp.stdout)
    if pid_m:
        out["pid"] = int(pid_m.group(1))
    exit_m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+);', cp.stdout)
    if exit_m:
        out["last_exit"] = int(exit_m.group(1))
    out["interval_s"] = _launchd_read_interval()
    return out


# ---------------------------------------------------------------------------
# systemd --user (Linux)
# ---------------------------------------------------------------------------

def _systemctl_show(unit: str, fields: list[str]) -> dict[str, str]:
    """Return the requested systemctl Show= fields as a dict.

    `systemctl --user show UNIT --property=A,B` prints `A=value\\nB=value`.
    Returns an empty dict on any failure so callers degrade cleanly.
    """
    try:
        cp = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "--property=" + ",".join(fields)],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if cp.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _systemd_status() -> dict:
    """Probe the ``brain-auto-extract.timer`` (+ its bound service) via
    ``systemctl --user show``. Parses:

      - timer.ActiveState    → loaded (if 'active' or 'activating', job is registered)
      - service.MainPID      → pid (0 when not running; mapped to None)
      - service.ExecMainStatus → last_exit
      - timer.TimersMonotonic → interval_s (when the unit is OnUnitActiveSec=...)
                                or timer.AccuracyUSec / OnCalendar fallback
    """
    out: dict = {
        "backend": "systemd",
        "loaded": False,
        "pid": None,
        "last_exit": None,
        "interval_s": None,
        "label": SYSTEMD_UNIT,
    }

    timer = _systemctl_show(
        SYSTEMD_UNIT,
        ["ActiveState", "LoadState", "TimersMonotonic",
         "TimersCalendar", "NextElapseUSecMonotonic"],
    )
    if not timer:
        return out
    active_state = timer.get("ActiveState", "")
    load_state = timer.get("LoadState", "")
    # A timer is "loaded" in our sense if systemd knows about it AND it
    # hasn't been masked / disabled. `active` means primed; `inactive`
    # with LoadState=loaded means disabled but installed.
    if load_state == "loaded" and active_state in ("active", "activating"):
        out["loaded"] = True
    out["interval_s"] = _parse_timer_interval(timer.get("TimersMonotonic", ""))

    # The service is bound to the timer by naming convention: same stem,
    # `.service` instead of `.timer`. That's how systemd user timers
    # are generally wired.
    svc = _systemctl_show(
        f"{LABEL_BASE}.service",
        ["MainPID", "ExecMainStatus", "ExecMainCode"],
    )
    if svc:
        try:
            pid = int(svc.get("MainPID", "0") or 0)
            out["pid"] = pid if pid > 0 else None
        except ValueError:
            out["pid"] = None
        try:
            out["last_exit"] = int(svc.get("ExecMainStatus", "") or 0)
        except ValueError:
            out["last_exit"] = None

    return out


def _parse_timer_interval(raw: str) -> int | None:
    """Parse ``TimersMonotonic=... { OnUnitActiveSec 900s }`` into 900.

    systemd serialises the same timer expression in several shapes
    depending on whether you used OnUnitActiveSec / OnActiveSec / etc.
    We accept any of them and fall back to None when the expression is
    calendar-driven (OnCalendar=hourly style), since "unknown" is
    honest in that case.
    """
    if not raw:
        return None
    # Typical shape: "{ OnUnitActiveSec ... 15min } { OnBootSec ... 5min }"
    # We pick the smallest literal duration we can parse — whichever
    # trigger fires first is the effective cadence.
    candidates: list[int] = []
    for m in re.finditer(r"\b(\d+(?:\.\d+)?)(us|ms|s|min|h)\b", raw):
        value, unit = float(m.group(1)), m.group(2)
        secs = {
            "us": value / 1e6, "ms": value / 1e3, "s": value,
            "min": value * 60, "h": value * 3600,
        }.get(unit)
        if secs is None:
            continue
        candidates.append(int(round(secs)))
    positive = [c for c in candidates if c > 0]
    if not positive:
        # All triggers rounded to 0s (sub-second cadence) — the
        # dashboard renders "unknown" rather than misleading "0s".
        return None
    return min(positive)


# ---------------------------------------------------------------------------
# null fallback
# ---------------------------------------------------------------------------

def _null_status() -> dict:
    """Used on platforms we don't recognise (Windows / unknown BSDs)."""
    return {
        "backend": "none",
        "loaded": False,
        "pid": None,
        "last_exit": None,
        "interval_s": None,
        "label": LABEL_BASE,
    }


# ---------------------------------------------------------------------------
# public dispatcher
# ---------------------------------------------------------------------------

def current_backend() -> str:
    """Name of the scheduler appropriate for this host.

    Indirected so tests can force a specific backend via monkeypatch
    without faking `platform.system()` globally.
    """
    system = platform.system()
    if system == "Darwin":
        return "launchd"
    if system == "Linux":
        return "systemd"
    return "none"


def get_status() -> dict:
    """Return scheduler state for the host platform.

    Falls through to `_null_status` on any unexpected exception — the
    dashboard must never crash because a scheduler probe failed.
    """
    try:
        backend = current_backend()
        if backend == "launchd":
            return _launchd_status()
        if backend == "systemd":
            return _systemd_status()
    except Exception:
        pass
    return _null_status()
