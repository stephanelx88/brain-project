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
from typing import Callable, Iterable

from brain import semantic


SearchFn = Callable[[str, int, str | None], list[dict]]


@dataclass
class GoldenQuery:
    query: str
    expected: list[str]                  # acceptable identifiers (any = hit)
    type: str | None = None              # optional hybrid_search type filter
    description: str = ""                # for reports / debugging


@dataclass
class BenchmarkReport:
    total: int
    precision_at_1: float
    precision_at_3: float
    precision_at_10: float
    mrr: float                           # mean reciprocal rank; 0 if missed
    hit_rate: float                      # fraction with ≥1 hit in top-k
    per_query: list[dict] = field(default_factory=list)
    duration_ms: int = 0

    def headline(self) -> str:
        return (
            f"p@1={self.precision_at_1:.3f} "
            f"p@3={self.precision_at_3:.3f} "
            f"p@10={self.precision_at_10:.3f} "
            f"MRR={self.mrr:.3f} "
            f"hit={self.hit_rate:.3f} "
            f"(n={self.total})"
        )

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "precision_at_1": round(self.precision_at_1, 4),
            "precision_at_3": round(self.precision_at_3, 4),
            "precision_at_10": round(self.precision_at_10, 4),
            "mrr": round(self.mrr, 4),
            "hit_rate": round(self.hit_rate, 4),
            "duration_ms": self.duration_ms,
            "per_query": self.per_query,
        }


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

    for gq in golden_list:
        expected_set = set(gq.expected)
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
            })
            continue

        identifiers = [hit_identifier(h) for h in hits[:k]]
        # First rank at which an expected identifier appears (1-indexed).
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
        })

    total = len(golden_list)

    def _frac(n: int) -> float:
        return (n / total) if total else 0.0

    return BenchmarkReport(
        total=total,
        precision_at_1=_frac(p1),
        precision_at_3=_frac(p3),
        precision_at_10=_frac(p10),
        mrr=(mrr_sum / total) if total else 0.0,
        hit_rate=_frac(hit_count),
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
        "improved": (
            after.precision_at_1 > before.precision_at_1
            or after.mrr > before.mrr + 1e-6
        ),
    }
