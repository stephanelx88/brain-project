"""Tests for `brain.config._read_seed_types_from_config` edge cases.

These exist because the function is the single point that decides which
entity folders the brain seeds at startup, and bad ~/.brain/brain-config.yaml
content (hand-edited, partially written, accidentally wrong shape) must
never crash a session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import brain.config as config


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point CONFIG_FILE at a tmp file so we can swap contents per test."""
    cfg = tmp_path / "brain-config.yaml"
    monkeypatch.setattr(config, "CONFIG_FILE", cfg)
    return cfg


def test_missing_file_returns_none(isolated_config):
    assert not isolated_config.exists()
    assert config._read_seed_types_from_config() is None


def test_empty_file_returns_none(isolated_config):
    isolated_config.write_text("")
    assert config._read_seed_types_from_config() is None


def test_unparseable_yaml_returns_none(isolated_config):
    isolated_config.write_text("{[ invalid")
    assert config._read_seed_types_from_config() is None


def test_yaml_string_at_root_returns_none(isolated_config):
    """`yaml.safe_load(':::not yaml{[')` returns a *string*, not None.
    The function must not call `.get()` on it."""
    isolated_config.write_text(":::not yaml{[")
    assert config._read_seed_types_from_config() is None


def test_yaml_list_at_root_returns_none(isolated_config):
    isolated_config.write_text("- one\n- two\n")
    assert config._read_seed_types_from_config() is None


def test_missing_entity_types_key_returns_none(isolated_config):
    isolated_config.write_text("version: '0.1.0'\nllm_provider: claude\n")
    assert config._read_seed_types_from_config() is None


def test_entity_types_wrong_shape_returns_none(isolated_config):
    isolated_config.write_text("entity_types: not-a-list\n")
    assert config._read_seed_types_from_config() is None


def test_entity_types_with_non_string_items_returns_none(isolated_config):
    isolated_config.write_text("entity_types:\n  - patients\n  - 42\n")
    assert config._read_seed_types_from_config() is None


def test_entity_types_with_empty_string_returns_none(isolated_config):
    isolated_config.write_text("entity_types:\n  - patients\n  - ''\n")
    assert config._read_seed_types_from_config() is None


def test_valid_entity_types_returned(isolated_config):
    isolated_config.write_text("entity_types:\n  - patients\n  - conditions\n  - studies\n")
    assert config._read_seed_types_from_config() == ["patients", "conditions", "studies"]
