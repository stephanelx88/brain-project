"""Integration test for the full harvest → extract pipeline."""

import json
from pathlib import Path
from unittest.mock import patch

import brain.auto_extract as ae
import brain.harvest_session as hs
from brain import config


def test_harvest_then_extract_creates_entity(tmp_brain, sample_jsonl, monkeypatch):
    """Full pipeline: JSONL → harvest → raw file → extract → entity file."""
    # Derive dirs that harvest_session expects
    claude_dir = sample_jsonl.parent.parent.parent  # tmp_path/.claude
    projects_dir = claude_dir / "projects"           # tmp_path/.claude/projects

    # Point harvest_session at temp dirs
    monkeypatch.setattr(hs, "BRAIN_RAW", tmp_brain / "raw")
    monkeypatch.setattr(hs, "HARVESTED_FILE", tmp_brain / ".harvested")
    monkeypatch.setattr(hs, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(hs, "CLAUDE_DIR", claude_dir)

    # Point config at temp dirs (all modules read from config at call time)
    temp_entity_types = {
        k: tmp_brain / "entities" / k for k in config.ENTITY_TYPES
    }
    monkeypatch.setattr(config, "BRAIN_DIR", tmp_brain)
    monkeypatch.setattr(config, "ENTITIES_DIR", tmp_brain / "entities")
    monkeypatch.setattr(config, "RAW_DIR", tmp_brain / "raw")
    monkeypatch.setattr(config, "INDEX_FILE", tmp_brain / "index.md")
    monkeypatch.setattr(config, "LOG_FILE", tmp_brain / "log.md")
    monkeypatch.setattr(config, "IDENTITY_DIR", tmp_brain / "identity")
    monkeypatch.setattr(config, "ENTITY_TYPES", temp_entity_types)

    # Step 1: Harvest
    count = hs.harvest_all()
    assert count == 1

    raw_files = list((tmp_brain / "raw").glob("session-*.md"))
    assert len(raw_files) == 1

    # Step 2: Mock claude -p to return valid extraction JSON
    mock_response = json.dumps({
        "people": [],
        "clients": [],
        "projects": [
            {
                "name": "Test Project",
                "client": "Test Client",
                "facts": ["Bug fix in auth module"],
                "is_new": True,
            }
        ],
        "domains": [],
        "decisions": [],
        "issues": [],
        "insights": [],
        "corrections": [],
        "evolutions": [],
        "contested": [],
        "high_value_outputs": [],
    })

    with patch.object(ae, "call_claude", return_value=mock_response):
        ae.main()

    # Raw file should be cleaned up
    remaining_raw = list((tmp_brain / "raw").glob("session-*.md"))
    assert len(remaining_raw) == 0

    # Entity should exist
    project_files = list((tmp_brain / "entities" / "projects").glob("*.md"))
    assert len(project_files) >= 1
    assert any("test-project" in f.name for f in project_files)
