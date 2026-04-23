"""Tests for the golden-set benchmark harness.

Unlike recall_metric's cosine-average score, benchmark tests assert
**retrieval correctness**: was the right thing returned, and at what
rank? This is the shape a 10x recall claim needs to prove.

Uses the same stub-embedder fixture from test_semantic so tests run
fast without the real transformer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from brain import benchmark, semantic


@pytest.fixture
def tmp_brain_for_bench(tmp_path, monkeypatch):
    """Minimal brain fixture: 4 entities across 2 types, 4 facts, stub embedder
    that returns a unit vector aligned with the query's first token. This
    gives us deterministic-but-realistic rankings.
    """
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)

    from brain import db
    db_path = brain_dir / ".brain.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    monkeypatch.setattr(semantic, "VEC_DIR", brain_dir / ".vec")
    monkeypatch.setattr(semantic, "FACTS_NPY", brain_dir / ".vec" / "facts.npy")
    monkeypatch.setattr(semantic, "FACTS_JSON", brain_dir / ".vec" / "facts.json")
    monkeypatch.setattr(semantic, "ENT_NPY", brain_dir / ".vec" / "entities.npy")
    monkeypatch.setattr(semantic, "ENT_JSON", brain_dir / ".vec" / "entities.json")
    monkeypatch.setattr(semantic, "META_JSON", brain_dir / ".vec" / "meta.json")

    # Deterministic embedder: hash text → 8-d unit vector.
    def fake_embed(texts, batch_size=64):
        if not texts:
            return np.zeros((0, semantic.DIM), dtype=np.float32)
        out = []
        for t in texts:
            seed = abs(hash(t)) % (2**32)
            rng = np.random.default_rng(seed)
            v = rng.standard_normal(semantic.DIM).astype(np.float32)
            v /= np.linalg.norm(v) + 1e-9
            out.append(v)
        return np.stack(out)

    monkeypatch.setattr(semantic, "_embed", fake_embed)

    # Seed 2 projects + 2 people + 4 facts that are token-distinct so BM25
    # fires reliably on the keyword in each golden query.
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/projects/alpha-proj.md", "projects", "alpha-proj",
             "Alpha Project", "about alpha"),
        )
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/projects/beta-proj.md", "projects", "beta-proj",
             "Beta Project", "about beta"),
        )
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/people/nova.md", "people", "nova",
             "Nova Engineer", "nova works on alpha"),
        )
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/people/ray.md", "people", "ray",
             "Ray Scientist", "ray studies beta"),
        )
        facts = [
            (1, "alpha distribution cleanup pipeline", "src1"),
            (2, "beta release gamma coordinator note", "src2"),
            (3, "nova engineer alpha project lead", "src3"),
            (4, "ray researcher beta publication draft", "src4"),
        ]
        for eid, text, src in facts:
            cur = conn.execute(
                "INSERT INTO facts (entity_id, text, source) VALUES (?, ?, ?)",
                (eid, text, src),
            )
            rowid = cur.lastrowid
            conn.execute(
                "INSERT INTO fts_facts (rowid, text, source) VALUES (?, ?, ?)",
                (rowid, text, src),
            )

    return brain_dir


def test_hit_identifier_fact():
    hit = {"kind": "fact", "type": "people", "slug": "son", "name": "Son"}
    assert benchmark.hit_identifier(hit) == "fact:people/son"


def test_hit_identifier_note():
    hit = {"kind": "note", "path": "son-in-long-xuyen.md", "title": "son"}
    assert benchmark.hit_identifier(hit) == "note:son-in-long-xuyen.md"


def test_hit_identifier_missing_slug_falls_back_to_name():
    hit = {"kind": "fact", "type": "people", "name": "Alpha"}
    assert benchmark.hit_identifier(hit) == "fact:people/Alpha"


def test_run_benchmark_empty_golden_set():
    rep = benchmark.run_benchmark([])
    assert rep.total == 0
    assert rep.precision_at_1 == 0.0
    assert rep.mrr == 0.0


def test_run_benchmark_default_search_fn_uses_hybrid(tmp_brain_for_bench):
    """Smoke test: with no search_fn override, run_benchmark delegates to
    semantic.hybrid_search. We don't assert on exact hit/miss here —
    ranking depends on the real fusion stack and would make this test
    flaky. We only assert the harness wires the branches correctly and
    returns a well-formed report.
    """
    semantic.build()
    golden = [
        benchmark.GoldenQuery(
            query="alpha distribution",
            expected=["fact:projects/alpha-proj"],
        ),
    ]
    rep = benchmark.run_benchmark(golden, k=10)
    assert rep.total == 1
    assert len(rep.per_query) == 1
    row = rep.per_query[0]
    # Every row must carry diagnostics so manual debugging of 10x
    # regressions is possible without re-running with different flags.
    assert "top_identifiers" in row
    assert "rank" in row
    assert "hit" in row


def test_run_benchmark_rank_and_mrr_arithmetic(tmp_brain_for_bench):
    """Two queries, one hits at rank 1 (MRR contribution=1.0), one at rank 3
    (contribution=1/3); MRR = (1.0 + 1/3) / 2 ≈ 0.667. Uses a custom
    search_fn so we don't depend on real ranking quirks.
    """
    def fake_search(q, k, t=None):
        if q == "first-query":
            return [
                {"kind": "fact", "type": "people", "slug": "son"},
                {"kind": "fact", "type": "projects", "slug": "other"},
            ]
        if q == "third-query":
            return [
                {"kind": "fact", "type": "projects", "slug": "decoy1"},
                {"kind": "fact", "type": "projects", "slug": "decoy2"},
                {"kind": "fact", "type": "people", "slug": "madhav"},
            ]
        return []

    golden = [
        benchmark.GoldenQuery(
            query="first-query", expected=["fact:people/son"]
        ),
        benchmark.GoldenQuery(
            query="third-query", expected=["fact:people/madhav"]
        ),
    ]
    rep = benchmark.run_benchmark(golden, k=10, search_fn=fake_search)
    assert rep.total == 2
    assert rep.precision_at_1 == 0.5            # only first-query hit @1
    assert rep.precision_at_3 == 1.0            # both hit within top-3
    assert abs(rep.mrr - (1.0 + 1/3) / 2) < 1e-9
    assert rep.hit_rate == 1.0


def test_run_benchmark_miss_records_no_rank(tmp_brain_for_bench):
    def empty_search(q, k, t=None):
        return []

    golden = [
        benchmark.GoldenQuery(
            query="unmatchable",
            expected=["fact:people/ghost"],
        )
    ]
    rep = benchmark.run_benchmark(golden, k=10, search_fn=empty_search)
    assert rep.total == 1
    assert rep.hit_rate == 0.0
    assert rep.mrr == 0.0
    assert rep.per_query[0]["rank"] is None


def test_run_benchmark_swallows_search_errors(tmp_brain_for_bench):
    """A search_fn that blows up on one query must not kill the whole run —
    we need the rest of the report to land so 10x experiments can finish.
    """
    def flaky(q, k, t=None):
        if q == "bomb":
            raise RuntimeError("boom")
        return [{"kind": "fact", "type": "people", "slug": "son"}]

    golden = [
        benchmark.GoldenQuery(query="ok", expected=["fact:people/son"]),
        benchmark.GoldenQuery(query="bomb", expected=["fact:people/son"]),
    ]
    rep = benchmark.run_benchmark(golden, k=10, search_fn=flaky)
    assert rep.total == 2
    assert rep.per_query[1]["hit"] is False
    assert "error" in rep.per_query[1]


def test_run_benchmark_supports_type_filter_in_signature(tmp_brain_for_bench):
    """Some search_fns only accept (query, k). run_benchmark must still
    work — falling back to the 2-arg call when TypeError fires.
    """
    calls: list[tuple] = []

    def two_arg(q, k):
        calls.append((q, k))
        return [{"kind": "fact", "type": "people", "slug": "son"}]

    golden = [benchmark.GoldenQuery(query="x", expected=["fact:people/son"])]
    rep = benchmark.run_benchmark(golden, k=5, search_fn=two_arg)
    assert rep.hit_rate == 1.0
    assert calls[0] == ("x", 5)


def test_diff_benchmarks_reports_deltas():
    before = benchmark.BenchmarkReport(
        total=10,
        precision_at_1=0.3,
        precision_at_3=0.5,
        precision_at_10=0.7,
        mrr=0.4,
        hit_rate=0.7,
    )
    after = benchmark.BenchmarkReport(
        total=10,
        precision_at_1=0.6,
        precision_at_3=0.8,
        precision_at_10=0.9,
        mrr=0.7,
        hit_rate=0.9,
    )
    d = benchmark.diff_benchmarks(before, after)
    assert d["p1_delta"] == 0.3
    assert d["mrr_delta"] == 0.3
    assert d["improved"] is True


def test_diff_benchmarks_flat_is_not_improved():
    r = benchmark.BenchmarkReport(
        total=5, precision_at_1=0.4, precision_at_3=0.5,
        precision_at_10=0.5, mrr=0.45, hit_rate=0.5,
    )
    d = benchmark.diff_benchmarks(r, r)
    assert d["improved"] is False
    assert d["p1_delta"] == 0
    assert d["mrr_delta"] == 0


def test_headline_renders_all_metrics():
    r = benchmark.BenchmarkReport(
        total=10, precision_at_1=0.3, precision_at_3=0.5,
        precision_at_10=0.7, mrr=0.42, hit_rate=0.75,
    )
    h = r.headline()
    assert "p@1=0.300" in h
    assert "MRR=0.420" in h
    assert "n=10" in h
