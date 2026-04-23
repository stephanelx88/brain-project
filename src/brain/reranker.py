"""LLM reranker — top-N candidates re-scored by a small model.

`hybrid_search` fuses BM25 + semantic ranks, which captures surface
relevance but not query intent. On a query like "where is son living
now", BM25 boosts every fact that contains "son", and semantic finds
paraphrases of "location" — but neither checks whether a given
candidate *answers* the question. The reranker asks Haiku: "given
this query, how relevant is each of these passages?" and reorders
the top-N by LLM score before returning top-k.

Design:

  - One LLM call per recall (batch scoring of N candidates), not N
    calls. Candidates are presented as a numbered list so the model
    returns a compact JSON map `{"1": 9, "2": 3, ...}`.

  - Gated on `BRAIN_RERANK=1`; `rerank(candidates)` degrades to
    returning the original top-k when disabled or on any failure.
    The reranker is strictly a refinement — never a bottleneck.

  - Candidates are the `hybrid_search` output dicts; we extract a
    short text blob (fact.text or note.snippet) for the prompt, and
    keep the original dict intact so nothing downstream needs to
    change.

  - Cached by (query_hash, candidate_ids_hash). Two recalls with the
    same query and the same candidate set reuse the LLM score;
    changing ANY candidate invalidates the cache.

Tests inject a stub LLM via `set_llm` so reranking is deterministic
and doesn't hit the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable

import brain.config as config

CACHE_DIR = config.BRAIN_DIR / ".cache" / "rerank"
CACHE_TTL_DAYS = 7

# Upper bound on candidates per LLM call. Haiku handles ~30 short
# passages comfortably in one JSON response; beyond that per-token
# latency eats the recall benefit.
MAX_RERANK_N = 30

# WS7b (2026-04-23): timeout tightened from 20s to 4s on the recall
# path — same reasoning as query_rewriter.DEFAULT_TIMEOUT_SEC. Rerank
# is one LLM call per recall (not per candidate), so 4s is enough for
# Haiku to score ≤30 passages; longer budgets only matter if the
# model is overloaded, and in that case we'd rather fall back to the
# RRF ordering than block the agent.
DEFAULT_TIMEOUT_SEC = 4.0


def _enabled() -> bool:
    """Evaluated per call so tests and admin can flip via env without
    reloading the module. Defaults OFF per WS7b bench gate — see
    `brain.query_rewriter._enabled` for the measurement outcome.
    Flip with `BRAIN_RERANK=1`.
    """
    return os.environ.get("BRAIN_RERANK", "0") == "1"


def _timeout_sec() -> float:
    try:
        return float(os.environ.get("BRAIN_RERANK_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))
    except (ValueError, TypeError):
        return DEFAULT_TIMEOUT_SEC


RERANK_PROMPT = """You are ranking search results for a personal \
knowledge-base lookup.

Query: {query}

Candidates (numbered):
{candidates}

For each candidate, rate how well it ANSWERS the query, on a scale \
from 0 (irrelevant) to 10 (perfect answer). Return ONLY a JSON object \
mapping candidate number to score, no prose, no code fence.

Example: {{"1": 9, "2": 3, "3": 7}}"""


# ---------------------------------------------------------------------------
# LLM injection for tests
# ---------------------------------------------------------------------------

_llm_fn: Callable[[str], str | None] | None = None


def set_llm(fn: Callable[[str], str | None] | None) -> None:
    global _llm_fn
    _llm_fn = fn


def _default_llm(prompt: str) -> str | None:
    try:
        from brain.auto_extract import call_claude
    except ImportError:
        return None
    return call_claude(prompt, timeout=int(round(_timeout_sec())))


def _call_llm(prompt: str) -> str | None:
    fn = _llm_fn or _default_llm
    try:
        return fn(prompt)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# candidate → short prompt-line
# ---------------------------------------------------------------------------

def _candidate_text(hit: dict) -> str:
    """Short, human-readable one-liner for a candidate. Trimmed to 240
    chars because the LLM doesn't need full docs to score relevance."""
    kind = hit.get("kind")
    if kind == "note":
        title = (hit.get("title") or "").strip()
        snippet = (hit.get("snippet") or "").strip()
        body = f"{title}: {snippet}".strip().strip(":")
        label = "note"
    else:
        text = (hit.get("text") or "").strip()
        name = (hit.get("name") or "").strip()
        body = f"[{hit.get('type')}/{name}] {text}".strip()
        label = "fact"
    body = body.replace("\n", " ").strip()
    if len(body) > 240:
        body = body[:237] + "..."
    return f"{label}: {body}"


def _candidate_fingerprint(hit: dict) -> str:
    """Stable key per candidate — used for the cache invalidator."""
    kind = hit.get("kind", "fact")
    if kind == "note":
        return f"note:{hit.get('path') or hit.get('title', '')}"
    return (
        f"fact:{hit.get('type')}/{hit.get('slug') or hit.get('name', '')}:"
        f"{(hit.get('text') or '')[:60]}"
    )


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def _parse_scores(raw: str, n: int) -> dict[int, float]:
    """Parse LLM JSON `{"1": 9, ...}` response. Tolerant to code fences
    and prose. Returns dict keyed by 1-indexed candidate number; any
    entry missing or unparseable is dropped."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.startswith("```"))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}") + 1
        if s < 0 or e <= s:
            return {}
        try:
            data = json.loads(text[s:e])
        except json.JSONDecodeError:
            return {}
    if not isinstance(data, dict):
        return {}
    scores: dict[int, float] = {}
    for key, val in data.items():
        try:
            idx = int(str(key).strip())
            score = float(val)
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= n:
            # Clamp to [0, 10]; models occasionally stray.
            scores[idx] = max(0.0, min(10.0, score))
    return scores


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _cache_key(query: str, candidates: list[dict]) -> str:
    payload = query.strip().lower() + "||" + "|".join(
        _candidate_fingerprint(c) for c in candidates
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def _cache_load(key: str) -> dict[int, float] | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    ts = data.get("ts", 0)
    if time.time() - ts > CACHE_TTL_DAYS * 86400:
        return None
    raw_scores = data.get("scores")
    if not isinstance(raw_scores, dict):
        return None
    out: dict[int, float] = {}
    for k, v in raw_scores.items():
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _cache_store(key: str, scores: dict[int, float]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{key}.json"
        path.write_text(json.dumps({
            "scores": {str(k): v for k, v in scores.items()},
            "ts": int(time.time()),
        }))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def rerank(
    query: str,
    candidates: list[dict],
    *,
    k: int = 10,
    use_cache: bool = True,
) -> list[dict]:
    """Re-score and re-sort candidates via LLM relevance judgement.

    When disabled, on LLM failure, or when the scoring call returns
    nothing usable, the original order is preserved and the top-k
    slice is returned — the reranker never takes anything *away* from
    the baseline.

    The returned hits are the SAME dicts as the input (not copies)
    with an added `rerank_score` field so downstream code can see the
    model's judgement without losing the RRF score under `rrf`.
    """
    if not candidates:
        return []
    if not _enabled():
        return candidates[:k]

    top_n = candidates[:MAX_RERANK_N]

    if use_cache:
        key = _cache_key(query, top_n)
        cached = _cache_load(key)
        if cached is not None:
            return _apply_scores(top_n, cached, k=k)

    numbered = "\n".join(
        f"{i + 1}. {_candidate_text(hit)}" for i, hit in enumerate(top_n)
    )
    prompt = RERANK_PROMPT.format(query=query.strip(), candidates=numbered)
    raw = _call_llm(prompt)
    if not raw:
        return candidates[:k]
    scores = _parse_scores(raw, n=len(top_n))
    if not scores:
        return candidates[:k]

    if use_cache:
        _cache_store(_cache_key(query, top_n), scores)
    return _apply_scores(top_n, scores, k=k)


def _apply_scores(
    candidates: list[dict],
    scores: dict[int, float],
    *,
    k: int,
) -> list[dict]:
    """Merge LLM scores into the candidate dicts and re-sort.

    Candidates missing from the score map keep rank order among
    themselves (stable sort on original position) but rank AFTER
    every scored candidate with score > 0. This preserves the RRF
    fallback ordering for hits the LLM happened not to touch.
    """
    decorated = []
    for i, hit in enumerate(candidates):
        score = scores.get(i + 1)
        hit["rerank_score"] = score
        decorated.append((score, i, hit))
    # Sort key: (-score, original_rank). None scores collapse below
    # any positive score (sorted as -0.0, after every real score).
    decorated.sort(
        key=lambda triple: (
            -(triple[0] if triple[0] is not None else -1.0),
            triple[1],
        )
    )
    return [hit for _, _, hit in decorated[:k]]
