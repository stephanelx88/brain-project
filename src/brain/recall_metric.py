"""Question Coverage Score — recall-quality metric.

A miss is a recall whose top result has semantic score < threshold.
The **primary** scalar `score = 1.0 - avg_top_score` is monotone with
semantic recall quality and never floor-saturates, unlike the binary
`miss_rate` which hits 0 once every eval query clears the threshold.
Both are reported; `score` is the trajectory signal.

Two ways to drive it:

  1. **Eval set mode**: a fixed list of queries representing typical
     Son recall patterns is scored; the delta across runs tells you
     whether changes to the brain actually moved the needle.

  2. **Live mode**: every real `brain_recall` call gets logged to
     `~/.brain/recall-ledger.jsonl` with its top score; this module
     reads the ledger and reports rolling 7-day coverage.

The eval set lives at `~/.brain/eval-queries.md` (one `- query` item
per line). When the file is missing we seed defaults so the metric
works out of the box.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import brain.config as config
from brain import semantic

EVAL_FILE = config.BRAIN_DIR / "eval-queries.md"
LEDGER = config.BRAIN_DIR / "recall-ledger.jsonl"

#  The brain ships paraphrase-multilingual-MiniLM-L12-v2. Empirical
#  recall-ledger over 14 days showed 6/10 top-miss queries scored in
#  [0.44, 0.55] with the correct target at rank 1 — a ranker hit being
#  flagged as a miss by a too-strict threshold. 0.45 splits real hits
#  from noise on code-mixed VI/EN + technical queries without flipping
#  unrelated hits into false positives. Override via BRAIN_MISS_THRESHOLD.
import os as _os
MISS_THRESHOLD = float(_os.environ.get("BRAIN_MISS_THRESHOLD", "0.45"))

#  Live-mode (real `brain_recall` calls) measures hybrid RRF, NOT raw
#  cosine, because that's the path real users hit. RRF scores live on a
#  different scale (~0.07 = strong hit, ~0.04 = weak). The default
#  matches `BRAIN_RECALL_WEAK_RRF` in mcp_server (0.035) but raised
#  slightly so a "miss" means "even weaker than the weak-match guard
#  itself flagged as low-confidence". Without this split, every
#  ledger-driven miss-rate report inflates by ~50% because the cosine
#  threshold (0.45) was being applied to RRF-driven retrieval.
MISS_RRF_THRESHOLD = float(_os.environ.get("BRAIN_MISS_RRF_THRESHOLD", "0.05"))


DEFAULT_EVAL_QUERIES = [
    # brain self-knowledge
    "playground promotion to entities",
    "Question Coverage Score metric",
    # X crawl project
    "x crawl playwright cookies authenticated session",
    "Karpathy tweets crawled persistent memory",
    # cross-project / people
    "Madhav Kamath role at Honeywell BMS",
    "stakeholder requirements driver in BMS Honeywell",
    # operational lessons
    "dual-instance Mac freeze prevention",
    "active session guard auto extract idle",
    "semantic deduplication recency weighting",
    "brain_status MCP dashboard",
    # personal context
    "son in long xuyen",
    "Vietnamese tone preferences corrections",
    # shape-of-system queries
    "session harvest pipeline ingest extract reconcile",
]


# ---------------------------------------------------------------------------
# eval-set loader
# ---------------------------------------------------------------------------

def load_eval_queries() -> list[str]:
    """Read `~/.brain/eval-queries.md` if it exists; else return defaults
    (and seed the file so the human can edit it)."""
    if EVAL_FILE.exists():
        out: list[str] = []
        for raw in EVAL_FILE.read_text(errors="replace").splitlines():
            line = raw.strip()
            #  Strict: only `- ` items are queries. Skip headers, blanks,
            #  prose intros — anything else is documentation.
            if not line.startswith("- "):
                continue
            q = line[2:].strip()
            if q and not q.startswith("[done]"):
                out.append(q)
        if out:
            return out

    # First run: seed the file from defaults so the human can iterate.
    EVAL_FILE.write_text(
        "# Brain Recall Eval Set\n\n"
        "One `- query` item per line. Lines starting with `#` are ignored.\n"
        "Edit this file as your recall patterns evolve — it's the brain's\n"
        "`val` set for measuring recall quality deltas across changes.\n\n"
        + "\n".join(f"- {q}" for q in DEFAULT_EVAL_QUERIES)
        + "\n"
    )
    return list(DEFAULT_EVAL_QUERIES)


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

@dataclass
class CoverageReport:
    timestamp: str
    threshold: float
    total: int
    misses: int
    #  Continuous primary metric: 1 - avg_top_score, clamped to [0, 1].
    #  Lower-is-better, monotone with semantic recall quality, and never
    #  floor-saturates while the brain has any room to improve.
    score: float
    #  Binary spec metric (program.md): misses / total. Preserved for
    #  back-compat and as a "did any query cross the threshold?" signal.
    miss_rate: float
    avg_top_score: float  # mean of per-query top scores — higher is better
    per_query: list[dict] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "threshold": self.threshold,
            "total": self.total,
            "misses": self.misses,
            "score": round(self.score, 4),
            "miss_rate": round(self.miss_rate, 4),
            "avg_top_score": round(self.avg_top_score, 4),
            "duration_ms": self.duration_ms,
            "per_query": self.per_query,
        }

    def headline(self) -> str:
        #  Continuous primary score + binary miss-rate, both
        #  lower-is-better, on a single line so trajectory tail logs
        #  are unambiguous.
        return (
            f"score={self.score:.3f} "
            f"miss={self.miss_rate:.3f} "
            f"({self.misses}/{self.total} @ thr={self.threshold:.2f}) "
            f"avg_top={self.avg_top_score:.3f}"
        )


def _top_score_for(query: str, k: int = 3) -> tuple[float, str]:
    """Return (max_cosine_score, source_label). Used by the **eval-set**
    coverage path: each eval query is independent and scored the same way
    (raw semantic cosine over facts ∪ notes) so trajectory deltas across
    runs stay comparable.

    Live-mode scoring uses `_hybrid_top_score` instead, because that's
    the path real `brain_recall` calls take.
    """
    fact_hits = semantic.search_facts(query, k=k) or []
    note_hits = semantic.search_notes(query, k=k) or []
    best = -1.0
    label = "(no hit)"
    for h in fact_hits:
        s = float(h.get("score", 0.0))
        if s > best:
            best = s
            label = f"fact:{h.get('type')}/{h.get('name')}"
    for h in note_hits:
        s = float(h.get("score", 0.0))
        if s > best:
            best = s
            label = f"note:{h.get('path')}"
    return best, label


def _hybrid_top_score(query: str, k: int = 3) -> tuple[float, float, str]:
    """Return (top_rrf, top_cosine, label) — live-mode scorer.

    Mirrors the `hybrid_search` path that `brain_recall` actually serves
    so the live ledger reflects retrieval quality as the user experiences
    it. Returns BOTH metrics so old consumers reading `top_score`
    (cosine) keep working, while new consumers can switch to `top_rrf`
    for a faithful miss-detection signal.

    `top_cosine` is sourced independently from `_top_score_for` (raw
    semantic) rather than from the hybrid hit's `score` field — that
    field can be BM25 (often negative) or absent depending on which
    branch won the fusion, which would corrupt the cosine column in
    the ledger. The two embeddings calls share the same model, so the
    cost is one extra ~5 ms cosine pass; the win is a ledger where
    `top_score` always means "best raw semantic similarity".
    """
    hits = semantic.hybrid_search(query, k=k) or []
    if not hits:
        return -1.0, -1.0, "(no hit)"
    top = hits[0]
    rrf = float(top.get("rrf", 0.0))
    cos_best, _ = _top_score_for(query, k=k)
    cosine = max(0.0, cos_best)
    if top.get("kind") == "note":
        label = f"note:{top.get('path') or top.get('title','')}"
    else:
        label = f"fact:{top.get('type')}/{top.get('name')}"
    return rrf, cosine, label


def score_coverage(
    queries: list[str] | None = None,
    *,
    threshold: float = MISS_THRESHOLD,
    persist: bool = True,
) -> CoverageReport:
    """Run the eval set, count misses, return a report. The semantic index
    must be up to date — call `semantic.ensure_built()` first if you've
    just written new playground items."""
    semantic.ensure_built()
    qs = queries if queries is not None else load_eval_queries()
    t0 = time.time()
    per_query: list[dict] = []
    misses = 0
    for q in qs:
        score, label = _top_score_for(q)
        is_miss = score < threshold
        if is_miss:
            misses += 1
        per_query.append({
            "query": q,
            "top_score": round(score, 4),
            "top_hit": label,
            "miss": is_miss,
        })
    total = len(qs)
    miss_rate = (misses / total) if total else 0.0
    avg_top = sum(p["top_score"] for p in per_query) / total if total else 0.0
    #  Clamp into [0, 1]: cosine top-scores can dip slightly below 0 on
    #  unrelated queries, which would push score above 1; the inverse
    #  bound (avg_top > 1) is impossible in practice but cheap to guard.
    score = max(0.0, min(1.0, 1.0 - avg_top))
    report = CoverageReport(
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        threshold=threshold,
        total=total,
        misses=misses,
        score=score,
        miss_rate=miss_rate,
        avg_top_score=avg_top,
        per_query=per_query,
        duration_ms=int((time.time() - t0) * 1000),
    )
    if persist:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps({
                "ts": report.timestamp,
                "kind": "eval",
                #  `score` was binary miss-rate before 2026-04-21 and is
                #  continuous (1 - avg_top) from then on. Older rows still
                #  parse as `score` + `avg_top`; new rows additionally
                #  carry `miss_rate` so consumers can disambiguate.
                "score": round(report.score, 4),
                "miss_rate": round(report.miss_rate, 4),
                "avg_top": round(avg_top, 4),
                "misses": report.misses,
                "total": report.total,
                "threshold": threshold,
            }) + "\n")
    return report


def log_live_recall(
    query: str,
    *,
    threshold: float = MISS_THRESHOLD,  # back-compat: cosine threshold
    rrf_threshold: float = MISS_RRF_THRESHOLD,
) -> None:
    """Append one `kind: "live"` row to the recall ledger.

    Called from the MCP `brain_recall` handler on every real query Son
    fires at the brain. The ledger is the data source for
    `live_coverage()`, which gives rolling-window coverage computed
    from actual usage (as opposed to the synthetic eval set).

    Miss decision is made on **hybrid RRF** (the same retrieval path
    `brain_recall` serves), not raw cosine — otherwise good hybrid hits
    get flagged as misses just because their constituent semantic
    cosine landed near the cosine threshold. Both `top_rrf` (new) and
    `top_score` (cosine, kept for back-compat) are written to every row
    so historical comparisons keep parsing.

    When the query misses (top_rrf < rrf_threshold), also mirror the
    event into `failures.jsonl` with source=`recall_miss`, so the
    failure ledger can aggregate repeated-miss topics (see
    `learning_gaps`). This closes the loop between what son asks and
    what the brain surfaces as "you keep missing X — want to note
    something?".

    Failures are silenced — a logging hiccup must never break the
    user-facing recall path.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return  # don't pollute the ledger with empty / single-char pings
    try:
        top_rrf, cosine, label = _hybrid_top_score(q)
    except Exception:
        return
    is_miss = top_rrf < rrf_threshold
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": "live",
                "query": query[:200],
                "top_score": round(cosine, 4),       # back-compat (cosine)
                "top_rrf": round(top_rrf, 4),        # new: drives miss flag
                "top_hit": label,
                "miss": is_miss,
                "threshold": threshold,              # cosine, historical
                "rrf_threshold": rrf_threshold,
            }) + "\n")
    except Exception:
        pass
    if is_miss:
        try:
            from brain import failures
            failures.record_failure(
                source="recall_miss",
                tool="brain_recall",
                query=query[:200],
                result_digest=(
                    f"top_rrf={round(top_rrf, 4)} "
                    f"top_score={round(cosine, 4)} top_hit={label}"
                ),
                extra={
                    "top_rrf": round(top_rrf, 4),
                    "rrf_threshold": rrf_threshold,
                    "top_score": round(cosine, 4),
                    "threshold": threshold,
                },
            )
        except Exception:
            pass


def live_coverage(days: int = 7) -> dict:
    """Compute rolling coverage over `kind: "live"` ledger entries
    within the last `days`. Returns a small dict ready to render.

    Unlike the eval-set score, this reflects *actual* usage. A high
    miss rate here means Son keeps hitting topics the brain doesn't
    cover, regardless of how well it scores on the fixed eval set.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    hits = 0
    misses = 0
    score_sum = 0.0
    rrf_sum = 0.0
    rrf_rows = 0  # count rows that carry the new top_rrf field
    queries: set[str] = set()
    if not LEDGER.exists():
        return {
            "available": False, "days": days, "queries": 0,
            "total_calls": 0, "misses": 0, "hits": 0,
            "miss_rate": 0.0, "avg_top": 0.0, "avg_rrf": 0.0,
        }
    for raw in LEDGER.read_text(errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("kind") != "live":
            continue
        ts = row.get("ts", "")
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            continue
        if t < cutoff:
            continue
        score_sum += float(row.get("top_score", 0.0))
        if "top_rrf" in row:
            rrf_sum += float(row.get("top_rrf", 0.0))
            rrf_rows += 1
        if row.get("miss"):
            misses += 1
        else:
            hits += 1
        queries.add(row.get("query", ""))
    total = hits + misses
    return {
        "available": total > 0,
        "days": days,
        "queries": len(queries),
        "total_calls": total,
        "misses": misses,
        "hits": hits,
        "miss_rate": (misses / total) if total else 0.0,
        # `avg_top` stays cosine for back-compat with old dashboards.
        # `avg_rrf` is the live-mode trajectory signal — the average
        # hybrid RRF score of real queries this week.
        "avg_top": (score_sum / total) if total else 0.0,
        "avg_rrf": (rrf_sum / rrf_rows) if rrf_rows else 0.0,
        "rrf_rows": rrf_rows,
    }


def top_miss_queries(days: int = 7, n: int = 10) -> list[dict]:
    """Group live-ledger misses by (normalized) query, return the worst
    offenders within the last `days`.

    Surfaces "what is the brain consistently failing to recall?" — the
    queries that deserve either a canonical entity (write the answer
    down) or an eval-set entry (make the miss loud). Each item:

        { "query": str, "misses": int, "hits": int,
          "best_score": float, "latest_hit": str | None }
    """
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    groups: dict[str, dict] = {}
    if not LEDGER.exists():
        return []
    for raw in LEDGER.read_text(errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("kind") != "live":
            continue
        try:
            t = datetime.strptime(row.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            continue
        if t < cutoff:
            continue
        q = (row.get("query") or "").strip().lower()
        if not q:
            continue
        g = groups.setdefault(q, {
            "query": q, "misses": 0, "hits": 0,
            "best_score": 0.0, "latest_hit": None,
        })
        score = float(row.get("top_score", 0.0))
        if row.get("miss"):
            g["misses"] += 1
        else:
            g["hits"] += 1
        if score > g["best_score"]:
            g["best_score"] = score
            g["latest_hit"] = row.get("top_hit")
    # Sort by miss count desc, then best_score asc (worst-performing first)
    ranked = sorted(
        (g for g in groups.values() if g["misses"] > 0),
        key=lambda g: (-g["misses"], g["best_score"]),
    )
    return ranked[:n]


def diff_reports(before: CoverageReport, after: CoverageReport) -> dict:
    """Compute a structured delta between two coverage reports run on the
    same eval set."""
    by_q_before = {p["query"]: p for p in before.per_query}
    flipped_to_hit: list[str] = []
    flipped_to_miss: list[str] = []
    score_gains: list[tuple[str, float]] = []
    for after_p in after.per_query:
        q = after_p["query"]
        before_p = by_q_before.get(q)
        if not before_p:
            continue
        if before_p["miss"] and not after_p["miss"]:
            flipped_to_hit.append(q)
        if not before_p["miss"] and after_p["miss"]:
            flipped_to_miss.append(q)
        delta = after_p["top_score"] - before_p["top_score"]
        if abs(delta) >= 0.01:
            score_gains.append((q, round(delta, 4)))
    score_gains.sort(key=lambda x: -x[1])
    avg_delta = round(after.avg_top_score - before.avg_top_score, 4)
    score_delta = round(after.score - before.score, 4)
    miss_rate_delta = round(after.miss_rate - before.miss_rate, 4)
    return {
        "score_before": before.score,
        "score_after": after.score,
        "score_delta": score_delta,
        "miss_rate_before": round(before.miss_rate, 4),
        "miss_rate_after": round(after.miss_rate, 4),
        "miss_rate_delta": miss_rate_delta,
        "avg_top_before": round(before.avg_top_score, 4),
        "avg_top_after": round(after.avg_top_score, 4),
        "avg_top_delta": avg_delta,
        #  Now that `score` is continuous (1 - avg_top), it moves whenever
        #  semantic recall does, so `score_delta < 0` ≡ `avg_delta > 0` to
        #  4-dp precision. We still also accept `miss_rate_delta < 0` so a
        #  threshold-flip without an avg_top change registers as progress.
        "improved": (score_delta < 0) or (miss_rate_delta < 0),
        "flipped_to_hit": flipped_to_hit,
        "flipped_to_miss": flipped_to_miss,
        "biggest_score_gains": score_gains[:5],
        "biggest_score_drops": score_gains[-5:][::-1] if score_gains else [],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=float, default=MISS_THRESHOLD)
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON instead of a summary")
    ap.add_argument("--no-persist", action="store_true",
                    help="don't append to recall-ledger.jsonl")
    ap.add_argument("--live", action="store_true",
                    help="print rolling live-ledger coverage instead of "
                         "running the eval set (no LLM / embeddings called)")
    ap.add_argument("--days", type=int, default=7,
                    help="window (days) for --live mode (default 7)")
    ap.add_argument("--top-miss", type=int, default=10,
                    help="how many worst miss queries to surface in --live")
    args = ap.parse_args()
    if args.live:
        summary = live_coverage(days=args.days)
        misses = top_miss_queries(days=args.days, n=args.top_miss)
        if args.json:
            print(json.dumps({"summary": summary,
                              "top_miss_queries": misses},
                             indent=2, ensure_ascii=False))
            return 0
        if not summary.get("available"):
            print(f"live recall: no calls in last {args.days}d")
            return 0
        print(
            f"live recall: miss {summary['miss_rate']*100:.1f}% "
            f"({summary['misses']}/{summary['total_calls']}) · "
            f"avg-top {summary['avg_top']:.3f}  "
            f"[{summary['queries']} uniq queries, {args.days}d]"
        )
        if misses:
            print()
            print("top miss queries:")
            for m in misses:
                print(f"  {m['misses']}x  best={m['best_score']:.3f}  "
                      f"{m['query'][:70]}")
        return 0
    report = score_coverage(threshold=args.threshold, persist=not args.no_persist)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.headline())
        for p in report.per_query:
            mark = "MISS" if p["miss"] else "  ok"
            print(f"  {mark}  {p['top_score']:.3f}  {p['query'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
