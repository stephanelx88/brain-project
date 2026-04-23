"""resource_guard — adaptive clearance level for background brain jobs.

Clearance levels (all listed conditions must hold):
  0  always                                              harvest, WatchPaths
  1  CPU < 60% + MEM < 90%                               note_extract, index rebuild
  2  CPU < 40% + MEM < 80% + idle 60s                    LLM extract, reconcile
  3  CPU < 20% + MEM < 70% + idle 180s + AC              dedupe
  4  CPU < 15% + MEM < 60% + idle 300s + AC + screen idle  backfill, revalidate

CLI usage:
  python -m brain.resource_guard            # prints level (0-4)
  python -m brain.resource_guard --min-level 3   # exits 0 if level >= 3, else 1
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path

import psutil

import brain.config as config

_SYSTEM = platform.system()

# ---------------------------------------------------------------------------
# Tunable thresholds (override via env vars for testing)
# ---------------------------------------------------------------------------

_CPU_L1 = float(os.environ.get("BRAIN_RG_CPU_L1", "60"))   # %
_CPU_L2 = float(os.environ.get("BRAIN_RG_CPU_L2", "40"))
_CPU_L3 = float(os.environ.get("BRAIN_RG_CPU_L3", "20"))
_CPU_L4 = float(os.environ.get("BRAIN_RG_CPU_L4", "15"))

_MEM_L1 = float(os.environ.get("BRAIN_RG_MEM_L1", "90"))   # % used
_MEM_L2 = float(os.environ.get("BRAIN_RG_MEM_L2", "80"))
_MEM_L3 = float(os.environ.get("BRAIN_RG_MEM_L3", "70"))
_MEM_L4 = float(os.environ.get("BRAIN_RG_MEM_L4", "60"))

_IDLE_L2 = float(os.environ.get("BRAIN_RG_IDLE_L2", "60"))    # seconds
_IDLE_L3 = float(os.environ.get("BRAIN_RG_IDLE_L3", "180"))
_IDLE_L4 = float(os.environ.get("BRAIN_RG_IDLE_L4", "300"))

_SCREEN_L4 = float(os.environ.get("BRAIN_RG_SCREEN_L4", "120"))  # seconds


# ---------------------------------------------------------------------------
# Sensor helpers
# ---------------------------------------------------------------------------

def _cpu_percent() -> float:
    """1-second CPU sample across all cores."""
    return psutil.cpu_percent(interval=1)


def _memory_percent() -> float:
    """Current system memory usage as a percentage (0-100)."""
    return psutil.virtual_memory().percent


def _on_ac_power_macos() -> bool | None:
    try:
        out = subprocess.check_output(
            ["pmset", "-g", "ps"], text=True, timeout=3, stderr=subprocess.DEVNULL
        )
        return "AC Power" in out
    except Exception:
        return None


def _on_ac_power_linux() -> bool | None:
    """Read `/sys/class/power_supply/*/online`.

    Laptops expose one or more AC adapters (`ACAD`, `AC`, `ADP1`, …) with
    `online` set to 1 when plugged in. Desktops and servers have no such
    entries — we return True there (headless servers are effectively
    always on mains power). Returns None only when the sysfs path exists
    but reading every entry failed, so the caller falls back to the safe
    default.
    """
    ps_root = Path("/sys/class/power_supply")
    if not ps_root.is_dir():
        return True                                 # no power_supply → desktop / VM / server
    ac_entries = [
        p for p in ps_root.iterdir()
        if (p / "type").exists()
        and p.joinpath("type").read_text(errors="replace").strip() == "Mains"
    ]
    if not ac_entries:
        return True                                 # battery-only machine w/o AC probe — assume AC
    any_read_ok = False
    for entry in ac_entries:
        try:
            val = (entry / "online").read_text(errors="replace").strip()
            any_read_ok = True
            if val == "1":
                return True
        except OSError:
            continue
    return False if any_read_ok else None


def _on_ac_power() -> bool:
    """True when the machine is plugged in (or is a desktop / server).

    Dispatches on platform so the probe uses the right interface on each
    OS. `True` is the safe default — failing to detect AC must not stop
    background jobs from running, especially on headless servers that
    have no battery concept at all.
    """
    if _SYSTEM == "Darwin":
        result = _on_ac_power_macos()
    elif _SYSTEM == "Linux":
        result = _on_ac_power_linux()
    else:
        result = None
    return True if result is None else result


def _screen_idle_seconds_macos() -> float | None:
    try:
        out = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem", "-r", "-k", "HIDIdleTime"],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                val = line.split("=")[-1].strip()
                return int(val) / 1_000_000_000
    except Exception:
        return None
    return None


def _screen_idle_seconds_linux() -> float | None:
    """Best-effort HID idle detection on Linux.

    Headless servers (no DISPLAY, no Wayland socket) have no screen to
    track — we return a large value so the idle-based clearance gates
    always pass. That's the right call for Ubuntu Server: the idle
    guard exists to avoid stealing CPU from a human using the
    keyboard, and there is no such human.

    Desktop Linux with X11 + `xprintidle` installed gets real idle
    time in milliseconds. Wayland has no standard idle API (each
    compositor exposes its own), so we fall back to `loginctl` or
    ultimately None (caller → 0.0 → conservative).
    """
    headless = not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
    if headless:
        # No screen to worry about — return a big number so the idle
        # thresholds for L2–L4 always clear. 1e9 s ≈ 31 years.
        return 1e9
    # X11 path — xprintidle returns milliseconds.
    try:
        out = subprocess.check_output(
            ["xprintidle"], text=True, timeout=3, stderr=subprocess.DEVNULL,
        )
        return float(out.strip()) / 1000.0
    except Exception:
        pass
    # loginctl SessionIdle fallback (works on some systemd sessions).
    try:
        sid = os.environ.get("XDG_SESSION_ID", "")
        if not sid:
            return None
        out = subprocess.check_output(
            ["loginctl", "show-session", sid, "--property=IdleSinceHint"],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
        )
        _, _, value = out.strip().partition("=")
        usec = int(value or 0)
        if usec <= 0:
            return None
        now_usec = time.time() * 1_000_000
        return max(0.0, (now_usec - usec) / 1_000_000)
    except Exception:
        return None


def _screen_idle_seconds() -> float:
    """Seconds since the last user HID event; 0.0 when unknown.

    0.0 is the conservative choice: the idle-based clearance gates fail
    closed, so uncertain inputs never elevate the level. Linux headless
    servers skip this path entirely (see `_screen_idle_seconds_linux`).
    """
    if _SYSTEM == "Darwin":
        val = _screen_idle_seconds_macos()
    elif _SYSTEM == "Linux":
        val = _screen_idle_seconds_linux()
    else:
        val = None
    return 0.0 if val is None else val


def _session_idle_seconds() -> float:
    """Seconds since the most recent file was written to the brain raw/ dir.

    Harvest writes files to raw/ within ~1 s of a session producing output.
    When no raw file has been updated for N seconds we consider sessions idle.
    Returns 0.0 if raw/ doesn't exist or is empty (conservative).
    """
    raw_dir = config.RAW_DIR
    if not raw_dir.is_dir():
        return 0.0
    try:
        mtimes = [p.stat().st_mtime for p in raw_dir.iterdir() if p.is_file()]
        if not mtimes:
            return 0.0
        return time.time() - max(mtimes)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def clearance_level(
    *,
    cpu: float | None = None,
    mem: float | None = None,
    session_idle: float | None = None,
    on_ac: bool | None = None,
    screen_idle: float | None = None,
) -> int:
    """Return the current clearance level (0–4).

    Keyword overrides are for testing — omit them in production.
    """
    if cpu is None:
        cpu = _cpu_percent()
    if mem is None:
        mem = _memory_percent()
    if session_idle is None:
        session_idle = _session_idle_seconds()
    if on_ac is None:
        on_ac = _on_ac_power()
    if screen_idle is None:
        screen_idle = _screen_idle_seconds()

    # Levels are additive — each level requires all lower conditions plus more.
    if (
        cpu < _CPU_L4 and mem < _MEM_L4
        and session_idle >= _IDLE_L4 and on_ac and screen_idle >= _SCREEN_L4
    ):
        return 4
    if cpu < _CPU_L3 and mem < _MEM_L3 and session_idle >= _IDLE_L3 and on_ac:
        return 3
    if cpu < _CPU_L2 and mem < _MEM_L2 and session_idle >= _IDLE_L2:
        return 2
    if cpu < _CPU_L1 and mem < _MEM_L1:
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Print current resource clearance level (0-4) or gate on --min-level."
    )
    parser.add_argument(
        "--min-level", type=int, default=None, metavar="N",
        help="Exit 0 if clearance >= N, else exit 1.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print sensor readings alongside level.",
    )
    args = parser.parse_args()

    cpu = _cpu_percent()
    mem = _memory_percent()
    session_idle = _session_idle_seconds()
    on_ac = _on_ac_power()
    screen_idle = _screen_idle_seconds()
    level = clearance_level(
        cpu=cpu, mem=mem, session_idle=session_idle, on_ac=on_ac, screen_idle=screen_idle
    )

    if args.verbose:
        print(
            f"level={level}  cpu={cpu:.1f}%  mem={mem:.1f}%  session_idle={session_idle:.0f}s"
            f"  ac={on_ac}  screen_idle={screen_idle:.0f}s"
        )
    else:
        print(level)

    if args.min_level is not None:
        sys.exit(0 if level >= args.min_level else 1)


if __name__ == "__main__":
    main()
