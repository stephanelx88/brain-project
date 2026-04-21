"""Tests for fact supersession: note > session, newer > older."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    vault.mkdir()
    (vault / "entities" / "people").mkdir(parents=True)

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", vault)
    monkeypatch.setattr(
        config, "ENTITY_TYPES", {"people": vault / "entities" / "people"}
    )

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", vault / ".brain.db")
    return vault


def _write_entity(vault: Path, slug: str, facts: list[str]) -> Path:
    path = vault / "entities" / "people" / f"{slug}.md"
    body = "\n".join(f"- {f}" for f in facts)
    path.write_text(
        f"---\ntype: person\nname: {slug.title()}\n---\n\n"
        f"# {slug.title()}\n\n## Key Facts\n{body}\n"
    )
    return path


def test_classify_predicate_matches_location_phrases():
    from brain.supersede import classify_predicate

    assert classify_predicate("Currently in Cần Thơ") == "location"
    assert classify_predicate("is located in Long Xuyên") == "location"
    assert classify_predicate("đang ở Hà Nội") == "location"
    assert classify_predicate("lives in Hanoi") == "location"


def test_classify_predicate_travel_history_does_not_match():
    from brain.supersede import classify_predicate

    # Travel-history phrasing has no "currently/is-in" cue — must not
    # collide with current-location facts.
    assert classify_predicate(
        "Previously traveled through Switzerland and Austria"
    ) is None


def test_note_source_beats_session_source(tmp_vault):
    from brain import db, supersede

    epath = _write_entity(
        tmp_vault,
        "thuha",
        [
            "Currently in Long Xuyên (source: session-x, 2026-04-21)",
            "Currently in Cần Thơ (source: note:thuha.md, 2026-04-21)",
            "Previously traveled through Switzerland (source: session-x, 2026-04-21)",
        ],
    )
    db.upsert_entity_from_file(epath)

    res = supersede.recompute_for_entity(epath)
    assert res["facts_superseded"] == 1
    assert res["buckets_resolved"] == 1

    body = epath.read_text()
    # Loser (session-sourced Long Xuyên) gets struck through
    assert "~~Currently in Long Xuyên~~" in body
    assert "[superseded" in body
    # Winner (note) stays clean
    assert "- Currently in Cần Thơ" in body
    assert "~~Currently in Cần Thơ~~" not in body
    # Travel history (different bucket) untouched
    assert "- Previously traveled through Switzerland" in body
    assert "~~Previously traveled" not in body


def test_newer_date_wins_among_session_sources(tmp_vault):
    from brain import db, supersede

    epath = _write_entity(
        tmp_vault,
        "annie",
        [
            "Currently in Paris (source: session-a, 2026-03-01)",
            "Currently in Berlin (source: session-b, 2026-04-20)",
        ],
    )
    db.upsert_entity_from_file(epath)

    res = supersede.recompute_for_entity(epath)
    assert res["facts_superseded"] == 1

    body = epath.read_text()
    assert "~~Currently in Paris~~" in body
    assert "- Currently in Berlin" in body
    assert "~~Currently in Berlin~~" not in body


def test_recompute_is_idempotent(tmp_vault):
    from brain import db, supersede

    epath = _write_entity(
        tmp_vault,
        "trinh",
        [
            "Currently in Paris (source: session-a, 2026-03-01)",
            "Currently in Berlin (source: session-b, 2026-04-20)",
        ],
    )
    db.upsert_entity_from_file(epath)

    r1 = supersede.recompute_for_entity(epath)
    assert r1["facts_superseded"] == 1
    body1 = epath.read_text()

    # Second pass on the same file must not add another layer of
    # strikethrough or re-mark already-superseded lines.
    r2 = supersede.recompute_for_entity(epath)
    assert r2["facts_superseded"] == 0
    body2 = epath.read_text()
    assert body1 == body2


def test_singleton_bucket_not_touched(tmp_vault):
    from brain import db, supersede

    # Only one location fact — no contradiction, no change.
    epath = _write_entity(
        tmp_vault,
        "khai",
        [
            "Currently in Saigon (source: session-a, 2026-04-20)",
            "Previously traveled through Japan (source: session-a, 2026-04-20)",
        ],
    )
    db.upsert_entity_from_file(epath)

    res = supersede.recompute_for_entity(epath)
    assert res["facts_superseded"] == 0
    assert "~~" not in epath.read_text()


def test_superseded_fact_is_excluded_from_bm25_search(tmp_vault):
    from brain import db, supersede

    epath = _write_entity(
        tmp_vault,
        "thuha2",
        [
            "Currently in Long Xuyên (source: session-x, 2026-04-21)",
            "Currently in Cần Thơ (source: note:thuha.md, 2026-04-21)",
        ],
    )
    db.upsert_entity_from_file(epath)
    supersede.recompute_for_entity(epath)

    # BM25 search must only return the winner
    hits = db.search("Long")
    loser_hits = [h for h in hits if "Long Xuyên" in h.get("text", "")]
    assert loser_hits == []

    # The superseded row still exists in facts table (audit trail),
    # just not in fts_facts. Confirm via raw count.
    with db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE status='superseded'"
        ).fetchone()[0]
        assert total >= 1


def test_schema_migration_adds_status_columns(tmp_vault):
    from brain import db

    # connect() triggers _migrate, which ALTER-TABLEs the new cols.
    with db.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
        assert "status" in cols
        assert "superseded_by" in cols
        assert "superseded_at" in cols
