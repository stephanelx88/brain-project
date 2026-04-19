"""Tests for auto_extract pipeline."""

import json
import time
from pathlib import Path
from unittest.mock import patch

from brain.auto_extract import parse_extraction, get_pending_files, get_existing_index


def test_parse_extraction_plain_json():
    raw = '{"people": [], "corrections": []}'
    result = parse_extraction(raw)
    assert result == {"people": [], "corrections": []}


def test_parse_extraction_with_markdown_fences():
    raw = '```json\n{"people": [], "corrections": []}\n```'
    result = parse_extraction(raw)
    assert result == {"people": [], "corrections": []}


def test_parse_extraction_with_surrounding_text():
    raw = 'Here is the extraction:\n{"people": [{"name": "Alice"}], "corrections": []}\nDone.'
    result = parse_extraction(raw)
    assert result is not None
    assert len(result["people"]) == 1


def test_parse_extraction_garbage_returns_none():
    result = parse_extraction("this is not json at all")
    assert result is None


def test_get_pending_files_returns_sorted_oldest_first(tmp_path, monkeypatch):
    import brain.auto_extract as ae
    import brain.config as config
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    monkeypatch.setattr(config, "RAW_DIR", raw_dir)

    (raw_dir / "session-2026-04-11-100000-aaa.md").write_text("old")
    time.sleep(0.05)
    (raw_dir / "session-2026-04-11-110000-bbb.md").write_text("new")

    files = get_pending_files()
    assert len(files) == 2
    assert "aaa" in files[0].name
    assert "bbb" in files[1].name


def test_get_existing_index_compact(tmp_path, monkeypatch):
    """Cached entity-name list should be compact: section + bullet names only."""
    import brain.auto_extract as ae
    import brain.config as config

    brain_dir = tmp_path / "brain"
    entities_dir = brain_dir / "entities"
    (entities_dir / "people").mkdir(parents=True)
    (entities_dir / "projects").mkdir(parents=True)

    (entities_dir / "people" / "alice-smith.md").write_text(
        "---\ntype: person\nname: Alice Smith\n---\n\n# Alice Smith\n\nEngineer at Acme\n"
    )
    (entities_dir / "people" / "bob-jones.md").write_text(
        "---\ntype: person\nname: Bob Jones\n---\n\n# Bob Jones\n\nManager at Acme\n"
    )
    (entities_dir / "projects" / "widget.md").write_text(
        "---\ntype: project\nname: Widget Project\n---\n\n# Widget Project\n\nMain product\n"
    )

    monkeypatch.setattr(config, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(config, "ENTITIES_DIR", entities_dir)
    monkeypatch.setattr(config, "INDEX_FILE", brain_dir / "index.md")
    monkeypatch.setattr(
        config,
        "ENTITY_TYPES",
        {"people": entities_dir / "people", "projects": entities_dir / "projects"},
    )
    monkeypatch.setattr(ae, "CACHE_FILE", brain_dir / ".entity-names.cache")

    result = get_existing_index()
    assert "Alice Smith" in result
    assert "Bob Jones" in result
    assert "Widget Project" in result
    assert "Engineer at Acme" not in result
    assert "Main product" not in result
    assert "## people" in result
    assert "## projects" in result
