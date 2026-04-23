"""Tests for the LLM query rewriter.

Tests never hit the real LLM — `query_rewriter.set_llm` injects a fake
so expansion is deterministic. The real LLM would introduce flakiness
and network dependency; mocking is sufficient because the rewriter's
job is shape-preservation (input query → list of paraphrases) and
fusion math, not LLM intelligence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import query_rewriter


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(query_rewriter, "CACHE_DIR", tmp_path / "cache")
    yield tmp_path / "cache"


@pytest.fixture
def rewriter_enabled(monkeypatch):
    """Enable the rewriter (default is off). Tests that assert the
    disabled-path explicitly unset this."""
    monkeypatch.setattr(query_rewriter, "_ENABLED", True)


@pytest.fixture
def stub_llm(monkeypatch):
    """Install a configurable fake LLM and restore the real one on teardown."""
    holder: dict[str, str | None] = {"response": None, "calls": 0}

    def fake(prompt: str) -> str | None:
        holder["calls"] += 1
        return holder["response"]

    query_rewriter.set_llm(fake)
    yield holder
    query_rewriter.set_llm(None)


# ---------- expand_query --------------------------------------------------


def test_expand_query_disabled_returns_original_only(tmp_cache, stub_llm):
    """When BRAIN_QUERY_REWRITE=0 (default), no LLM call happens and the
    caller sees just [query] — the rewriter is opt-in."""
    stub_llm["response"] = '["paraphrase one", "paraphrase two"]'
    # Do NOT enable: _ENABLED stays False
    out = query_rewriter.expand_query("where is son")
    assert out == ["where is son"]
    assert stub_llm["calls"] == 0


def test_expand_query_returns_original_plus_variants(
    tmp_cache, rewriter_enabled, stub_llm
):
    stub_llm["response"] = json.dumps([
        "son's current location",
        "son ở đâu",
        "where is son right now",
    ])
    out = query_rewriter.expand_query("where is son", n=3)
    assert out[0] == "where is son"              # original first
    assert "son's current location" in out
    assert len(out) == 4                         # original + 3
    # No duplicates across variants.
    assert len(out) == len(set(map(str.lower, out)))


def test_expand_query_empty_input_returns_empty(tmp_cache, rewriter_enabled, stub_llm):
    assert query_rewriter.expand_query("") == []
    assert query_rewriter.expand_query("   ") == []
    assert stub_llm["calls"] == 0


def test_expand_query_falls_back_when_llm_returns_none(
    tmp_cache, rewriter_enabled, stub_llm
):
    stub_llm["response"] = None
    out = query_rewriter.expand_query("where is son")
    assert out == ["where is son"]


def test_expand_query_falls_back_when_llm_returns_garbage(
    tmp_cache, rewriter_enabled, stub_llm
):
    """Non-JSON responses must not crash the recall path — return original."""
    stub_llm["response"] = "here are some thoughts but no JSON"
    out = query_rewriter.expand_query("where is son")
    assert out == ["where is son"]


def test_expand_query_handles_code_fenced_json(
    tmp_cache, rewriter_enabled, stub_llm
):
    """Haiku sometimes wraps JSON in ```json ... ``` — strip and parse."""
    stub_llm["response"] = '```json\n["a", "b"]\n```'
    out = query_rewriter.expand_query("q")
    assert out == ["q", "a", "b"]


def test_expand_query_handles_json_with_prose(
    tmp_cache, rewriter_enabled, stub_llm
):
    """Fallback to the first `[...]` chunk when the model prepends prose."""
    stub_llm["response"] = 'Here you go: ["foo", "bar"] hope that helps'
    out = query_rewriter.expand_query("q")
    assert out == ["q", "foo", "bar"]


def test_expand_query_caps_at_max_variants(
    tmp_cache, rewriter_enabled, stub_llm
):
    stub_llm["response"] = json.dumps([f"v{i}" for i in range(20)])
    out = query_rewriter.expand_query("q", n=99)
    assert len(out) == 1 + query_rewriter.MAX_VARIANTS


def test_expand_query_dedupes_variants_against_original(
    tmp_cache, rewriter_enabled, stub_llm
):
    """Model sometimes echoes the original query in its variants — drop it."""
    stub_llm["response"] = json.dumps([
        "where is son",                           # echo
        "Where Is Son",                           # case-only duplicate
        "son location",
    ])
    out = query_rewriter.expand_query("where is son")
    # Original + 'son location' only — echoes collapse.
    assert out == ["where is son", "son location"]


def test_expand_query_ignores_non_string_list_elements(
    tmp_cache, rewriter_enabled, stub_llm
):
    stub_llm["response"] = json.dumps(["ok", 42, None, "also ok", {"x": 1}])
    out = query_rewriter.expand_query("q")
    assert out == ["q", "ok", "also ok"]


# ---------- cache --------------------------------------------------------


def test_expand_query_caches_and_reuses(tmp_cache, rewriter_enabled, stub_llm):
    stub_llm["response"] = json.dumps(["foo", "bar"])
    out1 = query_rewriter.expand_query("query a")
    out2 = query_rewriter.expand_query("query a")
    assert out1 == out2
    assert stub_llm["calls"] == 1                 # second call served from cache


def test_cache_is_query_specific(tmp_cache, rewriter_enabled, stub_llm):
    stub_llm["response"] = json.dumps(["foo"])
    query_rewriter.expand_query("query a")
    query_rewriter.expand_query("query b")
    assert stub_llm["calls"] == 2


def test_cache_miss_when_use_cache_false(tmp_cache, rewriter_enabled, stub_llm):
    stub_llm["response"] = json.dumps(["foo"])
    query_rewriter.expand_query("q", use_cache=True)
    query_rewriter.expand_query("q", use_cache=False)
    assert stub_llm["calls"] == 2


def test_cache_corrupt_file_falls_back_to_llm(
    tmp_cache, rewriter_enabled, stub_llm, monkeypatch
):
    stub_llm["response"] = json.dumps(["foo"])
    # Write a malformed cache file at the expected location.
    cache_dir = query_rewriter.CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{query_rewriter._cache_key('q')}.json").write_text("{not json")
    out = query_rewriter.expand_query("q")
    assert out == ["q", "foo"]


def test_cache_expired_entry_triggers_refresh(
    tmp_cache, rewriter_enabled, stub_llm
):
    stub_llm["response"] = json.dumps(["fresh"])
    cache_dir = query_rewriter.CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Seed a stale entry (ts far in the past).
    (cache_dir / f"{query_rewriter._cache_key('q')}.json").write_text(json.dumps({
        "query": "q",
        "variants": ["stale"],
        "ts": 0,
    }))
    out = query_rewriter.expand_query("q")
    assert "fresh" in out
    assert "stale" not in out


# ---------- expanded_hybrid_search ---------------------------------------


def test_expanded_search_fast_path_when_no_variants(tmp_cache):
    """Rewriter disabled → expand_query returns [query] → single-call path
    that never touches the fusion loop."""
    calls: list[str] = []

    def fake(q, k, t=None):
        calls.append(q)
        return [{"kind": "fact", "type": "people", "slug": "son"}]

    hits = query_rewriter.expanded_hybrid_search("q", k=5, search_fn=fake)
    assert calls == ["q"]                          # only one call
    assert hits[0]["slug"] == "son"


def test_expanded_search_fans_out_to_all_variants(
    tmp_cache, rewriter_enabled, stub_llm
):
    stub_llm["response"] = json.dumps(["alt one", "alt two"])

    seen: list[str] = []

    def fake(q, k, t=None):
        seen.append(q)
        return [{"kind": "fact", "type": "people", "slug": "son"}]

    query_rewriter.expanded_hybrid_search("q", k=5, search_fn=fake)
    assert seen == ["q", "alt one", "alt two"]     # original + variants


def test_expanded_search_fuses_ranks_across_variants(
    tmp_cache, rewriter_enabled, stub_llm
):
    """A document that lands rank-2 on two variants should beat one that
    lands rank-1 on a single variant, as long as the RRF arithmetic
    adds up. This is the signal the rewriter exists to produce:
    cross-paraphrase consensus."""
    stub_llm["response"] = json.dumps(["variant one", "variant two"])

    def fake(q, k, t=None):
        if q == "q":
            # Original query: only hits "popular" at rank 2.
            return [
                {"kind": "fact", "type": "people", "slug": "niche"},
                {"kind": "fact", "type": "people", "slug": "popular"},
            ]
        # Both paraphrases hit "popular" at rank 1.
        return [{"kind": "fact", "type": "people", "slug": "popular"}]

    fused = query_rewriter.expanded_hybrid_search("q", k=5, search_fn=fake)
    # "popular" accumulates score across all three calls (bonus for rank 1
    # in two variants) and should outrank "niche".
    slugs = [h["slug"] for h in fused]
    assert slugs[0] == "popular"
    assert "niche" in slugs


def test_expanded_search_handles_two_arg_search_fn(
    tmp_cache, rewriter_enabled, stub_llm
):
    """Back-compat: some hybrid-search adapters don't accept a `type`
    argument. expanded_hybrid_search must fall through to (q, k)."""
    stub_llm["response"] = json.dumps(["alt"])
    calls: list[tuple] = []

    def two_arg(q, k):
        calls.append((q, k))
        return [{"kind": "fact", "type": "people", "slug": "son"}]

    query_rewriter.expanded_hybrid_search("q", k=5, search_fn=two_arg)
    assert calls                                    # didn't raise TypeError
    assert all(len(c) == 2 for c in calls)


def test_expanded_search_original_query_outweighs_paraphrases(
    tmp_cache, rewriter_enabled, stub_llm
):
    """Deliberate bias: the original query carries a small weight boost
    so exact-intent matches still win against drift. Simulates each
    variant landing a different doc at rank 1 — the original's pick
    must win."""
    stub_llm["response"] = json.dumps(["para"])

    def fake(q, k, t=None):
        if q == "q":
            return [{"kind": "fact", "type": "people", "slug": "original_pick"}]
        return [{"kind": "fact", "type": "people", "slug": "para_pick"}]

    fused = query_rewriter.expanded_hybrid_search("q", k=5, search_fn=fake)
    # Both hit rank 1 on one variant apiece, but the original gets a boost.
    assert fused[0]["slug"] == "original_pick"


def test_expanded_search_drops_empty_variant_results(
    tmp_cache, rewriter_enabled, stub_llm
):
    """A variant that returns no hits must not crash the fusion — it's
    a null contribution, not a failure."""
    stub_llm["response"] = json.dumps(["dud variant"])

    def fake(q, k, t=None):
        if q == "q":
            return [{"kind": "fact", "type": "people", "slug": "son"}]
        return []                                   # variant misses entirely

    fused = query_rewriter.expanded_hybrid_search("q", k=5, search_fn=fake)
    assert len(fused) == 1
    assert fused[0]["slug"] == "son"
