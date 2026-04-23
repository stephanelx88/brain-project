"""Tests for brain.self_entity — the owner anchor entity guarantor."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def tmp_brain(tmp_path, monkeypatch):
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(config, "IDENTITY_DIR", tmp_path / "identity")
    monkeypatch.setattr(config, "ENTITIES_DIR", tmp_path / "entities")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "brain-config.yaml")
    (tmp_path / "identity").mkdir()
    (tmp_path / "entities").mkdir()
    (tmp_path / "entities" / "people").mkdir()
    return tmp_path


def _write_config(tmp_brain, identity_block: str) -> None:
    (tmp_brain / "brain-config.yaml").write_text(
        "version: 0.1.0\n"
        "identity:\n"
        f"{identity_block}"
    )


class TestResolveOwner:
    def test_display_name_preferred_over_name(self, tmp_brain):
        from brain.self_entity import owner_display_name
        _write_config(tmp_brain, "  name: stephanelx88\n  display_name: Son\n")
        assert owner_display_name() == "Son"

    def test_falls_back_to_name(self, tmp_brain):
        from brain.self_entity import owner_display_name
        _write_config(tmp_brain, "  name: Alice\n")
        assert owner_display_name() == "Alice"

    def test_falls_back_to_env_user_when_config_missing(self, tmp_brain, monkeypatch):
        from brain.self_entity import owner_display_name
        monkeypatch.setenv("USER", "bob")
        # no config file
        assert owner_display_name() == "bob"

    def test_returns_none_when_nothing_resolves(self, tmp_brain, monkeypatch):
        from brain.self_entity import owner_display_name
        monkeypatch.delenv("USER", raising=False)
        # no config file, no USER
        assert owner_display_name() is None


class TestEnsureSelfEntity:
    def test_creates_stub_with_expected_frontmatter(self, tmp_brain):
        from brain.self_entity import ensure_self_entity
        _write_config(tmp_brain, "  name: stephanelx88\n  display_name: Son\n")
        created = ensure_self_entity()
        assert created is not None
        assert created == tmp_brain / "entities" / "people" / "son.md"
        body = created.read_text()
        assert "type: people" in body
        assert "name: Son" in body
        assert "# Son" in body
        assert "Brain owner" in body

    def test_idempotent_second_call_returns_none(self, tmp_brain):
        from brain.self_entity import ensure_self_entity
        _write_config(tmp_brain, "  name: Alice\n")
        first = ensure_self_entity()
        assert first is not None
        second = ensure_self_entity()
        assert second is None

    def test_no_op_when_owner_cannot_resolve(self, tmp_brain, monkeypatch):
        from brain.self_entity import ensure_self_entity
        monkeypatch.delenv("USER", raising=False)
        assert ensure_self_entity() is None
        assert list((tmp_brain / "entities" / "people").glob("*.md")) == []

    def test_slug_is_filesystem_safe(self, tmp_brain):
        from brain.self_entity import ensure_self_entity
        _write_config(tmp_brain, "  name: null\n  display_name: 'Người Dùng'\n")
        created = ensure_self_entity()
        assert created is not None
        assert created.name == "nguoi-dung.md"
        assert "name: Người Dùng" in created.read_text()
