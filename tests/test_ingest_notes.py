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


def test_ingest_one_changes_note_without_walking_vault(tmp_vault):
    from brain import ingest_notes, db

    note = tmp_vault / "standalone.md"
    note.write_text("# standalone\n\nbody\n")
    out = ingest_notes.ingest_one(note)
    assert out == {"status": "changed", "rel_path": "standalone.md",
                   "changed": True, "deleted": False}

    rows = db.search_notes("standalone", k=5)
    assert any(r["path"] == "standalone.md" for r in rows)


def test_ingest_one_unchanged_on_second_call(tmp_vault):
    from brain import ingest_notes

    note = tmp_vault / "idempotent.md"
    note.write_text("same content")
    first = ingest_notes.ingest_one(note)
    assert first["status"] == "changed"
    # Touch mtime to force the ledger probe past the content check
    import os
    st = note.stat()
    os.utime(note, (st.st_atime, st.st_mtime + 5))
    second = ingest_notes.ingest_one(note)
    assert second["status"] == "unchanged"


def test_ingest_one_skips_entity_dir(tmp_vault):
    from brain import ingest_notes

    ef = tmp_vault / "entities" / "people" / "e.md"
    ef.parent.mkdir(parents=True, exist_ok=True)
    ef.write_text("- fact")
    out = ingest_notes.ingest_one(ef)
    assert out["status"] == "skipped"


def test_ingest_one_skips_machine_dir(tmp_vault):
    from brain import ingest_notes

    (tmp_vault / "raw").mkdir(exist_ok=True)
    trans = tmp_vault / "raw" / "session.md"
    trans.write_text("transient")
    out = ingest_notes.ingest_one(trans)
    assert out["status"] == "skipped"


def test_ingest_one_rejects_path_outside_vault(tmp_vault, tmp_path):
    from brain import ingest_notes
    outside = tmp_path / "nope.md"
    outside.write_text("nope")
    out = ingest_notes.ingest_one(outside)
    assert out["status"] == "skipped"


def test_ingest_one_handles_delete(tmp_vault):
    from brain import ingest_notes, db

    note = tmp_vault / "tmp-note.md"
    note.write_text("x")
    ingest_notes.ingest_one(note)
    assert any(r["path"] == "tmp-note.md" for r in db.search_notes("x", k=5))

    note.unlink()
    out = ingest_notes.ingest_one(note)
    assert out["status"] == "deleted"
    assert out["deleted"] is True


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


# ---------------------------------------------------------------------------
# Note → fact provenance + invalidation
#
# Pins the 2026-04-21 fix: when a vault note is deleted, every entity
# fact whose provenance points back at that note is strikethroughed
# (not silently retained, as the legacy pipeline did with the
# Long-Xuyen/Saigon location data).
# ---------------------------------------------------------------------------

def _make_entity(vault, type_: str, slug: str, body: str):
    """Helper: drop a minimal entity markdown into the temp vault."""
    type_dir = vault / "entities" / type_
    type_dir.mkdir(parents=True, exist_ok=True)
    p = type_dir / f"{slug}.md"
    p.write_text(
        f"---\ntype: {type_[:-1] if type_.endswith('s') else type_}\n"
        f"name: {slug}\n---\n\n# {slug}\n\n## Key Facts\n{body}\n"
    )
    return p


def test_provenance_round_trip(tmp_vault):
    """record_fact_provenance → facts_invalidated_by_note → forget."""
    from brain import db

    epath = _make_entity(tmp_vault, "people", "son", "- Son is in Long Xuyen")
    fact = "Son is in Long Xuyen"
    n = db.record_fact_provenance(epath, fact, ["where-is-son.md"])
    assert n == 1

    rows = db.facts_invalidated_by_note("where-is-son.md")
    assert len(rows) == 1
    assert rows[0][0] == "entities/people/son.md"
    assert rows[0][1] == db.canonical_fact_hash(fact)

    dropped = db.forget_note_provenance("where-is-son.md")
    assert dropped == 1
    assert db.facts_invalidated_by_note("where-is-son.md") == []


def test_canonical_fact_hash_strips_source_suffix(tmp_vault):
    """Two extractions of the same fact with different source labels
    hash to the same key — provenance survives re-extraction."""
    from brain import db

    h1 = db.canonical_fact_hash("Son is in Long Xuyen (source: session-1, 2026-04-19)")
    h2 = db.canonical_fact_hash("Son is in Long Xuyen (source: session-2, 2026-04-21)")
    assert h1 == h2


def test_note_delete_strikethroughs_provenance_linked_facts(tmp_vault):
    """End-to-end: provenance row + note delete → fact invalidated."""
    from brain import db, ingest_notes

    epath = _make_entity(
        tmp_vault, "people", "son",
        "- Son is in Long Xuyen (source: session-x, 2026-04-19)\n"
        "- Son likes bun rieu (source: session-y, 2026-04-19)",
    )
    note = tmp_vault / "where-is-son.md"
    note.write_text("Son is in Long Xuyen, Vietnam.")

    ingest_notes.ingest_all()  # populate notes ledger
    db.record_fact_provenance(
        epath, "Son is in Long Xuyen", ["where-is-son.md"]
    )

    note.unlink()
    out = ingest_notes.ingest_all()
    assert out["deleted"] == 1
    assert out["facts_invalidated"] == 1
    assert out["entities_touched_by_invalidation"] == 1

    body = epath.read_text()
    assert "~~Son is in Long Xuyen~~" in body
    assert "[invalidated" in body
    assert "where-is-son.md` deleted" in body
    assert "Son likes bun rieu" in body  # unrelated fact untouched
    assert "~~Son likes bun rieu~~" not in body


def test_strikethrough_marks_fact_as_superseded(tmp_vault):
    """Strikethrough bullets are preserved in `_facts_from_body` with
    status='superseded' so the audit trail stays intact. The FTS
    write path in `upsert_entity_from_file` is what drops them from
    BM25 recall — verified separately."""
    from brain import db

    body = (
        "- ~~Son is in Long Xuyen~~ (source: s, 2026-04-19) "
        "[invalidated 2026-04-21: source note `where-is-son.md` deleted]\n"
        "- Son likes bun rieu (source: s, 2026-04-19)"
    )
    facts = list(db._facts_from_body(body))
    assert len(facts) == 2
    superseded = [f for f in facts if f[3] == "superseded"]
    live = [f for f in facts if f[3] is None]
    assert len(superseded) == 1
    assert len(live) == 1
    assert "Long Xuyen" in superseded[0][0]
    assert live[0][0] == "Son likes bun rieu"


def test_invalidation_is_idempotent(tmp_vault):
    """A second ingest pass after the note is gone must not re-strikethrough."""
    from brain import db, ingest_notes

    epath = _make_entity(
        tmp_vault, "people", "son",
        "- Son is in Long Xuyen (source: session-x, 2026-04-19)",
    )
    note = tmp_vault / "where-is-son.md"
    note.write_text("Son is in Long Xuyen.")
    ingest_notes.ingest_all()
    db.record_fact_provenance(epath, "Son is in Long Xuyen", ["where-is-son.md"])

    note.unlink()
    out1 = ingest_notes.ingest_all()
    assert out1["facts_invalidated"] == 1

    out2 = ingest_notes.ingest_all()
    assert out2["facts_invalidated"] == 0  # provenance row already cleared

    body = epath.read_text()
    assert body.count("~~Son is in Long Xuyen~~") == 1  # not double-wrapped


def test_no_provenance_means_no_silent_invalidation(tmp_vault):
    """Notes without provenance rows must NOT touch entity files when
    they vanish — silent rewrites of unrelated facts would be worse
    than the original bug.
    """
    from brain import ingest_notes

    epath = _make_entity(
        tmp_vault, "people", "son",
        "- Son is in Long Xuyen (source: session-x, 2026-04-19)",
    )
    note = tmp_vault / "completely-unrelated.md"
    note.write_text("nothing about son")
    ingest_notes.ingest_all()

    note.unlink()
    out = ingest_notes.ingest_all()
    assert out["deleted"] == 1
    assert out["facts_invalidated"] == 0
    assert "~~" not in epath.read_text()


# ---------------------------------------------------------------------------
# File-type whitelist covers .txt / .markdown / .text in addition to .md
#
# Motivating incident 2026-04-23: user wrote `~/.brain/son.txt` containing
# "son dang o biet thu mau xanh", queried brain → empty. Cause: filter
# `path.suffix.lower() != ".md"` hard-skipped the file. Restricting to
# `.md` was convention, not a technical requirement; silent-skip-on-other
# is a trust-breaking class. The whitelist now includes the three common
# plain-text / markdown aliases a user might reach for.
# ---------------------------------------------------------------------------


def test_ingest_accepts_txt_and_markdown_aliases(tmp_vault):
    """A note written as `.txt` / `.markdown` / `.text` must be ingested,
    not silently skipped."""
    from brain import ingest_notes, db
    for name, body in [
        ("plain.txt",          "son dang o biet thu mau xanh"),
        ("verbose.markdown",   "architect deadline moved to friday"),
        ("unusual.text",       "meeting notes: discuss Q2 goals"),
        # control: .md still works
        ("normal.md",          "standard markdown note"),
    ]:
        (tmp_vault / name).write_text(body)

    out = ingest_notes.ingest_all()
    # ingest_all's `scanned` counts files whose content hash changed (new or
    # edited); on first ingest of all four fresh files that's 4.
    assert out["scanned"] == 4, f"expected all 4 extensions indexed, got {out}"

    with db.connect() as c:
        paths = {row[0] for row in c.execute("SELECT path FROM notes").fetchall()}
    assert "plain.txt" in paths
    assert "verbose.markdown" in paths
    assert "unusual.text" in paths
    assert "normal.md" in paths


def test_ingest_rejects_unsupported_extensions(tmp_vault):
    """Unsupported extensions remain skipped (scope protection: .log, .json
    etc. are not user prose and should not leak into the notes index)."""
    from brain import ingest_notes, db
    (tmp_vault / "huge.log").write_text("lots of log lines")
    (tmp_vault / "config.json").write_text('{"ignored": true}')
    (tmp_vault / "kept.md").write_text("this stays")

    ingest_notes.ingest_all()
    with db.connect() as c:
        paths = {row[0] for row in c.execute("SELECT path FROM notes").fetchall()}
    assert "kept.md" in paths
    assert "huge.log" not in paths
    assert "config.json" not in paths


# ---------------------------------------------------------------------------
# Playbook directory — index any extension under <vault>/playbooks/
#
# Brain stores runnable knowledge: a playbook .md doc + an executable
# script (.sh/.py/.js). The agent reads both via brain_recall and runs
# the script through its own shell tool. Ingest must therefore index
# script files, but only inside the playbooks/ dir — outside it, .sh
# stays skipped so we don't flood the notes index.
# ---------------------------------------------------------------------------


def test_ingest_indexes_scripts_under_playbooks_dir(tmp_vault):
    """A .sh / .py inside playbooks/ is indexed; a .sh elsewhere isn't."""
    from brain import ingest_notes, db
    (tmp_vault / "playbooks").mkdir()
    (tmp_vault / "playbooks" / "deploy.md").write_text(
        "# Deploy staging\n\n## Steps\n1. run deploy.sh"
    )
    (tmp_vault / "playbooks" / "deploy.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\necho deploying"
    )
    (tmp_vault / "playbooks" / "rotate" / "verify.py").parent.mkdir()
    (tmp_vault / "playbooks" / "rotate" / "verify.py").write_text(
        "import os\nprint('rotated')"
    )
    # Control: a .sh outside playbooks/ stays skipped.
    (tmp_vault / "stray.sh").write_text("#!/bin/bash\necho stray")
    (tmp_vault / "kept.md").write_text("normal note")

    ingest_notes.ingest_all()
    with db.connect() as c:
        paths = {row[0] for row in c.execute("SELECT path FROM notes").fetchall()}

    assert "playbooks/deploy.md" in paths
    assert "playbooks/deploy.sh" in paths, "scripts under playbooks/ must be indexed"
    assert "playbooks/rotate/verify.py" in paths, "nested playbook scripts must be indexed"
    assert "kept.md" in paths
    assert "stray.sh" not in paths, "scripts outside playbooks/ must NOT be indexed"


def test_ingest_skips_underscore_prefix_inside_playbooks(tmp_vault):
    """Underscore-prefix exclusion applies even inside playbooks/, so users
    can stage drafts as _wip.md without polluting recall."""
    from brain import ingest_notes, db
    (tmp_vault / "playbooks").mkdir()
    (tmp_vault / "playbooks" / "real.md").write_text("# Real\n\nfor real")
    (tmp_vault / "playbooks" / "_draft.md").write_text("# WIP\n\nnot ready")
    (tmp_vault / "playbooks" / "_template.md").write_text("# Template")

    ingest_notes.ingest_all()
    with db.connect() as c:
        paths = {row[0] for row in c.execute("SELECT path FROM notes").fetchall()}

    assert "playbooks/real.md" in paths
    assert "playbooks/_draft.md" not in paths
    assert "playbooks/_template.md" not in paths



def test_ingest_one_delete_calls_semantic_invalidate_for_touched_entities(tmp_vault, monkeypatch):
    from brain import ingest_notes
    calls = []

    class _FakeSem:
        @staticmethod
        def invalidate_for(entity_type, slug=None):
            calls.append((entity_type, slug))
            return {"facts_dropped": 0, "entities_dropped": 0}
        @staticmethod
        def update_notes_via_worker(**kw): pass

    monkeypatch.setattr(ingest_notes, "invalidate_facts_for_note",
        lambda rel, verbose=False: {
            "facts_invalidated": 2, "entities_touched": 2, "tombstones_written": 0,
            "entity_paths": ["entities/people/thuha.md", "entities/projects/brain.md"],
        })
    # Both patches needed: `from brain import semantic` does
    # `getattr(brain, 'semantic')`, so the attribute has to be
    # replaced too — not just the sys.modules entry.
    import sys, brain as _brain_pkg
    monkeypatch.setitem(sys.modules, "brain.semantic", _FakeSem)
    monkeypatch.setattr(_brain_pkg, "semantic", _FakeSem)

    note = tmp_vault / "some-note.md"
    note.write_text("seed")
    ingest_notes.ingest_one(note)
    note.unlink()
    out = ingest_notes.ingest_one(note)
    assert out["status"] == "deleted"
    assert ("people", "thuha") in calls
    assert ("projects", "brain") in calls
