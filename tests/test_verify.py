"""Tests for brain.verify: gc_orphaned_entities, find_stale_provenance, verify.gc/stale."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    vault.mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", vault)
    monkeypatch.setattr(config, "ENTITIES_DIR", vault / "entities")
    monkeypatch.setattr(config, "INDEX_FILE", vault / "index.md")
    monkeypatch.setattr(config, "LOG_FILE", vault / "log.md")
    monkeypatch.setattr(config, "RAW_DIR", vault / "raw")
    config.ensure_dirs()

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", vault / ".brain.db")

    import brain.git_ops as git_ops
    monkeypatch.setattr(git_ops, "commit", lambda *a, **kw: True)

    return vault


def _write_entity(vault: Path, type_: str, slug: str, body: str = "") -> Path:
    d = vault / "entities" / type_
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.md"
    p.write_text(
        f"---\ntype: {type_}\nname: {slug}\nstatus: current\n"
        f"first_seen: 2026-04-22\nlast_updated: 2026-04-22\nsource_count: 1\n---\n"
        f"## Key Facts\n{body}\n"
    )
    return p


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# gc_orphaned_entities
# ---------------------------------------------------------------------------

def test_gc_removes_phantom_entries(tmp_vault):
    from brain import db

    p = _write_entity(tmp_vault, "people", "alice", "- Alice exists (source: test, 2026-04-22)\n")
    db.upsert_entity_from_file(p)

    # Confirm it's indexed
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 1

    # Delete the file without going through the DB
    p.unlink()

    # GC should clean the orphan
    removed = db.gc_orphaned_entities()
    assert len(removed) == 1
    assert "people/alice.md" in removed[0]

    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 0


def test_gc_leaves_existing_entities_untouched(tmp_vault):
    from brain import db

    p = _write_entity(tmp_vault, "people", "bob", "- Bob is here (source: test, 2026-04-22)\n")
    db.upsert_entity_from_file(p)

    removed = db.gc_orphaned_entities()
    assert removed == []

    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 1


def test_gc_empty_db_is_noop(tmp_vault):
    from brain import db
    assert db.gc_orphaned_entities() == []


# ---------------------------------------------------------------------------
# record_fact_provenance with source_sha
# ---------------------------------------------------------------------------

def test_provenance_stores_source_sha(tmp_vault):
    from brain import db, ingest_notes

    note_text = "# Alice\nAlice lives in Hanoi."
    note_path = tmp_vault / "alice.md"
    note_path.write_text(note_text)
    ingest_notes.ingest_all()

    entity_p = _write_entity(tmp_vault, "people", "alice")
    sha = _sha(note_text)

    db.record_fact_provenance(entity_p, "Alice lives in Hanoi", ["alice.md"],
                              source_sha=sha)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT source_sha FROM fact_provenance WHERE note_path='alice.md'"
        ).fetchone()
    assert row is not None
    assert row[0] == sha


def test_provenance_null_source_sha_is_allowed(tmp_vault):
    from brain import db, ingest_notes

    note_path = tmp_vault / "bob.md"
    note_path.write_text("# Bob\nBob works at Aitomatic.")
    ingest_notes.ingest_all()

    entity_p = _write_entity(tmp_vault, "people", "bob")
    db.record_fact_provenance(entity_p, "Bob works at Aitomatic", ["bob.md"],
                              source_sha=None)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT source_sha FROM fact_provenance WHERE note_path='bob.md'"
        ).fetchone()
    assert row is not None
    assert row[0] is None


# ---------------------------------------------------------------------------
# find_stale_provenance
# ---------------------------------------------------------------------------

def test_find_stale_detects_edited_note(tmp_vault):
    from brain import db, ingest_notes

    note_text = "# Carol\nCarol is in Da Nang."
    note_path = tmp_vault / "carol.md"
    note_path.write_text(note_text)
    ingest_notes.ingest_all()

    entity_p = _write_entity(tmp_vault, "people", "carol")
    old_sha = _sha(note_text)
    db.record_fact_provenance(entity_p, "Carol is in Da Nang", ["carol.md"],
                              source_sha=old_sha)

    # Simulate note being edited — update ingest so notes.sha changes
    note_path.write_text("# Carol\nCarol moved to Hoi An.")
    ingest_notes.ingest_all()

    stale = db.find_stale_provenance()
    assert len(stale) == 1
    assert stale[0]["note_path"] == "carol.md"
    assert stale[0]["status"] == "stale"
    assert stale[0]["source_sha"] == old_sha
    assert stale[0]["current_sha"] != old_sha


def test_find_stale_detects_deleted_note(tmp_vault):
    from brain import db, ingest_notes

    note_text = "# Dave\nDave is in HCMC."
    note_path = tmp_vault / "dave.md"
    note_path.write_text(note_text)
    ingest_notes.ingest_all()

    entity_p = _write_entity(tmp_vault, "people", "dave")
    sha = _sha(note_text)
    db.record_fact_provenance(entity_p, "Dave is in HCMC", ["dave.md"],
                              source_sha=sha)

    # Delete the note from disk and from the notes table
    note_path.unlink()
    with db.connect() as conn:
        conn.execute("DELETE FROM notes WHERE path='dave.md'")

    stale = db.find_stale_provenance()
    assert len(stale) == 1
    assert stale[0]["status"] == "orphaned"
    assert stale[0]["current_sha"] is None


def test_find_stale_ignores_null_source_sha(tmp_vault):
    """Pre-migration provenance rows (source_sha=NULL) must not surface as stale."""
    from brain import db, ingest_notes

    note_path = tmp_vault / "eve.md"
    note_path.write_text("# Eve\nEve is in Hue.")
    ingest_notes.ingest_all()

    entity_p = _write_entity(tmp_vault, "people", "eve")
    db.record_fact_provenance(entity_p, "Eve is in Hue", ["eve.md"],
                              source_sha=None)

    # Even if we update the note, NULL source_sha rows should be ignored
    note_path.write_text("# Eve\nEve moved to Can Tho.")
    ingest_notes.ingest_all()

    stale = db.find_stale_provenance()
    assert stale == []


def test_find_stale_clean_when_sha_matches(tmp_vault):
    from brain import db, ingest_notes

    note_text = "# Frank\nFrank is in Hanoi."
    note_path = tmp_vault / "frank.md"
    note_path.write_text(note_text)
    ingest_notes.ingest_all()

    entity_p = _write_entity(tmp_vault, "people", "frank")
    # Get the actual sha from the notes table (ingest computes it)
    with db.connect() as conn:
        row = conn.execute("SELECT sha FROM notes WHERE path='frank.md'").fetchone()
    current_sha = row[0]

    db.record_fact_provenance(entity_p, "Frank is in Hanoi", ["frank.md"],
                              source_sha=current_sha)

    assert db.find_stale_provenance() == []


# ---------------------------------------------------------------------------
# verify.gc integrates with semantic (smoke test, semantic build optional)
# ---------------------------------------------------------------------------

def test_verify_gc_removes_phantom_and_returns_count(tmp_vault):
    from brain import db
    from brain import verify

    p = _write_entity(tmp_vault, "people", "ghost")
    db.upsert_entity_from_file(p)
    p.unlink()

    result = verify.gc()
    assert result["removed"] == 1
    assert any("ghost" in path for path in result["removed_paths"])


def test_index_untracked_adds_missing_entity(tmp_vault):
    from brain import db

    p = _write_entity(tmp_vault, "people", "missing")
    # File exists on disk but was never upserted
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM entities WHERE path LIKE '%missing%'").fetchone()[0]
    assert count == 0

    added = db.index_untracked_entities()
    assert any("missing" in path for path in added)

    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM entities WHERE path LIKE '%missing%'").fetchone()[0]
    assert count == 1


def test_verify_gc_adds_untracked_entities(tmp_vault):
    from brain import verify

    p = _write_entity(tmp_vault, "people", "untracked")
    result = verify.gc()
    assert result["added"] >= 1
    assert any("untracked" in path for path in result["added_paths"])


def test_verify_stale_returns_stale_facts(tmp_vault):
    from brain import db, ingest_notes, verify

    note_text = "# Hana\nHana is in Quy Nhon."
    note_path = tmp_vault / "hana.md"
    note_path.write_text(note_text)
    ingest_notes.ingest_all()

    entity_p = _write_entity(tmp_vault, "people", "hana")
    db.record_fact_provenance(entity_p, "Hana is in Quy Nhon", ["hana.md"],
                              source_sha=_sha(note_text))

    note_path.write_text("# Hana\nHana moved to Nha Trang.")
    ingest_notes.ingest_all()

    rows = verify.stale()
    assert len(rows) == 1
    assert rows[0]["status"] == "stale"


# ---------------------------------------------------------------------------
# post_extraction_sync
# ---------------------------------------------------------------------------

def test_post_sync_gc_removes_phantom(tmp_vault):
    from brain import db, verify

    p = _write_entity(tmp_vault, "people", "phantom2")
    db.upsert_entity_from_file(p)
    p.unlink()

    result = verify.post_extraction_sync()
    assert result["gc_removed"] >= 1
    assert result["gc_added"] == 0


def test_post_sync_gc_indexes_untracked(tmp_vault):
    from brain import verify

    _write_entity(tmp_vault, "people", "untracked2")
    result = verify.post_extraction_sync()
    assert result["gc_added"] >= 1
    assert result["gc_removed"] == 0


def test_post_sync_requeues_stale_notes(tmp_vault):
    from brain import db, ingest_notes, verify

    note_text = "# Ivan\nIvan is in Hanoi."
    note_path = tmp_vault / "ivan.md"
    note_path.write_text(note_text)
    ingest_notes.ingest_all()

    entity_p = _write_entity(tmp_vault, "people", "ivan")
    db.record_fact_provenance(entity_p, "Ivan is in Hanoi", ["ivan.md"],
                              source_sha=_sha(note_text))
    db.mark_note_extracted("ivan.md", _sha(note_text))

    # Edit the note → sha changes
    note_path.write_text("# Ivan\nIvan moved to Can Tho.")
    ingest_notes.ingest_all()

    result = verify.post_extraction_sync()
    assert result["notes_requeued"] == 1

    # Confirm notes.extracted_sha was reset to NULL
    with db.connect() as conn:
        row = conn.execute(
            "SELECT extracted_sha FROM notes WHERE path='ivan.md'"
        ).fetchone()
    assert row[0] is None


def test_post_sync_no_requeue_when_clean(tmp_vault):
    from brain import db, ingest_notes, verify

    note_text = "# Julia\nJulia is in HCMC."
    note_path = tmp_vault / "julia.md"
    note_path.write_text(note_text)
    ingest_notes.ingest_all()

    entity_p = _write_entity(tmp_vault, "people", "julia")
    with db.connect() as conn:
        current_sha = conn.execute(
            "SELECT sha FROM notes WHERE path='julia.md'"
        ).fetchone()[0]

    db.record_fact_provenance(entity_p, "Julia is in HCMC", ["julia.md"],
                              source_sha=current_sha)
    db.mark_note_extracted("julia.md", current_sha)

    result = verify.post_extraction_sync()
    assert result["notes_requeued"] == 0


def test_post_sync_idempotent(tmp_vault):
    """Running post_extraction_sync twice on a clean vault is a noop."""
    from brain import verify

    r1 = verify.post_extraction_sync()
    r2 = verify.post_extraction_sync()
    assert r1 == r2 == {"gc_removed": 0, "gc_added": 0, "notes_requeued": 0}


# ---------------------------------------------------------------------------
# auto_clean calls gc before applying rules (integration)
# ---------------------------------------------------------------------------

def test_auto_clean_runs_gc_on_phantom_entries(tmp_vault):
    from brain import db, auto_clean

    p = _write_entity(tmp_vault, "people", "phantom")
    db.upsert_entity_from_file(p)
    p.unlink()

    with db.connect() as conn:
        count_before = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count_before == 1

    # apply_rules with no rules file → empty rules but GC still runs
    auto_clean.apply_rules(dry_run=False)

    with db.connect() as conn:
        count_after = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count_after == 0
