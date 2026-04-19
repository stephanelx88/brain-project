"""Tests for vault-note ingestion (path #2 of the brain's two extractors)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    vault.mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", vault)

    from brain import db
    monkeypatch.setattr(db, "DB_PATH", vault / ".brain.db")

    return vault


def test_walker_skips_machine_dirs(tmp_vault, monkeypatch):
    from brain import ingest_notes

    (tmp_vault / "entities" / "people").mkdir(parents=True)
    (tmp_vault / "entities" / "people" / "annie.md").write_text("- entity fact")
    (tmp_vault / ".obsidian").mkdir()
    (tmp_vault / ".obsidian" / "config.md").write_text("config junk")
    (tmp_vault / "raw").mkdir()
    (tmp_vault / "raw" / "session.md").write_text("transient")
    (tmp_vault / "_archive").mkdir()
    (tmp_vault / "_archive" / "old.md").write_text("archived")

    (tmp_vault / "real-note.md").write_text("# real")
    (tmp_vault / "subdir").mkdir()
    (tmp_vault / "subdir" / "deep.md").write_text("# deep")

    paths = sorted(p.name for p in ingest_notes._iter_note_paths(tmp_vault))
    assert paths == ["deep.md", "real-note.md"]


def test_filename_becomes_title_when_no_heading(tmp_vault):
    from brain import ingest_notes

    (tmp_vault / "son dang o long xuyen.md").write_text("")
    out = ingest_notes.ingest_all()
    assert out["changed"] == 1

    from brain import db
    rows = db.search_notes("long xuyen", k=5)
    assert any("long xuyen" in r["title"] for r in rows)


def test_first_heading_overrides_filename_for_title(tmp_vault):
    from brain import ingest_notes

    (tmp_vault / "ugly-slug.md").write_text("# Pretty Title\n\nbody here")
    ingest_notes.ingest_all()

    from brain import db
    rows = db.search_notes("Pretty", k=5)
    assert rows
    assert rows[0]["title"] == "Pretty Title"


def test_diff_walker_is_idempotent(tmp_vault):
    """Re-running ingest with no changes touches nothing."""
    from brain import ingest_notes

    (tmp_vault / "a.md").write_text("alpha")
    out1 = ingest_notes.ingest_all()
    assert out1["changed"] == 1

    out2 = ingest_notes.ingest_all()
    assert out2["changed"] == 0
    assert out2["deleted"] == 0


def test_deleted_file_is_pruned(tmp_vault):
    from brain import ingest_notes, db

    note_path = tmp_vault / "ephemeral.md"
    note_path.write_text("temp content")
    ingest_notes.ingest_all()
    assert db.search_notes("temp content", k=5)

    note_path.unlink()
    out = ingest_notes.ingest_all()
    assert out["deleted"] == 1
    assert not db.search_notes("temp content", k=5)


def test_modified_file_replaces_old_body(tmp_vault):
    from brain import ingest_notes, db

    p = tmp_vault / "diary.md"
    p.write_text("alpha bravo")
    ingest_notes.ingest_all()

    import os
    p.write_text("delta echo")
    os.utime(p, None)  # bump mtime
    ingest_notes.ingest_all()

    assert not db.search_notes("alpha", k=5)
    assert db.search_notes("delta", k=5)


def test_oversized_files_skipped(tmp_vault, monkeypatch):
    from brain import ingest_notes

    monkeypatch.setattr(ingest_notes, "MAX_BYTES", 50)
    (tmp_vault / "big.md").write_text("X" * 5000)
    out = ingest_notes.ingest_all()
    assert out["skipped_large"] == 1
    assert out["changed"] == 0


def test_db_search_notes_returns_filename_match(tmp_vault):
    from brain import ingest_notes, db
    (tmp_vault / "son-is-in-tokyo.md").write_text("")
    ingest_notes.ingest_all()
    res = db.search_notes("tokyo", k=3)
    assert res
    # filename → title → searchable
    assert any("tokyo" in r["title"].lower() for r in res)
