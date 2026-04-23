"""Tests for WS7a subject_reject filter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    (vault / "identity").mkdir(parents=True)
    (vault / "entities" / "people").mkdir(parents=True)
    (vault / "entities" / "projects").mkdir(parents=True)
    (vault / ".audit").mkdir()
    # Minimal who-i-am — owner = "stephane".
    (vault / "identity" / "who-i-am.md").write_text(
        "---\ntype: identity\n---\n\n# Who\n\n- Name: stephane\n"
    )

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", vault)
    monkeypatch.setattr(config, "IDENTITY_DIR", vault / "identity")
    monkeypatch.setattr(config, "ENTITIES_DIR", vault / "entities")

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", vault / ".brain.db")

    # Drop the subject_reject module caches so tests see the fresh
    # identity file they just planted.
    from brain import subject_reject
    subject_reject.reset_caches()

    return vault


def _make_entity(vault: Path, type_: str, slug: str, name: str,
                 facts: list[str], aliases: list[str] | None = None) -> Path:
    p = vault / "entities" / type_ / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    header = f"---\nname: {name}\nslug: {slug}\n"
    if aliases:
        header += "aliases: [" + ",".join(aliases) + "]\n"
    header += "---\n\n"
    body = header + f"# {name}\n\n" + "\n".join(facts) + "\n"
    p.write_text(body)

    from brain import db
    db.upsert_entity_from_file(p)
    return p


# ---------------------------------------------------------------------------
# Parser — proper-noun path
# ---------------------------------------------------------------------------


def test_proper_noun_wins(tmp_vault):
    from brain import subject_reject
    _make_entity(tmp_vault, "people", "trinh", "Trinh",
                 ["- fact about trinh"])
    hint = subject_reject.parse_query_subject("where is Trinh?")
    assert hint.subject_slug == "trinh"
    assert hint.subject_type == "people"
    assert hint.source == "proper_noun"
    assert hint.confidence == 1.0


def test_owner_self_reference_treated_as_possessive(tmp_vault):
    """'stephane ...' by the owner is the *owner's voice*, not a
    proper-noun search. Critical carve-out."""
    from brain import subject_reject
    _make_entity(tmp_vault, "people", "stephane", "stephane",
                 ["- owner fact"])
    hint = subject_reject.parse_query_subject("what did stephane eat today")
    assert hint.subject_slug == "stephane"
    assert hint.source == "possessive"


def test_longest_match_wins(tmp_vault):
    """'Long Xuyên' beats 'Long' alone."""
    from brain import subject_reject
    _make_entity(tmp_vault, "projects", "long", "Long", ["- short"])
    _make_entity(tmp_vault, "projects", "long-xuyen", "Long Xuyen",
                 ["- longer"])
    hint = subject_reject.parse_query_subject("tell me about Long Xuyen")
    assert hint.subject_slug == "long-xuyen"


def test_multi_subject_is_inert(tmp_vault):
    """Two different proper nouns → filter stays inert, ranker decides."""
    from brain import subject_reject
    _make_entity(tmp_vault, "people", "trinh", "Trinh", ["- x"])
    _make_entity(tmp_vault, "people", "thuha", "Thuha", ["- y"])
    hint = subject_reject.parse_query_subject("Thuha and Trinh ate where")
    assert hint.subject_slug is None
    assert hint.ambiguous is True
    assert hint.source == "multi_subject"


def test_alias_match_resolves_to_canonical_slug(tmp_vault):
    """Query mentions an alias; parser returns the canonical entity slug."""
    from brain import subject_reject
    _make_entity(tmp_vault, "people", "stephane", "stephane",
                 ["- owner fact"], aliases=["son"])
    # "son" is an alias of the owner → still owner voice.
    hint = subject_reject.parse_query_subject("son ăn gì hôm qua")
    assert hint.subject_slug == "stephane"
    assert hint.source == "possessive"


# ---------------------------------------------------------------------------
# Parser — possessive path
# ---------------------------------------------------------------------------


def test_vietnamese_possessive_hits_owner(tmp_vault):
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("đôi dép tôi đâu?")
    assert hint.subject_slug == "stephane"
    assert hint.source == "possessive"


def test_english_possessive_hits_owner(tmp_vault):
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("where are my slippers")
    assert hint.subject_slug == "stephane"
    assert hint.source == "possessive"


def test_chinese_possessive_hits_owner(tmp_vault):
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("我的钥匙在哪")
    assert hint.subject_slug == "stephane"
    assert hint.source == "possessive"


def test_no_subject_generic_query(tmp_vault):
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("how does TCP work")
    assert hint.subject_slug is None
    assert hint.source == "none"


def test_empty_query_returns_none(tmp_vault):
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("")
    assert hint.subject_slug is None


def test_possessive_overrides_when_proper_noun_is_owner(tmp_vault):
    """'stephane' as the only proper-noun hit should still register as
    possessive voice (owner carve-out)."""
    from brain import subject_reject
    _make_entity(tmp_vault, "people", "stephane", "stephane",
                 ["- me"])
    hint = subject_reject.parse_query_subject("stephane nơi ở")
    assert hint.source == "possessive"


# ---------------------------------------------------------------------------
# Possessive overrides file
# ---------------------------------------------------------------------------


def test_possessives_override_jsonl_layers_defaults(tmp_vault):
    from brain import subject_reject

    # Plant an override that adds a new Esperanto row.
    override = tmp_vault / "identity" / "possessives.jsonl"
    override.write_text(
        json.dumps({"lang": "eo", "pronouns": ["mi"],
                    "possessive_particles": ["mia"]}) + "\n"
    )
    subject_reject.reset_caches()

    hint = subject_reject.parse_query_subject("mia ŝuoj kie estas")
    assert hint.source == "possessive"


def test_malformed_possessives_jsonl_ignored(tmp_vault):
    from brain import subject_reject
    (tmp_vault / "identity" / "possessives.jsonl").write_text("{not json\n")
    subject_reject.reset_caches()
    # Defaults still load → English 'my' still triggers.
    hint = subject_reject.parse_query_subject("where are my shoes")
    assert hint.source == "possessive"


# ---------------------------------------------------------------------------
# Filter behaviour
# ---------------------------------------------------------------------------


def test_filter_drops_mismatched_subject(tmp_vault):
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("đôi dép tôi đâu?")
    hits = [
        {"kind": "fact", "slug": "thuha", "text": "Thuha in LX"},
        {"kind": "fact", "slug": "stephane", "text": "my dép in bedroom"},
    ]
    kept = subject_reject.filter_hits(hits, hint, query="đôi dép tôi đâu?")
    assert len(kept) == 1
    assert kept[0]["slug"] == "stephane"


def test_filter_passthrough_when_hint_has_no_subject(tmp_vault):
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("how does TCP work")
    hits = [{"kind": "fact", "slug": "trinh", "text": "x"}]
    assert subject_reject.filter_hits(hits, hint) == hits


def test_filter_passes_notes(tmp_vault):
    """Notes have no subject_slug; filter must never drop them."""
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("đôi dép tôi đâu?")
    hits = [
        {"kind": "note", "path": "journal/2026-04-23.md", "text": "n/a"},
        {"kind": "fact", "slug": "thuha", "text": "wrong"},
    ]
    kept = subject_reject.filter_hits(hits, hint)
    assert any(h.get("kind") == "note" for h in kept)
    assert all(h.get("slug") != "thuha" for h in kept if h.get("kind") == "fact")


def test_filter_passes_hit_without_slug(tmp_vault):
    """Legacy / pre-WS6 rows without subject_slug PASS (conservative)."""
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("đôi dép tôi đâu?")
    hits = [{"kind": "fact", "text": "something"}]  # no slug
    kept = subject_reject.filter_hits(hits, hint)
    assert kept == hits


def test_filter_alias_match_passes(tmp_vault):
    """Hit tagged with an alias passes when the canonical slug matches
    the query subject."""
    from brain import subject_reject
    _make_entity(tmp_vault, "people", "stephane", "stephane",
                 ["- fact"], aliases=["son"])
    hint = subject_reject.parse_query_subject("what did I eat")
    # Hit slug is the alias "son" — must still pass.
    hits = [{"kind": "fact", "slug": "son", "text": "son ate pho"}]
    assert subject_reject.filter_hits(hits, hint) == hits


def test_filter_writes_audit_jsonl_on_reject(tmp_vault):
    from brain import subject_reject
    hint = subject_reject.parse_query_subject("đôi dép tôi đâu?")
    hits = [{"kind": "fact", "slug": "thuha", "text": "wrong subject"}]
    subject_reject.filter_hits(hits, hint, query="đôi dép tôi đâu?")
    audit = tmp_vault / ".audit" / "subject_reject.jsonl"
    assert audit.exists()
    row = json.loads(audit.read_text().splitlines()[-1])
    assert row["query"] == "đôi dép tôi đâu?"
    assert row["query_subject_slug"] == "stephane"
    assert row["hit_subject_slug"] == "thuha"
    assert row["reason"] == "subject_mismatch"


# ---------------------------------------------------------------------------
# hybrid_search integration
# ---------------------------------------------------------------------------


def test_hybrid_search_filter_off_by_default(tmp_vault, monkeypatch):
    """Baseline: with the flag off, hybrid_search returns all hits,
    including wrong-subject ones. Must hold so the gated rollout is safe."""
    monkeypatch.delenv("BRAIN_SUBJECT_REJECT", raising=False)
    from brain import db, semantic
    _make_entity(tmp_vault, "people", "stephane", "stephane",
                 ["- owner fact"])
    _make_entity(tmp_vault, "people", "thuha", "Thuha",
                 ["- Thuha is in LX dep"])
    # Use db.search directly (hybrid_search's BM25 branch) to avoid
    # needing the semantic index.
    hits = db.search("dep", k=10)
    # Without the flag, the Thuha fact still surfaces.
    slugs = {h["slug"] for h in hits}
    assert "thuha" in slugs


def test_hybrid_search_filter_on_drops_mismatched(tmp_vault, monkeypatch):
    """With the flag on and an owner-possessive query, Thuha facts must
    not survive hybrid_search. This is the payoff for WS7a."""
    monkeypatch.setenv("BRAIN_SUBJECT_REJECT", "1")
    # Disable semantic probe — FTS-only path is enough.
    monkeypatch.setenv("BRAIN_WARMUP", "0")
    from brain import semantic, subject_reject
    _make_entity(tmp_vault, "people", "stephane", "stephane",
                 ["- my dép in bedroom"])
    _make_entity(tmp_vault, "people", "thuha", "Thuha",
                 ["- Thuha took the dép"])

    # Stub the semantic/note branches so the test doesn't need the
    # embedding model; we exercise the filter on the BM25-only path.
    monkeypatch.setattr(semantic, "search_facts", lambda q, k=8, type=None: [])
    monkeypatch.setattr(semantic, "search_notes", lambda q, k=8: [])
    # BM25 notes branch reads from db.search_notes; return []
    from brain import db
    monkeypatch.setattr(db, "search_notes", lambda q, k=8: [])

    subject_reject.reset_caches()
    hits = semantic.hybrid_search("đôi dép tôi đâu?", k=10)
    slugs = {h["slug"] for h in hits if h.get("kind") == "fact"}
    assert "thuha" not in slugs
    # The owner's dép fact is still reachable.
    assert slugs == {"stephane"} or slugs == set()


def test_hybrid_search_no_subject_is_inert(tmp_vault, monkeypatch):
    """Generic query with the flag on must behave identically to the
    flag-off path."""
    monkeypatch.setenv("BRAIN_SUBJECT_REJECT", "1")
    from brain import semantic
    _make_entity(tmp_vault, "people", "trinh", "Trinh",
                 ["- Trinh likes xyzzy"])

    monkeypatch.setattr(semantic, "search_facts", lambda q, k=8, type=None: [])
    monkeypatch.setattr(semantic, "search_notes", lambda q, k=8: [])
    from brain import db
    monkeypatch.setattr(db, "search_notes", lambda q, k=8: [])

    from brain import subject_reject
    subject_reject.reset_caches()
    hits = semantic.hybrid_search("xyzzy", k=10)
    # No subject parsed → Trinh fact survives.
    assert any(h.get("slug") == "trinh" for h in hits)
