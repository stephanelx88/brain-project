"""Tests for WS4 scrub-version cross-reference (semantic.ensure_built).

Verifies:
  * `sanitize.VERSION` constant exists and is stamped into sanitize.jsonl.
  * `build()` stores `scrub_tag` in `.vec/meta.json`.
  * `ensure_built()` detects a scrub-version mismatch and forces a full
    rebuild (covers both "missing scrub_tag" and "stored ≠ current").
  * The forced rebuild emits one `scrub_version_bump_reingest` row to
    the WS5 hash-chained audit ledger.
  * A matching scrub_tag does NOT trigger a rebuild.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from brain import sanitize, semantic


# ---------------------------------------------------------------------------
# fixture — copy of the semantic test fixture, plus ledger monkeypatch
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_brain_with_db(tmp_path, monkeypatch):
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setenv("BRAIN_DIR", str(brain_dir))

    from brain import db
    db_path = brain_dir / ".brain.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    monkeypatch.setattr(semantic, "VEC_DIR", brain_dir / ".vec")
    monkeypatch.setattr(semantic, "FACTS_NPY", brain_dir / ".vec" / "facts.npy")
    monkeypatch.setattr(semantic, "FACTS_JSON", brain_dir / ".vec" / "facts.json")
    monkeypatch.setattr(semantic, "ENT_NPY", brain_dir / ".vec" / "entities.npy")
    monkeypatch.setattr(semantic, "ENT_JSON", brain_dir / ".vec" / "entities.json")
    monkeypatch.setattr(semantic, "META_JSON", brain_dir / ".vec" / "meta.json")

    def fake_embed(texts, batch_size=64):
        if not texts:
            return np.zeros((0, semantic.DIM), dtype=np.float32)
        out = []
        for t in texts:
            seed = abs(hash(t)) % (2**32)
            rng = np.random.default_rng(seed)
            v = rng.standard_normal(semantic.DIM).astype(np.float32)
            v /= np.linalg.norm(v) + 1e-9
            out.append(v)
        return np.stack(out)

    monkeypatch.setattr(semantic, "_embed", fake_embed)

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/projects/foo.md", "projects", "foo", "Foo", ""),
        )
        conn.execute(
            "INSERT INTO facts (entity_id, text, source) VALUES (?, ?, ?)",
            (1, "alpha bravo", "src1"),
        )
        conn.execute(
            "INSERT INTO fts_facts (rowid, text, source) VALUES (1, 'alpha bravo', 'src1')"
        )

    return brain_dir


def _read_ledger(brain_dir):
    path = brain_dir / ".audit" / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# sanitize.VERSION constant
# ---------------------------------------------------------------------------


def test_version_constant_exists_and_format():
    assert hasattr(sanitize, "VERSION")
    assert isinstance(sanitize.VERSION, str)
    # `ws4-vN` lexicographic format — consumers may compare tuple(N,).
    assert sanitize.VERSION.startswith("ws4-v")


def test_sanitize_audit_entry_carries_scrub_tag(tmp_brain_with_db):
    # Trigger a sanitize hit that writes to sanitize.jsonl.
    sanitize.sanitize(
        "oops AKIAIOSFODNN7EXAMPLE",
        source_kind="note",
        source_path="t.md",
    )
    audit = tmp_brain_with_db / ".audit" / "sanitize.jsonl"
    assert audit.exists()
    entries = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    assert len(entries) == 1
    assert entries[0].get("scrub_tag") == sanitize.VERSION


# ---------------------------------------------------------------------------
# .vec/meta.json stamping
# ---------------------------------------------------------------------------


def test_build_stamps_scrub_tag_in_meta(tmp_brain_with_db):
    semantic.build()
    meta = json.loads(semantic.META_JSON.read_text())
    assert meta["scrub_tag"] == sanitize.VERSION


def test_meta_scrub_tag_helpers(tmp_brain_with_db):
    assert semantic._current_scrub_tag() == sanitize.VERSION
    # Pre-build: meta missing → None.
    assert semantic._meta_scrub_tag() is None
    semantic.build()
    assert semantic._meta_scrub_tag() == sanitize.VERSION


# ---------------------------------------------------------------------------
# ensure_built — force rebuild on mismatch
# ---------------------------------------------------------------------------


def test_ensure_built_migrates_on_missing_scrub_tag_without_rebuild(tmp_brain_with_db):
    """Pre-WS4-cross-ref vault: stamp the tag in-place, no rebuild.

    Forcing a full rebuild on every legacy vault penalises correct
    behaviour. We don't know whether the embedded content actually
    predates the current scrubber, only that the previous build didn't
    record which ruleset it ran. Safer migration: adopt current tag.
    """
    semantic.build()
    meta = json.loads(semantic.META_JSON.read_text())
    original_built_at = meta["built_at"]
    meta.pop("scrub_tag", None)
    semantic.META_JSON.write_text(json.dumps(meta, indent=2))

    # Drain the ledger so we can isolate the next op.
    ledger = tmp_brain_with_db / ".audit" / "ledger.jsonl"
    if ledger.exists():
        ledger.unlink()

    semantic.ensure_built()

    meta2 = json.loads(semantic.META_JSON.read_text())
    # Tag stamped in place.
    assert meta2["scrub_tag"] == sanitize.VERSION
    # built_at unchanged — no rebuild happened.
    assert meta2["built_at"] == original_built_at

    # Ledger carries an `init` op (distinct from `bump`).
    rows = _read_ledger(tmp_brain_with_db)
    init_rows = [r for r in rows if r.get("op") == "scrub_version_init"]
    bump_rows = [r for r in rows if r.get("op") == "scrub_version_bump_reingest"]
    assert len(init_rows) == 1
    assert init_rows[0]["target"]["new_scrub_tag"] == sanitize.VERSION
    assert not bump_rows


def test_ensure_built_forces_rebuild_on_mismatched_scrub_tag(
    tmp_brain_with_db, monkeypatch
):
    semantic.build()
    meta = json.loads(semantic.META_JSON.read_text())
    meta["scrub_tag"] = "ws4-v0"   # pretend the on-disk bundle is older
    semantic.META_JSON.write_text(json.dumps(meta, indent=2))
    old_built_at = meta["built_at"]

    semantic.ensure_built()

    meta2 = json.loads(semantic.META_JSON.read_text())
    assert meta2["scrub_tag"] == sanitize.VERSION
    assert meta2["built_at"] > old_built_at


def test_ensure_built_emits_scrub_version_bump_audit(
    tmp_brain_with_db, monkeypatch
):
    semantic.build()
    meta = json.loads(semantic.META_JSON.read_text())
    meta["scrub_tag"] = "ws4-v0"
    semantic.META_JSON.write_text(json.dumps(meta, indent=2))

    # Drain any earlier audit rows from the build() call.
    ledger = tmp_brain_with_db / ".audit" / "ledger.jsonl"
    if ledger.exists():
        ledger.unlink()

    semantic.ensure_built()

    rows = _read_ledger(tmp_brain_with_db)
    bump_rows = [r for r in rows if r.get("op") == "scrub_version_bump_reingest"]
    assert len(bump_rows) == 1
    t = bump_rows[0]["target"]
    assert t["old_scrub_tag"] == "ws4-v0"
    assert t["new_scrub_tag"] == sanitize.VERSION
    # reingested should reflect the pre-rebuild fact count (1 in the fixture).
    assert t["reingested"] == 1
    # WS5 hash-chain fields present.
    assert "prev_hash" in bump_rows[0]
    assert "hash" in bump_rows[0]
    assert bump_rows[0]["actor"] == "semantic.ensure_built"


def test_ensure_built_no_rebuild_when_scrub_tag_matches(
    tmp_brain_with_db, monkeypatch
):
    semantic.build()
    meta = json.loads(semantic.META_JSON.read_text())
    expected_built_at = meta["built_at"]
    # Drain ledger to isolate the next-call audit state.
    ledger = tmp_brain_with_db / ".audit" / "ledger.jsonl"
    if ledger.exists():
        ledger.unlink()

    # Current tag already in meta — `ensure_built` should be a no-op.
    semantic.ensure_built()

    meta2 = json.loads(semantic.META_JSON.read_text())
    # No-rebuild → built_at unchanged (incremental path is a noop
    # because there are no new rows, so `_has_new_rows` is False and
    # the meta is not touched).
    assert meta2["built_at"] == expected_built_at
    rows = _read_ledger(tmp_brain_with_db)
    assert not [r for r in rows if r.get("op") == "scrub_version_bump_reingest"]


def test_audit_counters_only_no_raw_fact_text(tmp_brain_with_db, monkeypatch):
    """The bump audit carries tags + count, never content. Regression
    guard against accidentally surfacing fact text into the ledger."""
    semantic.build()
    meta = json.loads(semantic.META_JSON.read_text())
    meta["scrub_tag"] = "ws4-v0"
    semantic.META_JSON.write_text(json.dumps(meta, indent=2))

    ledger = tmp_brain_with_db / ".audit" / "ledger.jsonl"
    if ledger.exists():
        ledger.unlink()

    semantic.ensure_built()

    body = (tmp_brain_with_db / ".audit" / "ledger.jsonl").read_text()
    # "alpha bravo" is the only fact body in the fixture — must NOT
    # appear in the ledger.
    assert "alpha bravo" not in body
    assert "src1" not in body    # source column also not content-leaked
