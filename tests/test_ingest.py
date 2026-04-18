"""Tests for brain.ingest file ingestion."""

import json
from pathlib import Path
from unittest.mock import patch

from brain.ingest import read_file_content, ingest_file


def test_read_file_content_markdown(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# My Notes\n\nSome knowledge here.")
    content = read_file_content(f)
    assert "My Notes" in content
    assert "Some knowledge here." in content


def test_read_file_content_csv(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("name,role\nAlice,Engineer\nBob,Manager\n")
    content = read_file_content(f)
    assert "Alice" in content
    assert "Engineer" in content
    assert " | " in content  # CSV formatted as table


def test_read_file_content_tsv(tmp_path):
    f = tmp_path / "data.tsv"
    f.write_text("name\trole\nAlice\tEngineer\n")
    content = read_file_content(f)
    assert "Alice" in content


def test_ingest_file_creates_entity(tmp_brain, monkeypatch):
    """Full ingest: file → haiku extraction → entity created."""
    from brain import config

    # Monkeypatch all config paths (all modules read from config at call time)
    monkeypatch.setattr(config, "BRAIN_DIR", tmp_brain)
    monkeypatch.setattr(config, "ENTITIES_DIR", tmp_brain / "entities")
    monkeypatch.setattr(config, "INDEX_FILE", tmp_brain / "index.md")
    monkeypatch.setattr(config, "LOG_FILE", tmp_brain / "log.md")
    monkeypatch.setattr(config, "IDENTITY_DIR", tmp_brain / "identity")
    monkeypatch.setattr(config, "ENTITY_TYPES", {
        k: tmp_brain / "entities" / k for k in config.ENTITY_TYPES
    })

    # Create a knowledge file
    knowledge_file = tmp_brain / "test-knowledge.md"
    knowledge_file.write_text(
        "# BMS Findings\n\n"
        "Chiller Plant 1 rated power is 245 kW based on nameplate.\n"
        "All pump status points are dead — showing OFF while chiller runs.\n"
    )

    mock_response = json.dumps({
        "people": [],
        "clients": [],
        "projects": [],
        "domains": [
            {
                "name": "BMS Chiller Diagnostics",
                "source_context": "work",
                "facts": [
                    "Plant 1 chiller rated power is 245 kW per nameplate",
                    "All pump status BMS points are dead",
                ],
                "is_new": True,
            }
        ],
        "decisions": [],
        "issues": [],
        "insights": [],
        "corrections": [],
        "evolutions": [],
        "contested": [],
        "high_value_outputs": [],
    })

    with patch("brain.ingest.call_claude", return_value=mock_response):
        result = ingest_file(knowledge_file)

    assert len(result["created"]) >= 1
    domain_files = list((tmp_brain / "entities" / "domains").glob("*.md"))
    assert any("bms-chiller" in f.name for f in domain_files)
