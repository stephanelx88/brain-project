"""Extraction pipeline progress — backlog + throughput + health.

Reports the user-facing question "is brain extracting from notes/raw
into claims, and how far along is it?". Pulls from:
  - notes table         (notes pending extraction = sha != extracted_sha)
  - fact_claims table   (claims created/superseded last hour)
  - raw/ directory      (raw sessions harvested last hour, by mtime)
  - .extract.lock.d/    (currently-extracting indicator)
  - logs/auto-extract.log  (last extract run timestamp)

NO imports from brain.entities, brain.semantic, brain.graph,
brain.consolidation. Read-only.
"""
from __future__ import annotations

import calendar
import os
import re
import time
from pathlib import Path
from typing import Optional

import brain.config as config
from brain import db


_RUN_HEADER_RE = re.compile(
    r"=== (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) auto-extract run "
)


def extraction_progress() -> dict:
    """Snapshot of extraction pipeline state. ~5-15ms to compute."""
    now = time.time()
    one_hour_ago = now - 3600.0

    notes_total, notes_extracted, notes_pending, last_pending_path = _notes_progress()
    claims_created, claims_superseded = _claims_throughput(one_hour_ago)
    raw_harvested = _raw_harvested_last_hour(one_hour_ago)
    notes_ingested = _notes_ingested_last_hour(one_hour_ago)
    last_extract_ts, extract_runs_last_hour = _extract_runs(one_hour_ago)
    last_extract_age = (now - last_extract_ts) if last_extract_ts else None

    in_flight_pid, in_flight_started_age = _in_flight()

    percent = (notes_extracted / notes_total * 100.0) if notes_total else 100.0
    health = _health(
        notes_pending=notes_pending,
        last_extract_age=last_extract_age,
        in_flight=in_flight_pid is not None,
    )

    return {
        "section": "Extraction progress",
        "notes_progress_percent": round(percent, 1),
        "notes_total": notes_total,
        "notes_extracted": notes_extracted,
        "notes_pending": notes_pending,
        "throughput_last_hour": {
            "raw_sessions_harvested": raw_harvested,
            "notes_ingested": notes_ingested,
            "claims_created": claims_created,
            "claims_superseded": claims_superseded,
            "extract_runs": extract_runs_last_hour,
        },
        "last_extract": {
            "ts": _iso(last_extract_ts) if last_extract_ts else None,
            "age_sec": last_extract_age,
        },
        "backlog": {
            "notes_pending_extraction": notes_pending,
            "last_pending_note": last_pending_path,
            "currently_extracting": in_flight_pid,
            "currently_extracting_age_sec": in_flight_started_age,
        },
        "health": health,
    }


def _notes_progress() -> tuple[int, int, int, Optional[str]]:
    """Return (total, extracted, pending, newest_pending_path).

    Mirrors the extractor's own exclusion list — if a file is in
    `EXCLUDED_DIR_PREFIXES` / `EXCLUDED_PATHS` it will never be
    processed, so it must not show up as "pending" in the progress
    bar (otherwise the bar can never reach 100%).
    """
    try:
        return db.note_extraction_counts(
            exclude_prefixes=config.NOTE_EXTRACT_EXCLUDED_DIR_PREFIXES,
            exclude_paths=config.NOTE_EXTRACT_EXCLUDED_PATHS,
        )
    except Exception:  # noqa: BLE001
        return 0, 0, 0, None


def _claims_throughput(since_epoch: float) -> tuple[int, int]:
    """Return (claims_created, claims_superseded) in window."""
    try:
        with db.connect() as conn:
            created = conn.execute(
                "SELECT COUNT(*) FROM fact_claims WHERE observed_at >= ?",
                (since_epoch,),
            ).fetchone()[0] or 0
            # Superseded: rows whose status='superseded' and superseded_at
            # is in the window. superseded_at is TEXT (ISO); compare via
            # observed_at as an approximation since it's REAL epoch.
            superseded = conn.execute(
                "SELECT COUNT(*) FROM fact_claims "
                "WHERE status='superseded' AND observed_at >= ?",
                (since_epoch,),
            ).fetchone()[0] or 0
            return created, superseded
    except Exception:  # noqa: BLE001
        return 0, 0


def _raw_harvested_last_hour(since_epoch: float) -> int:
    raw_dir = config.RAW_DIR
    if not raw_dir.is_dir():
        return 0
    n = 0
    try:
        for p in raw_dir.iterdir():
            if not p.is_file():
                continue
            try:
                if p.stat().st_mtime >= since_epoch:
                    n += 1
            except OSError:
                continue
    except OSError:
        return 0
    return n


def _notes_ingested_last_hour(since_epoch: float) -> int:
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE last_indexed >= ?",
                (since_epoch,),
            ).fetchone()
            return (row[0] or 0) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _extract_runs(since_epoch: float) -> tuple[Optional[float], int]:
    """Parse logs/auto-extract.log for runs in window. Returns (last_ts, count)."""
    log = config.BRAIN_DIR / "logs" / "auto-extract.log"
    if not log.exists():
        return None, 0
    try:
        # Tail last 64 KB — enough for ~1000 run headers
        size = log.stat().st_size
        with log.open("rb") as f:
            if size > 64_000:
                f.seek(-64_000, os.SEEK_END)
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None, 0
    last_ts: Optional[float] = None
    count = 0
    for m in _RUN_HEADER_RE.finditer(text):
        try:
            ts_struct = time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ")
            # The header timestamps are UTC (`Z` suffix). calendar.timegm
            # converts a UTC struct_time directly to epoch. The previous
            # `mktime(struct) - time.timezone` was correct only outside
            # DST — during DST in zones that observe it (continental US,
            # most of Europe), `time.timezone` is the standard-time
            # offset and the result is one hour off, making the
            # last-extract-age in `brain status` wrong every summer.
            epoch = calendar.timegm(ts_struct)
        except (ValueError, OverflowError):
            continue
        if epoch >= since_epoch:
            count += 1
        if last_ts is None or epoch > last_ts:
            last_ts = epoch
    return last_ts, count


def _in_flight() -> tuple[Optional[int], Optional[float]]:
    """Detect a running extract via lock dir. Returns (pid, started_age_sec)."""
    lock = config.BRAIN_DIR / ".extract.lock.d"
    if not lock.is_dir():
        return None, None
    pid_file = lock / "pid"
    if not pid_file.exists():
        return None, None
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None, None
    if not _pid_alive(pid):
        return None, None
    try:
        started_age = time.time() - lock.stat().st_mtime
    except OSError:
        started_age = None
    return pid, started_age


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _health(
    *,
    notes_pending: int,
    last_extract_age: Optional[float],
    in_flight: bool,
) -> str:
    """GREEN | YELLOW | RED."""
    if in_flight and notes_pending <= 50:
        return "GREEN"
    if last_extract_age is not None and last_extract_age > 1800:
        return "RED"
    if notes_pending > 50:
        return "RED"
    if notes_pending > 10:
        return "YELLOW"
    if last_extract_age is not None and last_extract_age > 600:
        return "YELLOW"
    return "GREEN"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# ---------------------------------------------------------------------------
# Pretty printer for CLI / MCP
# ---------------------------------------------------------------------------

_BAR_WIDTH = 24


def format_text(p: dict) -> str:
    """Render the progress dict as a human-readable text block."""
    pct = p.get("notes_progress_percent", 0.0)
    total = p.get("notes_total", 0)
    extracted = p.get("notes_extracted", 0)
    pending = p.get("notes_pending", 0)
    backlog = p.get("backlog", {}) or {}
    last = p.get("last_extract", {}) or {}
    tput = p.get("throughput_last_hour", {}) or {}

    filled = int(round(pct / 100.0 * _BAR_WIDTH))
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)

    lines: list[str] = []
    lines.append(f"Extracting knowledge: [{bar}] {pct:.0f}% ({extracted}/{total} notes)")
    if backlog.get("currently_extracting"):
        age = backlog.get("currently_extracting_age_sec")
        suffix = f", running {int(age)}s" if age is not None else ""
        lines.append(f"Currently extracting (pid {backlog['currently_extracting']}{suffix})")
    elif backlog.get("last_pending_note"):
        lines.append(f"Newest pending: {backlog['last_pending_note']}")
    if last.get("age_sec") is not None:
        lines.append(f"Last extract run: {_human_age(last['age_sec'])} ago")
    else:
        lines.append("Last extract run: never (or log unreadable)")

    lines.append("")
    lines.append("throughput (last hour):")
    lines.append(f"  raw sessions harvested: {tput.get('raw_sessions_harvested', 0)}")
    lines.append(f"  notes ingested:         {tput.get('notes_ingested', 0)}")
    lines.append(f"  claims created:         {tput.get('claims_created', 0)}")
    lines.append(f"  claims superseded:      {tput.get('claims_superseded', 0)}")
    lines.append(f"  extract runs:           {tput.get('extract_runs', 0)}")
    lines.append("")
    lines.append("backlog:")
    lines.append(f"  notes pending extraction: {pending}")
    if backlog.get("last_pending_note"):
        lines.append(f"  last pending note:        {backlog['last_pending_note']}")

    health = p.get("health", "UNKNOWN")
    glyph = {"GREEN": "✓", "YELLOW": "⚠", "RED": "✗"}.get(health, "?")
    lines.append("")
    lines.append(f"health: {health} {glyph}")
    return "\n".join(lines)


def _human_age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"
