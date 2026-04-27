"""`brain status` — single-shot operational dashboard.

Surfaces the answers to "is the brain doing anything right now?" without
the user having to know the scheduler job label, log path, lock-dir
name, ledger format, etc.

Two consumers:

  - CLI (`brain status` → `cli._cmd_status`) prints the human-readable
    block.
  - MCP (`brain_status` tool) returns the same data as JSON so an agent
    can decide whether to nudge the user ("a dedupe pass is in flight,
    hold off on heavy edits") without parsing log lines.

Design notes:

- Read-only. Scheduler state comes from `brain.scheduler.get_status()`,
  which dispatches between `launchctl` (macOS) and `systemctl --user`
  (Linux) — one shell-out, always-on system call. An optional `ps -A`
  snapshot is the only other subprocess. We never call into Anthropic,
  never mutate ledgers.
- Tolerant of every component being missing — fresh installs without
  any scheduler loaded, vaults without logs, ledgers not yet created.
  Each field falls back to `None` / "unknown" rather than raising.
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
from brain import scheduler

# Public aliases preserved so older callers that imported these from
# brain.status keep working. New code should reference brain.scheduler
# directly. LAUNCHD_PLIST stays present for back-compat with any test
# monkeypatch that redirected it — the plist read now lives in
# scheduler._launchd_read_interval, but the symbol is still exported.
LAUNCHD_LABEL = scheduler.LAUNCHD_LABEL
LAUNCHD_PLIST = scheduler._LAUNCHD_PLIST
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
    "claude --print",
]


@dataclass
class StatusReport:
    brain_dir: str
    scheduler: dict
    in_flight: dict
    last_run: dict
    next_run: dict
    spawned_procs: list[dict]
    ledgers: dict
    pending_audit: dict
    vault: dict
    coverage: dict = field(default_factory=dict)
    live_coverage: dict = field(default_factory=dict)
    claims: dict = field(default_factory=dict)

    # Back-compat alias: older clients (older MCP consumers, earlier
    # test fixtures) read `launchd`. Field aliasing doesn't survive
    # `asdict`, so `to_json` injects the alias explicitly.
    @property
    def launchd(self) -> dict:
        return self.scheduler


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


def _next_run(scheduler_state: dict, last_run: dict) -> dict:
    """Estimate time-until-next-tick. Only meaningful when the scheduler
    is loaded and we know both the interval and a last-run timestamp."""
    interval = scheduler_state.get("interval_s")
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

    Each eval run appends a JSON line with `score` (miss rate, lower is
    better) and `avg_top` (mean top-k similarity, higher is better). We
    only need the latest two rows to show "current vs. previous" —
    everything further back lives in the ledger for offline analysis.

    Tolerant of the ledger not existing (fresh install before the first
    eval run) and of malformed lines (hand-editing, partial writes
    during a crash)."""
    out: dict = {
        "available": False,
        #  Continuous score (1 - avg_top). Lower is better.
        "latest_score": None,
        "prev_score": None,
        "delta_score": None,
        #  Binary spec metric (misses / total above threshold). Lower
        #  is better. Floor-saturates at 0 once every eval query clears
        #  threshold; that's why `score` is the primary trajectory signal.
        "latest_miss_rate": None,
        "latest_avg_top": None,
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
    out["last_ts"] = latest.get("ts")
    out["threshold"] = latest.get("threshold")
    if "avg_top" in latest:
        out["latest_avg_top"] = float(latest["avg_top"])
    #  Ledger row schema changed on 2026-04-21: rows written before that
    #  date have `score` = binary miss-rate; rows after that date have
    #  `score` = continuous (1 - avg_top) AND `miss_rate` = binary. Read
    #  both so the dashboard works on any history.
    out["latest_miss_rate"] = (
        float(latest["miss_rate"]) if "miss_rate" in latest
        else float(latest.get("score", 0.0))  # legacy: score WAS miss-rate
    )
    out["latest_score"] = (
        float(latest["score"]) if "miss_rate" in latest  # new schema
        else None  # legacy rows have no continuous score
    )
    if len(rows) >= 2:
        prev = rows[-2]
        if "miss_rate" in prev and out["latest_score"] is not None:
            out["prev_score"] = float(prev["score"])
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
        "misses": 0, "miss_rate": 0.0, "avg_top": 0.0,
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


def inbox_health() -> dict:
    """Doctor check for the runtime inbox subsystem (transport layer).

    Reports whether the runtime root exists and is writable, how many
    pending messages are queued across all sessions, and whether the
    UserPromptSubmit hook for inbox surface is wired in Claude Code.
    Independent of the vault — works even when BRAIN_DIR is missing.
    """
    import os
    from brain.runtime import paths as _rt_paths

    rt = _rt_paths.runtime_root()
    rt.mkdir(parents=True, exist_ok=True)
    writable = os.access(rt, os.W_OK)

    inbox_dir = rt / "inbox"
    pending_total = 0
    if inbox_dir.exists():
        for sid_dir in inbox_dir.iterdir():
            pending_dir = sid_dir / "pending"
            if pending_dir.is_dir():
                pending_total += sum(
                    1 for p in pending_dir.iterdir() if p.suffix == ".json"
                )

    settings = Path.home() / ".claude" / "settings.json"
    hook_wired = False
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
            for grp in (data.get("hooks") or {}).get("UserPromptSubmit") or []:
                for h in grp.get("hooks") or []:
                    if "inbox-surface-hook" in (h.get("command") or ""):
                        hook_wired = True
                        break
        except Exception:  # noqa: BLE001 — best-effort doctor read
            pass

    return {
        "section": "Inbox (runtime transport)",
        "runtime_dir": str(rt),
        "runtime_dir_writable": writable,
        "pending_total": pending_total,
        "user_prompt_submit_hook_wired": hook_wired,
    }


def claims_health() -> dict:
    """Doctor check for the claim store (knowledge layer).

    Reports whether claim flags are set, claim counts by status, age
    of newest claim (proxy for extraction pipeline health), and the
    effective extract idle threshold.
    """
    use = os.environ.get("BRAIN_USE_CLAIMS", "0") == "1"
    strict = os.environ.get("BRAIN_STRICT_CLAIMS", "0") == "1"
    try:
        idle = int(os.environ.get("BRAIN_EXTRACT_IDLE_LEVEL2_SEC", "20"))
    except (ValueError, TypeError):
        idle = 20

    total = current = superseded = 0
    newest_age: float | None = None
    try:
        from brain import db as _db
        with _db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN status='current' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='superseded' THEN 1 ELSE 0 END), "
                "MAX(observed_at) "
                "FROM fact_claims"
            ).fetchone()
            if row:
                total = row[0] or 0
                current = row[1] or 0
                superseded = row[2] or 0
                if row[3]:
                    newest_age = max(0.0, time.time() - float(row[3]))
    except Exception:  # noqa: BLE001 — best-effort doctor read
        pass

    return {
        "section": "Claims (knowledge layer)",
        "use_claims": use,
        "strict_mode": strict,
        "fact_claims_total": total,
        "fact_claims_current": current,
        "fact_claims_superseded": superseded,
        "newest_claim_age_sec": newest_age,
        "extract_idle_threshold_sec": idle,
    }


def gather() -> StatusReport:
    sched = scheduler.get_status()
    in_flight = _in_flight()
    last_run = _last_run()
    next_run = _next_run(sched, last_run)
    return StatusReport(
        brain_dir=str(config.BRAIN_DIR),
        scheduler=sched,
        in_flight=in_flight,
        last_run=last_run,
        next_run=next_run,
        spawned_procs=_spawned_procs(),
        ledgers=_ledgers(),
        pending_audit=_pending_audit(),
        vault=_vault_counts(),
        coverage=_coverage(),
        live_coverage=_live_coverage(),
        claims=claims_health(),
    )


def to_json(report: StatusReport) -> str:
    # `asdict` excludes the computed @property — inject the `launchd`
    # alias manually so MCP consumers that haven't migrated yet keep
    # parsing. Can drop once everything calls `scheduler` instead.
    payload = asdict(report)
    payload["launchd"] = payload["scheduler"]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def format_text(report: StatusReport) -> str:
    """Human-readable dashboard. ~12 lines, fits in one terminal screen."""
    S = report.scheduler
    F = report.in_flight
    R = report.last_run
    N = report.next_run
    A = report.pending_audit
    V = report.vault
    LD = report.ledgers

    backend_name = S.get("backend") or "none"
    if S["loaded"]:
        every = f"every {S['interval_s']}s" if S["interval_s"] else "schedule unknown"
        scheduler_line = f"loaded ({backend_name}: {S['label']}, {every})"
        if S.get("last_exit") not in (None, 0):
            scheduler_line += f" — last exit {S['last_exit']}"
    else:
        scheduler_line = f"NOT LOADED ({backend_name}: {S['label']})"

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
                 else f"unknown ({backend_name} not loaded?)")

    procs = report.spawned_procs
    procs_line = (f"{len(procs)} brain/LLM procs running"
                  + (": " + ", ".join(p["cmd"].split()[0:2][-1] for p in procs)
                     if procs else ""))

    audit_line = (f"{A['count']} item(s) — {A['by_kind']}"
                  if A["count"] else "0 — clean")

    C = report.coverage
    if C.get("available"):
        avg_top = (f"avg-top {C['latest_avg_top']:.3f}"
                   if C.get("latest_avg_top") is not None else "avg-top n/a")
        miss_pct = f"{C['latest_miss_rate'] * 100:.1f}%"
        thr = (f"@ thr {C['threshold']:.2f}"
               if C.get("threshold") is not None else "")
        #  Prefer the continuous score for the headline + delta — it
        #  moves on every cycle even when no query crosses the binary
        #  threshold, so the trajectory is actually visible.
        if C.get("latest_score") is not None:
            score_str = f"score {C['latest_score']:.3f}"
            if C.get("delta_score") is not None:
                d = C["delta_score"]
                arrow = "↓" if d < 0 else ("↑" if d > 0 else "·")
                delta = f"Δ{arrow}{abs(d):.3f}"
            else:
                delta = "Δ—"
            coverage_line = (
                f"{score_str} ({delta}) · miss {miss_pct} · {avg_top} {thr}  "
                f"[{C['runs_logged']} eval runs logged]"
            )
        else:
            #  Legacy ledger only — no continuous score to display.
            coverage_line = (
                f"miss {miss_pct} · {avg_top} {thr}  "
                f"[{C['runs_logged']} eval runs logged, legacy schema]"
            )
    else:
        coverage_line = "no eval runs logged yet"

    LC = report.live_coverage or {}
    if LC.get("available"):
        live_miss_pct = f"{LC['miss_rate'] * 100:.1f}%"
        live_avg = f"avg-top {LC['avg_top']:.3f}"
        live_line = (f"miss {live_miss_pct} · {live_avg}  "
                     f"[{LC['total_calls']} calls, "
                     f"{LC.get('queries', 0)} uniq, last {LC['days']}d]")
    else:
        live_line = None  # hidden until the MCP has logged real calls

    CL = report.claims or {}
    if CL:
        flags = (f"use={'on' if CL.get('use_claims') else 'off'} · "
                 f"strict={'on' if CL.get('strict_mode') else 'off'}")
        counts = (f"{CL.get('fact_claims_total', 0)} total "
                  f"({CL.get('fact_claims_current', 0)} current, "
                  f"{CL.get('fact_claims_superseded', 0)} superseded)")
        age = CL.get("newest_claim_age_sec")
        if age is None:
            age_str = "no claims yet"
        else:
            age_str = f"newest {_delta_str(age)} ago"
            if age > 600:
                age_str += " ⚠ extraction stalled"
        claims_line = f"{flags} · {counts} · {age_str}"
    else:
        claims_line = None

    lines = [
        "🧠 Brain status",
        f"  vault       : {report.brain_dir}",
        f"  scheduler   : {scheduler_line}",
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
    if claims_line is not None:
        lines.append(f"  Claims      : {claims_line}")
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
