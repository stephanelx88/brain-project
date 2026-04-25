"""Claim read API — current(), lookup(), search_text()."""
from __future__ import annotations

import pytest

from brain import db
from brain.claims import read


@pytest.fixture
def claims_brain(tmp_path, monkeypatch):
    """Tmp BRAIN_DIR with a couple entities + claims pre-seeded."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(db, "DB_PATH", brain_dir / ".brain.db")

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/people/son.md", "people", "son", "Son", "owner"),
        )
        son_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/organizations/aitomatic.md", "organizations",
             "aitomatic", "Aitomatic", "employer"),
        )
        # Two current claims about son
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son currently in long xuyen",
            source="note:journal/2026-04-25.md", fact_date=None, status="current",
        )
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son works at Aitomatic",
            source="note:journal/2026-04-25.md", fact_date=None, status="current",
        )
        # One superseded claim
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son was in saigon",
            source="note:journal/2026-04-23.md", fact_date=None, status="superseded",
        )
    return brain_dir


def test_current_returns_only_current_status(claims_brain):
    claims = read.current(subject_slug="son")
    statuses = {c.status for c in claims}
    assert statuses == {"current"}
    assert len(claims) == 2


def test_current_filtered_by_predicate_key(claims_brain):
    claims = read.current(subject_slug="son", predicate_key="locatedin")
    assert len(claims) == 1
    assert "long xuyen" in claims[0].text


def test_lookup_by_id(claims_brain):
    all_claims = read.current(subject_slug="son")
    cid = all_claims[0].id
    fetched = read.lookup(cid)
    assert fetched is not None
    assert fetched.id == cid


def test_lookup_returns_none_for_missing(claims_brain):
    assert read.lookup(99999) is None


def test_search_text_finds_subject_match(claims_brain):
    hits = read.search_text("son long xuyen", k=8)
    assert len(hits) >= 1
    top = hits[0]
    assert "son" in top.path
    assert "long xuyen" in top.text.lower()


def test_search_text_returns_empty_on_no_match(claims_brain):
    hits = read.search_text("completely-unrelated-noun", k=8)
    assert hits == []


def test_search_text_excludes_superseded(claims_brain):
    hits = read.search_text("saigon", k=8)
    assert all("saigon" not in h.text.lower() for h in hits)


def test_search_text_respects_k_limit(claims_brain):
    hits = read.search_text("son", k=1)
    assert len(hits) <= 1
