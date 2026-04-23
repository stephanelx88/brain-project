"""MCP response projection — compact / verbose / debug tiers.

Shared shape for every agent-facing MCP tool (`brain_recall`,
`brain_search`, `brain_semantic`, `brain_entities`, `brain_notes`).

Token cost is the dominant UX metric for MCP: every tool round-trip
lands as raw bytes in the caller's context. Pre-WS2 a single
`brain_recall` hit carried ~12 fields at JSON indent=2, and
`entity_summary` was duplicated per fact rather than emitted once per
entity. The projection here trades three things for bytes: (a) tiers
(the agent opts into detail with `verbose=True` / `debug=True`), (b) a
snippet cap (env-override `BRAIN_RECALL_SNIPPET_CHARS`, clamped
[80, 2000]), and (c) envelope-layer canonical-fact-hash dedup.

Spec source: Ontologist 2026-04-23 15:15 chat.md post. Keep this
module and that post in sync.
"""

from __future__ import annotations

import os

from brain import db


DEFAULT_SNIPPET_CHARS = 240
_SNIPPET_MIN = 80
_SNIPPET_MAX = 2000
_ELLIPSIS = "…"


def snippet_cap() -> int:
    """Effective snippet cap for this process.

    Resolved from `BRAIN_RECALL_SNIPPET_CHARS` at call time (not import
    time) so tests can override via monkeypatch without reloading.
    Invalid or out-of-range values fall back to the clamp boundary.
    """
    raw = os.environ.get("BRAIN_RECALL_SNIPPET_CHARS")
    if not raw:
        return DEFAULT_SNIPPET_CHARS
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return DEFAULT_SNIPPET_CHARS
    return max(_SNIPPET_MIN, min(n, _SNIPPET_MAX))


def default_verbose() -> bool:
    """Migration-grace env switch: set `BRAIN_MCP_DEFAULT_VERBOSE=1` to
    restore the pre-WS2 verbose shape as the default for every tool.
    Lets an older caller keep working without upgrading its call site.
    """
    return os.environ.get("BRAIN_MCP_DEFAULT_VERBOSE", "0") == "1"


def truncate(text: str, cap: int) -> tuple[str, bool]:
    """Return `(truncated_text, was_truncated)`.

    Truncation rule (per Ontologist spec §4):
      - If `len(text) <= cap`, return as-is.
      - Prefer cutting at the last whitespace at position ≤ cap.
      - If no whitespace within 40 chars of the cap, hard-cut at cap.
      - Append U+2026 ellipsis after the cut.

    An empty / None input returns ("", False) — the caller never has to
    branch on the absence of text.
    """
    if not text:
        return "", False
    if len(text) <= cap:
        return text, False
    window = text[:cap]
    idx = window.rfind(" ")
    if idx < cap - 40 or idx <= 0:
        # No whitespace close to the cap — hard-cut at cap.
        return text[:cap].rstrip() + _ELLIPSIS, True
    return text[:idx].rstrip() + _ELLIPSIS, True


def _normalise_kind(hit: dict) -> str:
    """Some upstream callers (db.search_notes) omit `kind`. Infer it so
    the projection can route per-kind fields deterministically."""
    kind = hit.get("kind")
    if kind in ("fact", "note"):
        return kind
    # db.search_notes returns {title, path, body, mtime, snippet, score}
    # — no `type/name/slug`. Detect by shape.
    if "snippet" in hit and "type" not in hit:
        return "note"
    return "fact"


def _text_for(hit: dict, kind: str) -> str:
    """Best-effort text for the projection.

    Fact hits carry `text`. Note hits from the hybrid_search path carry
    `snippet`; note hits from the db.search_notes path carry both
    `snippet` and `body`. Prefer `text`, then `snippet`, then `body`.
    """
    for field in ("text", "snippet", "body"):
        v = hit.get(field)
        if v:
            return v
    return ""


def project_hits(
    raw_hits: list[dict],
    *,
    k: int,
    verbose: bool = False,
    debug: bool = False,
    cap: int | None = None,
) -> list[dict]:
    """Shape raw hybrid_search / db.search output into the MCP envelope
    hits list.

    In-order pipeline:
      1. Canonical-fact-hash dedup across fact-kind hits; notes bypass.
         Dropped dupes DO NOT consume the k budget (per spec §5).
      2. entity_summary first-per-(type, name) rule.
      3. Snippet cap (default tier only); verbose/debug get full text.
      4. Field projection per tier.
      5. Stop at `k` emitted hits.

    Ordering of the input list is preserved — the caller ranks first
    (BM25+semantic RRF, optional LLM reranker), projection does not
    resort.

    `cap=None` reads `snippet_cap()` once per call.
    """
    if cap is None:
        cap = snippet_cap()
    if debug:
        verbose = True  # debug implies verbose (per spec)

    seen_hashes: set[str] = set()
    seen_entities: set[tuple[str, str]] = set()
    out: list[dict] = []

    for hit in raw_hits:
        kind = _normalise_kind(hit)
        text = _text_for(hit, kind)

        if kind == "fact" and text:
            key = db.canonical_fact_hash(text)
            if key in seen_hashes:
                continue
            seen_hashes.add(key)

        projected = _project_one(
            hit, kind=kind, text=text,
            verbose=verbose, debug=debug,
            seen_entities=seen_entities, cap=cap,
        )
        out.append(projected)
        if len(out) >= k:
            break
    return out


def _project_one(
    hit: dict, *,
    kind: str,
    text: str,
    verbose: bool,
    debug: bool,
    seen_entities: set[tuple[str, str]],
    cap: int,
) -> dict:
    # Path is the citation anchor — always present. For fact hits we
    # reconstruct from type+slug when upstream dropped it (db.search path
    # does; semantic.search_facts doesn't always).
    path = hit.get("path")
    if not path and kind == "fact":
        t, s = hit.get("type"), hit.get("slug")
        if t and s:
            path = f"entities/{t}/{s}.md"
    result: dict = {"kind": kind}
    if path:
        result["path"] = path

    # Text field. In default tier we cap; in verbose/debug we emit full.
    if verbose:
        result["text"] = text
    else:
        truncated_text, was_truncated = truncate(text, cap)
        result["text"] = truncated_text
        if was_truncated:
            result["text_truncated"] = True

    if kind == "fact":
        name = hit.get("name")
        if name:
            result["name"] = name
        entity_type = hit.get("type")
        # First-per-entity rule for summary, regardless of tier.
        entity_key = (entity_type, name) if entity_type and name else None
        if entity_key and entity_key not in seen_entities:
            seen_entities.add(entity_key)
            summary = hit.get("entity_summary") or hit.get("summary")
            if summary:
                result["entity_summary"] = summary

    if verbose:
        if kind == "fact":
            for field in ("type", "slug"):
                v = hit.get(field)
                if v:
                    result[field] = v
            source = hit.get("source")
            if source:
                result["source"] = source
            date = hit.get("date")
            if date:
                result["date"] = date
            status = hit.get("status")
            # Only surface when non-current. "current" / None / empty → omit.
            if status and status != "current":
                result["status"] = status
        else:
            # Notes don't carry type/slug/source/date/status, but they do
            # carry title + mtime when sourced from db.search_notes.
            for field in ("title", "mtime"):
                v = hit.get(field)
                if v:
                    result[field] = v

    if debug:
        for field in ("score", "rrf", "lexical_rank", "semantic_rank", "sem_score"):
            if field in hit and hit[field] is not None:
                result[field] = hit[field]

    return result


def envelope(
    query: str,
    hits: list[dict],
    *,
    weak_match: bool,
    guidance: str | None,
    debug: bool = False,
    top_score: float | None = None,
    threshold: float | None = None,
    fetch_k: int | None = None,
    rerank_on: bool | None = None,
    query_rewriter_on: bool | None = None,
) -> dict:
    """Build the envelope dict for a projected hits list.

    Default shape: `{query, weak_match, guidance, hits}`.
    Debug adds: `{top_score, threshold, fetch_k, rerank_on, query_rewriter_on}`.

    `guidance=None` is kept — callers MUST see the key so they can
    distinguish "no guidance" from "forgot to set it".
    """
    out: dict = {
        "query": query,
        "weak_match": weak_match,
        "guidance": guidance,
        "hits": hits,
    }
    if debug:
        out["top_score"] = top_score
        out["threshold"] = threshold
        out["fetch_k"] = fetch_k
        out["rerank_on"] = rerank_on
        out["query_rewriter_on"] = query_rewriter_on
    return out
