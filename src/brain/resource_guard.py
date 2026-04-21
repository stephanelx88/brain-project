"""resource_guard — adaptive clearance level for background brain jobs.

Clearance levels:
  0  always              harvest, WatchPaths
  1  CPU < 60%           note_extract, index rebuild (pure Python, no LLM)
  2  CPU < 40% + idle 60s    LLM extract, reconcile
  3  CPU < 20% + idle 180s + AC  autoresearch, dedupe
  4  CPU < 15% + idle 300s + AC + screen idle  backfill, revalidate

CLI usage:
  python -m brain.resource_guard            # prints level (0-4)
  python -m brain.resource_guard --min-level 3   # exits 0 if level >= 3, else 1
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import psutil

import brain.config as config

# ---------------------------------------------------------------------------
# Tunable thresholds (override via env vars for testing)
# ---------------------------------------------------------------------------

_CPU_L1 = float(os.environ.get("BRAIN_RG_CPU_L1", "60"))   # %
_CPU_L2 = float(os.environ.get("BRAIN_RG_CPU_L2", "40"))
_CPU_L3 = float(os.environ.get("BRAIN_RG_CPU_L3", "20"))
_CPU_L4 = float(os.environ.get("BRAIN_RG_CPU_L4", "15"))

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


def _on_ac_power() -> bool:
    """True when the machine is plugged in (or is a desktop / has no battery)."""
    try:
        out = subprocess.check_output(
            ["pmset", "-g", "ps"], text=True, timeout=3, stderr=subprocess.DEVNULL
        )
        # 'AC Power' appears in the output when plugged in
        return "AC Power" in out
    except Exception:
        # Can't determine → assume AC (safe default: don't block jobs on desktops)
        return True


def _screen_idle_seconds() -> float:
    """Seconds since the last user HID event (keyboard / mouse / touch).

    Uses ioreg on macOS; returns 0.0 on failure so the guard stays conservative.
    """
    try:
        out = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem", "-r", "-k", "HIDIdleTime"],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                # value is in nanoseconds
                val = line.split("=")[-1].strip()
                return int(val) / 1_000_000_000
    except Exception:
        pass
    return 0.0


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
    session_idle: float | None = None,
    on_ac: bool | None = None,
    screen_idle: float | None = None,
) -> int:
    """Return the current clearance level (0–4).

    Keyword overrides are for testing — omit them in production.
    """
    if cpu is None:
        cpu = _cpu_percent()
    if session_idle is None:
        session_idle = _session_idle_seconds()
    if on_ac is None:
        on_ac = _on_ac_power()
    if screen_idle is None:
        screen_idle = _screen_idle_seconds()

    # Levels are additive — each level requires all lower conditions plus more.
    if cpu < _CPU_L4 and session_idle >= _IDLE_L4 and on_ac and screen_idle >= _SCREEN_L4:
        return 4
    if cpu < _CPU_L3 and session_idle >= _IDLE_L3 and on_ac:
        return 3
    if cpu < _CPU_L2 and session_idle >= _IDLE_L2:
        return 2
    if cpu < _CPU_L1:
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
    session_idle = _session_idle_seconds()
    on_ac = _on_ac_power()
    screen_idle = _screen_idle_seconds()
    level = clearance_level(
        cpu=cpu, session_idle=session_idle, on_ac=on_ac, screen_idle=screen_idle
    )

    if args.verbose:
        print(
            f"level={level}  cpu={cpu:.1f}%  session_idle={session_idle:.0f}s"
            f"  ac={on_ac}  screen_idle={screen_idle:.0f}s"
        )
    else:
        print(level)

    if args.min_level is not None:
        sys.exit(0 if level >= args.min_level else 1)


if __name__ == "__main__":
    main()
