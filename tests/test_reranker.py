"""Tests for the LLM reranker.

LLM is stubbed via `reranker.set_llm`. Tests focus on:
  - the disabled path never changes ordering or calls the LLM
  - score parsing tolerance (code fences, prose, out-of-range scores)
  - reorder behaviour (scored candidates outrank unscored, ties break
    on original rank)
  - cache miss/hit/expire/corrupt
  - candidate → prompt-line shaping (fact vs note, truncation)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import reranker


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(reranker, "CACHE_DIR", tmp_path / "cache")
    yield tmp_path / "cache"


@pytest.fixture
def reranker_enabled(monkeypatch):
    monkeypatch.setattr(reranker, "_ENABLED", True)


@pytest.fixture
def stub_llm():
    holder: dict = {"response": None, "calls": 0, "last_prompt": None}

    def fake(prompt: str) -> str | None:
        holder["calls"] += 1
        holder["last_prompt"] = prompt
        return holder["response"]

    reranker.set_llm(fake)
    yield holder
    reranker.set_llm(None)


def _sample_candidates() -> list[dict]:
    return [
        {"kind": "fact", "type": "people", "slug": "son",
         "name": "Son", "text": "Son lives in Long Xuyen"},
        {"kind": "fact", "type": "projects", "slug": "brain",
         "name": "brain", "text": "brain is a personal memory system"},
        {"kind": "note", "path": "son-snippet.md",
         "title": "Son snippet", "snippet": "casual note about Son"},
    ]


# ---------- disabled path ------------------------------------------------


def test_rerank_disabled_returns_slice_without_llm(tmp_cache, stub_llm):
    out = reranker.rerank("where is son", _sample_candidates(), k=2)
    assert [h["slug"] if "slug" in h else h["path"] for h in out[:2]] == ["son", "brain"]
    assert stub_llm["calls"] == 0


def test_rerank_empty_candidates_returns_empty(tmp_cache, reranker_enabled, stub_llm):
    assert reranker.rerank("q", []) == []
    assert stub_llm["calls"] == 0


# ---------- LLM call + reorder ------------------------------------------


def test_rerank_reorders_by_llm_score(tmp_cache, reranker_enabled, stub_llm):
    """LLM says note is most relevant (10), brain-project is least (1).
    The reranker must reorder to note → son → brain."""
    stub_llm["response"] = '{"1": 5, "2": 1, "3": 10}'
    out = reranker.rerank("where is son", _sample_candidates(), k=3)
    ids = [h.get("path") or h.get("slug") for h in out]
    assert ids == ["son-snippet.md", "son", "brain"]
    # Every hit carries the LLM's rerank_score for downstream inspection.
    assert all("rerank_score" in h for h in out)


def test_rerank_unscored_candidates_go_below_scored(
    tmp_cache, reranker_enabled, stub_llm
):
    """Model returns scores for 1 and 3 only — candidate 2 is implicitly
    unscored. Scored wins preserve order; unscored falls to the end with
    rerank_score=None."""
    stub_llm["response"] = '{"1": 3, "3": 8}'
    out = reranker.rerank("q", _sample_candidates(), k=3)
    # Candidate 3 scored 8 → first; candidate 1 scored 3 → second;
    # candidate 2 unscored → last.
    ids = [h.get("path") or h.get("slug") for h in out]
    assert ids == ["son-snippet.md", "son", "brain"]
    assert out[-1]["rerank_score"] is None


def test_rerank_ties_broken_by_original_rank(
    tmp_cache, reranker_enabled, stub_llm
):
    """Equal LLM scores fall back to the hybrid_search order — preserves
    the baseline's tie-breaking rather than arbitrary reshuffling."""
    stub_llm["response"] = '{"1": 7, "2": 7, "3": 7}'
    out = reranker.rerank("q", _sample_candidates(), k=3)
    ids = [h.get("path") or h.get("slug") for h in out]
    # All scored the same → original order preserved.
    assert ids == ["son", "brain", "son-snippet.md"]


def test_rerank_truncates_at_max_n(tmp_cache, reranker_enabled, stub_llm, monkeypatch):
    """More than MAX_RERANK_N candidates → only top-N sent to LLM; the
    overflow is returned after the reranked slice only if k > reranked
    length. Within the reranked slice, ordering reflects LLM scores."""
    monkeypatch.setattr(reranker, "MAX_RERANK_N", 2)
    stub_llm["response"] = '{"1": 1, "2": 9}'
    out = reranker.rerank("q", _sample_candidates(), k=2)
    # Candidate 2 (brain) scored higher than candidate 1 (son).
    ids = [h.get("path") or h.get("slug") for h in out]
    assert ids == ["brain", "son"]


def test_rerank_clamps_out_of_range_scores(
    tmp_cache, reranker_enabled, stub_llm
):
    """Haiku occasionally emits 15, -2, etc. These must be clamped into
    [0, 10] so downstream ordering math behaves."""
    stub_llm["response"] = '{"1": -5, "2": 100, "3": 7}'
    out = reranker.rerank("q", _sample_candidates(), k=3)
    # candidate 2 → 10 (clamped), candidate 3 → 7, candidate 1 → 0 (clamped)
    ids = [h.get("path") or h.get("slug") for h in out]
    assert ids == ["brain", "son-snippet.md", "son"]


# ---------- failure modes degrade gracefully ----------------------------


def test_rerank_llm_none_returns_original_slice(
    tmp_cache, reranker_enabled, stub_llm
):
    stub_llm["response"] = None
    out = reranker.rerank("q", _sample_candidates(), k=2)
    ids = [h.get("path") or h.get("slug") for h in out]
    assert ids == ["son", "brain"]                  # unchanged order


def test_rerank_llm_unparseable_returns_original_slice(
    tmp_cache, reranker_enabled, stub_llm
):
    stub_llm["response"] = "not JSON at all"
    out = reranker.rerank("q", _sample_candidates(), k=2)
    ids = [h.get("path") or h.get("slug") for h in out]
    assert ids == ["son", "brain"]


def test_rerank_llm_empty_json_returns_original_slice(
    tmp_cache, reranker_enabled, stub_llm
):
    stub_llm["response"] = "{}"
    out = reranker.rerank("q", _sample_candidates(), k=2)
    ids = [h.get("path") or h.get("slug") for h in out]
    assert ids == ["son", "brain"]


def test_rerank_handles_code_fenced_json(
    tmp_cache, reranker_enabled, stub_llm
):
    stub_llm["response"] = '```json\n{"1": 9, "2": 1, "3": 5}\n```'
    out = reranker.rerank("q", _sample_candidates(), k=3)
    ids = [h.get("path") or h.get("slug") for h in out]
    assert ids == ["son", "son-snippet.md", "brain"]


def test_rerank_handles_json_with_prose(tmp_cache, reranker_enabled, stub_llm):
    stub_llm["response"] = 'Here are the scores: {"1": 9, "2": 1, "3": 5}'
    out = reranker.rerank("q", _sample_candidates(), k=3)
    ids = [h.get("path") or h.get("slug") for h in out]
    assert ids == ["son", "son-snippet.md", "brain"]


def test_rerank_ignores_out_of_bounds_indices(
    tmp_cache, reranker_enabled, stub_llm
):
    """Model returns scores for indices that don't exist — must not crash."""
    stub_llm["response"] = '{"1": 8, "99": 10, "0": 5}'
    out = reranker.rerank("q", _sample_candidates(), k=3)
    # Only index 1 is valid; 99 and 0 dropped. Candidate 1 ranks first.
    assert out[0]["slug"] == "son"


# ---------- cache -------------------------------------------------------


def test_rerank_caches_scores(tmp_cache, reranker_enabled, stub_llm):
    stub_llm["response"] = '{"1": 9, "2": 3, "3": 5}'
    candidates = _sample_candidates()
    reranker.rerank("q", candidates, k=3)
    reranker.rerank("q", candidates, k=3)
    assert stub_llm["calls"] == 1


def test_rerank_cache_invalidated_by_different_candidates(
    tmp_cache, reranker_enabled, stub_llm
):
    stub_llm["response"] = '{"1": 9, "2": 3}'
    reranker.rerank("q", _sample_candidates()[:2], k=2)
    reranker.rerank("q", _sample_candidates()[:3], k=3)
    assert stub_llm["calls"] == 2                   # different candidate set


def test_rerank_cache_invalidated_by_different_query(
    tmp_cache, reranker_enabled, stub_llm
):
    stub_llm["response"] = '{"1": 9, "2": 3, "3": 5}'
    reranker.rerank("q1", _sample_candidates(), k=3)
    reranker.rerank("q2", _sample_candidates(), k=3)
    assert stub_llm["calls"] == 2


def test_rerank_cache_bypass_flag(tmp_cache, reranker_enabled, stub_llm):
    stub_llm["response"] = '{"1": 9, "2": 3, "3": 5}'
    reranker.rerank("q", _sample_candidates(), k=3, use_cache=True)
    reranker.rerank("q", _sample_candidates(), k=3, use_cache=False)
    assert stub_llm["calls"] == 2


def test_rerank_cache_expired_triggers_refresh(
    tmp_cache, reranker_enabled, stub_llm
):
    stub_llm["response"] = '{"1": 9, "2": 3, "3": 5}'
    candidates = _sample_candidates()
    cache_key = reranker._cache_key("q", candidates[:reranker.MAX_RERANK_N])
    cache_dir = reranker.CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{cache_key}.json").write_text(json.dumps({
        "scores": {"1": 1, "2": 1, "3": 1},
        "ts": 0,                                    # ancient
    }))
    out = reranker.rerank("q", candidates, k=3)
    # Fresh LLM score should have won over the stale cache.
    assert out[0]["slug"] == "son"


def test_rerank_corrupt_cache_recovers(tmp_cache, reranker_enabled, stub_llm):
    stub_llm["response"] = '{"1": 9, "2": 3, "3": 5}'
    candidates = _sample_candidates()
    cache_key = reranker._cache_key("q", candidates[:reranker.MAX_RERANK_N])
    cache_dir = reranker.CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{cache_key}.json").write_text("{broken json")
    out = reranker.rerank("q", candidates, k=3)
    assert out[0]["slug"] == "son"                  # recovered via LLM


# ---------- prompt-line shaping -----------------------------------------


def test_candidate_text_for_fact():
    hit = {"kind": "fact", "type": "people", "name": "Son",
           "text": "Son lives in Long Xuyen"}
    line = reranker._candidate_text(hit)
    assert line.startswith("fact:")
    assert "[people/Son] Son lives in Long Xuyen" in line


def test_candidate_text_for_note():
    hit = {"kind": "note", "title": "Son place", "snippet": "casual note"}
    line = reranker._candidate_text(hit)
    assert line.startswith("note:")
    assert "Son place" in line
    assert "casual note" in line


def test_candidate_text_truncates_long_body():
    hit = {"kind": "fact", "type": "projects", "name": "x",
           "text": "a" * 500}
    line = reranker._candidate_text(hit)
    # Hard cap at ~240 char body + fixed prefix.
    assert len(line) <= 260
    assert line.endswith("...")


def test_candidate_text_flattens_newlines():
    """Prompt lines must not contain raw newlines — the LLM uses
    line-separated numbering for candidate addressing."""
    hit = {"kind": "fact", "type": "x", "name": "y",
           "text": "line one\nline two"}
    line = reranker._candidate_text(hit)
    assert "\n" not in line


# ---------- integration: LLM prompt includes query + candidates ---------


def test_llm_prompt_includes_query_and_numbered_candidates(
    tmp_cache, reranker_enabled, stub_llm
):
    stub_llm["response"] = '{"1": 5, "2": 5, "3": 5}'
    reranker.rerank("where is son", _sample_candidates(), k=3)
    prompt = stub_llm["last_prompt"]
    assert "where is son" in prompt
    assert "1." in prompt and "2." in prompt and "3." in prompt
