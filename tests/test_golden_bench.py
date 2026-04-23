"""Golden-set recall benchmark — WS1.

Two layers:
  1. Schema tests (always run): the yaml loads, every entry has a sane
     shape, the PM-verbatim queries are present and unchanged.
  2. Live bench (`-m bench` marker): runs `run_benchmark` against the
     real vault and the real hybrid_search stack. Skipped unless the
     user explicitly opts in via `pytest -m bench`, because it depends
     on a populated `~/.brain/` and pays the embedder cost.

The live bench is intentionally lenient on pass/fail thresholds at
this stage — the point of the WS1 PR is to establish the measurement,
not to gate on a target that isn't yet budgeted. Once a baseline run
lands in main, follow-up PRs add the regression guards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain import benchmark


GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "recall.yaml"


# ---------------------------------------------------------------------------
# Schema tests — always run
# ---------------------------------------------------------------------------

def test_golden_yaml_exists():
    assert GOLDEN_PATH.exists(), f"golden set missing at {GOLDEN_PATH}"


def test_golden_yaml_loads():
    queries = benchmark.load_golden_yaml(GOLDEN_PATH)
    # PM's 15:13 scope requires ≥20 queries.
    assert len(queries) >= 20, f"golden set has {len(queries)}; minimum is 20"


def test_golden_yaml_entries_are_well_formed():
    queries = benchmark.load_golden_yaml(GOLDEN_PATH)
    for gq in queries:
        assert gq.query, "empty query"
        # Every entry is either positive (has expected ids) or weak-match
        # (has expected_weak_match=True). No entry is both.
        if gq.expected_weak_match:
            assert not gq.expected, (
                f"{gq.query!r} has both expected and expected_weak_match"
            )
        else:
            assert gq.expected, (
                f"{gq.query!r} has no expected ids and is not weak-match"
            )
            for ident in gq.expected:
                # identifier shape matches hit_identifier output
                assert ident.startswith(("fact:", "note:", "entity:")), (
                    f"{ident!r} not in expected id-prefix set"
                )


def test_pm_verbatim_queries_present():
    """PM 2026-04-23 15:13 specified four queries verbatim; they must
    all appear in the golden set unchanged."""
    queries = benchmark.load_golden_yaml(GOLDEN_PATH)
    texts = {gq.query for gq in queries}
    required = {
        "forget primitive tombstone retract persist",
        "test isolation config BRAIN_DIR monkeypatch",
        "stephane nơi ở ngôn ngữ",
        "compact MCP envelope token reduction",
    }
    missing = required - texts
    assert not missing, f"PM-verbatim queries missing: {missing}"


def test_distribution_matches_pm_shape():
    """PM 2026-04-23 13:30: people ~25% / projects ~30% / decisions ~15%
    / cross-lingual ~10%. Checked loosely (±10 pp) — distribution is a
    design target, not a hard invariant.
    """
    queries = benchmark.load_golden_yaml(GOLDEN_PATH)
    positives = [q for q in queries if not q.expected_weak_match]

    def fraction_for(type_key: str) -> float:
        hit = sum(
            1 for q in positives
            if any(e.startswith(f"fact:{type_key}/") for e in q.expected)
        )
        return hit / len(positives) if positives else 0.0

    assert 0.15 <= fraction_for("people") <= 0.45
    assert 0.20 <= fraction_for("projects") <= 0.50
    assert 0.05 <= fraction_for("decisions") <= 0.30


# ---------------------------------------------------------------------------
# Weak-match scoring unit tests (no vault dependency) — always run
# ---------------------------------------------------------------------------

def test_compute_weak_match_flags_low_rrf():
    """Hits with top rrf below threshold AND sem_score below fallback
    must be classified weak. Uses an ASCII query so the non-ASCII
    scaling doesn't kick in.
    """
    hits = [
        {"rrf": 0.01, "sem_score": 0.1, "semantic_rank": 0},
        {"rrf": 0.005, "sem_score": 0.05, "semantic_rank": 1},
    ]
    weak, top, thr = benchmark.compute_weak_match("ascii query only", hits)
    assert weak is True
    assert top == 0.01
    assert thr == pytest.approx(0.035)


def test_compute_weak_match_rejects_confident_rrf():
    """A top rrf above threshold is never weak, regardless of sem_score."""
    hits = [{"rrf": 0.1, "sem_score": 0.0, "semantic_rank": 0}]
    weak, _, _ = benchmark.compute_weak_match("ascii", hits)
    assert weak is False


def test_compute_weak_match_semantic_fallback_overrides_weak():
    """Low rrf but confident cosine → not weak. This is the
    cross-lingual rescue path from mcp_server.brain_recall.
    """
    hits = [{"rrf": 0.001, "sem_score": 0.35, "semantic_rank": 0}]
    weak, _, _ = benchmark.compute_weak_match("ascii", hits)
    assert weak is False


def test_compute_weak_match_scales_threshold_for_non_ascii():
    """Non-ASCII queries get a lower threshold — BM25 misses on CJK/VN
    inputs cut the achievable RRF in half. Same knob as mcp_server.
    """
    _, _, thr_ascii = benchmark.compute_weak_match("ascii only", [])
    _, _, thr_vi = benchmark.compute_weak_match("son ở đâu", [])
    assert thr_vi < thr_ascii
    # Default scale is 0.55; allow a small band for env overrides.
    assert thr_vi <= thr_ascii * 0.7


def test_run_benchmark_scores_weak_match_branch():
    """Mixed pool: one positive (hits @1), one weak-anchor (correctly
    flagged weak). Both rates must populate; they must not pollute
    each other's denominator.
    """
    golden = [
        benchmark.GoldenQuery(
            query="positive query",
            expected=["fact:people/son"],
        ),
        benchmark.GoldenQuery(
            query="weak anchor",
            expected_weak_match=True,
        ),
    ]

    def fake_search(q, k, t=None):
        if q == "positive query":
            return [{
                "kind": "fact", "type": "people", "slug": "son",
                "rrf": 0.2, "sem_score": 0.8, "semantic_rank": 0,
            }]
        # weak anchor: return hits below threshold
        return [{"rrf": 0.001, "sem_score": 0.05, "semantic_rank": 0}]

    rep = benchmark.run_benchmark(golden, k=10, search_fn=fake_search)
    assert rep.total == 2
    assert rep.precision_at_1 == 1.0        # positive pool had 1, hit 1
    assert rep.weak_total == 1
    assert rep.weak_hit_rate == 1.0
    # Denominators don't cross-contaminate: positive pool size is 1
    # (the other entry is in the weak pool), so p@1 = 1/1 = 1.0.


def test_run_benchmark_weak_fails_on_confident_hit():
    """Weak-anchor must fail when the recall layer returns a confident
    hit — this is the failure mode WS7a eventually fixes.
    """
    golden = [
        benchmark.GoldenQuery(query="weak anchor", expected_weak_match=True),
    ]

    def fake_search(q, k, t=None):
        return [{"rrf": 0.2, "sem_score": 0.9, "semantic_rank": 0}]

    rep = benchmark.run_benchmark(golden, k=10, search_fn=fake_search)
    assert rep.weak_total == 1
    assert rep.weak_hit_rate == 0.0


def test_headline_includes_weak_when_present():
    rep = benchmark.BenchmarkReport(
        total=3, precision_at_1=0.5, precision_at_3=0.5,
        precision_at_10=0.5, mrr=0.5, hit_rate=0.5,
        weak_total=1, weak_hit_rate=1.0,
    )
    h = rep.headline()
    assert "weak=1.000" in h
    assert "weak=1" in h  # the (n=3,weak=1) footer


# ---------------------------------------------------------------------------
# Live bench — depends on populated vault
# ---------------------------------------------------------------------------

@pytest.mark.bench
def test_run_benchmark_against_real_vault():
    """Integration: run the golden set against the real hybrid_search.

    Skipped if the vault isn't populated (no facts table, no vec dir),
    so this works on a clean CI checkout. When it does run, it asserts
    the report is well-formed and non-empty — the actual p@1 numbers
    are the baseline, not a gate, at this stage.
    """
    import brain.config as _cfg
    if not (_cfg.BRAIN_DIR / "entities").exists():
        pytest.skip("brain vault not populated")

    queries = benchmark.load_golden_yaml(GOLDEN_PATH)
    assert queries, "golden set is empty"

    rep = benchmark.run_benchmark(queries, k=10)
    assert rep.total == len(queries)
    # Report shape sanity — not quality gates (see module docstring).
    assert 0.0 <= rep.precision_at_1 <= 1.0
    assert 0.0 <= rep.mrr <= 1.0
    assert rep.weak_total == sum(1 for q in queries if q.expected_weak_match)
    assert 0.0 <= rep.weak_hit_rate <= 1.0
    # Every per-query row carries the diagnostic fields used by
    # `brain bench --verbose`.
    for row in rep.per_query:
        assert "query" in row
        assert "hit" in row
        assert "weak_expected" in row
