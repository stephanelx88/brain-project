"""Tests for the forget primitive — tombstones + sticky retract.

Design brief (2026-04-23): the brain historically had only a "remember"
pipeline (session → extract → entity fact). Retract was in-place on the
markdown but the LLM would happily re-extract the same claim from the
next session that mentioned it. The forget primitive closes that gap by
recording a canonical-hash tombstone consulted at extraction time.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    vault.mkdir()
    for sub in ("entities", "identity", "timeline", "graphify-out", "raw"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    for t in ("people", "projects", "insights"):
        (vault / "entities" / t).mkdir(parents=True, exist_ok=True)

    # Reset module-level path constants the way test_retract does: set
    # BRAIN_DIR env var and reload the config + db modules so all their
    # derived constants (ENTITIES_DIR, DB_PATH, …) repoint at the tmp vault.
    monkeypatch.setenv("BRAIN_DIR", str(vault))
    import importlib
    import brain.config as cfg
    import brain.db as db_mod
    importlib.reload(cfg)
    importlib.reload(db_mod)
    yield vault
    importlib.reload(cfg)
    importlib.reload(db_mod)


def _make_entity(vault, type_, slug, facts_body):
    type_dir = vault / "entities" / type_
    type_dir.mkdir(parents=True, exist_ok=True)
    p = type_dir / f"{slug}.md"
    p.write_text(
        f"---\ntype: {type_[:-1] if type_.endswith('s') else type_}\n"
        f"name: {slug.title()}\n---\n\n# {slug.title()}\n\n"
        f"## Key Facts\n{facts_body}\n"
    )
    from brain import db
    db.upsert_entity_from_file(p)
    return p


# --- tombstone CRUD ----------------------------------------------------

def test_add_tombstone_is_idempotent(tmp_vault):
    from brain import db
    assert db.add_tombstone("Thuha is in Cần Thơ", reason="r1") is True
    # Same claim, same scope → idempotent.
    assert db.add_tombstone("THUHA is in cần thơ  ") is False


def test_is_forgotten_matches_canonical_hash(tmp_vault):
    from brain import db
    db.add_tombstone("Thuha is in Cần Thơ")
    # Case + whitespace + trailing source suffix are all normalised away.
    assert db.is_forgotten("thuha is in cần thơ")
    assert db.is_forgotten("Thuha is in Cần Thơ (source: s, 2026-04-23)")
    assert not db.is_forgotten("Thuha is in Long Xuyên")


def test_global_tombstone_blocks_any_scope(tmp_vault):
    from brain import db
    db.add_tombstone("works at aitomatic")
    assert db.is_forgotten("works at aitomatic", entity_type="people",
                           entity_name="Son")


def test_scoped_tombstone_does_not_block_other_entities(tmp_vault):
    from brain import db
    db.add_tombstone(
        "slippers in bedroom", entity_type="people", entity_name="Son"
    )
    # Same claim text, different entity → not blocked.
    assert not db.is_forgotten(
        "slippers in bedroom", entity_type="people", entity_name="Thuha"
    )
    # Same scope → blocked.
    assert db.is_forgotten(
        "slippers in bedroom", entity_type="people", entity_name="Son"
    )


def test_remove_tombstone(tmp_vault):
    from brain import db
    db.add_tombstone("Thuha is in Cần Thơ")
    assert db.is_forgotten("Thuha is in Cần Thơ")
    assert db.remove_tombstone("Thuha is in Cần Thơ") == 1
    assert not db.is_forgotten("Thuha is in Cần Thơ")


# --- retract → tombstone (sticky retract) ------------------------------

def test_retract_writes_tombstone(tmp_vault):
    _make_entity(
        tmp_vault, "people", "thuha",
        "- Thuha is in Cần Thơ (source: s, 2026-04-23)",
    )
    from brain.retract import retract_fact
    from brain import db

    retract_fact("people", "Thuha", "thuha is in cần thơ")
    # Tombstone recorded, scoped to the entity.
    assert db.is_forgotten(
        "Thuha is in Cần Thơ", entity_type="people", entity_name="Thuha"
    )


def test_correct_fact_tombstones_wrong_phrasing(tmp_vault):
    _make_entity(
        tmp_vault, "people", "son",
        "- currently in Long Xuyên (source: s, 2026-04-23)",
    )
    from brain.retract import correct_fact
    from brain import db

    correct_fact(
        "people", "Son",
        wrong_fact="long xuyên",
        correct_fact_text="currently in Cần Thơ",
    )
    assert db.is_forgotten(
        "currently in Long Xuyên", entity_type="people", entity_name="Son"
    )
    # The correction itself is not tombstoned.
    assert not db.is_forgotten(
        "currently in Cần Thơ", entity_type="people", entity_name="Son"
    )


# --- extractor respects tombstones -------------------------------------

def test_extractor_skips_tombstoned_fact(tmp_vault, monkeypatch):
    from brain import db
    # Pre-tombstone a claim globally.
    db.add_tombstone("Thuha is in Cần Thơ")

    # Stub out the side effects apply_extraction would otherwise trigger
    # (git commit + rebuild_index) so the test stays hermetic.
    from brain import apply_extraction as ae
    monkeypatch.setattr(ae, "commit", lambda *a, **kw: None)
    monkeypatch.setattr(ae, "rebuild_index", lambda: None)

    payload = {
        "entities": [{
            "type": "people",
            "name": "Thuha",
            "is_new": True,
            "facts": ["Thuha is in Cần Thơ"],
            "metadata": {},
        }],
    }
    ae.apply_extraction(
        payload, "test-session", do_commit=False, do_rebuild_index=False
    )

    # Entity should not be created when every proposed fact was
    # tombstoned — resurrecting a skeleton entity is the same bug.
    p = tmp_vault / "entities" / "people" / "thuha.md"
    assert not p.exists()


def test_extractor_keeps_other_facts_when_one_is_tombstoned(tmp_vault, monkeypatch):
    from brain import db
    db.add_tombstone("Thuha is in Cần Thơ")

    from brain import apply_extraction as ae
    monkeypatch.setattr(ae, "commit", lambda *a, **kw: None)
    monkeypatch.setattr(ae, "rebuild_index", lambda: None)

    payload = {
        "entities": [{
            "type": "people",
            "name": "Thuha",
            "is_new": True,
            "facts": [
                "Thuha is in Cần Thơ",
                "Thuha works at Aitomatic",
            ],
            "metadata": {},
        }],
    }
    ae.apply_extraction(
        payload, "test-session", do_commit=False, do_rebuild_index=False
    )

    p = tmp_vault / "entities" / "people" / "thuha.md"
    assert p.exists()
    body = p.read_text()
    assert "Thuha is in Cần Thơ" not in body
    assert "works at Aitomatic" in body


# --- note deletion cascade writes tombstones ---------------------------

def test_note_delete_writes_tombstone(tmp_vault):
    from brain import db, ingest_notes

    epath = _make_entity(
        tmp_vault, "people", "son",
        "- Son is in Long Xuyen (source: session-x, 2026-04-19)",
    )
    note = tmp_vault / "where-is-son.md"
    note.write_text("Son is in Long Xuyen.")
    ingest_notes.ingest_all()
    db.record_fact_provenance(
        epath, "Son is in Long Xuyen", ["where-is-son.md"]
    )

    note.unlink()
    out = ingest_notes.ingest_all()
    assert out["deleted"] == 1
    assert out["facts_invalidated"] == 1
    assert out["tombstones_written"] >= 1
    # Tombstone is active, scoped to the entity.
    assert db.is_forgotten(
        "Son is in Long Xuyen", entity_type="people", entity_name="Son"
    )


def test_note_edit_does_not_tombstone(tmp_vault):
    """Edits must not tombstone — otherwise re-extraction from the edited
    content could be blocked from restating a claim the user still wrote."""
    from brain import db, ingest_notes

    epath = _make_entity(
        tmp_vault, "people", "son",
        "- Son is in Long Xuyen (source: session-x, 2026-04-19)",
    )
    db.record_fact_provenance(
        epath, "Son is in Long Xuyen", ["notes/son.md"]
    )

    # Simulate the edit path by calling invalidate directly with reason='edited'
    from brain.ingest_notes import invalidate_facts_for_note
    result = invalidate_facts_for_note("notes/son.md", reason="edited")
    assert result["facts_invalidated"] == 1
    assert result["tombstones_written"] == 0
    assert not db.is_forgotten(
        "Son is in Long Xuyen", entity_type="people", entity_name="Son"
    )


# --- semantic search orphan-rowid leak ---------------------------------

def test_semantic_drops_orphan_rowid(tmp_vault, monkeypatch):
    """After upsert DELETE+INSERTs a fact, the old rowid no longer exists
    in the facts table. Its embedding still lives in .vec/facts.npy.
    search_facts must drop the orphan instead of surfacing it."""
    import numpy as np
    from brain import semantic, db

    # Seed one real fact, then manually craft an orphan entry in the
    # semantic cache alongside it.
    _make_entity(
        tmp_vault, "people", "son",
        "- Son works at Aitomatic (source: s, 2026-04-23)",
    )
    semantic.VEC_DIR.mkdir(parents=True, exist_ok=True)
    # Override the module-level paths to this temp vault's .vec/ dir.
    vec_dir = tmp_vault / ".vec"
    vec_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(semantic, "VEC_DIR", vec_dir)
    monkeypatch.setattr(semantic, "FACTS_NPY", vec_dir / "facts.npy")
    monkeypatch.setattr(semantic, "FACTS_JSON", vec_dir / "facts.json")
    monkeypatch.setattr(semantic, "ENT_NPY", vec_dir / "entities.npy")
    monkeypatch.setattr(semantic, "ENT_JSON", vec_dir / "entities.json")
    monkeypatch.setattr(semantic, "NOTES_NPY", vec_dir / "notes.npy")
    monkeypatch.setattr(semantic, "NOTES_JSON", vec_dir / "notes.json")
    monkeypatch.setattr(semantic, "META_JSON", vec_dir / "meta.json")

    semantic.build()

    # Inject an orphan entry: a fake rowid (999999) that isn't in the
    # facts table, with an arbitrary embedding + meta.
    import json
    meta = json.loads(semantic.FACTS_JSON.read_text())
    vecs = np.load(semantic.FACTS_NPY)
    orphan = {
        "rowid": 999999,
        "text": "Thuha is in Cần Thơ",
        "source": "orphan-test",
        "entity_id": 1,
        "type": "people",
        "name": "Thuha",
        "slug": "thuha",
        "date": "2026-04-23",
        "path": "entities/people/thuha.md",
    }
    meta.append(orphan)
    # Any unit vector — we just need a hit.
    orphan_vec = np.random.rand(vecs.shape[1]).astype(np.float32)
    orphan_vec /= np.linalg.norm(orphan_vec)
    vecs = np.concatenate([vecs, orphan_vec[None, :]], axis=0)
    semantic._atomic_save_npy(semantic.FACTS_NPY, vecs)
    semantic.FACTS_JSON.write_text(json.dumps(meta))

    # Search for the orphan's text — it MUST NOT leak.
    results = semantic.search_facts("Thuha", k=5)
    assert all(r.get("text") != "Thuha is in Cần Thơ" for r in results), \
        f"orphan leaked: {results}"


# --- freshness: entity mtime sync --------------------------------------

def test_sync_mutated_entities_reindexes_edited_file(tmp_vault):
    """Directly editing an entity file must be reflected after
    sync_mutated_entities runs — without waiting for the next scheduled
    pipeline tick."""
    import time
    from brain import db

    p = _make_entity(
        tmp_vault, "people", "son",
        "- Son works at Aitomatic (source: s, 2026-04-23)",
    )
    # Edit the file on disk behind the DB's back.
    time.sleep(0.01)  # ensure mtime strictly increases
    new_body = p.read_text().replace("works at Aitomatic", "works at NewCo")
    p.write_text(new_body)
    import os
    os.utime(p, None)

    changed = db.sync_mutated_entities()
    rel = "entities/people/son.md"
    assert rel in changed

    # BM25 now finds the new phrasing and not the old.
    from brain import db as dbm
    assert dbm.search("NewCo", k=3)
    # Old text is gone from FTS (live facts only).
    old_hits = [r for r in dbm.search("Aitomatic", k=5) if r.get("status") is None]
    assert not any("Aitomatic" in r["text"] for r in old_hits)
