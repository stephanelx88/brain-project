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


# --- A-11: pin search_text scoring order across competing matches ---


@pytest.fixture
def ordering_brain(tmp_path, monkeypatch):
    """Tmp BRAIN_DIR with two entities whose claims tie on token overlap
    so other scoring components decide ordering."""
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
            ("entities/people/alice.md", "people", "alice", "Alice", "person"),
        )
        alice_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"brain_dir": brain_dir, "son_id": son_id, "alice_id": alice_id}


def _seed_claim(entity_id, subject_slug, text, observed_at, salience):
    """Insert one current claim then patch observed_at + salience to
    deterministic test values."""
    with db.connect() as conn:
        db._insert_fact_claim(
            conn, entity_id=entity_id, subject_slug=subject_slug,
            text=text, source="note:journal/x.md", fact_date=None,
            status="current",
        )
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE fact_claims SET observed_at=?, salience=? WHERE id=?",
            (observed_at, salience, cid),
        )
        conn.commit()
    return cid


def test_search_text_ranks_subject_match_highest(ordering_brain):
    """Query 'son' against two entities with identical-cardinality token
    overlap: the subject_slug='son' claim must rank first because the
    subject_match boost (+1.0) outweighs all other components."""
    import time as _t
    now = _t.time()
    # Both claims contain "son" once in their text. Only the first
    # has subject_slug == "son", so subject_match fires only there.
    _seed_claim(
        ordering_brain["son_id"], "son",
        "son in saigon",
        observed_at=now, salience=0.3,
    )
    _seed_claim(
        ordering_brain["alice_id"], "alice",
        "alice in long_xuyen mentions son once",
        observed_at=now, salience=0.3,
    )
    hits = read.search_text("son", k=8)
    assert len(hits) >= 2
    # subject_match boost (+1.0) must dominate -> son first.
    assert hits[0].path.endswith("son.md"), (
        f"expected son entity first, got {hits[0].path}"
    )


def test_search_text_ranks_recency_when_tokens_equal(ordering_brain):
    """Two claims, identical token overlap (both contain query term),
    same salience, neither hits subject_match — only observed_at differs.
    The newer claim must rank first."""
    import time as _t
    now = _t.time()
    older = _seed_claim(
        ordering_brain["alice_id"], "alice",
        "alice mentions widget once",
        observed_at=now - 90 * 86400,  # 90 days old
        salience=0.3,
    )
    newer = _seed_claim(
        ordering_brain["alice_id"], "alice",
        "alice notes widget today",
        observed_at=now,  # fresh
        salience=0.3,
    )
    hits = read.search_text("widget", k=8)
    assert len(hits) >= 2
    top_ids = [h.claim_id for h in hits]
    assert top_ids[0] == newer, (
        f"expected newer claim first, got order={top_ids} "
        f"(older={older}, newer={newer})"
    )


def test_search_text_ranks_salience_when_recency_equal(ordering_brain):
    """Two claims, identical token overlap, same observed_at, neither
    hits subject_match — only salience differs. The higher-salience
    claim must rank first."""
    import time as _t
    now = _t.time()
    low = _seed_claim(
        ordering_brain["alice_id"], "alice",
        "alice and widget appear together once",
        observed_at=now, salience=0.1,
    )
    high = _seed_claim(
        ordering_brain["alice_id"], "alice",
        "alice mentions widget prominently",
        observed_at=now, salience=0.9,
    )
    hits = read.search_text("widget", k=8)
    assert len(hits) >= 2
    top_ids = [h.claim_id for h in hits]
    assert top_ids[0] == high, (
        f"expected high-salience claim first, got order={top_ids} "
        f"(low={low}, high={high})"
    )
