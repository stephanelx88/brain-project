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
