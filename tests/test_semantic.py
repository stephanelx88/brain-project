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
    # Each hit must carry path + date keys so hybrid_search's recency
    # factor and path-penalty can apply to semantic-only hits. Without
    # them, a fact found only via cosine silently skipped both adjustments.
    assert all("path" in r and "date" in r for r in res)
    assert any(r["path"] and r["path"].startswith("entities/projects/") for r in res)


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


def test_hybrid_preserves_cosine_for_dual_hit_facts(tmp_brain_with_db):
    """When a fact hits BOTH BM25 and semantic, `score` gets overwritten by
    the BM25 value (negative). The raw cosine must still be reachable via
    `sem_score` so brain_recall's semantic-fallback check can read it —
    otherwise any cross-branch hit silently zeroes out the fallback path.
    """
    semantic.build()
    res = semantic.hybrid_search("alpha", k=5)
    # "alpha bravo charlie" exists and is a token that BM25 can hit.
    dual = [h for h in res
            if h.get("semantic_rank") is not None
            and h.get("lexical_rank") is not None]
    assert dual, "expected at least one hit from both BM25 and semantic"
    for h in dual:
        assert "sem_score" in h, "cosine must be preserved under sem_score"
        # Cosine from normalised embeddings lives in [-1, 1]. BM25's
        # `score` column for the same hit is commonly ≪ -1 (SQLite FTS5
        # returns values like -0.15 … -5.0). As long as sem_score stays
        # in the cosine range, it's distinguishable from the BM25 value.
        assert -1.0 <= h["sem_score"] <= 1.0


# ---------- incremental update of facts/entities ------------------------


def _count_vec_rows(npy_path) -> int:
    import numpy as np
    return int(np.load(npy_path).shape[0])


def test_build_records_max_ids_in_meta(tmp_brain_with_db):
    """build() must write fact_max_id and entity_max_id so the incremental
    path can cheaply detect new rows."""
    semantic.build()
    meta = json.loads(semantic.META_JSON.read_text())
    assert meta["fact_max_id"] == 2                # two facts seeded
    assert meta["entity_max_id"] == 2              # two entities seeded


def test_has_new_rows_false_right_after_build(tmp_brain_with_db):
    semantic.build()
    assert semantic._has_new_rows() is False


def test_has_new_rows_true_when_db_has_new_fact(tmp_brain_with_db):
    from brain import db
    semantic.build()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO facts (entity_id, text, source) VALUES (?, ?, ?)",
            (1, "brand new fact", "src"),
        )
    assert semantic._has_new_rows() is True


def test_has_new_rows_true_when_meta_missing(tmp_brain_with_db):
    # Never built.
    assert not semantic.META_JSON.exists()
    assert semantic._has_new_rows() is True


def test_incremental_update_appends_new_fact_without_full_rebuild(
    tmp_brain_with_db
):
    """Common 10x-relevant scenario: an extraction just added one fact.
    The incremental path must embed only that fact and append it to
    facts.npy — not re-embed the existing corpus."""
    from brain import db
    semantic.build()
    before_rows = _count_vec_rows(semantic.FACTS_NPY)
    before_meta = json.loads(semantic.META_JSON.read_text())

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO facts (entity_id, text, source) VALUES (?, ?, ?)",
            (1, "fresh fact just landed", "src_live"),
        )

    out = semantic.incremental_update_facts_entities()
    assert out == {"facts_added": 1, "entities_added": 0, "incremental": True}

    after_rows = _count_vec_rows(semantic.FACTS_NPY)
    after_meta = json.loads(semantic.META_JSON.read_text())
    assert after_rows == before_rows + 1
    assert after_meta["fact_count"] == before_meta["fact_count"] + 1
    assert after_meta["fact_max_id"] > before_meta["fact_max_id"]


def test_incremental_update_appends_new_entity_without_full_rebuild(
    tmp_brain_with_db
):
    from brain import db
    semantic.build()
    before_rows = _count_vec_rows(semantic.ENT_NPY)
    before_meta = json.loads(semantic.META_JSON.read_text())

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO entities (path, type, slug, name, summary)"
            " VALUES (?, ?, ?, ?, ?)",
            ("entities/projects/gamma.md", "projects", "gamma",
             "Gamma Project", "new proj"),
        )

    out = semantic.incremental_update_facts_entities()
    assert out["entities_added"] == 1
    assert _count_vec_rows(semantic.ENT_NPY) == before_rows + 1
    after_meta = json.loads(semantic.META_JSON.read_text())
    assert after_meta["entity_count"] == before_meta["entity_count"] + 1
    assert after_meta["entity_max_id"] > before_meta["entity_max_id"]


def test_incremental_update_excludes_superseded_new_facts(
    tmp_brain_with_db
):
    """A fresh fact already born 'superseded' (e.g. an extraction race)
    must NOT be added to the semantic index — that's a BM25-only row
    and surfacing it via cosine would resurrect the bug the status
    filter was designed to prevent."""
    from brain import db
    semantic.build()
    before_rows = _count_vec_rows(semantic.FACTS_NPY)

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO facts (entity_id, text, source, status) VALUES (?, ?, ?, ?)",
            (1, "stale fact", "src", "superseded"),
        )

    out = semantic.incremental_update_facts_entities()
    # The superseded row still advances the high-water mark so we don't
    # keep re-probing it; only the embedded count stays flat.
    assert out["facts_added"] == 0
    assert _count_vec_rows(semantic.FACTS_NPY) == before_rows


def test_incremental_update_no_new_rows_is_noop(tmp_brain_with_db):
    semantic.build()
    before_meta = json.loads(semantic.META_JSON.read_text())
    out = semantic.incremental_update_facts_entities()
    assert out == {"facts_added": 0, "entities_added": 0, "incremental": True}
    # built_at refreshed even when nothing embedded (cheap freshness bump).
    after_meta = json.loads(semantic.META_JSON.read_text())
    assert after_meta["built_at"] >= before_meta["built_at"]


def test_incremental_update_falls_back_to_full_build_when_index_missing(
    tmp_brain_with_db
):
    """When someone wiped .vec/ between runs, incremental must not crash
    with FileNotFoundError — it must silently full-rebuild."""
    # Delete the fact index to simulate a partial-wipe state.
    semantic.build()
    semantic.FACTS_NPY.unlink()
    out = semantic.incremental_update_facts_entities()
    # Full build return shape is different (keys 'facts'/'entities').
    assert "facts" in out
    assert semantic.FACTS_NPY.exists()


def test_ensure_built_triggers_incremental_when_db_has_new_rows(
    tmp_brain_with_db
):
    """The freshness-blindness fix: any caller that went through
    ensure_built() used to see 0–6 hour stale recall. Now ensure_built
    picks up DB changes for free."""
    from brain import db
    semantic.build()
    before_rows = _count_vec_rows(semantic.FACTS_NPY)

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO facts (entity_id, text, source) VALUES (?, ?, ?)",
            (2, "just-extracted fact, must be recallable immediately", "src"),
        )

    # Default rebuild_if_stale=False, but incremental path still runs.
    semantic.ensure_built()
    assert _count_vec_rows(semantic.FACTS_NPY) == before_rows + 1


def test_search_facts_drops_stale_text_at_query_time(
    tmp_brain_with_db, monkeypatch
):
    """Stale-embedding guard (incident 2026-04-23 `Thuha ở Cần Thơ`):
    a fact's text is mutated in the DB after embedding; the `.npy`
    row still holds the old vector + meta. Without the fact_hash
    guard, cosine happily serves the ghost text with high confidence
    — exactly the "brain confidently returned wrong answer" failure."""
    from brain import db
    semantic.build()
    # Mutate fact 1's text in the DB AFTER indexing. The embedding +
    # meta for fact 1 still carry the original "alpha bravo charlie";
    # the DB now says something completely different.
    with db.connect() as conn:
        conn.execute(
            "UPDATE facts SET text='completely rewritten fact' WHERE id=1"
        )

    # Isolate the WS5 ledger to the test vault.
    from brain import _audit_ledger
    ledger_rows_before = 0
    if _audit_ledger.ledger_path().exists():
        ledger_rows_before = len(
            _audit_ledger.ledger_path().read_text().splitlines()
        )

    results = semantic.search_facts("alpha bravo", k=8)
    # Fact 1 must be dropped — its meta's fact_hash no longer matches
    # db.canonical_fact_hash(current_text).
    assert all(h["name"] != "Foo Project" for h in results), (
        "stale-text fact resurfaced via cosine — guard failed"
    )

    # And the WS5 audit ledger gained at least one `stale_snippet_served` row.
    lines = _audit_ledger.ledger_path().read_text().splitlines()
    new_rows = [json.loads(l) for l in lines[ledger_rows_before:]]
    assert any(
        r.get("op") == "stale_snippet_served" and r.get("target", {}).get("fact_id") == 1
        for r in new_rows
    ), f"no stale_snippet_served audit row for fact 1: {new_rows}"


def test_search_facts_preserves_hit_when_text_unchanged(tmp_brain_with_db):
    """Sanity flip of the guard: fact text stays in sync with DB,
    hit survives. Prevents an over-eager guard from regressing the
    happy path."""
    semantic.build()
    results = semantic.search_facts("alpha bravo", k=8)
    names = [h["name"] for h in results]
    assert "Foo Project" in names


def test_build_writes_fact_hash_to_meta(tmp_brain_with_db):
    """build() must attach canonical_fact_hash to every fact meta —
    without it, search_facts can't compare and the guard silently
    passes every hit."""
    from brain import db
    semantic.build()
    meta = json.loads(semantic.FACTS_JSON.read_text())
    assert meta, "empty fact meta"
    for m in meta:
        assert "fact_hash" in m, f"fact_hash missing on {m}"
        assert m["fact_hash"] == db.canonical_fact_hash(m["text"])


def test_count_stale_fact_meta_reports_stale(tmp_brain_with_db):
    """`brain doctor` consumer. After a DB-side text mutation, the
    doctor's stale-ratio check must surface the drift."""
    from brain import db
    semantic.build()
    with db.connect() as conn:
        conn.execute("UPDATE facts SET text='edited in place' WHERE id=2")
    report = semantic.count_stale_fact_meta()
    assert report["total"] == 2
    assert report["stale"] == 1
    assert report["ratio"] > 0


def test_search_facts_drops_superseded_at_query_time(tmp_brain_with_db):
    """Race condition: a fact indexed as active gets marked superseded
    between rebuilds. Without query-time filtering, cosine search keeps
    surfacing it because the embedding store is append-only."""
    from brain import db
    semantic.build()
    # Mark fact 1 as superseded AFTER indexing.
    with db.connect() as conn:
        conn.execute("UPDATE facts SET status='superseded' WHERE id=1")

    results = semantic.search_facts("alpha bravo", k=8)
    # Fact 1 ("alpha bravo charlie") must NOT be in results anymore.
    assert all(h["name"] != "Foo Project" for h in results), (
        "superseded fact resurfaced via cosine"
    )


def test_search_facts_survives_db_lookup_failure(
    tmp_brain_with_db, monkeypatch
):
    """If the status lookup fails (DB locked, wiped), search_facts must
    still return results rather than going silent. Recall is
    user-visible; status filtering is a soft guarantee."""
    from brain import db as db_module
    semantic.build()

    # Break connect() so the status probe raises.
    def boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(db_module, "connect", boom)

    # Must not raise; must still return something.
    results = semantic.search_facts("alpha", k=5)
    assert isinstance(results, list)


def test_ensure_built_incremental_failure_swallowed(
    tmp_brain_with_db, monkeypatch
):
    """Any failure in the incremental path must not break the recall hot
    path — the caller must still get a useable (if stale) index."""
    from brain import db
    semantic.build()

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO facts (entity_id, text, source) VALUES (?, ?, ?)",
            (1, "will fail to embed", "src"),
        )

    # Force incremental to blow up.
    def boom():
        raise RuntimeError("embedding backend exploded")
    monkeypatch.setattr(
        semantic, "incremental_update_facts_entities", boom
    )

    # Must not raise even though _has_new_rows is True.
    semantic.ensure_built()                        # no exception → pass



# ---------------------------------------------------------------------------
# invalidate_for — proactive cache invalidation (WS3 follow-up, 2026-04-23)
# ---------------------------------------------------------------------------

def test_invalidate_for_with_slug_pops_matching_rows(tmp_brain_with_db):
    semantic.build()
    before_meta = json.loads(semantic.FACTS_JSON.read_text())
    before_vecs = np.load(semantic.FACTS_NPY)
    assert any(m["slug"] == "foo" for m in before_meta)
    out = semantic.invalidate_for("projects", "foo")
    assert out["facts_dropped"] == 1
    assert out["entities_dropped"] == 1
    after_meta = json.loads(semantic.FACTS_JSON.read_text())
    after_vecs = np.load(semantic.FACTS_NPY)
    assert not any(m["slug"] == "foo" for m in after_meta)
    assert len(after_meta) == len(before_meta) - 1
    assert after_vecs.shape[0] == before_vecs.shape[0] - 1
    assert any(m["slug"] == "bar" for m in after_meta)


def test_invalidate_for_type_wide_drops_all_of_type(tmp_brain_with_db):
    semantic.build()
    semantic.invalidate_for("projects")
    fact_meta = json.loads(semantic.FACTS_JSON.read_text())
    assert not any(m["type"] == "projects" for m in fact_meta)
    ent_meta = json.loads(semantic.ENT_JSON.read_text())
    assert not any(m["type"] == "projects" for m in ent_meta)


def test_invalidate_for_nonexistent_is_noop(tmp_brain_with_db):
    semantic.build()
    before = json.loads(semantic.FACTS_JSON.read_text())
    out = semantic.invalidate_for("projects", "does-not-exist")
    assert out == {"facts_dropped": 0, "entities_dropped": 0}
    after = json.loads(semantic.FACTS_JSON.read_text())
    assert len(after) == len(before)


def test_invalidate_for_handles_missing_vec_files(tmp_brain_with_db):
    out = semantic.invalidate_for("projects", "foo")
    assert out == {"facts_dropped": 0, "entities_dropped": 0}


def test_upsert_entity_from_file_hook_calls_invalidate_for(tmp_path, monkeypatch):
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    from brain import db
    monkeypatch.setattr(db, "DB_PATH", brain_dir / ".brain.db")

    calls = []
    monkeypatch.setattr(semantic, "invalidate_for",
        lambda t, s=None: (calls.append((t, s)), {"facts_dropped": 0, "entities_dropped": 0})[1])

    entities_dir = brain_dir / "entities" / "projects"
    entities_dir.mkdir(parents=True)
    ef = entities_dir / "alphabetised.md"
    ef.write_text("---\ntype: projects\nname: Alphabetised\n---\n\n"
                  "# Alphabetised\n\n- first fact (source: seed, 2026-04-23)\n")
    db.upsert_entity_from_file(ef)
    assert ("projects", "alphabetised") in calls
