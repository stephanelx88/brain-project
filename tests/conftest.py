"""Shared fixtures for brain tests."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_brain(tmp_path):
    """Create a temporary brain directory structure."""
    brain = tmp_path / ".brain"
    brain.mkdir()
    (brain / "raw").mkdir()
    (brain / "entities").mkdir()
    for subdir in ("people", "clients", "projects", "domains",
                    "decisions", "issues", "insights", "evolutions"):
        (brain / "entities" / subdir).mkdir()
    (brain / "identity").mkdir()
    (brain / "timeline").mkdir()
    (brain / "timeline" / "weekly").mkdir()
    (brain / "graphify-out").mkdir()

    # Create minimal index
    (brain / "index.md").write_text(
        "# Brain Index\n\nEntity catalog for fast lookup.\n\n## People\n\n## Projects\n"
    )
    # Create minimal log
    (brain / "log.md").write_text("")
    # Create corrections file
    (brain / "identity" / "corrections.md").write_text(
        "---\ntype: corrections\n---\n\n# Corrections\n\n## Active Corrections\n"
    )
    return brain


@pytest.fixture
def sample_jsonl(tmp_path):
    """Create a sample session JSONL file."""
    projects_dir = tmp_path / ".claude" / "projects" / "-Users-son-test-project"
    projects_dir.mkdir(parents=True)
    jsonl_path = projects_dir / "abc123-def456.jsonl"
    entries = [
        {"type": "user", "message": {"content": "fix the bug in auth"}, "timestamp": "2026-04-11T10:00:00Z"},
        {"type": "assistant", "message": {"content": "I'll look at the auth module."}, "timestamp": "2026-04-11T10:00:05Z"},
        {"type": "user", "message": {"content": "also check the database connection"}, "timestamp": "2026-04-11T10:01:00Z"},
        {"type": "assistant", "message": {"content": "Found the issue in db.py."}, "timestamp": "2026-04-11T10:01:30Z"},
        {"type": "user", "message": {"content": "great, commit it"}, "timestamp": "2026-04-11T10:02:00Z"},
        {"type": "assistant", "message": {"content": "Done. Committed the fix."}, "timestamp": "2026-04-11T10:02:30Z"},
    ]
    lines = [json.dumps(e) for e in entries]
    jsonl_path.write_text("\n".join(lines) + "\n")
    return jsonl_path


@pytest.fixture
def sample_jsonl_with_tools(tmp_path):
    """Create a JSONL with tool_use blocks including Write (tests variable shadowing fix)."""
    projects_dir = tmp_path / ".claude" / "projects" / "-Users-son-tool-project"
    projects_dir.mkdir(parents=True)
    jsonl_path = projects_dir / "tool-session-123.jsonl"
    entries = [
        {"type": "user", "message": {"content": "write a config file"}, "timestamp": "2026-04-11T10:00:00Z"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Creating the config file now."},
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {
                            "file_path": "/tmp/config.yaml",
                            "content": "database:\n  host: localhost\n  port: 5432\n  name: brain_db\n"
                        }
                    },
                    {"type": "text", "text": "Config file created."}
                ]
            },
            "timestamp": "2026-04-11T10:00:10Z"
        },
        {"type": "user", "message": {"content": "looks good"}, "timestamp": "2026-04-11T10:01:00Z"},
        {"type": "assistant", "message": {"content": "Glad it works!"}, "timestamp": "2026-04-11T10:01:10Z"},
    ]
    lines = [json.dumps(e) for e in entries]
    jsonl_path.write_text("\n".join(lines) + "\n")
    return jsonl_path
