"""Tests for brain.freshness (WS3 watermark module)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "brain"
    vault.mkdir()
    (vault / "entities").mkdir()

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", vault)
    monkeypatch.setattr(config, "ENTITIES_DIR", vault / "entities")
    return vault


def test_load_returns_zeros_when_absent(tmp_vault):
    from brain import freshness
    data = freshness.load()
    assert data == {"entities": 0.0, "notes": 0.0, "raw": 0.0}


def test_save_roundtrips_known_keys(tmp_vault):
    from brain import freshness
    freshness.save({"entities": 1714200000.5, "notes": 1714200001.0})
    reloaded = freshness.load()
    assert reloaded["entities"] == pytest.approx(1714200000.5)
    assert reloaded["notes"] == pytest.approx(1714200001.0)
    # raw stays at default
    assert reloaded["raw"] == 0.0


def test_save_drops_unknown_keys(tmp_vault):
    from brain import freshness
    freshness.save({"entities": 123.0, "made_up": 999.0})  # type: ignore[dict-item]
    reloaded = freshness.load()
    assert reloaded["entities"] == 123.0
    assert "made_up" not in reloaded


def test_load_tolerates_corrupt_json(tmp_vault):
    from brain import freshness
    freshness._path().write_text("{not json")
    data = freshness.load()
    assert data["entities"] == 0.0


def test_load_tolerates_non_dict_payload(tmp_vault):
    from brain import freshness
    freshness._path().write_text(json.dumps([1, 2, 3]))
    data = freshness.load()
    assert data["entities"] == 0.0


def test_bump_advances_watermark_to_now(tmp_vault):
    from brain import freshness
    before = time.time()
    freshness.bump("entities")
    after = time.time()
    val = freshness.load()["entities"]
    assert before - 0.01 <= val <= after + 0.01


def test_bump_ignores_unknown_key(tmp_vault):
    from brain import freshness
    freshness.bump("bogus_key")  # must not raise
    assert freshness.load()["entities"] == 0.0


def test_entities_dir_mtime_walks_markdown_only(tmp_vault):
    from brain import freshness
    entities = tmp_vault / "entities"
    (entities / "people").mkdir()
    md = entities / "people" / "foo.md"
    md.write_text("# foo")
    # Non-.md file must not lift the mtime beyond the .md file.
    other = entities / "people" / "sidecar.txt"
    other.write_text("noise")
    # Touch .md to a known past, other to a known future.
    import os
    os.utime(md, (1_700_000_000, 1_700_000_000))
    os.utime(other, (1_900_000_000, 1_900_000_000))
    assert freshness.entities_dir_mtime() == pytest.approx(1_700_000_000.0)


def test_entities_dir_mtime_empty_dir_returns_zero(tmp_vault):
    from brain import freshness
    assert freshness.entities_dir_mtime() == 0.0


def test_notes_dir_mtime_skips_machine_dirs(tmp_vault):
    import os
    from brain import freshness
    # One valid note at vault root
    note = tmp_vault / "my-note.md"
    note.write_text("# hi")
    os.utime(note, (1_700_000_000, 1_700_000_000))

    # Machine-managed dir with newer mtime must not count.
    (tmp_vault / ".git").mkdir()
    junk = tmp_vault / ".git" / "HEAD.md"
    junk.write_text("junk")
    os.utime(junk, (1_900_000_000, 1_900_000_000))

    # entities/ is excluded from notes scope.
    ent = tmp_vault / "entities" / "people"
    ent.mkdir(parents=True)
    efile = ent / "bar.md"
    efile.write_text("- a fact")
    os.utime(efile, (1_900_000_001, 1_900_000_001))

    # Filename prefixed with underscore = machine-managed (placeholder).
    placeholder = tmp_vault / "_MOC.md"
    placeholder.write_text("placeholder")
    os.utime(placeholder, (1_900_000_002, 1_900_000_002))

    assert freshness.notes_dir_mtime() == pytest.approx(1_700_000_000.0)


def test_needs_sweep_compares_mtime_to_watermark(tmp_vault):
    from brain import freshness
    freshness.save({"entities": 1000.0})
    assert not freshness.needs_sweep("entities", probe_mtime=999.0)
    assert not freshness.needs_sweep("entities", probe_mtime=1000.0)
    assert freshness.needs_sweep("entities", probe_mtime=1000.1)


def test_needs_sweep_unknown_key_returns_true(tmp_vault):
    from brain import freshness
    assert freshness.needs_sweep("bogus") is True


def test_bump_survives_disk_error_silently(tmp_vault, monkeypatch):
    from brain import freshness

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(freshness, "atomic_write_text", boom)
    # Must not raise — freshness writes are best-effort.
    freshness.bump("entities")
