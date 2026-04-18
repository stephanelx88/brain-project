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
    """Index should be sent as compact entity names only."""
    import brain.auto_extract as ae
    import brain.config as config
    index_file = tmp_path / "index.md"
    index_file.write_text(
        "# Brain Index\n\n## People\n"
        "- [[entities/people/alice.md|Alice Smith]] — Engineer at Acme\n"
        "- [[entities/people/bob.md|Bob Jones]] — Manager at Acme\n"
        "\n## Projects\n"
        "- [[entities/projects/widget.md|Widget Project]] — Main product\n"
    )
    monkeypatch.setattr(config, "INDEX_FILE", index_file)

    result = get_existing_index()
    # Should contain names but NOT descriptions
    assert "Alice Smith" in result
    assert "Bob Jones" in result
    assert "Widget Project" in result
    assert "Engineer at Acme" not in result
    assert "Main product" not in result
    # Should contain section headers
    assert "## People" in result
    assert "## Projects" in result
