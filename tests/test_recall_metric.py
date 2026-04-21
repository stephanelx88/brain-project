"""Tests for `brain.recall_metric` — the Question Coverage Score harness.

These exercise the pure-Python parts: ledger logging, live_coverage
parsing, and diff_reports. Scoring itself requires the semantic
stack and is covered end-to-end in higher-level smoke tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from brain import recall_metric


@pytest.fixture
def fake_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "recall-ledger.jsonl"
    monkeypatch.setattr(recall_metric, "LEDGER", ledger)
    return ledger


def _stub_hybrid_top(
    monkeypatch,
    rrf: float,
    cosine: float | None = None,
    label: str = "fact:insights/x",
):
    """Bypass the real embedding stack for live-mode logging."""
    if cosine is None:
        cosine = max(0.0, rrf * 6.0)  # rough proxy; tests don't depend on it
    monkeypatch.setattr(
        recall_metric, "_hybrid_top_score",
        lambda q, k=3: (rrf, cosine, label),
    )


def _stub_top_score(monkeypatch, score: float, label: str = "fact:insights/x"):
    """Bypass the cosine-only scorer (eval-set path + back-compat tests)."""
    monkeypatch.setattr(recall_metric, "_top_score_for",
                        lambda q, k=3: (score, label))


def test_log_live_recall_writes_one_row(fake_ledger, monkeypatch):
    _stub_hybrid_top(monkeypatch, rrf=0.08, cosine=0.82)
    recall_metric.log_live_recall("what is the brain vault")
    lines = fake_ledger.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["kind"] == "live"
    assert row["query"] == "what is the brain vault"
    assert row["top_score"] == 0.82  # cosine, kept for back-compat
    assert row["top_rrf"] == 0.08    # new: drives miss flag
    assert row["miss"] is False      # 0.08 > default 0.05 RRF threshold
    assert row["threshold"] == recall_metric.MISS_THRESHOLD
    assert row["rrf_threshold"] == recall_metric.MISS_RRF_THRESHOLD


def test_log_live_recall_flags_miss_below_rrf_threshold(fake_ledger, monkeypatch):
    """Miss is decided on RRF, not cosine — even a 0.50 cosine is a
    miss when the hybrid winner couldn't accumulate any rank-fusion
    weight (RRF below threshold).
    """
    _stub_hybrid_top(monkeypatch, rrf=0.02, cosine=0.50)
    recall_metric.log_live_recall("some obscure query")
    row = json.loads(fake_ledger.read_text().splitlines()[0])
    assert row["miss"] is True


def test_log_live_recall_hit_when_rrf_strong_despite_low_cosine(
    fake_ledger, monkeypatch
):
    """Regression for the measurement-bug-as-recall-bug confusion: a
    BM25-only hit (0 cosine) with strong RRF must register as a hit,
    because that's exactly what the user sees in `brain_recall`.
    """
    _stub_hybrid_top(monkeypatch, rrf=0.07, cosine=0.42)
    recall_metric.log_live_recall("logic cpu ram khong lam hang computer")
    row = json.loads(fake_ledger.read_text().splitlines()[0])
    assert row["miss"] is False
    assert row["top_score"] == 0.42  # logged but not used for miss
    assert row["top_rrf"] == 0.07


def test_log_live_recall_truncates_long_queries(fake_ledger, monkeypatch):
    _stub_hybrid_top(monkeypatch, rrf=0.07)
    recall_metric.log_live_recall("x" * 500)
    row = json.loads(fake_ledger.read_text().splitlines()[0])
    assert len(row["query"]) == 200


def test_log_live_recall_skips_empty_queries(fake_ledger, monkeypatch):
    _stub_hybrid_top(monkeypatch, rrf=0.09)
    recall_metric.log_live_recall("")
    recall_metric.log_live_recall("   ")
    recall_metric.log_live_recall("a")  # 1 char, skipped
    assert not fake_ledger.exists()


def test_log_live_recall_swallows_errors(fake_ledger, monkeypatch):
    """Any failure in the log path must not propagate — it's off the
    user-facing recall hot path."""
    def boom(*a, **kw):
        raise RuntimeError("scorer exploded")
    monkeypatch.setattr(recall_metric, "_hybrid_top_score", boom)
    # Must not raise
    recall_metric.log_live_recall("anything")
    assert not fake_ledger.exists()


def test_log_live_recall_mirrors_miss_into_failures_ledger(fake_ledger, tmp_path, monkeypatch):
    """Close-the-loop: a miss in recall-ledger.jsonl must also append
    one `source=recall_miss` row to failures.jsonl, so
    `brain.failures.list_miss_patterns` can aggregate repeated-miss
    topics. Hits must NOT mirror — failures ledger is miss-only."""
    import brain.config as config
    brain_dir = tmp_path / ".brain"
    brain_dir.mkdir()
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    _stub_hybrid_top(monkeypatch, rrf=0.02, cosine=0.30)  # RRF below threshold
    recall_metric.log_live_recall("what the brain doesn't know")
    failures_path = brain_dir / "failures.jsonl"
    assert failures_path.exists()
    rows = [json.loads(l) for l in failures_path.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["source"] == "recall_miss"
    assert rows[0]["tool"] == "brain_recall"
    assert rows[0]["query"] == "what the brain doesn't know"
    assert rows[0]["extra"]["top_rrf"] == 0.02
    assert rows[0]["extra"]["top_score"] == 0.3


def test_log_live_recall_hit_does_not_mirror(fake_ledger, tmp_path, monkeypatch):
    import brain.config as config
    brain_dir = tmp_path / ".brain"
    brain_dir.mkdir()
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    _stub_hybrid_top(monkeypatch, rrf=0.10, cosine=0.9)  # well above thresholds
    recall_metric.log_live_recall("fresh hit")
    failures_path = brain_dir / "failures.jsonl"
    assert not failures_path.exists()


def test_live_coverage_empty_ledger(fake_ledger):
    assert not fake_ledger.exists()
    data = recall_metric.live_coverage()
    assert data["available"] is False
    assert data["total_calls"] == 0


def test_live_coverage_counts_hits_and_misses(fake_ledger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_ledger.write_text(
        f'{{"ts":"{now}","kind":"live","query":"a","top_score":0.8,"miss":false}}\n'
        f'{{"ts":"{now}","kind":"live","query":"a","top_score":0.8,"miss":false}}\n'
        f'{{"ts":"{now}","kind":"live","query":"b","top_score":0.3,"miss":true}}\n'
    )
    data = recall_metric.live_coverage()
    assert data["available"] is True
    assert data["total_calls"] == 3
    assert data["hits"] == 2
    assert data["misses"] == 1
    assert data["queries"] == 2  # deduped
    #  `live_coverage` returns the binary miss-rate under `miss_rate`
    #  (renamed from the old `score` key on 2026-04-21 to disambiguate
    #  it from the eval-set continuous `score` metric).
    assert abs(data["miss_rate"] - 1 / 3) < 1e-6
    assert abs(data["avg_top"] - (0.8 + 0.8 + 0.3) / 3) < 1e-6
    # No top_rrf in these rows → avg_rrf=0, rrf_rows=0 (back-compat).
    assert data["avg_rrf"] == 0.0
    assert data["rrf_rows"] == 0


def test_live_coverage_reports_avg_rrf_when_rows_carry_it(fake_ledger):
    """New rows (post-2026-04-22) carry top_rrf alongside top_score.
    `live_coverage` averages it over the rows that have it, so the
    headline can quote a meaningful hybrid score even when older
    rows are still in the window."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_ledger.write_text(
        # Mixed: one old row (no top_rrf), two new rows.
        f'{{"ts":"{now}","kind":"live","query":"a","top_score":0.8,"miss":false}}\n'
        f'{{"ts":"{now}","kind":"live","query":"b","top_score":0.5,"top_rrf":0.07,"miss":false}}\n'
        f'{{"ts":"{now}","kind":"live","query":"c","top_score":0.4,"top_rrf":0.03,"miss":true}}\n'
    )
    data = recall_metric.live_coverage()
    assert data["rrf_rows"] == 2
    assert abs(data["avg_rrf"] - (0.07 + 0.03) / 2) < 1e-6
    # Cosine avg still includes all rows for back-compat.
    assert abs(data["avg_top"] - (0.8 + 0.5 + 0.4) / 3) < 1e-6


def test_live_coverage_ignores_eval_rows(fake_ledger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_ledger.write_text(
        f'{{"ts":"{now}","kind":"eval","score":0.1,"total":10,"misses":1}}\n'
        f'{{"ts":"{now}","kind":"live","query":"x","top_score":0.9,"miss":false}}\n'
    )
    data = recall_metric.live_coverage()
    assert data["total_calls"] == 1


def test_live_coverage_respects_window(fake_ledger):
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_ledger.write_text(
        f'{{"ts":"{old}","kind":"live","query":"old","top_score":0.9,"miss":false}}\n'
        f'{{"ts":"{now}","kind":"live","query":"new","top_score":0.8,"miss":false}}\n'
    )
    data = recall_metric.live_coverage(days=7)
    assert data["total_calls"] == 1
    assert data["queries"] == 1


def test_live_coverage_tolerates_malformed_lines(fake_ledger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_ledger.write_text(
        'not json\n'
        f'{{"ts":"{now}","kind":"live","query":"x","top_score":0.9,"miss":false}}\n'
        '\n'
        '{"ts": "bad-ts", "kind": "live", "top_score": 0.5}\n'
    )
    data = recall_metric.live_coverage()
    #  The bad-ts row is dropped (can't place it in the window), the
    #  "not json" row is dropped, and only one valid row remains.
    assert data["total_calls"] == 1


# ---------- diff_reports --------------------------------------------------


def _mk_report(scores: list[tuple[str, float, bool]]) -> recall_metric.CoverageReport:
    per_q = [{"query": q, "top_score": s, "top_hit": "-", "miss": m}
             for q, s, m in scores]
    total = len(per_q)
    misses = sum(1 for p in per_q if p["miss"])
    avg_top = sum(p["top_score"] for p in per_q) / total if total else 0.0
    return recall_metric.CoverageReport(
        timestamp="2026-04-20T00:00:00Z",
        threshold=0.6,
        total=total,
        misses=misses,
        score=max(0.0, min(1.0, 1.0 - avg_top)),  # continuous (1 - avg_top)
        miss_rate=(misses / total) if total else 0.0,  # binary spec metric
        avg_top_score=avg_top,
        per_query=per_q,
    )


def test_top_miss_queries_ranks_by_miss_count(fake_ledger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        ("common-miss", 0.4, True),
        ("common-miss", 0.42, True),
        ("common-miss", 0.45, True),
        ("occasional-miss", 0.5, True),
        ("always-hits", 0.9, False),
    ]
    fake_ledger.write_text("\n".join(
        json.dumps({"ts": now, "kind": "live", "query": q,
                    "top_score": s, "miss": m}) for q, s, m in rows
    ) + "\n")
    out = recall_metric.top_miss_queries()
    assert [r["query"] for r in out] == ["common-miss", "occasional-miss"]
    assert out[0]["misses"] == 3
    assert out[0]["best_score"] == 0.45


def test_top_miss_queries_empty_when_no_misses(fake_ledger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_ledger.write_text(
        json.dumps({"ts": now, "kind": "live", "query": "ok",
                    "top_score": 0.9, "miss": False}) + "\n"
    )
    assert recall_metric.top_miss_queries() == []


def test_top_miss_queries_caps_at_n(fake_ledger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_ledger.write_text("\n".join(
        json.dumps({"ts": now, "kind": "live", "query": f"q{i}",
                    "top_score": 0.3, "miss": True})
        for i in range(20)
    ) + "\n")
    assert len(recall_metric.top_miss_queries(n=5)) == 5


def test_diff_reports_flags_flipped_queries():
    before = _mk_report([("a", 0.5, True), ("b", 0.7, False)])
    after = _mk_report([("a", 0.75, False), ("b", 0.4, True)])
    d = recall_metric.diff_reports(before, after)
    assert d["flipped_to_hit"] == ["a"]
    assert d["flipped_to_miss"] == ["b"]
    #  Both queries moved 0.25 in their direction; gains sorted desc.
    assert d["biggest_score_gains"][0][0] == "a"


def test_diff_reports_marks_improvement_on_avg_top_only():
    """Even when no queries cross the threshold, a real avg-top gain
    should register as 'improved' — lets us see incremental progress.

    Regression for the "score = 0.0 saturated" bug: now that `score` is
    `1 - avg_top` instead of binary miss-rate, `score_delta` should also
    move (negative = improvement) when avg-top moves, even if no query
    flips. `miss_rate_delta` stays at 0 because the binary signal is
    floor-saturated."""
    before = _mk_report([("a", 0.3, True), ("b", 0.4, True)])
    after = _mk_report([("a", 0.4, True), ("b", 0.5, True)])
    d = recall_metric.diff_reports(before, after)
    assert d["improved"] is True
    assert d["score_delta"] < 0  # continuous score dropped
    assert d["miss_rate_delta"] == 0.0  # binary metric unchanged
    assert d["avg_top_delta"] > 0


def test_score_is_continuous_one_minus_avg_top():
    """Spec lock-in: `score` is `1 - avg_top`, lower-is-better, never
    saturates. This catches any future revert to binary `score`."""
    rep = _mk_report([("a", 0.5, True), ("b", 0.7, False), ("c", 0.9, False)])
    expected_avg_top = (0.5 + 0.7 + 0.9) / 3
    assert abs(rep.avg_top_score - expected_avg_top) < 1e-9
    assert abs(rep.score - (1.0 - expected_avg_top)) < 1e-9
    #  Binary spec metric is preserved separately.
    assert abs(rep.miss_rate - (1 / 3)) < 1e-9


def test_score_does_not_saturate_when_all_queries_pass():
    """Regression for the 2026-04-21 bug: every eval query above the
    threshold → binary `miss_rate` is 0, but `score` MUST still reflect
    the gap from perfect avg_top so cycle-to-cycle improvement is
    visible. Prior to the fix the headline showed `score=0.0` next to
    `avg_top=0.712`, hiding all subsequent progress."""
    saturated = _mk_report([("a", 0.71, False), ("b", 0.72, False), ("c", 0.70, False)])
    assert saturated.miss_rate == 0.0
    assert saturated.score > 0  # NOT floor-saturated
    assert abs(saturated.score - (1.0 - 0.71)) < 1e-9
    #  And it MUST move when avg_top moves, even with both states
    #  fully-passing on the binary metric.
    better = _mk_report([("a", 0.81, False), ("b", 0.82, False), ("c", 0.80, False)])
    d = recall_metric.diff_reports(saturated, better)
    assert d["miss_rate_delta"] == 0.0  # both at zero misses
    assert d["score_delta"] < -0.05  # but continuous score clearly improved
    assert d["improved"] is True
