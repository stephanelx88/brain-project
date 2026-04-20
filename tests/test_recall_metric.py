"""Tests for `brain.recall_metric` — the Question Coverage Score harness.

These exercise the pure-Python parts: ledger logging, live_coverage
parsing, and diff_reports. Scoring itself requires the semantic
stack and is covered end-to-end in the autoresearch smoke tests.
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


def _stub_top_score(monkeypatch, score: float, label: str = "fact:insights/x"):
    """Bypass the real embedding stack."""
    monkeypatch.setattr(recall_metric, "_top_score_for",
                        lambda q, k=3: (score, label))


def test_log_live_recall_writes_one_row(fake_ledger, monkeypatch):
    _stub_top_score(monkeypatch, 0.82)
    recall_metric.log_live_recall("what is brain autoresearch")
    lines = fake_ledger.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["kind"] == "live"
    assert row["query"] == "what is brain autoresearch"
    assert row["top_score"] == 0.82
    assert row["miss"] is False  # 0.82 > default 0.60
    assert row["threshold"] == recall_metric.MISS_THRESHOLD


def test_log_live_recall_flags_miss_below_threshold(fake_ledger, monkeypatch):
    _stub_top_score(monkeypatch, 0.35)
    recall_metric.log_live_recall("some obscure query")
    row = json.loads(fake_ledger.read_text().splitlines()[0])
    assert row["miss"] is True


def test_log_live_recall_truncates_long_queries(fake_ledger, monkeypatch):
    _stub_top_score(monkeypatch, 0.7)
    recall_metric.log_live_recall("x" * 500)
    row = json.loads(fake_ledger.read_text().splitlines()[0])
    assert len(row["query"]) == 200


def test_log_live_recall_swallows_errors(fake_ledger, monkeypatch):
    """Any failure in the log path must not propagate — it's off the
    user-facing recall hot path."""
    def boom(*a, **kw):
        raise RuntimeError("scorer exploded")
    monkeypatch.setattr(recall_metric, "_top_score_for", boom)
    # Must not raise
    recall_metric.log_live_recall("anything")
    assert not fake_ledger.exists()


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
    assert abs(data["score"] - 1 / 3) < 1e-6
    assert abs(data["avg_top"] - (0.8 + 0.8 + 0.3) / 3) < 1e-6


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
    return recall_metric.CoverageReport(
        timestamp="2026-04-20T00:00:00Z",
        threshold=0.6,
        total=total,
        misses=misses,
        score=(misses / total) if total else 0.0,
        avg_top_score=sum(p["top_score"] for p in per_q) / total if total else 0.0,
        per_query=per_q,
    )


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
    should register as 'improved' — lets us see incremental progress."""
    before = _mk_report([("a", 0.3, True), ("b", 0.4, True)])
    after = _mk_report([("a", 0.4, True), ("b", 0.5, True)])
    d = recall_metric.diff_reports(before, after)
    assert d["improved"] is True
    assert d["score_delta"] == 0.0
    assert d["avg_top_delta"] > 0
