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
