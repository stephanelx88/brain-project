"""Tests for note → entity-fact extraction with provenance auto-population.

Companion to test_ingest_notes (which covers the delete-side
strikethrough). Here we cover the *create* side: when a user types a
new vault note, note_extract should turn it into entity facts AND
record a fact_provenance row so the future delete invalidates them.
"""

from __future__ import annotations

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

    # Disable git in tests — git_ops.commit will no-op when not a repo.
    import brain.git_ops as git_ops
    monkeypatch.setattr(git_ops, "commit", lambda *a, **kw: True)

    return vault


def _ingest(tmp_vault):
    """Run ingest_notes against the temp vault so notes table is populated."""
    from brain import ingest_notes
    return ingest_notes.ingest_all()


# ---------------------------------------------------------------------------
# pending_note_extractions
# ---------------------------------------------------------------------------

def test_pending_returns_uningested_notes(tmp_vault):
    from brain import db

    (tmp_vault / "alpha.md").write_text("# alpha\nbody")
    (tmp_vault / "beta.md").write_text("# beta\nbody")
    _ingest(tmp_vault)

    pending = db.pending_note_extractions(limit=10)
    paths = {p["path"] for p in pending}
    assert paths == {"alpha.md", "beta.md"}


def test_mark_extracted_removes_from_pending(tmp_vault):
    from brain import db

    (tmp_vault / "alpha.md").write_text("# alpha\nbody")
    _ingest(tmp_vault)

    [row] = db.pending_note_extractions(limit=10)
    db.mark_note_extracted(row["path"], row["sha"])

    assert db.pending_note_extractions(limit=10) == []


def test_edited_note_becomes_pending_again(tmp_vault):
    from brain import db

    note = tmp_vault / "alpha.md"
    note.write_text("v1")
    _ingest(tmp_vault)
    [row] = db.pending_note_extractions(limit=10)
    db.mark_note_extracted(row["path"], row["sha"])
    assert db.pending_note_extractions(limit=10) == []

    # Edit → new sha → pending again.
    note.write_text("v2 with new content")
    _ingest(tmp_vault)
    pending = db.pending_note_extractions(limit=10)
    assert len(pending) == 1
    assert pending[0]["sha"] != row["sha"]


def test_exclude_prefixes_filters_out_managed_dirs(tmp_vault):
    from brain import db

    (tmp_vault / "alpha.md").write_text("user note")
    (tmp_vault / "playground").mkdir()
    (tmp_vault / "playground" / "cycle-0001.md").write_text("auto-gen")
    (tmp_vault / "timeline").mkdir()
    (tmp_vault / "timeline" / "promote.md").write_text("auto-gen")
    _ingest(tmp_vault)

    pending = db.pending_note_extractions(
        limit=10, exclude_prefixes=("playground", "timeline")
    )
    assert {p["path"] for p in pending} == {"alpha.md"}


def test_exclude_paths_filters_out_specific_files(tmp_vault):
    from brain import db

    (tmp_vault / "alpha.md").write_text("user")
    (tmp_vault / "log.md").write_text("auto log")
    _ingest(tmp_vault)

    pending = db.pending_note_extractions(limit=10, exclude_paths=("log.md",))
    assert {p["path"] for p in pending} == {"alpha.md"}


# ---------------------------------------------------------------------------
# process_pending — end-to-end with mocked LLM
# ---------------------------------------------------------------------------

def test_process_pending_writes_provenance(tmp_vault, monkeypatch):
    """The whole point: extracted facts must be linked back to the source
    note so a future delete can invalidate them."""
    from brain import db, note_extract

    (tmp_vault / "where-is-son.md").write_text("Son is in Hanoi.")
    _ingest(tmp_vault)

    # Mock the LLM to return a deterministic extraction.
    fake_output = """
{
  "entities": [
    {
      "type": "people",
      "name": "Son",
      "is_new": true,
      "facts": ["Son is in Hanoi"],
      "metadata": {}
    }
  ],
  "corrections": []
}
"""
    monkeypatch.setattr(note_extract, "call_claude", lambda prompt, **kw: fake_output)
    monkeypatch.setattr(note_extract, "get_existing_index", lambda: "(empty brain)")
    # Skip side-effects we don't care about here.
    monkeypatch.setattr(note_extract, "rebuild_index", lambda: None)
    monkeypatch.setattr(note_extract, "commit", lambda *a, **kw: True)

    summary = note_extract.process_pending(max_notes=5, verbose=False)
    assert summary["processed"] == 1
    assert summary["errors"] == 0

    # Provenance row must exist linking the new fact to the note path.
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT entity_path, note_path FROM fact_provenance WHERE note_path=?",
            ("where-is-son.md",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "entities/people/son.md"


def test_process_pending_is_idempotent(tmp_vault, monkeypatch):
    """Second run with no vault changes does zero LLM calls."""
    from brain import note_extract

    (tmp_vault / "alpha.md").write_text("Alpha is a person.")
    _ingest(tmp_vault)

    call_counter = {"n": 0}

    def fake_llm(prompt, **kw):
        call_counter["n"] += 1
        return '{"entities": [], "corrections": []}'

    monkeypatch.setattr(note_extract, "call_claude", fake_llm)
    monkeypatch.setattr(note_extract, "get_existing_index", lambda: "")
    monkeypatch.setattr(note_extract, "rebuild_index", lambda: None)
    monkeypatch.setattr(note_extract, "commit", lambda *a, **kw: True)

    note_extract.process_pending(max_notes=5)
    assert call_counter["n"] == 1

    note_extract.process_pending(max_notes=5)
    assert call_counter["n"] == 1, "second run with no edits must skip LLM"


def test_empty_extraction_marks_extracted(tmp_vault, monkeypatch):
    """LLM returning {} is a valid outcome — must mark sha so we don't
    re-call on every tick."""
    from brain import db, note_extract

    (tmp_vault / "trivial.md").write_text("xyz")
    _ingest(tmp_vault)

    monkeypatch.setattr(
        note_extract, "call_claude",
        lambda prompt, **kw: '{"entities": [], "corrections": []}'
    )
    monkeypatch.setattr(note_extract, "get_existing_index", lambda: "")
    monkeypatch.setattr(note_extract, "rebuild_index", lambda: None)
    monkeypatch.setattr(note_extract, "commit", lambda *a, **kw: True)

    summary = note_extract.process_pending(max_notes=5)
    assert summary["empty"] == 1
    assert summary["processed"] == 1
    assert db.pending_note_extractions(limit=5) == []


def test_llm_failure_does_not_advance_extracted_sha(tmp_vault, monkeypatch):
    """If the LLM call fails, leave extracted_sha alone so next tick retries."""
    from brain import db, note_extract

    (tmp_vault / "alpha.md").write_text("body")
    _ingest(tmp_vault)

    monkeypatch.setattr(note_extract, "call_claude", lambda prompt, **kw: None)
    monkeypatch.setattr(note_extract, "get_existing_index", lambda: "")
    monkeypatch.setattr(note_extract, "rebuild_index", lambda: None)
    monkeypatch.setattr(note_extract, "commit", lambda *a, **kw: True)

    summary = note_extract.process_pending(max_notes=5)
    assert summary["errors"] == 1
    assert summary["processed"] == 0
    # Still pending — must retry next tick.
    assert len(db.pending_note_extractions(limit=5)) == 1


def test_edit_retracts_old_facts_before_adding_new(tmp_vault, monkeypatch):
    """User-reported case: editing `Thuha va Trinh.md` from
    'they are in Can Tho' to 'they are in Con Dao' must retract the
    Can Tho fact, not pile both contradicting facts on top of each
    other in the entity file."""
    from brain import db, ingest_notes, note_extract

    note = tmp_vault / "where-they-are.md"
    note.write_text("They are in Can Tho.")
    ingest_notes.ingest_all()

    fake_results = iter([
        # First extraction: Can Tho fact.
        '{"entities":[{"type":"people","name":"Trinh","is_new":true,'
        '"facts":["Currently in Can Tho"],"metadata":{}}],"corrections":[]}',
        # Second extraction (after edit): Con Dao fact.
        '{"entities":[{"type":"people","name":"Trinh","is_new":false,'
        '"facts":["Currently in Con Dao"],"metadata":{}}],"corrections":[]}',
    ])
    monkeypatch.setattr(note_extract, "call_claude",
                        lambda prompt, **kw: next(fake_results))
    monkeypatch.setattr(note_extract, "get_existing_index", lambda: "")
    monkeypatch.setattr(note_extract, "rebuild_index", lambda: None)
    monkeypatch.setattr(note_extract, "commit", lambda *a, **kw: True)

    note_extract.process_pending(max_notes=5)

    trinh_md = tmp_vault / "entities" / "people" / "trinh.md"
    text = trinh_md.read_text()
    assert "Currently in Can Tho" in text
    assert "~~Currently in Can Tho~~" not in text

    note.write_text("They are in Con Dao now.")
    ingest_notes.ingest_all()
    note_extract.process_pending(max_notes=5)

    text = trinh_md.read_text()
    # Old fact strikethroughed with edit-invalidation tag, new fact live.
    assert "~~Currently in Can Tho~~" in text, (
        "edit must retract the old Can Tho fact"
    )
    assert "Currently in Con Dao" in text
    # The Con Dao fact must NOT be strikethroughed (it was added AFTER
    # invalidation cleared the old provenance row, so it's fresh).
    assert "~~Currently in Con Dao~~" not in text


def test_round_trip_create_extract_delete_invalidates(tmp_vault, monkeypatch):
    """The full circle: typing a note creates a fact; deleting the note
    strikethroughs that exact fact. This is the user-facing contract."""
    from brain import db, ingest_notes, note_extract

    note = tmp_vault / "where-is-son.md"
    note.write_text("Son is in Da Lat.")
    _ingest(tmp_vault)

    monkeypatch.setattr(
        note_extract, "call_claude",
        lambda prompt, **kw: (
            '{"entities":[{"type":"people","name":"Son","is_new":true,'
            '"facts":["Son is in Da Lat"],"metadata":{}}],"corrections":[]}'
        )
    )
    monkeypatch.setattr(note_extract, "get_existing_index", lambda: "")
    monkeypatch.setattr(note_extract, "rebuild_index", lambda: None)
    monkeypatch.setattr(note_extract, "commit", lambda *a, **kw: True)

    note_extract.process_pending(max_notes=5)

    son_md = tmp_vault / "entities" / "people" / "son.md"
    assert "Son is in Da Lat" in son_md.read_text()
    assert "~~Son is in Da Lat~~" not in son_md.read_text()

    note.unlink()
    _ingest(tmp_vault)

    text = son_md.read_text()
    assert "~~Son is in Da Lat~~" in text
    assert "[invalidated" in text
    assert "where-is-son.md" in text
