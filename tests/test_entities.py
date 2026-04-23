"""Tests for brain entity CRUD operations."""

from brain.entities import (
    create_entity,
    entity_exists,
    read_entity,
    append_to_entity,
    append_to_entity_path,
)
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


def test_append_to_entity_path_handles_date_prefixed_slug(tmp_brain, monkeypatch):
    """Regression: dedupe.apply_merge used to call append_to_entity by
    name, which re-slugified and missed entities whose real slug had a
    date prefix (e.g. file `2026-04-11-foo.md` with frontmatter
    `name: Foo`). The path-addressed variant must succeed even when
    `slugify(name)` ≠ `path.stem`.
    """
    monkeypatch.setattr(config, "ENTITIES_DIR", tmp_brain / "entities")
    monkeypatch.setattr(config, "ENTITY_TYPES", {
        k: tmp_brain / "entities" / k for k in config.ENTITY_TYPES
    })
    insights_dir = tmp_brain / "entities" / "insights"
    insights_dir.mkdir(exist_ok=True)
    odd_file = insights_dir / "2026-04-11-foo.md"
    odd_file.write_text(
        "---\n"
        "type: insight\n"
        "name: Foo\n"
        "status: current\n"
        "first_seen: 2026-04-11\n"
        "last_updated: 2026-04-11\n"
        "source_count: 1\n"
        "tags: []\n"
        "---\n\n"
        "# Foo\n\n"
        "## Key Facts\n"
        "- original fact (source: a)\n"
    )

    # By-name lookup would resolve to insights/foo.md and fail.
    import pytest
    with pytest.raises(FileNotFoundError):
        append_to_entity("insights", "Foo", "Key Facts", "- merged fact (source: b)")

    # Path-addressed lookup must succeed against the real file.
    append_to_entity_path(odd_file, "Key Facts", "- merged fact (source: b)")
    content = odd_file.read_text()
    assert "- merged fact (source: b)" in content
    assert "source_count: 2" in content


def test_append_to_entity_section_header_is_line_anchored(tmp_brain, monkeypatch):
    """Regression: the old substring match `section_header in text` would
    pick the first place `## Key Facts` appeared — including inside a
    fact line that *mentioned* the literal text — and splice new content
    there, corrupting the bullet list. The line-anchored match must
    ignore header-like strings that appear inside fact bodies.
    """
    monkeypatch.setattr(config, "ENTITIES_DIR", tmp_brain / "entities")
    monkeypatch.setattr(config, "ENTITY_TYPES", {
        k: tmp_brain / "entities" / k for k in config.ENTITY_TYPES
    })
    projects_dir = tmp_brain / "entities" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    odd_file = projects_dir / "meta-project.md"
    odd_file.write_text(
        "---\n"
        "type: project\n"
        "name: Meta Project\n"
        "status: current\n"
        "first_seen: 2026-04-23\n"
        "last_updated: 2026-04-23\n"
        "source_count: 1\n"
        "tags: []\n"
        "---\n\n"
        "# Meta Project\n\n"
        "## Notes\n"
        "- Discussed the `## Key Facts` convention in docs (source: a)\n\n"
        "## Key Facts\n"
        "- real fact one (source: a)\n"
    )

    append_to_entity_path(odd_file, "Key Facts", "- real fact two (source: b)")
    content = odd_file.read_text()

    # New fact must land immediately after the REAL `## Key Facts`
    # header, not inside the Notes bullet that mentions `## Key Facts`.
    notes_idx = content.index("## Notes")
    real_header_idx = content.index("\n## Key Facts\n")
    new_fact_idx = content.index("- real fact two")
    old_fact_idx = content.index("- real fact one")
    assert notes_idx < real_header_idx < new_fact_idx < old_fact_idx
    # The Notes bullet must not have been corrupted.
    assert "Discussed the `## Key Facts` convention" in content
