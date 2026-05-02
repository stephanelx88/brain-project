"""Extraction progress tool — counts, throughput, formatter."""
from __future__ import annotations

import time

import pytest

from brain import db
from brain.claims import progress


@pytest.fixture
def progress_brain(tmp_path, monkeypatch):
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    (brain_dir / "raw").mkdir()
    (brain_dir / "logs").mkdir()
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(config, "RAW_DIR", brain_dir / "raw")
    monkeypatch.setattr(db, "DB_PATH", brain_dir / ".brain.db")
    return brain_dir


def _seed_entity_and_claim(text: str, status: str = "current",
                            observed_offset: float = 0.0):
    """Insert an entity + claim with controlled observed_at offset from now."""
    with db.connect() as conn:
        # ensure 'son' entity exists
        existing = conn.execute("SELECT id FROM entities WHERE slug='son'").fetchone()
        if existing:
            son_id = existing[0]
        else:
            conn.execute(
                "INSERT INTO entities (path, type, slug, name, summary) "
                "VALUES ('entities/people/son.md','people','son','Son','owner')"
            )
            son_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        cid = db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text=text, source="note:foo.md", fact_date=None, status=status,
        )
        if observed_offset != 0.0 and cid is not None:
            conn.execute(
                "UPDATE fact_claims SET observed_at=? WHERE id=?",
                (time.time() + observed_offset, cid),
            )


def _seed_note(progress_brain, path: str, sha: str, extracted_sha: str | None,
               mtime_offset: float = 0.0):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO notes (path, title, body, mtime, sha, last_indexed, extracted_sha) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (path, path, "body", time.time() + mtime_offset, sha,
             time.time() + mtime_offset, extracted_sha),
        )


def test_progress_zero_notes(progress_brain):
    p = progress.extraction_progress()
    assert p["section"] == "Extraction progress"
    assert p["notes_total"] == 0
    assert p["notes_extracted"] == 0
    assert p["notes_pending"] == 0
    assert p["notes_progress_percent"] == 100.0  # 0/0 = no work pending = 100%


def test_progress_counts_notes(progress_brain):
    _seed_note(progress_brain, "a.md", sha="aaa", extracted_sha="aaa")
    _seed_note(progress_brain, "b.md", sha="bbb", extracted_sha="bbb")
    _seed_note(progress_brain, "c.md", sha="ccc", extracted_sha=None)
    _seed_note(progress_brain, "d.md", sha="ddd2", extracted_sha="ddd1")
    p = progress.extraction_progress()
    assert p["notes_total"] == 4
    assert p["notes_extracted"] == 2
    assert p["notes_pending"] == 2
    assert p["notes_progress_percent"] == 50.0


def test_progress_excludes_extractor_skipped_paths(progress_brain):
    """Files the extractor refuses to touch must not be counted as pending.

    Regression: `_notes_progress` used to query `notes` directly without
    applying the same EXCLUDED_DIR_PREFIXES / EXCLUDED_PATHS filter that
    `pending_note_extractions` uses → the progress bar got stuck below
    100% with `log.md`, `identity/*.md`, etc. permanently in the
    "pending" bucket (incident 2026-04-25).
    """
    # 1 real pending user note
    _seed_note(progress_brain, "real-pending.md", sha="r1", extracted_sha=None)
    # Files the extractor will never process — must not inflate the count
    _seed_note(progress_brain, "log.md", sha="l1", extracted_sha=None)
    _seed_note(progress_brain, "index.md", sha="i1", extracted_sha=None)
    _seed_note(progress_brain, "cursor-user-rules.md", sha="c1", extracted_sha=None)
    _seed_note(progress_brain, "identity/who-i-am.md", sha="w1", extracted_sha=None)
    _seed_note(progress_brain, "identity/preferences.md", sha="p1", extracted_sha=None)
    _seed_note(progress_brain, "playground/scratch.md", sha="s1", extracted_sha=None)
    _seed_note(progress_brain, "timeline/2026-04-25.md", sha="t1", extracted_sha=None)

    p = progress.extraction_progress()
    assert p["notes_total"] == 1
    assert p["notes_pending"] == 1
    assert p["notes_extracted"] == 0
    assert p["backlog"]["last_pending_note"] == "real-pending.md"


def test_progress_throughput_window(progress_brain):
    _seed_entity_and_claim("recent claim", observed_offset=-100.0)  # 100s ago
    _seed_entity_and_claim("older claim", observed_offset=-7200.0)  # 2h ago
    p = progress.extraction_progress()
    # Only the recent one counts in last-hour window
    assert p["throughput_last_hour"]["claims_created"] == 1


def test_progress_health_yellow_on_pending(progress_brain):
    for i in range(15):
        _seed_note(progress_brain, f"n{i}.md", sha=f"x{i}", extracted_sha=None)
    p = progress.extraction_progress()
    assert p["health"] == "YELLOW"


def test_progress_health_red_on_high_backlog(progress_brain):
    for i in range(60):
        _seed_note(progress_brain, f"n{i}.md", sha=f"x{i}", extracted_sha=None)
    p = progress.extraction_progress()
    assert p["health"] == "RED"


def test_progress_parses_utc_log_timestamp_as_utc(progress_brain):
    """The auto-extract log's run header is a UTC ISO string (`Z`
    suffix). Pre-fix code used `time.mktime(...) - time.timezone`
    which is correct outside DST but produces an epoch one hour off
    inside DST in zones that observe it. Fix uses calendar.timegm
    which always treats the struct as UTC.

    This test simulates a DST-observing host (America/Los_Angeles) by
    setting TZ + tzset, then writes a UTC header for a known recent
    time and asserts the parsed age tracks UTC reality — not shifted
    by the local offset. On the buggy code under TZ=America/Los_Angeles
    in summer, age_sec drifts by ~3600 seconds.
    """
    import os as _os
    import time as _time

    if not hasattr(_time, "tzset"):
        pytest.skip("time.tzset unavailable on this platform")

    # 90 seconds ago, in UTC. Pick a recent time so it falls inside
    # the one-hour throughput window the function counts.
    ref_epoch = _time.time() - 90.0
    ref_struct = _time.gmtime(ref_epoch)
    ref_ts_str = _time.strftime("%Y-%m-%dT%H:%M:%SZ", ref_struct)
    log_line = f"=== {ref_ts_str} auto-extract run (active=0)\n"
    (progress_brain / "logs" / "auto-extract.log").write_text(log_line)

    saved_tz = _os.environ.get("TZ")
    try:
        _os.environ["TZ"] = "America/Los_Angeles"
        _time.tzset()
        p = progress.extraction_progress()
    finally:
        if saved_tz is None:
            _os.environ.pop("TZ", None)
        else:
            _os.environ["TZ"] = saved_tz
        _time.tzset()

    assert p["last_extract"]["ts"] == ref_ts_str
    # age_sec should be close to 90s — the reference age. Allow 5s
    # window for test execution time. The buggy code yields ~3690s
    # on a DST-observing host in summer (or ~25290s outside DST in a
    # non-Pacific zone, since mktime uses the simulated PST offset).
    assert abs(p["last_extract"]["age_sec"] - 90.0) < 5.0, (
        f"age_sec={p['last_extract']['age_sec']} "
        f"should be ~90s; large drift means UTC parsing collapsed back "
        f"into local-time math"
    )
    assert p["throughput_last_hour"]["extract_runs"] == 1


def test_progress_format_text_renders_bar(progress_brain):
    _seed_note(progress_brain, "a.md", sha="aaa", extracted_sha="aaa")
    _seed_note(progress_brain, "b.md", sha="bbb", extracted_sha=None)
    p = progress.extraction_progress()
    out = progress.format_text(p)
    assert "Extracting knowledge" in out
    assert "50%" in out
    assert "1/2 notes" in out
    assert "throughput" in out.lower()
    assert "backlog" in out.lower()
    assert "health" in out.lower()


def test_progress_format_text_shows_pending_note(progress_brain):
    _seed_note(progress_brain, "journal/2026-04-25.md", sha="abc", extracted_sha=None)
    p = progress.extraction_progress()
    out = progress.format_text(p)
    assert "journal/2026-04-25.md" in out


def test_brain_progress_mcp_default_returns_text_with_bar(progress_brain):
    from brain import mcp_server
    _seed_note(progress_brain, "a.md", sha="aaa", extracted_sha=None)
    out = mcp_server.brain_progress()
    assert "Extracting knowledge" in out
    assert "[" in out and "]" in out  # progress bar brackets
    assert "throughput" in out.lower()


def test_brain_progress_mcp_json_format(progress_brain):
    import json as _json
    from brain import mcp_server
    _seed_note(progress_brain, "a.md", sha="aaa", extracted_sha=None)
    out = mcp_server.brain_progress(format="json")
    parsed = _json.loads(out)
    assert parsed["section"] == "Extraction progress"
    assert "notes_progress_percent" in parsed
