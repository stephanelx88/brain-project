"""Question Coverage Score — autoresearch's `val_bpb` analog.

Per `~/.brain/program.md`:

  > A miss is a recall whose top-3 results have semantic score < 0.35.
  > Question Coverage Score = unanswered_recall_misses / total_recalls
  > (single scalar, monotone, lower-is-better)

Two ways to drive it:

  1. **Eval set mode** (used by autoresearch run/cycle): a fixed list of
     queries representing typical Son recall patterns is scored before
     and after each cycle. The delta tells you whether the playground
     items the agent wrote actually moved the needle.

  2. **Live mode** (future): every real `brain_recall` call gets logged
     to `~/.brain/recall-ledger.jsonl` with its top score; this module
     reads the ledger and reports rolling 7-day coverage.

The eval set lives at `~/.brain/eval-queries.md` (one query per non-blank,
non-`#` line). When that file is missing we ship a sensible default seed
so the metric works out of the box.
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

#  program.md says 0.35, but that was tuned for English MiniLM. The brain
#  ships paraphrase-multilingual-MiniLM-L12-v2, whose cosine distribution
#  for related-but-loose pairs sits ~0.55-0.65. Empirically 0.60 splits
#  Son's eval set roughly in half on a clean brain — leaving room for the
#  agent to actually move the needle. Override with BRAIN_MISS_THRESHOLD.
import os as _os
MISS_THRESHOLD = float(_os.environ.get("BRAIN_MISS_THRESHOLD", "0.60"))


DEFAULT_EVAL_QUERIES = [
    # autoresearch / brain self-knowledge
    "what is brain autoresearch and how does a cycle work",
    "Karpathy autoresearch pattern program.md",
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
        "One query per line. Lines starting with `#` are ignored. The\n"
        "autoresearch loop scores each query before and after every cycle\n"
        "and reports the delta. Edit this file as your recall patterns\n"
        "evolve — it's the brain's `val` set.\n\n"
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
    score: float          # misses / total — lower is better (binary metric)
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
            "avg_top_score": round(self.avg_top_score, 4),
            "duration_ms": self.duration_ms,
            "per_query": self.per_query,
        }

    def headline(self) -> str:
        #  Binary metric (program.md spec) + continuous metric (smoother
        #  signal for cycle-to-cycle progress when nothing flips).
        return (
            f"coverage={self.score:.3f} "
            f"({self.misses}/{self.total} miss @ thr={self.threshold:.2f}) "
            f"avg_top={self.avg_top_score:.3f}"
        )


def _top_score_for(query: str, k: int = 3) -> tuple[float, str]:
    """Return (max_score, source_label). max over top-k facts ∪ top-k notes."""
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
    score = (misses / total) if total else 0.0
    avg_top = sum(p["top_score"] for p in per_query) / total if total else 0.0
    report = CoverageReport(
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        threshold=threshold,
        total=total,
        misses=misses,
        score=score,
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
                "score": report.score,
                "avg_top": round(avg_top, 4),
                "misses": report.misses,
                "total": report.total,
                "threshold": threshold,
            }) + "\n")
    return report


def log_live_recall(query: str, *, threshold: float = MISS_THRESHOLD) -> None:
    """Append one `kind: "live"` row to the recall ledger.

    Called from the MCP `brain_recall` handler on every real query Son
    fires at the brain. The ledger is the data source for
    `live_coverage()`, which gives rolling-window coverage computed
    from actual usage (as opposed to the synthetic eval set).

    Failures are silenced — a logging hiccup must never break the
    user-facing recall path.
    """
    try:
        score, label = _top_score_for(query)
    except Exception:
        return
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": "live",
                "query": query[:200],
                "top_score": round(score, 4),
                "top_hit": label,
                "miss": score < threshold,
                "threshold": threshold,
            }) + "\n")
    except Exception:
        return


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
    queries: set[str] = set()
    if not LEDGER.exists():
        return {
            "available": False, "days": days, "queries": 0,
            "total_calls": 0, "misses": 0, "hits": 0,
            "score": 0.0, "avg_top": 0.0,
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
        "score": (misses / total) if total else 0.0,
        "avg_top": (score_sum / total) if total else 0.0,
    }


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
    return {
        "score_before": before.score,
        "score_after": after.score,
        "score_delta": round(after.score - before.score, 4),
        "avg_top_before": round(before.avg_top_score, 4),
        "avg_top_after": round(after.avg_top_score, 4),
        "avg_top_delta": avg_delta,
        #  "improved" = either fewer misses, or strictly higher avg top
        #  score (a real per-query gain even when no query crosses thr).
        "improved": (after.score < before.score) or (avg_delta > 0),
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
    args = ap.parse_args()
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
