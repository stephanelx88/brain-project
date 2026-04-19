"""Tests for the semantic recall layer.

Uses a tiny stub embedder (deterministic, no network, no torch) so the
tests run in <1s and don't require sentence-transformers to be loaded.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from brain import semantic


@pytest.fixture
def tmp_brain_with_db(tmp_path, monkeypatch):
    """Set up a temp brain dir with a populated SQLite db + temp .vec/."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)

    # Wire the db module to a temp file
    from brain import db
    db_path = brain_dir / ".brain.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    # Wire semantic to temp dir
    monkeypatch.setattr(semantic, "VEC_DIR", brain_dir / ".vec")
    monkeypatch.setattr(semantic, "FACTS_NPY", brain_dir / ".vec" / "facts.npy")
    monkeypatch.setattr(semantic, "FACTS_JSON", brain_dir / ".vec" / "facts.json")
    monkeypatch.setattr(semantic, "ENT_NPY", brain_dir / ".vec" / "entities.npy")
    monkeypatch.setattr(semantic, "ENT_JSON", brain_dir / ".vec" / "entities.json")
    monkeypatch.setattr(semantic, "META_JSON", brain_dir / ".vec" / "meta.json")

    # Stub embedder: hash text bytes into a deterministic 8-d unit vector.
    def fake_embed(texts, batch_size=64):
        if not texts:
            return np.zeros((0, semantic.DIM), dtype=np.float32)
        rng = np.random.default_rng()
        out = []
        for t in texts:
            seed = abs(hash(t)) % (2**32)
            rng2 = np.random.default_rng(seed)
            v = rng2.standard_normal(semantic.DIM).astype(np.float32)
            v /= np.linalg.norm(v) + 1e-9
            out.append(v)
        return np.stack(out)

    monkeypatch.setattr(semantic, "_embed", fake_embed)

    # Populate the db
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/projects/foo.md", "projects", "foo", "Foo Project", "thing one"),
        )
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary) VALUES (?,?,?,?,?)",
            ("entities/projects/bar.md", "projects", "bar", "Bar Project", "thing two"),
        )
        conn.execute(
            "INSERT INTO facts (entity_id, text, source) VALUES (?, ?, ?)",
            (1, "alpha bravo charlie", "src1"),
        )
        conn.execute(
            "INSERT INTO facts (entity_id, text, source) VALUES (?, ?, ?)",
            (2, "delta echo foxtrot", "src2"),
        )
        conn.execute(
            "INSERT INTO fts_facts (rowid, text, source) VALUES (1, 'alpha bravo charlie', 'src1')"
        )
        conn.execute(
            "INSERT INTO fts_facts (rowid, text, source) VALUES (2, 'delta echo foxtrot', 'src2')"
        )

    return brain_dir


def test_build_writes_index(tmp_brain_with_db):
    out = semantic.build()
    assert out["facts"] == 2
    assert out["entities"] == 2
    assert semantic.FACTS_NPY.exists()
    assert semantic.META_JSON.exists()
    meta = json.loads(semantic.META_JSON.read_text())
    assert meta["fact_count"] == 2


def test_search_facts_returns_results(tmp_brain_with_db):
    semantic.build()
    res = semantic.search_facts("anything", k=2)
    assert len(res) == 2
    assert {r["name"] for r in res} == {"Foo Project", "Bar Project"}
    assert all("score" in r for r in res)


def test_search_facts_type_filter(tmp_brain_with_db):
    semantic.build()
    res = semantic.search_facts("anything", k=5, type="projects")
    assert all(r["type"] == "projects" for r in res)
    res2 = semantic.search_facts("anything", k=5, type="people")
    assert res2 == []


def test_status_reports_built(tmp_brain_with_db):
    assert semantic.status() == {"built": False}
    semantic.build()
    s = semantic.status()
    assert s["built"] is True
    assert s["fact_count"] == 2
    assert "age_hours" in s


def test_hybrid_search_fuses_branches(tmp_brain_with_db):
    semantic.build()
    res = semantic.hybrid_search("alpha", k=2)
    assert len(res) >= 1
    # alpha should win because BM25 hits the literal token
    assert any("alpha" in r["text"] for r in res)
    assert "rrf" in res[0]
