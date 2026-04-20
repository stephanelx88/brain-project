"""`brain status` — single-shot operational dashboard.

Surfaces the answers to "is the brain doing anything right now?" without
the user having to know the launchd job label, log path, lock-dir name,
ledger format, etc.

Two consumers:

  - CLI (`brain status` → `cli._cmd_status`) prints the human-readable
    block.
  - MCP (`brain_status` tool) returns the same data as JSON so an agent
    can decide whether to nudge the user ("a dedupe pass is in flight,
    hold off on heavy edits") without parsing log lines.

Design notes:

- Read-only. No subprocesses spawned beyond `launchctl list` (cheap,
  always-on system call) and an optional `ps -A` snapshot. We never
  call into Anthropic, never mutate ledgers.
- Tolerant of every component being missing — fresh installs without
  launchd loaded, vaults without logs, ledgers not yet created. Each
  field falls back to `None` / "unknown" rather than raising.
- Time formatting is delta-from-now ("8s ago", "4m52s") because absolute
  UTC timestamps in a status line are noise — the user wants to know
  "is this fresh?".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import brain.config as config

# Public so tests can patch them. The launchd label and lock dir are
# wired by the install templates and rarely change; if they ever do,
# this is the single point of update.
LAUNCHD_LABEL = "com.son.brain-auto-extract"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
EXTRACT_LOCK_DIR = config.BRAIN_DIR / ".extract.lock.d"
AUTO_EXTRACT_LOG = config.BRAIN_DIR / "logs" / "auto-extract.log"
HARVEST_LEDGER = config.BRAIN_DIR / ".harvested"
DEDUPE_LEDGER = config.BRAIN_DIR / ".dedupe.ledger.json"
RECALL_LEDGER = config.BRAIN_DIR / "recall-ledger.jsonl"

# Process names we care about when answering "is the brain spending
# tokens right now?". `claude --print` is the LLM caller; the rest are
# the orchestrators that spawn it.
LLM_PROC_PATTERNS = [
    "brain.auto_extract",
    "brain.reconcile",
    "brain.dedupe",
    "brain.autoresearch",
    "claude --print",
]


@dataclass
class StatusReport:
    brain_dir: str
    launchd: dict
    in_flight: dict
    last_run: dict
    next_run: dict
    spawned_procs: list[dict]
    ledgers: dict
    pending_audit: dict
    vault: dict
    coverage: dict = field(default_factory=dict)
    live_coverage: dict = field(default_factory=dict)


# ---------- helpers --------------------------------------------------------


def _delta_str(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    s = int(seconds)
    if s < 0:
        return f"in {_delta_str(-s)}"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _tail(path: Path, max_bytes: int = 64_000) -> str | None:
    """Cheap tail — read at most the trailing `max_bytes` of a file.

    The auto-extract log grows unbounded; reading it whole on every
    status call would do real work for no reason."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    try:
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


# ---------- launchd --------------------------------------------------------


def _launchd_state() -> dict:
    """Parse `launchctl list <label>`. Returns:

      {"loaded": bool, "pid": int|None, "last_exit": int|None,
       "interval_s": int|None, "label": str}

    We use the per-label form (`launchctl list LABEL`) because the
    no-arg form returns a giant table that's painful to grep reliably.
    `launchctl print` would be richer but is much slower (200ms+) and
    isn't needed for the dashboard."""
    out: dict = {
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
    #   {
    #     "PID" = 12345;        # only present while running
    #     "LastExitStatus" = 0;
    #     "Label" = "com.son.brain-auto-extract";
    #   };
    pid_m = re.search(r'"PID"\s*=\s*(\d+);', cp.stdout)
    if pid_m:
        out["pid"] = int(pid_m.group(1))
    exit_m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+);', cp.stdout)
    if exit_m:
        out["last_exit"] = int(exit_m.group(1))
    out["interval_s"] = _read_plist_interval()
    return out


def _read_plist_interval() -> int | None:
    """Pull StartInterval out of the plist with a regex.

    `plistlib` would be more correct, but the plist may also use
    StartCalendarInterval (a sequence of dicts); for the dashboard we
    only need to know "roughly how often", and StartInterval covers the
    current default install. Returns None if the plist uses the
    calendar form or doesn't exist."""
    text = _safe_read(LAUNCHD_PLIST)
    if not text:
        return None
    m = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", text)
    if m:
        return int(m.group(1))
    return None


# ---------- in-flight + last run -------------------------------------------


def _in_flight() -> dict:
    """Is auto-extract.sh executing right now? The script creates
    `~/.brain/.extract.lock.d/` with a `pid` file as its `flock`-style
    singleton. A stale lock (process gone) is reported but not cleaned
    up — that's the script's job on next launch."""
    if not EXTRACT_LOCK_DIR.exists():
        return {"running": False, "pid": None, "stale": False, "started_s_ago": None}
    pid_file = EXTRACT_LOCK_DIR / "pid"
    pid: int | None = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid = None
    alive = pid is not None and _pid_alive(pid)
    started_s_ago: float | None = None
    try:
        started_s_ago = time.time() - EXTRACT_LOCK_DIR.stat().st_mtime
    except OSError:
        pass
    return {
        "running": alive,
        "pid": pid,
        "stale": not alive,
        "started_s_ago": started_s_ago,
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


_RUN_HEADER_RE = re.compile(
    r"=== (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) auto-extract run "
    r"\(active_session=(?P<active>[01])\)"
)


def _last_run() -> dict:
    """Parse the most recent `=== ... auto-extract run ... ===` header
    out of the log tail. We also count the trailing `skip ...` lines so
    the dashboard can say *"last 5 runs all skipped — heavy session"*
    instead of just *"last run skipped"*."""
    out: dict = {
        "ts": None, "age_s": None, "active_session": None,
        "skipped_streak": 0, "log_path": str(AUTO_EXTRACT_LOG),
    }
    text = _tail(AUTO_EXTRACT_LOG)
    if not text:
        return out
    matches = list(_RUN_HEADER_RE.finditer(text))
    if not matches:
        return out
    last = matches[-1]
    ts_str = last.group("ts")
    out["ts"] = ts_str
    out["active_session"] = last.group("active") == "1"
    try:
        epoch = time.mktime(time.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ"))
        # `time.mktime` interprets the struct as local time; the ts is
        # UTC, so add the offset back.
        epoch -= time.timezone
        out["age_s"] = max(0.0, time.time() - epoch)
    except (ValueError, OverflowError):
        pass

    # Count the streak of recent runs that ended with the
    # `skip auto_extract+reconcile+dedupe` line — useful when the user
    # wonders why heavy stages haven't run in a while.
    skip_re = re.compile(r"^skip auto_extract\+reconcile\+dedupe", re.M)
    runs = list(_RUN_HEADER_RE.finditer(text))
    streak = 0
    for r in reversed(runs):
        chunk = text[r.end():(runs[runs.index(r) + 1].start() if r is not runs[-1] else len(text))]
        if skip_re.search(chunk):
            streak += 1
        else:
            break
    out["skipped_streak"] = streak
    return out


def _next_run(launchd: dict, last_run: dict) -> dict:
    """Estimate time-until-next-tick. Only meaningful when launchd is
    loaded and we know both the interval and a last-run timestamp."""
    interval = launchd.get("interval_s")
    age = last_run.get("age_s")
    if not interval or age is None:
        return {"in_s": None, "interval_s": interval}
    return {"in_s": max(0.0, interval - age), "interval_s": interval}


# ---------- spawned LLM procs ---------------------------------------------


def _spawned_procs() -> list[dict]:
    """Snapshot any brain-related subprocesses currently running.

    Runs `ps -A -o pid=,etime=,command=` once and pattern-matches the
    command column. We deliberately avoid psutil (extra dep) — `ps` is
    on every macOS / Linux box and the output is stable enough."""
    try:
        cp = subprocess.run(
            ["ps", "-A", "-o", "pid=,etime=,command="],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if cp.returncode != 0:
        return []
    procs: list[dict] = []
    self_pid = os.getpid()
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == self_pid:
            continue
        cmd = parts[2]
        if not any(pat in cmd for pat in LLM_PROC_PATTERNS):
            continue
        procs.append({"pid": pid, "etime": parts[1], "cmd": cmd[:200]})
    return procs


# ---------- ledgers + vault -----------------------------------------------


def _ledgers() -> dict:
    out: dict = {"harvested": None, "dedupe_verdicts": None}
    if HARVEST_LEDGER.exists():
        try:
            out["harvested"] = sum(1 for _ in HARVEST_LEDGER.open())
        except OSError:
            pass
    if DEDUPE_LEDGER.exists():
        try:
            data = json.loads(DEDUPE_LEDGER.read_text())
            if isinstance(data, dict):
                out["dedupe_verdicts"] = len(data)
            elif isinstance(data, list):
                out["dedupe_verdicts"] = len(data)
        except (OSError, json.JSONDecodeError):
            pass
    return out


def _pending_audit() -> dict:
    """Summary, not the full block — `brain_audit` is the tool for the
    full text. We just want the count for the dashboard line."""
    try:
        from brain import audit as audit_mod
    except Exception:
        return {"count": None, "by_kind": {}}
    try:
        items = audit_mod.top_n(limit=10)
    except Exception:
        return {"count": None, "by_kind": {}}
    by_kind: dict[str, int] = {}
    for it in items:
        k = getattr(it, "kind", "unknown")
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"count": len(items), "by_kind": by_kind}


def _coverage() -> dict:
    """Tail the recall-ledger to surface Question Coverage Score.

    Each autoresearch cycle appends a JSON line with `score` (miss rate,
    lower is better) and `avg_top` (mean top-k similarity, higher is
    better). We only need the latest two rows to show "current vs.
    previous" — everything further back lives in the ledger for offline
    analysis.

    Tolerant of the ledger not existing (fresh install before the first
    autoresearch run) and of malformed lines (hand-editing, partial
    writes during a crash)."""
    out: dict = {
        "available": False,
        "latest_score": None,
        "latest_avg_top": None,
        "prev_score": None,
        "delta_score": None,
        "last_ts": None,
        "runs_logged": 0,
        "threshold": None,
    }
    if not RECALL_LEDGER.exists():
        return out
    try:
        with RECALL_LEDGER.open("rb") as f:
            #  Read tail only — the ledger is append-only and a long
            #  run can accumulate thousands of lines; 32 KB is plenty
            #  to capture the last few runs without loading the world.
            try:
                size = f.seek(0, os.SEEK_END)
                if size > 32_768:
                    f.seek(-32_768, os.SEEK_END)
                else:
                    f.seek(0)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return out
    rows: list[dict] = []
    for raw in tail.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("kind") == "eval" and "score" in obj:
            rows.append(obj)
    out["runs_logged"] = len(rows)
    if not rows:
        return out
    latest = rows[-1]
    out["available"] = True
    out["latest_score"] = float(latest.get("score", 0.0))
    out["last_ts"] = latest.get("ts")
    out["threshold"] = latest.get("threshold")
    if "avg_top" in latest:
        out["latest_avg_top"] = float(latest["avg_top"])
    if len(rows) >= 2:
        prev = rows[-2]
        out["prev_score"] = float(prev.get("score", 0.0))
        out["delta_score"] = out["latest_score"] - out["prev_score"]
    return out


def _live_coverage(days: int = 7) -> dict:
    """Rolling-window coverage computed from real `brain_recall` calls
    the MCP server logged with `kind: "live"`. Complements `_coverage()`
    (which reads synthetic eval-set rows) by answering "how well is
    the brain serving actual queries this week?".

    Tolerant of missing ledger, missing/malformed rows, and no live
    calls yet (returns `available: False` — the formatter hides the
    line entirely)."""
    out = {
        "available": False, "days": days, "total_calls": 0,
        "misses": 0, "score": 0.0, "avg_top": 0.0,
    }
    if not RECALL_LEDGER.exists():
        return out
    try:
        from brain import recall_metric
    except Exception:
        return out
    try:
        data = recall_metric.live_coverage(days=days)
    except Exception:
        return out
    return data


def _vault_counts() -> dict:
    """Cheap entity / raw counts. Reads filesystem only — no SQLite, so
    this still works when the mirror hasn't been built yet."""
    out: dict = {"entities_total": 0, "by_type": {}, "raw_pending": 0}
    try:
        types = config._discover_entity_types()
    except Exception:
        types = {}
    for name, path in sorted(types.items()):
        try:
            n = sum(1 for p in path.glob("*.md") if not p.name.startswith("_"))
        except OSError:
            n = 0
        out["by_type"][name] = n
        out["entities_total"] += n
    if config.RAW_DIR.exists():
        try:
            out["raw_pending"] = sum(1 for _ in config.RAW_DIR.glob("*"))
        except OSError:
            pass
    return out


# ---------- public surface -------------------------------------------------


def gather() -> StatusReport:
    launchd = _launchd_state()
    in_flight = _in_flight()
    last_run = _last_run()
    next_run = _next_run(launchd, last_run)
    return StatusReport(
        brain_dir=str(config.BRAIN_DIR),
        launchd=launchd,
        in_flight=in_flight,
        last_run=last_run,
        next_run=next_run,
        spawned_procs=_spawned_procs(),
        ledgers=_ledgers(),
        pending_audit=_pending_audit(),
        vault=_vault_counts(),
        coverage=_coverage(),
        live_coverage=_live_coverage(),
    )


def to_json(report: StatusReport) -> str:
    return json.dumps(asdict(report), indent=2, ensure_ascii=False)


def format_text(report: StatusReport) -> str:
    """Human-readable dashboard. ~12 lines, fits in one terminal screen."""
    L = report.launchd
    F = report.in_flight
    R = report.last_run
    N = report.next_run
    A = report.pending_audit
    V = report.vault
    LD = report.ledgers

    if L["loaded"]:
        every = f"every {L['interval_s']}s" if L["interval_s"] else "schedule unknown"
        launchd_line = f"loaded ({L['label']}, {every})"
        if L.get("last_exit") not in (None, 0):
            launchd_line += f" — last exit {L['last_exit']}"
    else:
        launchd_line = f"NOT LOADED ({L['label']})"

    if F["running"]:
        flight_line = f"YES (pid {F['pid']}, started {_delta_str(F['started_s_ago'])} ago)"
    elif F.get("stale"):
        flight_line = f"stale lock dir present (pid {F['pid']}, gone)"
    else:
        flight_line = "no"

    if R["ts"]:
        last_line = f"{R['ts']} ({_delta_str(R['age_s'])} ago"
        if R["active_session"]:
            last_line += ", skipped — active session"
        last_line += ")"
        if R.get("skipped_streak", 0) >= 3:
            last_line += f"  [last {R['skipped_streak']} runs skipped]"
    else:
        last_line = "no run logged yet"

    next_line = (f"~{_delta_str(N['in_s'])}" if N["in_s"] is not None
                 else "unknown (launchd not loaded?)")

    procs = report.spawned_procs
    procs_line = (f"{len(procs)} brain/LLM procs running"
                  + (": " + ", ".join(p["cmd"].split()[0:2][-1] for p in procs)
                     if procs else ""))

    audit_line = (f"{A['count']} item(s) — {A['by_kind']}"
                  if A["count"] else "0 — clean")

    C = report.coverage
    if C.get("available"):
        miss_pct = f"{C['latest_score'] * 100:.1f}%"
        avg_top = (f"avg-top {C['latest_avg_top']:.3f}"
                   if C.get("latest_avg_top") is not None else "avg-top n/a")
        if C.get("delta_score") is not None:
            d = C["delta_score"]
            arrow = "↓" if d < 0 else ("↑" if d > 0 else "·")
            delta = f"Δ{arrow}{abs(d) * 100:.1f}pp"
        else:
            delta = "Δ—"
        thr = (f"@ thr {C['threshold']:.2f}"
               if C.get("threshold") is not None else "")
        coverage_line = (f"miss {miss_pct} ({delta}) · {avg_top} {thr}  "
                         f"[{C['runs_logged']} eval runs logged]")
    else:
        coverage_line = "no autoresearch cycles logged yet"

    L = report.live_coverage or {}
    if L.get("available"):
        live_miss_pct = f"{L['score'] * 100:.1f}%"
        live_avg = f"avg-top {L['avg_top']:.3f}"
        live_line = (f"miss {live_miss_pct} · {live_avg}  "
                     f"[{L['total_calls']} calls, "
                     f"{L.get('queries', 0)} uniq, last {L['days']}d]")
    else:
        live_line = None  # hidden until the MCP has logged real calls

    lines = [
        "🧠 Brain status",
        f"  vault       : {report.brain_dir}",
        f"  launchd     : {launchd_line}",
        f"  last run    : {last_line}",
        f"  next run    : {next_line}",
        f"  in flight   : {flight_line}",
        f"  procs       : {procs_line}",
        f"  ledgers     : {LD['harvested']} harvested, "
        f"{LD['dedupe_verdicts']} dedupe verdicts cached",
        f"  coverage    : {coverage_line}",
    ]
    if live_line is not None:
        lines.append(f"  live recall : {live_line}")
    lines.extend([
        f"  audit       : {audit_line}",
        f"  vault stats : {V['entities_total']} entities across "
        f"{len(V['by_type'])} types, {V['raw_pending']} raw pending",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Brain operational status")
    p.add_argument("--json", action="store_true",
                   help="Emit the report as JSON instead of the text dashboard")
    args = p.parse_args(argv)
    report = gather()
    print(to_json(report) if args.json else format_text(report))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
