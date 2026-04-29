"""Tests for runtime.playbooks — the self-improvement write path."""
from __future__ import annotations

import re

import pytest

from brain import config
from brain.runtime import playbooks


@pytest.fixture(autouse=True)
def _vault(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BRAIN_DIR", tmp_path)
    (tmp_path / "playbooks").mkdir()
    return tmp_path


def _read(p):
    return p.read_text()


def test_find_playbook_path_direct(_vault):
    path = _vault / "playbooks" / "deploy.md"
    path.write_text("# Deploy")
    assert playbooks.find_playbook_path("deploy") == path


def test_find_playbook_path_readme_in_subdir(_vault):
    sub = _vault / "playbooks" / "rotate-keys"
    sub.mkdir()
    readme = sub / "README.md"
    readme.write_text("# Rotate")
    assert playbooks.find_playbook_path("rotate-keys") == readme


def test_find_playbook_path_recursive_fallback(_vault):
    sub = _vault / "playbooks" / "deep" / "nest"
    sub.mkdir(parents=True)
    target = sub / "buried.md"
    target.write_text("# Buried")
    assert playbooks.find_playbook_path("buried") == target


def test_find_playbook_path_returns_none_when_missing(_vault):
    assert playbooks.find_playbook_path("ghost") is None


def test_record_lesson_appends_under_existing_section(_vault):
    path = _vault / "playbooks" / "deploy.md"
    path.write_text(
        "---\nname: Deploy\nslug: deploy\nlessons_count: 1\n---\n"
        "# Deploy\n\n"
        "## Lessons learned\n\n"
        "- 2026-04-20: prior lesson\n"
    )
    result = playbooks.record_lesson("deploy", "secret X expires after 90 days",
                                     source_uuid="abcd1234-...")
    assert result["ok"] is True
    assert result["lessons_count"] == 2

    text = _read(path)
    assert "secret X expires after 90 days" in text
    assert "prior lesson" in text  # existing lesson preserved
    # New bullet inserted directly under the heading (newest-first).
    new_idx = text.index("secret X expires")
    old_idx = text.index("prior lesson")
    assert new_idx < old_idx


def test_record_lesson_creates_section_when_missing(_vault):
    path = _vault / "playbooks" / "deploy.md"
    path.write_text(
        "---\nname: Deploy\nslug: deploy\n---\n"
        "# Deploy\n\nSome body without lessons section.\n"
    )
    result = playbooks.record_lesson("deploy", "first lesson")
    assert result["ok"] is True
    text = _read(path)
    assert "## Lessons learned" in text
    assert "first lesson" in text


def test_record_lesson_bumps_audit_fields_in_frontmatter(_vault):
    path = _vault / "playbooks" / "deploy.md"
    path.write_text("---\nname: Deploy\nslug: deploy\n---\n# Deploy\n")
    playbooks.record_lesson("deploy", "lesson 1")
    playbooks.record_lesson("deploy", "lesson 2")
    playbooks.record_lesson("deploy", "lesson 3")

    text = _read(path)
    # lessons_count should be 3 (bumped each call).
    assert re.search(r"^lessons_count:\s*3\s*$", text, re.MULTILINE)
    # last_updated set to a recent ISO timestamp; just check the field
    # is present and matches an ISO-like prefix.
    assert re.search(r"^last_updated:\s*20\d\d-\d\d-\d\dT", text, re.MULTILINE)


def test_record_lesson_preserves_other_frontmatter_fields(_vault):
    path = _vault / "playbooks" / "deploy.md"
    path.write_text(
        "---\n"
        "name: Deploy\n"
        "slug: deploy\n"
        "summary: Push to staging\n"
        "tags: [deploy, staging]\n"
        "safety: destructive\n"
        "---\n"
        "# Deploy\n"
    )
    playbooks.record_lesson("deploy", "lesson")
    text = _read(path)
    # Every original field still present, untouched.
    for field in ("name: Deploy", "slug: deploy", "summary: Push to staging",
                  "tags: [deploy, staging]", "safety: destructive"):
        assert field in text


def test_record_lesson_seeds_frontmatter_when_playbook_has_none(_vault):
    """A playbook authored without frontmatter still becomes auditable
    after the first lesson — seed last_updated and lessons_count."""
    path = _vault / "playbooks" / "barebones.md"
    path.write_text("# Barebones\n\nJust a doc.\n")
    playbooks.record_lesson("barebones", "first lesson")
    text = _read(path)
    assert text.startswith("---\n")
    assert "last_updated:" in text
    assert "lessons_count: 1" in text
    assert "## Lessons learned" in text
    assert "first lesson" in text


def test_record_lesson_returns_not_found_for_missing(_vault):
    result = playbooks.record_lesson("ghost", "lesson")
    assert result["ok"] is False
    assert result["error"] == "not_found"


def test_record_lesson_rejects_empty_lesson(_vault):
    path = _vault / "playbooks" / "deploy.md"
    path.write_text("# Deploy")
    result = playbooks.record_lesson("deploy", "   ")
    assert result["ok"] is False
    assert result["error"] == "empty_lesson"


def test_record_lesson_attribution_includes_short_uuid(_vault):
    path = _vault / "playbooks" / "deploy.md"
    path.write_text("# Deploy\n")
    playbooks.record_lesson("deploy", "lesson",
                            source_uuid="abcd1234-22a4-4a7c-b719-7fb62a972aa2")
    text = _read(path)
    # First 8 chars of source uuid surface in the bullet so future
    # readers can audit which session contributed the lesson.
    assert "session abcd1234" in text


def test_record_lesson_no_attribution_when_uuid_absent(_vault):
    path = _vault / "playbooks" / "deploy.md"
    path.write_text("# Deploy\n")
    playbooks.record_lesson("deploy", "lesson", source_uuid=None)
    text = _read(path)
    assert "(session" not in text  # no attribution suffix
