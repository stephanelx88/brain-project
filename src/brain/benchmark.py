"""Golden-set benchmark for recall quality.

The eval set in `recall_metric` answers "on average, how high does the
top hit score?" It does NOT answer "did we return the *right* thing?"
— a high cosine on an unrelated fact still counts.

This module adds the missing measurement: given a golden set of
`(query, expected_identifiers)` pairs, compute precision@k and MRR over
the hybrid_search output. That's the actual retrieval-quality signal
that 10x recall/accuracy claims should be measured against.

`expected_identifiers` are strings like:
  - ``fact:people/son``           (type + slug)
  - ``note:cursor-user-rules.md`` (note path)
  - ``entity:projects/x-crawl``   (type + slug, entity-level)

A hit matches if the hybrid_search result's identifier is in the
expected set. Multiple expected ids per query are OR'd — any one
of them in the top-k counts.

Tests that exercise real retrieval live in test_benchmark.py. The
API here is pure enough to let callers swap in a custom search_fn
(e.g. to benchmark a query-rewriter or reranker against the default).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from brain import semantic


SearchFn = Callable[[str, int, str | None], list[dict]]


@dataclass
class GoldenQuery:
    query: str
    expected: list[str] = field(default_factory=list)  # acceptable identifiers (any = hit)
    type: str | None = None              # optional hybrid_search type filter
    description: str = ""                # for reports / debugging
    # A weak-match anchor asserts the *opposite* of a positive match:
    # the recall layer MUST classify this query as weak (top RRF below
    # threshold, or threshold-equivalent). The dép-2026-04-21 class is
    # the motivating failure: subject-mismatch queries that today return
    # confident-but-wrong hits are the regression these anchors catch.
    # When True, `expected` is ignored (the query has no right answer).
    expected_weak_match: bool = False


@dataclass
class BenchmarkReport:
    total: int
    precision_at_1: float
    precision_at_3: float
    precision_at_10: float
    mrr: float                           # mean reciprocal rank; 0 if missed
    hit_rate: float                      # fraction with ≥1 hit in top-k
    # Weak-match pool is scored separately from the positive pool —
    # mixing them into a single rate would conflate "we found the right
    # thing" with "we correctly said nothing". Both rates are reported
    # so trajectory deltas in either direction are visible.
    weak_total: int = 0                  # number of weak-match queries
    weak_hit_rate: float = 0.0           # fraction correctly flagged weak
    per_query: list[dict] = field(default_factory=list)
    duration_ms: int = 0

    def headline(self) -> str:
        parts = [
            f"p@1={self.precision_at_1:.3f}",
            f"p@3={self.precision_at_3:.3f}",
            f"p@10={self.precision_at_10:.3f}",
            f"MRR={self.mrr:.3f}",
            f"hit={self.hit_rate:.3f}",
        ]
        if self.weak_total:
            parts.append(f"weak={self.weak_hit_rate:.3f}")
        parts.append(f"(n={self.total},weak={self.weak_total})")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "precision_at_1": round(self.precision_at_1, 4),
            "precision_at_3": round(self.precision_at_3, 4),
            "precision_at_10": round(self.precision_at_10, 4),
            "mrr": round(self.mrr, 4),
            "hit_rate": round(self.hit_rate, 4),
            "weak_total": self.weak_total,
            "weak_hit_rate": round(self.weak_hit_rate, 4),
            "duration_ms": self.duration_ms,
            "per_query": self.per_query,
        }


def compute_weak_match(query: str, hits: list[dict]) -> tuple[bool, float, float]:
    """Replicate `mcp_server.brain_recall`'s weak-match computation.

    Returns `(weak_match, top_score, threshold)`.

    Kept in sync with the MCP path so benchmark scoring matches what
    real `brain_recall` callers actually see. The three tuning knobs
    (`BRAIN_RECALL_WEAK_RRF`, `BRAIN_RECALL_NON_ASCII_SCALE`,
    `BRAIN_RECALL_SEMANTIC_FALLBACK`) are read from the environment;
    the defaults match mcp_server's defaults so a benchmark run without
    overrides reproduces the runtime decision for the same hits.
    """
    import os as _os
    try:
        threshold = float(_os.environ.get("BRAIN_RECALL_WEAK_RRF", "0.035"))
    except (ValueError, TypeError):
        threshold = 0.035
    # BM25 misses on non-ASCII queries cut achievable RRF roughly in half;
    # scale the threshold so cross-lingual hits aren't classed as weak
    # purely by language. Same knob as `mcp_server.brain_recall`.
    if any(ord(c) > 127 for c in query):
        try:
            scale = float(_os.environ.get("BRAIN_RECALL_NON_ASCII_SCALE", "0.55"))
        except (ValueError, TypeError):
            scale = 0.55
        threshold *= scale

    top_score = max((h.get("rrf") or 0.0 for h in hits), default=0.0)
    weak = top_score < threshold

    if weak and hits:
        # Semantic fallback: RRF being weak is not evidence against a hit
        # when the embedding model found a confident cosine match. Use
        # sem_score (true cosine), not `score` — on merged hits `score`
        # may be the BM25 branch's value (often negative) and would zero
        # out the fallback for cross-lingual merged hits.
        semantic_top = max(
            (h.get("sem_score") if h.get("sem_score") is not None
             else (h.get("score") or 0.0)
             for h in hits
             if h.get("semantic_rank") is not None),
            default=0.0,
        )
        try:
            sem_fallback = float(
                _os.environ.get("BRAIN_RECALL_SEMANTIC_FALLBACK", "0.20")
            )
        except (ValueError, TypeError):
            sem_fallback = 0.20
        if semantic_top >= sem_fallback:
            weak = False

    return weak, float(top_score), float(threshold)


def hit_identifier(hit: dict) -> str:
    """Stable string identifier for a hybrid_search hit.

    Mirrors the format used by GoldenQuery.expected so equality matches
    are cheap. The `entity:` prefix isn't produced by hybrid_search
    today, but we accept it here so callers can target entity-level
    recall (future: entity-level fusion branch).
    """
    kind = hit.get("kind")
    if kind == "note":
        return f"note:{hit.get('path') or hit.get('title', '')}"
    if kind == "fact":
        return f"fact:{hit.get('type')}/{hit.get('slug') or hit.get('name', '')}"
    # Fallback: raw semantic hit (no `kind`), treat as fact-shaped.
    if "slug" in hit:
        return f"fact:{hit.get('type')}/{hit['slug']}"
    return f"unknown:{hit.get('path') or hit.get('name', '')}"


def run_benchmark(
    golden: Iterable[GoldenQuery],
    *,
    k: int = 10,
    search_fn: SearchFn | None = None,
) -> BenchmarkReport:
    """Run golden-set queries through `search_fn` (default: hybrid_search).

    Callers benchmarking a new retrieval stage (query rewriter, reranker,
    graph walker) pass a custom search_fn; the comparison against the
    default baseline is the 10x claim.
    """
    if search_fn is None:
        search_fn = semantic.hybrid_search

    t0 = time.time()
    golden_list = list(golden)
    per_query: list[dict] = []
    p1 = p3 = p10 = 0
    mrr_sum = 0.0
    hit_count = 0
    # Weak-match pool is counted separately from the positive pool so
    # neither rate is polluted by queries from the other class.
    positive_total = 0
    weak_total = 0
    weak_hit = 0

    for gq in golden_list:
        try:
            hits = search_fn(gq.query, k, gq.type) or []
        except TypeError:
            # Some search_fn signatures don't accept `type`.
            hits = search_fn(gq.query, k) or []
        except Exception as exc:
            per_query.append({
                "query": gq.query,
                "error": str(exc),
                "hit": False,
                "rank": None,
                "weak_expected": gq.expected_weak_match,
            })
            if gq.expected_weak_match:
                weak_total += 1
            else:
                positive_total += 1
            continue

        if gq.expected_weak_match:
            weak, top_score, thr = compute_weak_match(gq.query, hits)
            weak_total += 1
            if weak:
                weak_hit += 1
            identifiers = [hit_identifier(h) for h in hits[:k]]
            per_query.append({
                "query": gq.query,
                "weak_expected": True,
                "weak_observed": weak,
                "top_score": round(top_score, 4),
                "threshold": round(thr, 4),
                "top_identifiers": identifiers[:min(k, 5)],
                "hit": weak,                # "hit" = "correctly flagged weak"
                "rank": None,
                "type": gq.type,
                "description": gq.description,
            })
            continue

        # Positive pool: score rank of first expected identifier.
        positive_total += 1
        expected_set = set(gq.expected)
        identifiers = [hit_identifier(h) for h in hits[:k]]
        rank: int | None = None
        for i, ident in enumerate(identifiers, start=1):
            if ident in expected_set:
                rank = i
                break

        if rank is not None:
            hit_count += 1
            mrr_sum += 1.0 / rank
            if rank <= 1:
                p1 += 1
            if rank <= 3:
                p3 += 1
            if rank <= 10:
                p10 += 1

        per_query.append({
            "query": gq.query,
            "expected": list(expected_set),
            "top_identifiers": identifiers[:min(k, 5)],
            "hit": rank is not None,
            "rank": rank,
            "type": gq.type,
            "description": gq.description,
            "weak_expected": False,
        })

    total = len(golden_list)

    def _frac(n: int, denom: int) -> float:
        return (n / denom) if denom else 0.0

    return BenchmarkReport(
        total=total,
        precision_at_1=_frac(p1, positive_total),
        precision_at_3=_frac(p3, positive_total),
        precision_at_10=_frac(p10, positive_total),
        mrr=(mrr_sum / positive_total) if positive_total else 0.0,
        hit_rate=_frac(hit_count, positive_total),
        weak_total=weak_total,
        weak_hit_rate=_frac(weak_hit, weak_total),
        per_query=per_query,
        duration_ms=int((time.time() - t0) * 1000),
    )


def diff_benchmarks(before: BenchmarkReport, after: BenchmarkReport) -> dict:
    """Structured delta between two benchmark reports run on the same
    golden set — the shape 10x claims need to surface.
    """
    return {
        "p1_before": round(before.precision_at_1, 4),
        "p1_after": round(after.precision_at_1, 4),
        "p1_delta": round(after.precision_at_1 - before.precision_at_1, 4),
        "p3_before": round(before.precision_at_3, 4),
        "p3_after": round(after.precision_at_3, 4),
        "p3_delta": round(after.precision_at_3 - before.precision_at_3, 4),
        "mrr_before": round(before.mrr, 4),
        "mrr_after": round(after.mrr, 4),
        "mrr_delta": round(after.mrr - before.mrr, 4),
        "hit_rate_before": round(before.hit_rate, 4),
        "hit_rate_after": round(after.hit_rate, 4),
        "hit_rate_delta": round(after.hit_rate - before.hit_rate, 4),
        "weak_hit_rate_before": round(before.weak_hit_rate, 4),
        "weak_hit_rate_after": round(after.weak_hit_rate, 4),
        "weak_hit_rate_delta": round(after.weak_hit_rate - before.weak_hit_rate, 4),
        "improved": (
            after.precision_at_1 > before.precision_at_1
            or after.mrr > before.mrr + 1e-6
            or after.weak_hit_rate > before.weak_hit_rate + 1e-6
        ),
    }


# ---------------------------------------------------------------------------
# YAML loader for the checked-in golden set
# ---------------------------------------------------------------------------

DEFAULT_GOLDEN_PATH = Path(__file__).resolve().parent.parent.parent / "tests" / "golden" / "recall.yaml"


def load_golden_yaml(path: Path | str | None = None) -> list[GoldenQuery]:
    """Load a list of GoldenQuery from a YAML file.

    File shape: a YAML sequence where each element is a mapping with
    `query` (str) + either `expected: [str, ...]` or
    `expected_weak_match: true`. Optional `type` and `description`.

    A missing file returns []. PyYAML is already a hard dep (see
    pyproject.toml) so no optional-import dance is needed here.
    """
    import yaml
    target = Path(path) if path is not None else DEFAULT_GOLDEN_PATH
    if not target.exists():
        return []
    data = yaml.safe_load(target.read_text()) or []
    out: list[GoldenQuery] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        q = entry.get("query")
        if not isinstance(q, str) or not q:
            continue
        expected = entry.get("expected") or []
        if not isinstance(expected, list):
            expected = []
        out.append(
            GoldenQuery(
                query=q,
                expected=[str(e) for e in expected],
                type=entry.get("type"),
                description=str(entry.get("description") or ""),
                expected_weak_match=bool(entry.get("expected_weak_match", False)),
            )
        )
    return out
