"""LLM-driven query expansion for recall.

The single biggest cross-lingual recall leak is the raw-query → raw-index
hit: a user asking "son ở đâu" ranks differently from "Son's current
location", which ranks differently from "where is Son". All three
should land on the same fact.

This module produces 3-6 paraphrase variants per query using a small
LLM (Haiku by default) and caches them on disk keyed by query hash.
Callers fan out `hybrid_search` across every variant and fuse the
results — a second-level RRF that catches the paraphrase that each
branch of the index happens to favour.

Design:

  - `expand_query(query)` returns a list of variants INCLUDING the
    original. When the LLM is unavailable, offline, or disabled, it
    degrades to just `[query]` — this is never a hard failure in the
    recall path.

  - Cache is on-disk (`~/.brain/.cache/query-rewrites/<hash>.json`) and
    cheap to invalidate (delete the directory). 30-day TTL matches the
    rate at which extraction/ontology vocabulary evolves.

  - `expanded_hybrid_search(query, k, type)` runs the original query
    AND the variants through `hybrid_search`, fuses with inter-variant
    RRF, and returns the merged top-k. The original query's variant
    gets a small boost so exact-intent queries outrank drifted
    paraphrases — the rewriter is a safety net, not a replacement.

Tests inject a stub LLM to avoid network calls and make the fusion
deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

import brain.config as config
from brain import semantic

CACHE_DIR = config.BRAIN_DIR / ".cache" / "query-rewrites"
CACHE_TTL_DAYS = 30
MAX_VARIANTS = 5                       # beyond this, latency outweighs recall gain
_ENABLED = os.environ.get("BRAIN_QUERY_REWRITE", "0") == "1"


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

REWRITE_PROMPT = """You are a query-rewriter for a personal-knowledge-base search.

Given a user's search query, produce {n} ALTERNATIVE phrasings that a \
retrieval system with a multilingual embedding model would be more \
likely to match against the stored documents.

Rules:
- PRESERVE the semantic intent. Never invent new concepts or entities.
- Include: literal paraphrases, translations (Vietnamese ↔ English when \
the query is in one of them), synonyms, and common alternative \
terminology.
- Keep each variant SHORT (under 12 words).
- Return ONLY a JSON array of strings, no prose, no code fence.

User query: {query}

JSON array:"""


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _cache_key(query: str) -> str:
    return hashlib.sha1(query.strip().lower().encode("utf-8")).hexdigest()[:16]


def _cache_load(query: str) -> list[str] | None:
    path = CACHE_DIR / f"{_cache_key(query)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    ts = data.get("ts", 0)
    if time.time() - ts > CACHE_TTL_DAYS * 86400:
        return None  # expired
    variants = data.get("variants")
    if isinstance(variants, list) and all(isinstance(v, str) for v in variants):
        return variants
    return None


def _cache_store(query: str, variants: list[str]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{_cache_key(query)}.json"
        path.write_text(json.dumps({
            "query": query,
            "variants": variants,
            "ts": int(time.time()),
        }))
    except OSError:
        pass  # cache write failure never breaks the recall path


# ---------------------------------------------------------------------------
# LLM call (injectable for tests)
# ---------------------------------------------------------------------------

# Test-time injection point. Production code leaves this None so we call
# through to the real Anthropic SDK via `brain.auto_extract.call_claude`.
_llm_fn: Callable[[str], str | None] | None = None


def set_llm(fn: Callable[[str], str | None] | None) -> None:
    """Override the LLM backend. Pass `None` to restore the default."""
    global _llm_fn
    _llm_fn = fn


def _default_llm(prompt: str) -> str | None:
    try:
        from brain.auto_extract import call_claude
    except ImportError:
        return None
    return call_claude(prompt, timeout=15)


def _call_llm(prompt: str) -> str | None:
    fn = _llm_fn or _default_llm
    try:
        return fn(prompt)
    except Exception:
        return None


def _parse_variants(raw: str) -> list[str]:
    """Tolerant JSON-array extraction: strip code fences, locate the
    first `[...]` chunk, accept partial failures. Garbage-in returns
    empty list — the caller falls back to `[query]`."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.startswith("```")]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("["), text.rfind("]") + 1
        if s < 0 or e <= s:
            return []
        try:
            data = json.loads(text[s:e])
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    return [str(v).strip() for v in data if isinstance(v, str) and v.strip()]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def expand_query(
    query: str,
    *,
    n: int = 4,
    use_cache: bool = True,
) -> list[str]:
    """Return [original, variant_1, ..., variant_n] — deduped, original first.

    On any failure (disabled, LLM timeout, unparseable output) returns
    `[query]` so callers can always treat the result as a non-empty list.
    """
    q = (query or "").strip()
    if not q:
        return []
    if not _ENABLED:
        return [q]
    if use_cache:
        cached = _cache_load(q)
        if cached is not None:
            return _dedupe_keeping_first([q] + cached)

    prompt = REWRITE_PROMPT.format(n=min(n, MAX_VARIANTS), query=q)
    raw = _call_llm(prompt)
    if not raw:
        return [q]
    variants = _parse_variants(raw)[: MAX_VARIANTS]
    if not variants:
        return [q]
    if use_cache:
        _cache_store(q, variants)
    return _dedupe_keeping_first([q] + variants)


def _dedupe_keeping_first(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


def expanded_hybrid_search(
    query: str,
    k: int = 8,
    type: str | None = None,
    *,
    n_variants: int = 4,
    search_fn: Callable | None = None,
) -> list[dict]:
    """Hybrid search that fans out across query variants and RRF-fuses.

    The `search_fn` hook lets tests drop in a deterministic fake.
    Production callers leave it None so we go through `hybrid_search`.

    Per-variant RRF constant matches the default K=60 used inside
    `hybrid_search`; we keep the same scale so ranks compose cleanly.
    The original query gets a small 1.3x boost so same-intent exact
    hits still win against drifted paraphrases — the rewriter is a
    safety net, not a replacement.
    """
    if search_fn is None:
        search_fn = semantic.hybrid_search

    variants = expand_query(query, n=n_variants)
    if len(variants) <= 1:
        # Fast path: rewriter disabled or LLM returned nothing useful.
        try:
            return search_fn(query, k, type)
        except TypeError:
            return search_fn(query, k)

    K = 60
    pool: dict[tuple, dict] = {}
    scores: dict[tuple, float] = defaultdict(float)

    def _key(hit: dict) -> tuple:
        kind = hit.get("kind", "fact")
        if kind == "note":
            return ("note", hit.get("path") or hit.get("title", ""))
        return ("fact", hit.get("type"), hit.get("slug") or hit.get("name", ""))

    for i, variant in enumerate(variants):
        variant_weight = 1.3 if i == 0 else 1.0  # original > paraphrases
        try:
            hits = search_fn(variant, k * 2, type)
        except TypeError:
            hits = search_fn(variant, k * 2)
        if not hits:
            continue
        for rank, hit in enumerate(hits):
            key = _key(hit)
            if key not in pool:
                pool[key] = dict(hit)
            scores[key] += variant_weight / (K + rank)

    fused = []
    for key, hit in pool.items():
        fused.append({**hit, "rrf": scores[key]})
    fused.sort(key=lambda h: -h["rrf"])
    return fused[:k]
