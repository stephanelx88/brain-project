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

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(config, "IDENTITY_DIR", brain_dir / "identity")

    from brain import db
    db_path = brain_dir / ".brain.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

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
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["name"] == "Foo Project"
    assert rows[0]["text"] == "alpha bravo charlie"


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
                                         query="q", env=None):
    """Stub out the semantic layer so brain_recall returns `hits` verbatim.

    Yields the parsed JSON envelope. Avoids pulling torch / the real
    embedding index into the unit test.
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
    return json.loads(mcp_server.brain_recall(query))


def test_brain_recall_envelope_has_expected_keys(monkeypatch):
    out = _call_brain_recall_with_stubbed_hits(monkeypatch, hits=[
        {"kind": "fact", "name": "Foo", "text": "alpha", "rrf": 0.08},
    ])
    assert set(out.keys()) == {"query", "weak_match", "top_score",
                               "threshold", "guidance", "hits"}
    assert out["query"] == "q"
    assert isinstance(out["hits"], list)


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
    rows = json.loads(mcp_server.brain_search("alpha", k=5))
    # BM25 and semantic both returned same fact — should appear once
    assert len(rows) == 1
    assert rows[0]["name"] == "Foo Project"


def test_brain_search_semantic_fills_when_bm25_empty(tmp_brain_for_mcp, monkeypatch):
    """Non-ASCII query: BM25 returns nothing, semantic backfills."""
    _stub_semantic_for_search(monkeypatch, fact_hits=[{
        "type": "people", "name": "Thuha", "slug": "thuha",
        "text": "Thuha lives in Long Xuyen", "source": "note:thuha.md", "score": 0.72,
    }])
    from brain import mcp_server
    rows = json.loads(mcp_server.brain_search("thuha ở đâu", k=5))
    # BM25 returns nothing for Vietnamese query; semantic backfills
    assert len(rows) >= 1
    assert any(r["name"] == "Thuha" for r in rows)


def test_brain_search_semantic_respects_k_cap(tmp_brain_for_mcp, monkeypatch):
    """Semantic results respect the k cap even when BM25 is empty."""
    many_hits = [
        {"type": "people", "name": f"P{i}", "slug": f"p{i}",
         "text": f"fact {i}", "source": "s", "score": 0.5}
        for i in range(10)
    ]
    _stub_semantic_for_search(monkeypatch, fact_hits=many_hits)
    from brain import mcp_server
    rows = json.loads(mcp_server.brain_search("unmatched query", k=3))
    assert len(rows) <= 3


def test_brain_search_dedup_across_bm25_and_semantic(tmp_brain_for_mcp, monkeypatch):
    """Same fact from BM25 and semantic must not appear twice."""
    _stub_semantic_for_search(monkeypatch, fact_hits=[{
        "type": "projects", "name": "Foo Project", "slug": "foo",
        "text": "alpha bravo charlie", "source": "src1", "score": 0.8,
    }])
    from brain import mcp_server
    rows = json.loads(mcp_server.brain_search("alpha", k=10))
    texts = [r["text"] for r in rows]
    assert texts.count("alpha bravo charlie") == 1


# ---------- brain_entities hybrid -------------------------------------------

def test_brain_entities_bm25_hit_returned(tmp_brain_for_mcp, monkeypatch):
    _stub_semantic_for_search(monkeypatch, entity_hits=[])
    from brain import mcp_server
    rows = json.loads(mcp_server.brain_entities("Foo", k=5))
    assert any(r["name"] == "Foo Project" for r in rows)


def test_brain_entities_semantic_fills_when_bm25_empty(tmp_brain_for_mcp, monkeypatch):
    """Vietnamese entity name: BM25 returns nothing, semantic backfills."""
    _stub_semantic_for_search(monkeypatch, entity_hits=[{
        "type": "people", "name": "Nguyễn Thị Thu Hà", "slug": "thu-ha",
        "path": "entities/people/thu-ha.md", "summary": "lives in HCMC",
        "score": 0.81,
    }])
    from brain import mcp_server
    rows = json.loads(mcp_server.brain_entities("thu ha", k=5))
    # BM25 finds nothing for "thu ha" (no entity named that in fixture)
    # but semantic fills in Nguyễn Thị Thu Hà
    assert any(r["name"] == "Nguyễn Thị Thu Hà" for r in rows)


def test_brain_entities_dedup_across_bm25_and_semantic(tmp_brain_for_mcp, monkeypatch):
    """Same entity from BM25 and semantic must appear once."""
    _stub_semantic_for_search(monkeypatch, entity_hits=[{
        "type": "projects", "name": "Foo Project", "slug": "foo",
        "path": "entities/projects/foo.md", "summary": "thing one",
        "score": 0.7,
    }])
    from brain import mcp_server
    rows = json.loads(mcp_server.brain_entities("Foo", k=10))
    names = [r["name"] for r in rows]
    assert names.count("Foo Project") == 1
