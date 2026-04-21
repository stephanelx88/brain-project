"""Tests for the failure ledger substrate (`brain.failures`).

Covers:
  - record -> list -> resolve round-trip
  - filter by source / tag / unresolved_only
  - concurrent appends from two threads don't corrupt the JSONL
  - BRAIN_DIR env-var override routes ledger to the right path
  - CLI `brain failure record/list/resolve` end-to-end via subprocess
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


@pytest.fixture
def tmp_ledger(tmp_path, monkeypatch):
    """Point BRAIN_DIR at a fresh tmpdir and reload brain.config.

    `brain.failures._ledger_path` reads `config.BRAIN_DIR` on every call,
    so a monkeypatch on the config module is enough — no need to reload.
    """
    brain_dir = tmp_path / ".brain"
    brain_dir.mkdir()
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    return brain_dir


def test_record_returns_id_and_writes_jsonl(tmp_ledger):
    from brain import failures

    fid = failures.record_failure(
        source="recall",
        tool="brain_recall",
        query="đôi dép tôi đâu",
        result_digest="RRF=0.026 hit=Thuha.md",
        user_correction="Note doesn't mention dép; brain has no record.",
        tags=["hallucination", "subject-mismatch"],
        session_id="sess-abc",
    )
    assert isinstance(fid, str) and len(fid) == 12

    ledger = tmp_ledger / "failures.jsonl"
    assert ledger.exists()
    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["id"] == fid
    assert row["source"] == "recall"
    assert row["tool"] == "brain_recall"
    assert row["tags"] == ["hallucination", "subject-mismatch"]
    assert row["resolution"] is None
    # Non-ASCII should round-trip legibly.
    assert row["query"] == "đôi dép tôi đâu"


def test_list_round_trip_and_filters(tmp_ledger):
    from brain import failures

    a = failures.record_failure(source="recall", tags=["hallucination"])
    b = failures.record_failure(source="extraction", tags=["dlq"])
    c = failures.record_failure(source="recall", tags=["subject-mismatch"])

    all_rows = failures.list_failures()
    assert {r["id"] for r in all_rows} == {a, b, c}
    # Newest-first: c was recorded last.
    assert all_rows[0]["id"] == c

    recalls = failures.list_failures(source="recall")
    assert {r["id"] for r in recalls} == {a, c}

    dlq = failures.list_failures(tag="dlq")
    assert [r["id"] for r in dlq] == [b]

    # Nothing resolved yet.
    assert len(failures.list_failures(unresolved_only=True)) == 3


def test_resolve_updates_row_and_unresolved_filter(tmp_ledger):
    from brain import failures

    fid = failures.record_failure(source="manual", tags=["test"])
    ok = failures.resolve_failure(
        fid,
        patch_ref="abc123def",
        outcome="fixed",
    )
    assert ok is True

    rows = failures.list_failures()
    assert len(rows) == 1
    assert rows[0]["resolution"]["patch_ref"] == "abc123def"
    assert rows[0]["resolution"]["outcome"] == "fixed"
    assert rows[0]["resolution"]["verified_at"]  # auto-populated

    # unresolved_only excludes it now.
    assert failures.list_failures(unresolved_only=True) == []


def test_resolve_unknown_id_returns_false(tmp_ledger):
    from brain import failures

    failures.record_failure(source="manual")
    ok = failures.resolve_failure("deadbeef0000", patch_ref="x", outcome="fixed")
    assert ok is False


def test_list_empty_when_no_ledger(tmp_ledger):
    from brain import failures

    assert failures.list_failures() == []
    assert not (tmp_ledger / "failures.jsonl").exists()


def test_concurrent_appends_no_corruption(tmp_ledger):
    """Two writer threads pounding the ledger shouldn't interleave lines.

    O_APPEND on POSIX is atomic for single `write()` calls up to PIPE_BUF
    (4KiB+ on regular files, well above our JSONL row size). If that
    contract holds, every line parses as valid JSON and we see exactly
    `2 * N` rows at the end.
    """
    from brain import failures

    N = 50
    errors: list[str] = []

    def writer(label: str) -> None:
        try:
            for i in range(N):
                failures.record_failure(
                    source="recall",
                    query=f"{label}-{i}",
                    tags=[label],
                )
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    t1 = threading.Thread(target=writer, args=("A",))
    t2 = threading.Thread(target=writer, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    lines = (tmp_ledger / "failures.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2 * N
    # Every line parses — no interleaving.
    parsed = [json.loads(line) for line in lines]
    assert len({r["id"] for r in parsed}) == 2 * N  # every id unique
    assert sum(1 for r in parsed if "A" in (r.get("tags") or [])) == N
    assert sum(1 for r in parsed if "B" in (r.get("tags") or [])) == N


def test_brain_dir_env_override_routes_ledger(tmp_path, monkeypatch):
    """The ledger path must follow BRAIN_DIR — not be frozen at import.

    We simulate a late `config.BRAIN_DIR` rebinding (what `brain init`
    effectively does) and confirm writes go to the new location.
    """
    alt = tmp_path / "alt-brain"
    alt.mkdir()
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", alt)

    from brain import failures
    failures.record_failure(source="manual", query="override check")

    assert (alt / "failures.jsonl").exists()
    rows = failures.list_failures()
    assert len(rows) == 1
    assert rows[0]["query"] == "override check"


# ---------- CLI end-to-end (subprocess, honours BRAIN_DIR env var) ----------

def _run_cli(args: list[str], brain_dir: Path) -> subprocess.CompletedProcess:
    """Run `python -m brain.cli <args>` with BRAIN_DIR pointing at `brain_dir`.

    We use the module form so we don't depend on a `brain` console-script
    being installed in the test venv.
    """
    env = os.environ.copy()
    env["BRAIN_DIR"] = str(brain_dir)
    # Make sure tests inherit the src/ pythonpath regardless of editable install.
    src = Path(__file__).resolve().parent.parent / "src"
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "brain.cli", *args],
        env=env,
        capture_output=True,
        text=True,
    )


def test_cli_record_list_resolve_end_to_end(tmp_path):
    brain_dir = tmp_path / ".brain"
    brain_dir.mkdir()

    rec = _run_cli(
        [
            "failure", "record",
            "--source", "recall",
            "--tool", "brain_recall",
            "--query", "where are my slippers",
            "--correction", "brain has no record",
            "--tag", "hallucination",
            "--tag", "subject-mismatch",
        ],
        brain_dir,
    )
    assert rec.returncode == 0, rec.stderr
    fid = rec.stdout.strip()
    assert len(fid) == 12

    lst = _run_cli(["failure", "list", "--json"], brain_dir)
    assert lst.returncode == 0, lst.stderr
    rows = json.loads(lst.stdout)
    assert len(rows) == 1
    assert rows[0]["id"] == fid
    assert rows[0]["tags"] == ["hallucination", "subject-mismatch"]

    # Filter by tag works via the CLI.
    lst2 = _run_cli(["failure", "list", "--tag", "hallucination", "--json"], brain_dir)
    assert json.loads(lst2.stdout)[0]["id"] == fid
    lst3 = _run_cli(["failure", "list", "--tag", "no-such-tag", "--json"], brain_dir)
    assert json.loads(lst3.stdout) == []

    res = _run_cli(
        ["failure", "resolve", fid, "--patch", "commit:abc123", "--outcome", "fixed"],
        brain_dir,
    )
    assert res.returncode == 0, res.stderr
    assert "resolved" in res.stdout

    lst_unres = _run_cli(["failure", "list", "--unresolved", "--json"], brain_dir)
    assert json.loads(lst_unres.stdout) == []


def test_cli_resolve_unknown_id_exits_nonzero(tmp_path):
    brain_dir = tmp_path / ".brain"
    brain_dir.mkdir()

    res = _run_cli(
        ["failure", "resolve", "ffffffffffff", "--patch", "x", "--outcome", "fixed"],
        brain_dir,
    )
    assert res.returncode == 1
    assert "No failure" in res.stderr


# ---------------------------------------------------------------------------
# list_miss_patterns — close-the-loop read side
# ---------------------------------------------------------------------------

def test_list_miss_patterns_groups_by_normalised_query(tmp_ledger):
    from brain import failures
    #  Same underlying query with different casing/whitespace/typo-ish
    #  variants — they should collapse into one bucket.
    for q in ["Brain refactoring", "brain  refactoring", "BRAIN REFACTORING"]:
        failures.record_failure(
            source="recall_miss", tool="brain_recall", query=q,
            extra={"top_score": 0.42, "threshold": 0.45},
        )
    out = failures.list_miss_patterns(min_count=2)
    assert len(out) == 1
    assert out[0]["query"] == "brain refactoring"
    assert out[0]["miss_count"] == 3
    assert sorted(out[0]["recent_queries"]) == sorted(
        ["Brain refactoring", "brain  refactoring", "BRAIN REFACTORING"]
    )


def test_list_miss_patterns_respects_min_count(tmp_ledger):
    from brain import failures
    failures.record_failure(source="recall_miss", query="asked twice")
    failures.record_failure(source="recall_miss", query="asked twice")
    failures.record_failure(source="recall_miss", query="asked once only")
    out = failures.list_miss_patterns(min_count=2)
    assert [b["query"] for b in out] == ["asked twice"]


def test_list_miss_patterns_ignores_non_recall_sources(tmp_ledger):
    from brain import failures
    failures.record_failure(source="extraction", query="should not appear", tags=["dlq"])
    failures.record_failure(source="extraction", query="should not appear", tags=["dlq"])
    failures.record_failure(source="extraction", query="should not appear", tags=["dlq"])
    assert failures.list_miss_patterns(min_count=2) == []


def test_list_miss_patterns_sorts_by_count_desc_then_recency(tmp_ledger):
    from brain import failures
    #  "low-count" has 2 events (below min_count=2 threshold below would
    #  cut it; we keep min_count=2 so both surface). "high-count" has 3.
    failures.record_failure(source="recall_miss", query="high-count")
    failures.record_failure(source="recall_miss", query="low-count")
    failures.record_failure(source="recall_miss", query="high-count")
    failures.record_failure(source="recall_miss", query="low-count")
    failures.record_failure(source="recall_miss", query="high-count")
    out = failures.list_miss_patterns(min_count=2)
    assert [b["query"] for b in out] == ["high-count", "low-count"]
    assert out[0]["miss_count"] == 3
    assert out[1]["miss_count"] == 2


def test_list_miss_patterns_captures_best_score(tmp_ledger):
    from brain import failures
    failures.record_failure(source="recall_miss", query="X",
                            extra={"top_score": 0.42})
    failures.record_failure(source="recall_miss", query="X",
                            extra={"top_score": 0.51})
    failures.record_failure(source="recall_miss", query="X",
                            extra={"top_score": 0.38})
    out = failures.list_miss_patterns(min_count=2)
    assert out[0]["best_score"] == 0.51
