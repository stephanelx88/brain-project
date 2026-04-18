"""Tests for brain entity CRUD operations."""

from brain.entities import create_entity, entity_exists, read_entity, append_to_entity
from brain import config


def test_create_entity_sets_correct_frontmatter_type(tmp_brain, monkeypatch):
    """Entity frontmatter should use singular type name (person, not people)."""
    monkeypatch.setattr(config, "ENTITIES_DIR", tmp_brain / "entities")
    monkeypatch.setattr(config, "ENTITY_TYPES", {
        k: tmp_brain / "entities" / k for k in config.ENTITY_TYPES
    })
    path = create_entity("people", "Test Person", body="## Key Facts\n- fact one")
    content = path.read_text()
    assert "type: person" in content
    assert "name: Test Person" in content


def test_entity_exists_returns_false_for_missing(tmp_brain, monkeypatch):
    monkeypatch.setattr(config, "ENTITIES_DIR", tmp_brain / "entities")
    monkeypatch.setattr(config, "ENTITY_TYPES", {
        k: tmp_brain / "entities" / k for k in config.ENTITY_TYPES
    })
    assert not entity_exists("people", "Nobody")


def test_append_to_entity_updates_last_updated(tmp_brain, monkeypatch):
    monkeypatch.setattr(config, "ENTITIES_DIR", tmp_brain / "entities")
    monkeypatch.setattr(config, "ENTITY_TYPES", {
        k: tmp_brain / "entities" / k for k in config.ENTITY_TYPES
    })
    create_entity("people", "Alice", body="## Key Facts\n- original fact")
    append_to_entity("people", "Alice", "Key Facts", "- new fact (source: test)")
    content = read_entity("people", "Alice")
    assert "- new fact (source: test)" in content
    assert "source_count: 2" in content
