"""Tests for WS6 `fact_claims` substrate.

Covers:
- schema shape (28 cols + 9 indices, CHECK enforced)
- dual-write gated by BRAIN_USE_CLAIMS
- reading from `facts` is unchanged
- backfill CLI: idempotent, superseded remap, dry-run reporting
"""

from __future__ import annotations

from pathlib import Path

import pytest


EXPECTED_COLUMNS = {
    "id", "entity_id", "subject_slug",
    "predicate", "predicate_key", "predicate_group",
    "object_entity", "object_text", "object_slug", "object_type",
    "text",
    "fact_time", "observed_at",
    "source_kind", "source_path", "source_sha", "scrub_tag", "episode_id",
    "confidence", "risk_level", "trust_source", "salience", "last_accessed",
    "kind", "status", "superseded_by", "superseded_at",
    "claim_key",
}


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    vault.mkdir()
    (vault / "entities" / "people").mkdir(parents=True)

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", vault)
    monkeypatch.setattr(config, "ENTITIES_DIR", vault / "entities")

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", vault / ".brain.db")
    return vault


def _make_entity(vault: Path, slug: str, name: str, facts: list[str]) -> Path:
    p = vault / "entities" / "people" / f"{slug}.md"
    body = f"---\nname: {name}\nslug: {slug}\n---\n\n# {name}\n\n"
    body += "\n".join(facts) + "\n"
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


def test_fact_claims_has_28_columns(tmp_vault):
    from brain import db
    with db.connect() as conn:
        rows = conn.execute("PRAGMA table_info(fact_claims)").fetchall()
    names = {r[1] for r in rows}
    assert names == EXPECTED_COLUMNS
    assert len(names) == 28


def test_fact_claims_has_nine_indices(tmp_vault):
    from brain import db
    with db.connect() as conn:
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='fact_claims' AND name NOT LIKE 'sqlite_autoindex%'"
        ).fetchall()
    idx_names = {r[0] for r in idx}
    assert {"fact_claims_entity_idx",
            "fact_claims_entity_pred_idx",
            "fact_claims_claim_key_idx",
            "fact_claims_status_kind_idx",
            "fact_claims_episode_idx",
            "fact_claims_observed_idx",
            "fact_claims_salience_idx",
            "fact_claims_source_idx",
            "fact_claims_object_entity_idx"} == idx_names


def test_fact_claims_check_constraint_rejects_null_object(tmp_vault):
    import sqlite3
    from brain import db
    _make_entity(tmp_vault, "a", "Ann", ["- x"])
    db.upsert_entity_from_file(tmp_vault / "entities" / "people" / "a.md")
    with db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fact_claims "
                "(entity_id, subject_slug, predicate, predicate_key, text, "
                " observed_at, source_kind, claim_key) "
                "VALUES (1,'a','x','x','t',1.0,'user','k')"
            )


# ---------------------------------------------------------------------------
# Dual-write
# ---------------------------------------------------------------------------


def test_dual_write_off_leaves_fact_claims_empty(tmp_vault, monkeypatch):
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    from brain import db
    path = _make_entity(tmp_vault, "flo", "Flo",
                        ["- currently in Paris (source: user)"])
    db.upsert_entity_from_file(path)
    with db.connect() as conn:
        fact_n = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        claim_n = conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0]
    assert fact_n == 1
    assert claim_n == 0


def test_dual_write_on_populates_fact_claims(tmp_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    from brain import db
    path = _make_entity(
        tmp_vault, "flo", "Flo",
        ["- currently in Paris (source: user)"],
    )
    db.upsert_entity_from_file(path)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT entity_id, subject_slug, predicate, predicate_key, "
            "predicate_group, object_text, object_type, text, "
            "source_kind, trust_source, risk_level, kind, status, "
            "scrub_tag, claim_key "
            "FROM fact_claims"
        ).fetchone()
    assert row is not None
    (entity_id, subject_slug, predicate, predicate_key, predicate_group,
     object_text, object_type, text, source_kind, trust_source,
     risk_level, kind, status, scrub_tag, claim_key) = row
    assert subject_slug == "flo"
    assert predicate == "locatedIn"
    assert predicate_key == "locatedin"
    assert predicate_group == "location"
    assert object_text == "Paris"
    assert object_type == "string"
    assert source_kind == "user"
    assert trust_source == "user"
    assert risk_level == "trusted"           # live dual-write default
    assert scrub_tag == "ws4"                # live-write tag; backfill uses 'pre-ws4'
    assert kind == "episodic"
    assert status == "current"
    assert claim_key  # non-empty sha256 hex


def test_dual_write_unknown_predicate_falls_back_to_unparsed(tmp_vault, monkeypatch):
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    from brain import db
    path = _make_entity(
        tmp_vault, "q", "Q",
        ["- enjoys long walks on the beach (source: user)"],
    )
    db.upsert_entity_from_file(path)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT predicate, predicate_key, predicate_group FROM fact_claims"
        ).fetchone()
    assert row == ("_unparsed", "_unparsed", None)


def test_dual_write_rebuilds_on_reupsert(tmp_vault, monkeypatch):
    """Re-upserting the same entity file wipes + rebuilds fact_claims
    for that entity, keeping symmetry with legacy facts."""
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    from brain import db

    path = _make_entity(tmp_vault, "tom", "Tom",
                        ["- currently in A (source: user)"])
    db.upsert_entity_from_file(path)
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0]
    assert before == 1

    # Replace the fact on disk and re-upsert.
    path.write_text(
        "---\nname: Tom\nslug: tom\n---\n\n# Tom\n\n"
        "- currently in B (source: user)\n- works at Acme (source: user)\n"
    )
    db.upsert_entity_from_file(path)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT predicate, object_text FROM fact_claims ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    preds = {r[0] for r in rows}
    assert preds == {"locatedIn", "worksAt"}


def test_dual_write_claim_key_deterministic(tmp_vault, monkeypatch):
    from brain import db
    k1 = db._claim_key("alice", "locatedin", None, "Paris")
    k2 = db._claim_key("alice", "locatedin", None, "Paris")
    k3 = db._claim_key("alice", "locatedin", None, "Lyon")
    assert k1 == k2
    assert k1 != k3


def test_dual_write_source_parsing(tmp_vault):
    from brain import db
    # Shape mirrors real sources produced by the extractor.
    assert db._parse_source("note:journal/2026-04-23.md") == (
        "note", "journal/2026-04-23.md", "journal/2026-04-23.md",
    )
    assert db._parse_source("session-2026-04-23-abc")[0] == "session"
    assert db._parse_source("user") == ("user", None, None)
    assert db._parse_source("correction-something")[0] == "correction"
    assert db._parse_source(None) == ("import", None, None)
    assert db._parse_source("random-legacy-string")[0] == "import"


def test_facts_table_read_path_unchanged(tmp_vault, monkeypatch):
    """A classic `db.search` call must return identical shape whether
    the flag is on or off — WS6 is additive, not cut-over."""
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    from brain import db
    _make_entity(tmp_vault, "ann", "Ann",
                 ["- currently in Paris (source: user)"])
    db.upsert_entity_from_file(tmp_vault / "entities" / "people" / "ann.md")

    # Shape assertion — keys mirror today's `db.search`.
    rows = db.search("Paris", k=5)
    assert rows
    expected = {"text", "source", "date", "status",
                "type", "name", "slug", "path", "score"}
    assert expected.issubset(rows[0].keys())


def test_use_claims_enabled_read_on_every_call(monkeypatch, tmp_vault):
    from brain import db
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    assert db.use_claims_enabled() is True
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "0")
    assert db.use_claims_enabled() is False


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def test_backfill_dry_run_counts_but_does_not_write(tmp_vault, monkeypatch):
    """Populate facts with BRAIN_USE_CLAIMS=0, then dry-run the backfill."""
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    from brain import db, backfill_facts

    _make_entity(tmp_vault, "ann", "Ann",
                 ["- currently in A (source: user)",
                  "- works at B (source: user)",
                  "- random hobby (source: user)"])
    db.upsert_entity_from_file(tmp_vault / "entities" / "people" / "ann.md")

    summary = backfill_facts.run(apply=False, verbose=False)
    assert summary["inserted"] == 3
    assert summary["already_populated"] is False
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0]
    assert n == 0   # dry-run wrote nothing


def test_backfill_apply_populates_and_marks_pre_ws4(tmp_vault, monkeypatch):
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    from brain import db, backfill_facts

    _make_entity(tmp_vault, "ann", "Ann",
                 ["- currently in A (source: user)",
                  "- enjoys walks (source: user)"])
    db.upsert_entity_from_file(tmp_vault / "entities" / "people" / "ann.md")

    summary = backfill_facts.run(apply=True)
    assert summary["inserted"] == 2
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT predicate, scrub_tag, kind, risk_level, trust_source "
            "FROM fact_claims"
        ).fetchall()
    predicates = {r[0] for r in rows}
    assert predicates == {"locatedIn", "_unparsed"}
    assert all(r[1] == "pre-ws4" for r in rows)
    assert all(r[2] == "semantic" for r in rows)    # D5 decision
    assert all(r[3] == "trusted" for r in rows)
    # Source is 'user' → trust_source='user'
    assert all(r[4] == "user" for r in rows)


def test_backfill_is_idempotent(tmp_vault, monkeypatch):
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    from brain import db, backfill_facts

    _make_entity(tmp_vault, "ann", "Ann",
                 ["- currently in A (source: user)"])
    db.upsert_entity_from_file(tmp_vault / "entities" / "people" / "ann.md")

    first = backfill_facts.run(apply=True)
    second = backfill_facts.run(apply=True)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["already_populated"] is True
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0]
    assert n == 1


def test_backfill_remaps_superseded_by(tmp_vault, monkeypatch):
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    from brain import db, backfill_facts

    # Create entity + seed two facts, then manually mark fact #1 as
    # superseded by fact #2 (mirrors what supersede.py does at runtime).
    path = _make_entity(tmp_vault, "ann", "Ann",
                        ["- currently in A (source: user)",
                         "- currently in B (source: user)"])
    db.upsert_entity_from_file(path)
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM facts ORDER BY id"
        ).fetchall()]
        assert len(ids) == 2
        conn.execute(
            "UPDATE facts SET status='superseded', superseded_by=? WHERE id=?",
            (ids[1], ids[0]),
        )
        # Delete the FTS shadow row for the superseded fact so rebuilds
        # stay consistent (legacy behaviour).
        conn.execute("DELETE FROM fts_facts WHERE rowid=?", (ids[0],))

    summary = backfill_facts.run(apply=True)
    assert summary["facts_superseded"] == 1
    assert summary["superseded_remap"] == 1

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, status, superseded_by FROM fact_claims ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    # Row 1 is superseded; its superseded_by points at row 2's new id.
    assert rows[0][1] == "superseded"
    assert rows[0][2] == rows[1][0]
    assert rows[1][1] == "current"


def test_backfill_entity_resolution_for_object(tmp_vault, monkeypatch):
    """When the object phrase matches an existing entity, the
    backfill links object_entity + object_slug and clears
    object_text (matches the live dual-write shape)."""
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    from brain import db, backfill_facts

    _make_entity(tmp_vault, "paris", "Paris", ["- a city"])
    db.upsert_entity_from_file(tmp_vault / "entities" / "people" / "paris.md")
    _make_entity(tmp_vault, "ann", "Ann",
                 ["- currently in Paris (source: user)"])
    db.upsert_entity_from_file(tmp_vault / "entities" / "people" / "ann.md")

    backfill_facts.run(apply=True)
    with db.connect() as conn:
        ann_row = conn.execute(
            "SELECT fc.object_entity, fc.object_slug, fc.object_text, fc.object_type "
            "FROM fact_claims fc JOIN entities e ON e.id=fc.entity_id "
            "WHERE e.slug='ann'"
        ).fetchone()
    # Ann's "currently in Paris" should resolve to the Paris entity.
    assert ann_row[0] is not None
    assert ann_row[1] == "paris"
    assert ann_row[2] is None
    assert ann_row[3] == "entity"
