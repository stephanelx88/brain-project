"""Smoke tests for brain.mcp_server tool functions.

We call the Python functions directly (not over stdio) — the FastMCP
decorator preserves callable signatures, so this exercises the real
tool code paths against a temp brain dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_brain_for_mcp(tmp_path, monkeypatch):
    brain_dir = tmp_path / "brain"
    (brain_dir / "identity").mkdir(parents=True)
    (brain_dir / "identity" / "who-i-am.md").write_text("I am the test user.")
    (brain_dir / "identity" / "preferences.md").write_text("Prefer brevity.")

    # Disable the read-time freshness sweep inside brain_recall for these
    # tests. The fixture intentionally inserts SQLite rows that don't match
    # any on-disk markdown; _ensure_fresh would "helpfully" reconcile that
    # inconsistency and wipe the summary the tests depend on.
    monkeypatch.setenv("BRAIN_RECALL_ENSURE_FRESH", "0")

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(config, "IDENTITY_DIR", brain_dir / "identity")

    from brain import db
    db_path = brain_dir / ".brain.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    # Isolate the semantic index — without this, sem.ensure_built() and
    # search_facts() read from the real ~/.brain/.vec/ and leak the user's
    # entities into test results.
    from brain import semantic
    monkeypatch.setattr(semantic, "VEC_DIR", brain_dir / ".vec")
    monkeypatch.setattr(semantic, "FACTS_NPY", brain_dir / ".vec" / "facts.npy")
    monkeypatch.setattr(semantic, "FACTS_JSON", brain_dir / ".vec" / "facts.json")
    monkeypatch.setattr(semantic, "ENT_NPY", brain_dir / ".vec" / "entities.npy")
    monkeypatch.setattr(semantic, "ENT_JSON", brain_dir / ".vec" / "entities.json")
    monkeypatch.setattr(semantic, "NOTES_NPY", brain_dir / ".vec" / "notes.npy")
    monkeypatch.setattr(semantic, "NOTES_JSON", brain_dir / ".vec" / "notes.json")
    monkeypatch.setattr(semantic, "META_JSON", brain_dir / ".vec" / "meta.json")

    # Stub the embedder so tests don't download/load the 120 MB ST model.
    import numpy as np

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
            ("entities/projects/foo.md", "projects", "foo", "Foo Project", "thing one"),
        )
        conn.execute(
            "INSERT INTO aliases (entity_id, alias) VALUES (1, 'foo-alias')"
        )
        conn.execute(
            "INSERT INTO facts (entity_id, text, source) VALUES (1, 'alpha bravo charlie', 'src1')"
        )
        conn.execute(
            "INSERT INTO fts_facts (rowid, text, source) VALUES (1, 'alpha bravo charlie', 'src1')"
        )
        conn.execute(
            "INSERT INTO fts_entity (rowid, name, aliases, summary) VALUES (1, 'Foo Project', 'foo-alias', 'thing one')"
        )

    # Write the entity file so brain_get can read it
    (brain_dir / "entities" / "projects").mkdir(parents=True)
    (brain_dir / "entities" / "projects" / "foo.md").write_text(
        "---\ntype: project\nname: Foo Project\n---\n\n# Foo Project\n"
    )

    return brain_dir


def test_brain_search_returns_json(tmp_brain_for_mcp):
    from brain import mcp_server
    out = mcp_server.brain_search("alpha", k=3)
    env = json.loads(out)
    assert "hits" in env
    hits = env["hits"]
    assert len(hits) == 1
    assert hits[0]["name"] == "Foo Project"
    assert hits[0]["text"] == "alpha bravo charlie"


def test_brain_get_via_alias(tmp_brain_for_mcp):
    from brain import mcp_server
    out = mcp_server.brain_get("projects", "foo-alias")
    assert "Foo Project" in out
    assert "type: project" in out


def test_brain_get_missing(tmp_brain_for_mcp):
    from brain import mcp_server
    out = mcp_server.brain_get("projects", "no-such-thing")
    assert json.loads(out)["error"].startswith("not found")


def test_brain_identity_concatenates_files(tmp_brain_for_mcp):
    from brain import mcp_server
    out = mcp_server.brain_identity()
    assert "I am the test user." in out
    assert "Prefer brevity." in out


def test_brain_stats_counts(tmp_brain_for_mcp):
    from brain import mcp_server
    stats = json.loads(mcp_server.brain_stats())
    assert stats["entities"] == 1
    assert stats["facts"] == 1
    assert stats["by_type"] == {"projects": 1}


# ---------- live-session tools ---------------------------------------------

def _write_cursor_jsonl(cursor_root: Path, workspace: str, sid: str,
                        entries: list[dict]) -> Path:
    session_dir = cursor_root / "projects" / workspace / "agent-transcripts" / sid
    session_dir.mkdir(parents=True)
    jsonl = session_dir / f"{sid}.jsonl"
    jsonl.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return jsonl


def _write_claude_session(claude_root: Path, sid: str, pid: int, cwd: str,
                          entries: list[dict] | None = None) -> Path:
    sessions_dir = claude_root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{pid}.json").write_text(
        json.dumps({"sessionId": sid, "pid": pid, "cwd": cwd})
    )
    projects_dir = claude_root / "projects" / "Users-x-foo"
    projects_dir.mkdir(parents=True, exist_ok=True)
    jsonl = projects_dir / f"{sid}.jsonl"
    if entries is not None:
        jsonl.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    else:
        jsonl.write_text("")
    return jsonl


def test_brain_live_sessions_lists_recent_cursor(tmp_path, monkeypatch):
    from brain import harvest_session, mcp_server
    cursor_root = tmp_path / "cursor"
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", cursor_root / "projects")
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", tmp_path / "no-claude")
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", tmp_path / "no-claude" / "projects")
    monkeypatch.setenv("USER", "x")

    _write_cursor_jsonl(cursor_root, "Users-x-code-myproj", "uuid-fresh", [
        {"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
    ])

    out = json.loads(mcp_server.brain_live_sessions(active_within_sec=300))
    assert len(out) == 1
    assert out[0]["source"] == "cursor"
    assert out[0]["session_id"] == "cursor:uuid-fresh"
    assert out[0]["project"] == "cursor/code/myproj"
    assert out[0]["age_sec"] is not None and out[0]["age_sec"] < 60


def test_brain_live_sessions_filters_old_cursor(tmp_path, monkeypatch):
    import os
    import time
    from brain import harvest_session, mcp_server
    cursor_root = tmp_path / "cursor"
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", cursor_root / "projects")
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", tmp_path / "no-claude")
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", tmp_path / "no-claude" / "projects")

    jsonl = _write_cursor_jsonl(cursor_root, "Users-x-foo", "uuid-stale", [
        {"role": "user", "message": {"content": "x"}},
    ])
    old = time.time() - 10_000
    os.utime(jsonl, (old, old))

    out = json.loads(mcp_server.brain_live_sessions(active_within_sec=300))
    assert out == []


def test_brain_live_sessions_includes_alive_claude(tmp_path, monkeypatch):
    import os
    from brain import harvest_session, mcp_server
    claude_root = tmp_path / "claude"
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", claude_root)
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", claude_root / "projects")
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", tmp_path / "no-cursor")

    my_pid = os.getpid()
    _write_claude_session(claude_root, "claude-uuid-1", my_pid, "/tmp/work", entries=[
        {"type": "user", "message": {"content": [{"type": "text", "text": "live"}]}},
    ])

    out = json.loads(mcp_server.brain_live_sessions(active_within_sec=300))
    claude_rows = [r for r in out if r["source"] == "claude"]
    assert len(claude_rows) == 1
    assert claude_rows[0]["session_id"] == "claude-uuid-1"
    assert claude_rows[0]["pid"] == my_pid
    assert claude_rows[0]["cwd"] == "/tmp/work"


def test_brain_live_sessions_skips_dead_claude(tmp_path, monkeypatch):
    from brain import harvest_session, mcp_server
    claude_root = tmp_path / "claude"
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", claude_root)
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", claude_root / "projects")
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", tmp_path / "no-cursor")

    _write_claude_session(claude_root, "claude-dead", 999_999, "/tmp/dead")

    out = json.loads(mcp_server.brain_live_sessions(active_within_sec=300))
    assert out == []


def test_brain_live_tail_returns_last_n_turns(tmp_path, monkeypatch):
    from brain import harvest_session, mcp_server
    cursor_root = tmp_path / "cursor"
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", cursor_root / "projects")
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", tmp_path / "no-claude")
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", tmp_path / "no-claude" / "projects")

    entries = []
    for i in range(5):
        entries.append({"role": "user", "message": {"content": [{"type": "text", "text": f"u{i}"}]}})
        entries.append({"role": "assistant", "message": {"content": [{"type": "text", "text": f"a{i}"}]}})
    _write_cursor_jsonl(cursor_root, "Users-x-foo", "uuid-tail", entries)

    out = json.loads(mcp_server.brain_live_tail("cursor:uuid-tail", n=3))
    assert out["source"] == "cursor"
    assert out["session_id"] == "cursor:uuid-tail"
    assert out["total_turns"] == 10
    assert len(out["turns"]) == 3
    assert out["turns"][-1]["text"] == "a4"
    assert out["turns"][0]["text"] == "a3"


def test_brain_live_tail_accepts_bare_cursor_uuid(tmp_path, monkeypatch):
    from brain import harvest_session, mcp_server
    cursor_root = tmp_path / "cursor"
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", cursor_root / "projects")
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", tmp_path / "no-claude")
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", tmp_path / "no-claude" / "projects")

    _write_cursor_jsonl(cursor_root, "Users-x-foo", "bare-uuid", [
        {"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
    ])
    out = json.loads(mcp_server.brain_live_tail("bare-uuid", n=10))
    assert "error" not in out
    assert out["turns"][0]["text"] == "hi"


def test_brain_live_tail_unknown_session(tmp_path, monkeypatch):
    from brain import harvest_session, mcp_server
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", tmp_path / "no-cursor")
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", tmp_path / "no-claude")
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", tmp_path / "no-claude" / "projects")

    out = json.loads(mcp_server.brain_live_tail("does-not-exist"))
    assert out["error"].startswith("session not found")


def test_brain_live_tail_returns_claude_session(tmp_path, monkeypatch):
    import os
    from brain import harvest_session, mcp_server
    claude_root = tmp_path / "claude"
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", claude_root)
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", claude_root / "projects")
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", tmp_path / "no-cursor")

    my_pid = os.getpid()
    _write_claude_session(claude_root, "claude-tail-1", my_pid, "/tmp/work", entries=[
        {"type": "user", "message": {"content": [{"type": "text", "text": "hello claude"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello back"}]}},
    ])

    out = json.loads(mcp_server.brain_live_tail("claude-tail-1", n=5))
    assert "error" not in out
    assert out["source"] == "claude"
    assert out["session_id"] == "claude-tail-1"
    assert out["total_turns"] == 2
    assert out["turns"][-1]["text"] == "hello back"


def test_brain_live_sessions_excludes_self_by_default(tmp_path, monkeypatch):
    import os
    from brain import harvest_session, mcp_server
    claude_root = tmp_path / "claude"
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", claude_root)
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", claude_root / "projects")
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", tmp_path / "no-cursor")

    self_pid = os.getppid()
    peer_pid = os.getpid()
    _write_claude_session(claude_root, "claude-self", self_pid, "/tmp/self")
    _write_claude_session(claude_root, "claude-peer", peer_pid, "/tmp/peer")

    out = json.loads(mcp_server.brain_live_sessions(active_within_sec=300))
    sids = {r["session_id"] for r in out}
    assert sids == {"claude-peer"}


def test_brain_live_sessions_include_self_returns_all(tmp_path, monkeypatch):
    import os
    from brain import harvest_session, mcp_server
    claude_root = tmp_path / "claude"
    monkeypatch.setattr(harvest_session, "CLAUDE_DIR", claude_root)
    monkeypatch.setattr(harvest_session, "PROJECTS_DIR", claude_root / "projects")
    monkeypatch.setattr(harvest_session, "CURSOR_PROJECTS_DIR", tmp_path / "no-cursor")

    self_pid = os.getppid()
    peer_pid = os.getpid()
    _write_claude_session(claude_root, "claude-self", self_pid, "/tmp/self")
    _write_claude_session(claude_root, "claude-peer", peer_pid, "/tmp/peer")

    out = json.loads(mcp_server.brain_live_sessions(active_within_sec=300, include_self=True))
    sids = {r["session_id"] for r in out}
    assert sids == {"claude-self", "claude-peer"}


# ---------- brain_recall envelope + weak_match ---------------------------

def _call_brain_recall_with_stubbed_hits(monkeypatch, hits, *,
                                         query="q", env=None, debug=True):
    """Stub out the semantic layer so brain_recall returns `hits` verbatim.

    Yields the parsed JSON envelope. Avoids pulling torch / the real
    embedding index into the unit test. `debug=True` by default so the
    envelope carries the `top_score`/`threshold` diagnostics these tests
    inspect — real agent callers get the compact default.
    """
    from brain import mcp_server

    class _FakeSemantic:
        @staticmethod
        def ensure_built():
            pass

        @staticmethod
        def hybrid_search(q, k=8, type=None):
            return list(hits)

    monkeypatch.setattr(mcp_server, "_semantic", lambda: _FakeSemantic)

    class _NoLog:
        @staticmethod
        def log_live_recall(q):
            pass

    monkeypatch.setattr(
        "brain.recall_metric.log_live_recall",
        _NoLog.log_live_recall,
        raising=False,
    )
    for key, val in (env or {}).items():
        monkeypatch.setenv(key, val)
    return json.loads(mcp_server.brain_recall(query, debug=debug))


def test_brain_recall_envelope_has_expected_keys(monkeypatch):
    # Default tier: compact envelope — top_score/threshold move behind debug.
    out = _call_brain_recall_with_stubbed_hits(monkeypatch, hits=[
        {"kind": "fact", "name": "Foo", "text": "alpha", "rrf": 0.08},
    ], debug=False)
    assert set(out.keys()) == {"query", "weak_match", "guidance", "hits"}
    assert out["query"] == "q"
    assert isinstance(out["hits"], list)


def test_brain_recall_debug_envelope_includes_diagnostics(monkeypatch):
    # debug=True adds the tuning signals back in.
    from brain import mcp_server

    class _FakeSemantic:
        @staticmethod
        def ensure_built(): pass
        @staticmethod
        def hybrid_search(q, k=8, type=None):
            return [{"kind": "fact", "name": "Foo", "text": "alpha", "rrf": 0.08}]

    monkeypatch.setattr(mcp_server, "_semantic", lambda: _FakeSemantic)
    monkeypatch.setattr("brain.recall_metric.log_live_recall",
                        lambda q: None, raising=False)
    out = json.loads(mcp_server.brain_recall("q", debug=True))
    assert {"top_score", "threshold", "fetch_k",
            "rerank_on", "query_rewriter_on"} <= set(out.keys())


def test_brain_recall_fact_hits_include_entity_summary(tmp_brain_for_mcp, monkeypatch):
    """Fact hits must carry entity_summary so the agent doesn't need brain_get."""
    from brain import mcp_server

    class _FakeSemantic:
        @staticmethod
        def ensure_built():
            pass

        @staticmethod
        def hybrid_search(q, k=8, type=None):
            return [{
                "kind": "fact",
                "type": "projects",
                "name": "Foo Project",
                "text": "alpha bravo charlie",
                "rrf": 0.09,
            }]

    monkeypatch.setattr(mcp_server, "_semantic", lambda: _FakeSemantic)
    monkeypatch.setattr("brain.recall_metric.log_live_recall",
                        lambda q: None, raising=False)

    out = json.loads(mcp_server.brain_recall("alpha"))
    hits = out["hits"]
    assert len(hits) == 1
    assert hits[0]["entity_summary"] == "thing one"


def test_brain_recall_note_hits_have_no_entity_summary(monkeypatch):
    """Note hits must not get entity_summary — they're not entities."""
    out = _call_brain_recall_with_stubbed_hits(monkeypatch, hits=[
        {"kind": "note", "path": "foo.md", "snippet": "some note", "rrf": 0.07},
    ])
    assert "entity_summary" not in out["hits"][0]


def test_brain_recall_entity_summary_absent_when_empty(tmp_brain_for_mcp, monkeypatch):
    """Fact hit for entity with no summary must not include the key."""
    from brain import db, mcp_server

    # Clear the summary
    with db.connect() as conn:
        conn.execute("UPDATE entities SET summary=NULL WHERE name='Foo Project'")

    class _FakeSemantic:
        @staticmethod
        def ensure_built():
            pass

        @staticmethod
        def hybrid_search(q, k=8, type=None):
            return [{
                "kind": "fact", "type": "projects", "name": "Foo Project",
                "text": "alpha bravo charlie", "rrf": 0.09,
            }]

    monkeypatch.setattr(mcp_server, "_semantic", lambda: _FakeSemantic)
    monkeypatch.setattr("brain.recall_metric.log_live_recall",
                        lambda q: None, raising=False)

    out = json.loads(mcp_server.brain_recall("alpha"))
    assert "entity_summary" not in out["hits"][0]


def test_brain_recall_strong_match_clears_weak_flag(monkeypatch):
    out = _call_brain_recall_with_stubbed_hits(monkeypatch, hits=[
        {"kind": "fact", "name": "Foo", "text": "alpha", "rrf": 0.08},
        {"kind": "fact", "name": "Bar", "text": "beta", "rrf": 0.05},
    ])
    assert out["weak_match"] is False
    assert out["guidance"] is None
    assert out["top_score"] == 0.08
    assert len(out["hits"]) == 2


def test_brain_recall_weak_match_flags_and_guides(monkeypatch):
    #  0.026 matches the real "đôi dép tôi đâu" failure (2026-04-21).
    out = _call_brain_recall_with_stubbed_hits(monkeypatch, hits=[
        {"kind": "note", "path": "Thuha va Trinh.md",
         "snippet": "gio ho ve long xuyen roi", "rrf": 0.026},
        {"kind": "fact", "name": "Other", "text": "x", "rrf": 0.025},
    ])
    assert out["weak_match"] is True
    assert out["top_score"] == 0.026
    assert out["guidance"] is not None
    #  Guidance must steer the agent away from fabrication.
    assert "fabricate" in out["guidance"].lower() \
        or "not literally" in out["guidance"].lower() \
        or "do not" in out["guidance"].lower()


def test_brain_recall_empty_result_is_flagged_weak(monkeypatch):
    out = _call_brain_recall_with_stubbed_hits(monkeypatch, hits=[])
    assert out["weak_match"] is True
    assert out["top_score"] == 0.0
    assert out["hits"] == []
    assert out["guidance"] and "no record" in out["guidance"].lower()


def test_brain_recall_threshold_boundary_just_above(monkeypatch):
    #  Top score exactly at default threshold (0.035) must NOT flag weak.
    out = _call_brain_recall_with_stubbed_hits(monkeypatch, hits=[
        {"kind": "fact", "name": "A", "text": "t", "rrf": 0.035},
    ])
    assert out["weak_match"] is False
    assert out["top_score"] == 0.035


def test_brain_recall_threshold_boundary_just_below(monkeypatch):
    out = _call_brain_recall_with_stubbed_hits(monkeypatch, hits=[
        {"kind": "fact", "name": "A", "text": "t", "rrf": 0.0349},
    ])
    assert out["weak_match"] is True


def test_brain_recall_threshold_env_override(monkeypatch):
    #  Raise the bar so a previously-strong score is now weak.
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        hits=[{"kind": "fact", "name": "A", "text": "t", "rrf": 0.05}],
        env={"BRAIN_RECALL_WEAK_RRF": "0.08"},
    )
    assert out["weak_match"] is True
    assert out["threshold"] == 0.08


def test_brain_recall_threshold_env_invalid_falls_back_to_default(monkeypatch):
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        hits=[{"kind": "fact", "name": "A", "text": "t", "rrf": 0.05}],
        env={"BRAIN_RECALL_WEAK_RRF": "not-a-number"},
    )
    assert out["weak_match"] is False
    assert out["threshold"] == 0.035


# ---------- Option A: non-ASCII threshold scaling -------------------------

def test_brain_recall_vietnamese_query_scaled_threshold(monkeypatch):
    # "thuha o dau" observed rrf=0.0259 — below 0.035 but above scaled threshold
    # (0.035 * 0.55 = 0.01925). Should NOT be weak_match.
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        query="thuha ở đâu",
        hits=[{"kind": "fact", "name": "Thuha", "text": "Thuha is at the beach",
               "rrf": 0.0259}],
    )
    assert out["weak_match"] is False, (
        f"Vietnamese query should use scaled threshold; got top_score={out['top_score']}, "
        f"threshold={out['threshold']}"
    )


def test_brain_recall_ascii_query_unchanged_threshold(monkeypatch):
    # Pure ASCII query: threshold must NOT be scaled
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        query="where is Thuha",
        hits=[{"kind": "fact", "name": "Thuha", "text": "Thuha is at the beach",
               "rrf": 0.0259}],
    )
    # rrf=0.0259 < 0.035 → weak for ASCII
    assert out["weak_match"] is True


def test_brain_recall_non_ascii_scale_env_override(monkeypatch):
    # Confirm scale is tunable via env var
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        query="thuha ở đâu",
        hits=[{"kind": "fact", "name": "Thuha", "text": "Thuha is at the beach",
               "rrf": 0.025}],
        env={"BRAIN_RECALL_NON_ASCII_SCALE": "0.8"},
    )
    # threshold = 0.035 * 0.8 = 0.028 > 0.025 → still weak
    assert out["weak_match"] is True


# ---------- Option C: semantic fallback override --------------------------

def test_brain_recall_semantic_fallback_overrides_weak_rrf(monkeypatch):
    # BM25 missed (no lexical_rank), semantic found confident hit (score=0.35)
    # RRF is low (0.020) but semantic score > 0.20 threshold → not weak_match
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        query="where is Thuha",
        hits=[{
            "kind": "fact", "name": "Thuha",
            "text": "Thuha is at the beach",
            "rrf": 0.020,
            "score": 0.35,          # cosine-sim from semantic branch
            "semantic_rank": 0,
        }],
    )
    assert out["weak_match"] is False, (
        "Semantic score 0.35 >= fallback threshold 0.20 should override weak_match"
    )


def test_brain_recall_semantic_fallback_below_threshold_stays_weak(monkeypatch):
    # Semantic score too low (0.12) — should remain weak_match
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        query="unrelated query",
        hits=[{
            "kind": "fact", "name": "Foo", "text": "something",
            "rrf": 0.018,
            "score": 0.12,
            "semantic_rank": 0,
        }],
    )
    assert out["weak_match"] is True


def test_brain_recall_semantic_fallback_no_semantic_rank_stays_weak(monkeypatch):
    # Hit came from BM25 only (no semantic_rank) — Option C should not fire
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        query="some query",
        hits=[{
            "kind": "fact", "name": "Foo", "text": "something",
            "rrf": 0.018,
            "score": 0.50,          # high score but no semantic_rank
            "lexical_rank": 0,
        }],
    )
    assert out["weak_match"] is True


def test_brain_recall_semantic_fallback_env_override(monkeypatch):
    # Raise fallback threshold so score=0.25 doesn't trigger
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        query="where is Thuha",
        hits=[{
            "kind": "fact", "name": "Thuha",
            "text": "Thuha is at the beach",
            "rrf": 0.018,
            "score": 0.25,
            "semantic_rank": 0,
        }],
        env={"BRAIN_RECALL_SEMANTIC_FALLBACK": "0.30"},
    )
    # 0.25 < 0.30 → no override → still weak
    assert out["weak_match"] is True


def test_brain_recall_both_options_vietnamese_with_semantic(monkeypatch):
    # Realistic case: Vietnamese query, BM25 miss, semantic hit (score=0.36)
    # Option A scales threshold; Option C fires regardless — both protect the hit
    out = _call_brain_recall_with_stubbed_hits(
        monkeypatch,
        query="thuha ở đâu",
        hits=[{
            "kind": "fact", "name": "Thuha",
            "text": "Thuha is at the beach",
            "rrf": 0.0259,
            "score": 0.36,
            "semantic_rank": 0,
        }],
    )
    assert out["weak_match"] is False


# ---------- brain_search hybrid (BM25 + semantic fallback) ------------------

def _stub_semantic_for_search(monkeypatch, *, fact_hits=None, entity_hits=None):
    """Replace brain.semantic with a fake that returns controlled hits."""
    from brain import mcp_server

    class _FakeSemantic:
        @staticmethod
        def ensure_built():
            pass

        @staticmethod
        def search_facts(query, k=8, type=None):
            return list(fact_hits or [])

        @staticmethod
        def search_entities(query, k=8):
            return list(entity_hits or [])

    monkeypatch.setattr(mcp_server, "_semantic", lambda: _FakeSemantic)


def test_brain_search_bm25_hit_no_semantic_needed(tmp_brain_for_mcp, monkeypatch):
    """BM25 finds a hit — semantic results deduped out."""
    _stub_semantic_for_search(monkeypatch, fact_hits=[{
        "type": "projects", "name": "Foo Project", "slug": "foo",
        "text": "alpha bravo charlie", "source": "src1", "score": 0.9,
    }])
    from brain import mcp_server
    env = json.loads(mcp_server.brain_search("alpha", k=5))
    hits = env["hits"]
    assert len(hits) == 1
    assert hits[0]["name"] == "Foo Project"


def test_brain_search_semantic_fills_when_bm25_empty(tmp_brain_for_mcp, monkeypatch):
    """Non-ASCII query: BM25 returns nothing, semantic backfills."""
    _stub_semantic_for_search(monkeypatch, fact_hits=[{
        "type": "people", "name": "Thuha", "slug": "thuha",
        "text": "Thuha lives in Long Xuyen", "source": "note:thuha.md", "score": 0.72,
    }])
    from brain import mcp_server
    hits = json.loads(mcp_server.brain_search("thuha ở đâu", k=5))["hits"]
    # BM25 returns nothing for Vietnamese query; semantic backfills
    assert len(hits) >= 1
    assert any(r["name"] == "Thuha" for r in hits)


def test_brain_search_semantic_respects_k_cap(tmp_brain_for_mcp, monkeypatch):
    """Semantic results respect the k cap even when BM25 is empty."""
    many_hits = [
        {"type": "people", "name": f"P{i}", "slug": f"p{i}",
         "text": f"fact {i}", "source": "s", "score": 0.5}
        for i in range(10)
    ]
    _stub_semantic_for_search(monkeypatch, fact_hits=many_hits)
    from brain import mcp_server
    hits = json.loads(mcp_server.brain_search("unmatched query", k=3))["hits"]
    assert len(hits) <= 3


def test_brain_search_dedup_across_bm25_and_semantic(tmp_brain_for_mcp, monkeypatch):
    """Same fact from BM25 and semantic must not appear twice."""
    _stub_semantic_for_search(monkeypatch, fact_hits=[{
        "type": "projects", "name": "Foo Project", "slug": "foo",
        "text": "alpha bravo charlie", "source": "src1", "score": 0.8,
    }])
    from brain import mcp_server
    hits = json.loads(mcp_server.brain_search("alpha", k=10))["hits"]
    texts = [r["text"] for r in hits]
    assert texts.count("alpha bravo charlie") == 1


# ---------- brain_entities hybrid -------------------------------------------

def test_brain_entities_bm25_hit_returned(tmp_brain_for_mcp, monkeypatch):
    _stub_semantic_for_search(monkeypatch, entity_hits=[])
    from brain import mcp_server
    hits = json.loads(mcp_server.brain_entities("Foo", k=5))["hits"]
    assert any(r["name"] == "Foo Project" for r in hits)


def test_brain_entities_semantic_fills_when_bm25_empty(tmp_brain_for_mcp, monkeypatch):
    """Vietnamese entity name: BM25 returns nothing, semantic backfills."""
    _stub_semantic_for_search(monkeypatch, entity_hits=[{
        "type": "people", "name": "Nguyễn Thị Thu Hà", "slug": "thu-ha",
        "path": "entities/people/thu-ha.md", "summary": "lives in HCMC",
        "score": 0.81,
    }])
    from brain import mcp_server
    hits = json.loads(mcp_server.brain_entities("thu ha", k=5))["hits"]
    # BM25 finds nothing for "thu ha" (no entity named that in fixture)
    # but semantic fills in Nguyễn Thị Thu Hà
    assert any(r["name"] == "Nguyễn Thị Thu Hà" for r in hits)


# ---------------------------------------------------------------------------
# _ensure_fresh runs on every read-path tool, not just brain_recall.
#
# Motivating incident 2026-04-23: a user wrote `son.md` into the vault
# root, asked about it via claude, and claude's brain call routed
# through `brain_notes` / `brain_search` — neither of which refreshed
# — so the just-written note never reached the index and claude
# truthfully reported "brain has no record". The fix makes the sweep
# uniform across read tools, throttled so back-to-back calls don't pay
# the stat-sweep tax three times in a row.
# ---------------------------------------------------------------------------


def test_ensure_fresh_throttle_skips_back_to_back_calls(monkeypatch):
    """Second call inside the throttle window must be a no-op."""
    from brain import mcp_server

    # Allow the real sweep to run (not env-disabled). Stub the three
    # expensive branches so the only observable effect is whether
    # `_LAST_FRESH_TICK` advances.
    monkeypatch.setenv("BRAIN_RECALL_ENSURE_FRESH", "1")
    monkeypatch.setenv("BRAIN_RECALL_FRESH_THROTTLE_SEC", "10.0")
    from brain import db as _db
    monkeypatch.setattr(_db, "sync_mutated_entities", lambda: None)
    monkeypatch.setattr(_db, "gc_orphaned_entities", lambda: None)
    import sys
    import types
    stub_ingest = types.ModuleType("brain.ingest_notes")
    stub_ingest.ingest_all = lambda: None
    monkeypatch.setitem(sys.modules, "brain.ingest_notes", stub_ingest)
    monkeypatch.setattr(mcp_server, "_semantic",
                        lambda: types.SimpleNamespace(ensure_built=lambda: None))

    mcp_server._LAST_FRESH_TICK = 0.0
    mcp_server._ensure_fresh()
    first = mcp_server._LAST_FRESH_TICK
    assert first > 0.0

    mcp_server._ensure_fresh()
    # Throttle window is 10 s in this test — tick must not have moved.
    assert mcp_server._LAST_FRESH_TICK == first


def test_ensure_fresh_env_disable_short_circuits(monkeypatch):
    """BRAIN_RECALL_ENSURE_FRESH=0 makes _ensure_fresh a pure no-op."""
    from brain import mcp_server
    monkeypatch.setenv("BRAIN_RECALL_ENSURE_FRESH", "0")
    mcp_server._LAST_FRESH_TICK = 0.0
    mcp_server._ensure_fresh()
    # Env-disable must NOT update the tick (which would otherwise falsely
    # throttle the next enabled call a millisecond later).
    assert mcp_server._LAST_FRESH_TICK == 0.0


def test_read_tools_all_call_ensure_fresh(tmp_brain_for_mcp, monkeypatch):
    """brain_search / brain_entities / brain_notes / brain_recent /
    brain_recall / brain_semantic each call `_ensure_fresh` exactly once.

    Regression test for the `son.md` incident (2026-04-23): previously
    only brain_recall refreshed, so a note-add immediately followed by
    a brain_notes / brain_search / brain_note_get query missed the note.
    """
    from brain import mcp_server

    calls: list[int] = []

    def spy():
        calls.append(1)
    monkeypatch.setattr(mcp_server, "_ensure_fresh", spy)

    # Stub semantic side so the tools don't call the real (slow) index.
    from brain import semantic
    import numpy as np
    monkeypatch.setattr(semantic, "search_facts", lambda q, k=8, type=None: [])
    monkeypatch.setattr(semantic, "search_entities", lambda q, k=8: [])
    monkeypatch.setattr(semantic, "search_notes", lambda q, k=8: [])
    monkeypatch.setattr(semantic, "ensure_built", lambda: None)
    monkeypatch.setattr(
        semantic, "_embed",
        lambda texts, batch_size=64: np.zeros((len(texts), semantic.DIM), dtype=np.float32),
    )

    mcp_server.brain_search("q", k=3)
    mcp_server.brain_entities("q", k=3)
    mcp_server.brain_notes("q", k=3)
    mcp_server.brain_recent(hours=1, k=3)
    mcp_server.brain_recall("q", k=3)
    mcp_server.brain_semantic("q", k=3)

    assert len(calls) == 6, (
        f"each of 6 read tools should call _ensure_fresh once; got {len(calls)}"
    )


def test_brain_entities_dedup_across_bm25_and_semantic(tmp_brain_for_mcp, monkeypatch):
    """Same entity from BM25 and semantic must appear once."""
    _stub_semantic_for_search(monkeypatch, entity_hits=[{
        "type": "projects", "name": "Foo Project", "slug": "foo",
        "path": "entities/projects/foo.md", "summary": "thing one",
        "score": 0.7,
    }])
    from brain import mcp_server
    hits = json.loads(mcp_server.brain_entities("Foo", k=10))["hits"]
    names = [r["name"] for r in hits]
    assert names.count("Foo Project") == 1


# ---------------------------------------------------------------------------
# _ensure_fresh is called from every read-path tool (not just brain_recall)
#
# Motivating incident 2026-04-23: a user wrote `son.md` into the vault
# root, asked about it via claude, and claude's brain call routed
# through `brain_notes` / `brain_note_get` / `brain_search` — none of
# which refreshed — so the just-written note never reached the index
# and claude truthfully reported "brain has no record". The fix makes
# the sweep uniform across read tools, throttled so back-to-back calls
# don't pay the stat-sweep tax three times in a row.
# ---------------------------------------------------------------------------


def _count_ensure_fresh_calls(monkeypatch):
    """Patch `_ensure_fresh` to a counter and return the counter list.

    The mutation stays out of the env-gated short-circuit path so we
    measure call *intent* from each tool regardless of whether the real
    sweep runs. Returns a list whose len = number of calls observed.
    """
    from brain import mcp_server
    calls: list[int] = []
    orig = mcp_server._ensure_fresh

    def spy():
        calls.append(1)
        # Do NOT delegate — tests for *which tool calls this* don't want
        # the real sweep mutating fixture state.
    monkeypatch.setattr(mcp_server, "_ensure_fresh", spy)
    return calls, orig


def test_ensure_fresh_throttle_skips_back_to_back_calls(monkeypatch):
    """Second call inside the throttle window is a no-op."""
    from brain import mcp_server

    # Allow the real sweep to run (not env-disabled) by stubbing the
    # three expensive branches so the only observable effect is whether
    # `_LAST_FRESH_TICK` advances.
    monkeypatch.setenv("BRAIN_RECALL_ENSURE_FRESH", "1")
    monkeypatch.setenv("BRAIN_RECALL_FRESH_THROTTLE_SEC", "10.0")
    from brain import db as _db
    monkeypatch.setattr(_db, "sync_mutated_entities", lambda: None)
    monkeypatch.setattr(_db, "gc_orphaned_entities", lambda: None)
    import sys, types
    stub_ingest = types.ModuleType("brain.ingest_notes")
    stub_ingest.ingest_all = lambda: None
    monkeypatch.setitem(sys.modules, "brain.ingest_notes", stub_ingest)
    monkeypatch.setattr(mcp_server, "_semantic", lambda: types.SimpleNamespace(ensure_built=lambda: None))

    mcp_server._LAST_FRESH_TICK = 0.0
    mcp_server._ensure_fresh()
    first = mcp_server._LAST_FRESH_TICK
    assert first > 0.0

    mcp_server._ensure_fresh()
    # Throttle window is 10 s in this test — tick must not have moved.
    assert mcp_server._LAST_FRESH_TICK == first


def test_ensure_fresh_env_disable_short_circuits(monkeypatch):
    """BRAIN_RECALL_ENSURE_FRESH=0 makes _ensure_fresh a pure no-op."""
    from brain import mcp_server
    monkeypatch.setenv("BRAIN_RECALL_ENSURE_FRESH", "0")
    mcp_server._LAST_FRESH_TICK = 0.0
    mcp_server._ensure_fresh()
    # Env disable must NOT update the tick (which would falsely throttle
    # the next enabled call a millisecond later).
    assert mcp_server._LAST_FRESH_TICK == 0.0


def test_read_tools_all_call_ensure_fresh(tmp_brain_for_mcp, monkeypatch):
    """brain_search / brain_entities / brain_notes / brain_recent /
    brain_recall / brain_semantic each call `_ensure_fresh` exactly once.

    Regression test for the `son.md` incident (2026-04-23): previously
    only brain_recall refreshed, so a note-add immediately followed by
    a brain_notes / brain_search / brain_note_get query missed the note.
    """
    calls, _ = _count_ensure_fresh_calls(monkeypatch)
    # Stub semantic side so the tools don't call the real (slow) index.
    from brain import semantic
    import numpy as np
    monkeypatch.setattr(semantic, "search_facts", lambda q, k=8, type=None: [])
    monkeypatch.setattr(semantic, "search_entities", lambda q, k=8: [])
    monkeypatch.setattr(semantic, "search_notes", lambda q, k=8: [])
    monkeypatch.setattr(semantic, "ensure_built", lambda: None)
    monkeypatch.setattr(semantic, "_embed",
        lambda texts, batch_size=64: np.zeros((len(texts), semantic.DIM), dtype=np.float32))

    from brain import mcp_server
    mcp_server.brain_search("q", k=3)
    mcp_server.brain_entities("q", k=3)
    mcp_server.brain_notes("q", k=3)
    mcp_server.brain_recent(hours=1, k=3)
    mcp_server.brain_recall("q", k=3)
    mcp_server.brain_semantic("q", k=3)

    assert len(calls) == 6, \
        f"each of 6 read tools should call _ensure_fresh once; got {len(calls)}"
