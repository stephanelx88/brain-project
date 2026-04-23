"""Tests for db.search — supersession filter, slug field, type filter.

The hybrid recall path relies on db.search dropping superseded facts,
otherwise a BM25-heavy query can surface obsolete rows above the
current fact. The semantic branch already filters in
`semantic.build()`; this test locks the BM25 branch to the same
behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_brain_db(tmp_path, monkeypatch):
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", brain_dir / ".brain.db")

    # Seed 2 entities + 3 facts: one active, one superseded.
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/people/son.md", "people", "son", "Son", "owner"),
        )
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/people/ray.md", "people", "ray", "Ray", "colleague"),
        )
        # Active fact
        conn.execute(
            "INSERT INTO facts (entity_id, text, source, status) VALUES (?, ?, ?, ?)",
            (1, "Son currently lives in Can Tho", "note:son-2026.md", None),
        )
        # Superseded fact (stale location — the one we DON'T want to surface)
        conn.execute(
            "INSERT INTO facts (entity_id, text, source, status) VALUES (?, ?, ?, ?)",
            (1, "Son lives in Long Xuyen", "note:son-2024.md", "superseded"),
        )
        # Active fact on a different entity
        conn.execute(
            "INSERT INTO facts (entity_id, text, source, status) VALUES (?, ?, ?, ?)",
            (2, "Ray works on embeddings", "session", None),
        )
        for rowid in (1, 2, 3):
            text = conn.execute(
                "SELECT text FROM facts WHERE id=?", (rowid,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO fts_facts (rowid, text, source) VALUES (?, ?, ?)",
                (rowid, text, "src"),
            )

    return brain_dir


def test_search_excludes_superseded_by_default(tmp_brain_db):
    from brain import db
    hits = db.search("Son lives", k=10)
    # Only the Cần Thơ fact (active) — the Long Xuyên one is superseded.
    texts = [h["text"] for h in hits]
    assert "Son currently lives in Can Tho" in texts
    assert "Son lives in Long Xuyen" not in texts


def test_search_include_superseded_returns_both(tmp_brain_db):
    """History / audit lookups explicitly need the obsolete rows."""
    from brain import db
    hits = db.search("Son lives", k=10, include_superseded=True)
    texts = {h["text"] for h in hits}
    assert "Son currently lives in Can Tho" in texts
    assert "Son lives in Long Xuyen" in texts


def test_search_carries_slug_field(tmp_brain_db):
    """Hybrid fusion identifies facts by (type, slug) — the BM25 branch
    previously only returned (type, name) and silently fell back to the
    name when slug was missing. Make sure slug is present."""
    from brain import db
    hits = db.search("Son lives", k=10)
    assert hits
    assert all("slug" in h for h in hits)
    assert any(h["slug"] == "son" for h in hits)


def test_search_carries_status_field(tmp_brain_db):
    """Downstream rankers may want to see status (None vs 'superseded')
    even though superseded rows don't surface by default — a future
    `include_superseded=True` caller benefits from having the raw flag."""
    from brain import db
    hits = db.search("Son lives", k=10)
    assert all("status" in h for h in hits)
    # Current-only filter → all returned rows are active.
    assert all(h["status"] is None for h in hits)


def test_search_type_filter_still_works(tmp_brain_db):
    from brain import db
    hits = db.search("lives OR works", k=10, type="people")
    assert all(h["type"] == "people" for h in hits)


def test_search_type_filter_combined_with_superseded_filter(tmp_brain_db):
    from brain import db
    hits = db.search("Son lives", k=10, type="people")
    # Type matches, superseded still excluded.
    assert len(hits) == 1
    assert hits[0]["status"] is None
