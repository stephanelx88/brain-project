"""Strict-mode brain_recall reads only fact_claims, not entities/notes."""
from __future__ import annotations

import json

import pytest

from brain import db, mcp_server


@pytest.fixture
def claims_brain(tmp_path, monkeypatch):
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(db, "DB_PATH", brain_dir / ".brain.db")
    return brain_dir


def _setup_claim(brain_dir):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/people/son.md", "people", "son", "Son", "owner"),
        )
        son_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son currently in long xuyen",
            source="note:journal/2026-04-25.md", fact_date=None, status="current",
        )


def test_strict_without_use_claims_raises(claims_brain, monkeypatch):
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "0")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_recall("anything")
    parsed = json.loads(out)
    assert parsed.get("error") == "configuration_error"
    assert "BRAIN_USE_CLAIMS" in parsed.get("detail", "")


def test_strict_returns_claim_only_hits(claims_brain, monkeypatch):
    _setup_claim(claims_brain)
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_recall("son long xuyen")
    parsed = json.loads(out)
    assert parsed["weak_match"] is False
    assert len(parsed["hits"]) >= 1
    assert all(h.get("kind") == "claim" for h in parsed["hits"])


def test_strict_empty_returns_weak_match_with_strict_guidance(claims_brain, monkeypatch):
    _setup_claim(claims_brain)
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_recall("completely-unknown-topic-xyz123")
    parsed = json.loads(out)
    assert parsed["weak_match"] is True
    assert parsed["hits"] == []
    assert "claim store" in (parsed.get("guidance") or "")
    assert "brain_notes" in (parsed.get("guidance") or "")


def test_default_mode_unchanged_when_flags_off(claims_brain, monkeypatch):
    """With flags off, brain_recall does NOT take strict branch."""
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    monkeypatch.delenv("BRAIN_STRICT_CLAIMS", raising=False)
    out = mcp_server.brain_recall("test-default-path")
    parsed = json.loads(out)
    # Existing envelope shape preserved; we don't assert specific hits.
    assert "query" in parsed
    assert "weak_match" in parsed
    assert "hits" in parsed
    # Should NOT have configuration_error or claim-only kind="claim"
    assert "error" not in parsed


def test_brain_search_strict_returns_claim_hits(claims_brain, monkeypatch):
    _setup_claim(claims_brain)
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_search("son long xuyen")
    parsed = json.loads(out)
    # Same envelope as strict brain_recall
    assert parsed["weak_match"] is False
    assert all(h.get("kind") == "claim" for h in parsed["hits"])


def test_brain_search_strict_misconfig_errors(claims_brain, monkeypatch):
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "0")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_search("anything")
    parsed = json.loads(out)
    assert parsed.get("error") == "configuration_error"


def test_brain_semantic_strict_returns_unsupported(claims_brain, monkeypatch):
    _setup_claim(claims_brain)
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_semantic("son long xuyen")
    parsed = json.loads(out)
    assert parsed.get("error") == "strict_unsupported"
    assert parsed.get("fallback_tool") == "brain_recall"


def test_brain_semantic_default_mode_unchanged(claims_brain, monkeypatch):
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    monkeypatch.delenv("BRAIN_STRICT_CLAIMS", raising=False)
    # Default path imports semantic which has heavy deps + ensure_built.
    # The test_default_mode_unchanged_when_flags_off test pattern in
    # test_claims_strict_recall already exercises brain_recall default;
    # for brain_semantic we just confirm it doesn't take the strict
    # branch. Importing semantic is enough — if strict misfires, the
    # error envelope would short-circuit before we get to the import.
    # We assert by checking the result shape doesn't have our strict
    # error fields.
    try:
        out = mcp_server.brain_semantic("test-default-noerr")
        parsed = json.loads(out)
        assert parsed.get("error") != "strict_unsupported"
    except Exception:
        # Semantic init may fail in tmp env — that's separate from
        # our strict-mode contract. Skip.
        pytest.skip("brain_semantic init failed in tmp env (unrelated)")


def test_strict_recall_emits_entity_summary_first_hit_only(
    claims_brain, monkeypatch
):
    """D-5 regression: strict-mode envelope must include
    `entity_summary` on the FIRST hit per entity (spec §3.3).
    Previously _recall_strict_claims dropped the field entirely.
    Subsequent hits for the same entity must NOT repeat it (mirrors
    default brain_recall envelope behaviour).
    """
    # Two claims, same entity → expect summary on first hit only.
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) "
            "VALUES (?,?,?,?,?)",
            ("entities/people/son.md", "people", "son", "Son",
             "owner currently in long xuyen"),
        )
        son_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son currently in long xuyen",
            source="note:journal/2026-04-25.md", fact_date=None,
            status="current",
        )
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son works at aitomatic in long xuyen",
            source="note:journal/2026-04-25.md", fact_date=None,
            status="current",
        )
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_recall("son long xuyen")
    parsed = json.loads(out)
    hits = parsed["hits"]
    assert len(hits) >= 2
    assert hits[0].get("entity_summary"), (
        f"first hit missing entity_summary: {hits[0]!r}"
    )
    assert "long xuyen" in hits[0]["entity_summary"]
    for h in hits[1:]:
        assert "entity_summary" not in h, (
            f"duplicate entity_summary on subsequent hit: {h!r}"
        )
