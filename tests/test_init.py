"""Tests for `brain init` and the preset library.

These tests exercise the non-interactive paths (`--yes`, `--no-install`,
`--preset <slug>`) and pure-function helpers. The interactive prompts are
not exercised here — they require a TTY and are covered by manual smoke
testing.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest
import yaml

from brain import init as init_mod
from brain.presets import list_presets, load_preset


# ─────────────────────────────────────────────────────────────────────────
# Presets
# ─────────────────────────────────────────────────────────────────────────
class TestPresets:
    def test_list_presets_returns_all_yamls_sorted(self):
        presets = list_presets()
        slugs = [p["_slug"] for p in presets]
        # Five canonical roles + custom must all be present.
        for expected in ("developer", "doctor", "lawyer", "researcher", "student", "custom"):
            assert expected in slugs, f"missing preset: {expected}"
        # Custom should sort last via its high `order:` value.
        assert slugs[-1] == "custom"

    def test_each_preset_has_required_fields(self):
        for preset in list_presets():
            assert "display_name" in preset
            assert "description" in preset
            assert "entity_types" in preset
            assert isinstance(preset["entity_types"], list)
            assert "identity" in preset
            assert isinstance(preset["identity"], dict)

    def test_load_preset_by_slug(self):
        p = load_preset("doctor")
        assert p["_slug"] == "doctor"
        names = [t["name"] for t in p["entity_types"]]
        assert "patients" in names
        assert "treatments" in names

    def test_load_preset_unknown_raises(self):
        with pytest.raises(FileNotFoundError):
            load_preset("notarealpreset")

    def test_developer_preset_has_dev_types(self):
        p = load_preset("developer")
        names = [t["name"] for t in p["entity_types"]]
        assert {"people", "projects", "decisions", "insights"} <= set(names)

    def test_custom_preset_has_no_default_types(self):
        p = load_preset("custom")
        assert p["entity_types"] == []


# ─────────────────────────────────────────────────────────────────────────
# Wizard mechanics (with BRAIN_DIR redirected to tmp_path)
# ─────────────────────────────────────────────────────────────────────────
@pytest.fixture
def isolated_brain(tmp_path, monkeypatch):
    """Redirect init's BRAIN_DIR + CONFIG_PATH to a tmp dir for safety.
    Without this a real `brain init --yes` would clobber the developer's
    own ~/.brain on `pytest` invocations."""
    brain = tmp_path / ".brain"
    monkeypatch.setattr(init_mod, "BRAIN_DIR", brain)
    monkeypatch.setattr(init_mod, "CONFIG_PATH", brain / "brain-config.yaml")
    return brain


class TestMergeConfig:
    def test_merge_writes_persona_keys(self, isolated_brain):
        preset = load_preset("doctor")
        cfg = init_mod._merge_config(
            preset,
            preset["entity_types"],
            {"name": "Dr Smith", "role": "Cardiologist", "field": "Medicine"},
            {"provider": "claude"},
        )
        assert cfg["preset"] == "doctor"
        assert cfg["entity_types"] == [t["name"] for t in preset["entity_types"]]
        assert cfg["identity"]["name"] == "Dr Smith"
        assert cfg["identity"]["role"] == "Cardiologist"
        assert cfg["llm_provider"] == "claude"
        assert cfg["version"] == "0.1.0"

    def test_merge_preserves_unrelated_existing_keys(self, isolated_brain):
        isolated_brain.mkdir()
        init_mod.CONFIG_PATH.write_text(yaml.safe_dump({
            "version": "9.9.9",
            "auto_commit": False,
            "custom_user_field": "keep-me",
            "models": {"extraction": "haiku"},
        }))
        preset = load_preset("developer")
        cfg = init_mod._merge_config(
            preset, preset["entity_types"],
            {"name": "X", "role": "Y", "field": "Z"},
            {"provider": "skip"},
        )
        assert cfg["custom_user_field"] == "keep-me"
        assert cfg["auto_commit"] is False
        assert cfg["models"]["extraction"] == "haiku"
        # version is sticky once set
        assert cfg["version"] == "9.9.9"

    def test_merge_skip_provider_does_not_overwrite(self, isolated_brain):
        isolated_brain.mkdir()
        init_mod.CONFIG_PATH.write_text(yaml.safe_dump({"llm_provider": "ollama"}))
        preset = load_preset("developer")
        cfg = init_mod._merge_config(
            preset, preset["entity_types"],
            {"name": "X", "role": "Y", "field": "Z"},
            {"provider": "skip"},
        )
        assert cfg["llm_provider"] == "ollama"

    def test_merge_handles_unparseable_existing_config(self, isolated_brain):
        isolated_brain.mkdir()
        init_mod.CONFIG_PATH.write_text("::: not valid yaml :::\n  - [")
        preset = load_preset("developer")
        cfg = init_mod._merge_config(
            preset, preset["entity_types"],
            {"name": "X", "role": "Y", "field": "Z"},
            {"provider": "claude"},
        )
        # Bad config gets backed up and a fresh one is built.
        assert cfg["preset"] == "developer"
        assert init_mod.CONFIG_PATH.with_suffix(".yaml.bak").exists()

    def test_merge_handles_yaml_that_parses_to_non_dict(self, isolated_brain):
        """Regression: `yaml.safe_load(":::not yaml{[")` returns a *string*,
        not None and without raising. Earlier code crashed on data.get()."""
        isolated_brain.mkdir()
        init_mod.CONFIG_PATH.write_text(":::not yaml{[")
        preset = load_preset("developer")
        cfg = init_mod._merge_config(
            preset, preset["entity_types"],
            {"name": "X", "role": "Y", "field": "Z"},
            {"provider": "claude"},
        )
        assert cfg["preset"] == "developer"
        assert init_mod.CONFIG_PATH.with_suffix(".yaml.bak").exists()

    def test_merge_handles_yaml_list_at_root(self, isolated_brain):
        """Same family: a top-level YAML list is parseable but unusable."""
        isolated_brain.mkdir()
        init_mod.CONFIG_PATH.write_text("- one\n- two\n")
        preset = load_preset("developer")
        cfg = init_mod._merge_config(
            preset, preset["entity_types"],
            {"name": "X", "role": "Y", "field": "Z"},
            {"provider": "claude"},
        )
        assert cfg["preset"] == "developer"
        assert init_mod.CONFIG_PATH.with_suffix(".yaml.bak").exists()


class TestRenderWhoIAm:
    def test_renders_full_identity_block(self, isolated_brain):
        preset = load_preset("doctor")
        identity = {"name": "Dr Ha", "role": "GP", "field": "Medicine"}
        init_mod._render_who_i_am(preset, identity, force=False)
        body = (isolated_brain / "identity" / "who-i-am.md").read_text()
        assert "Name: Dr Ha" in body
        assert "Role: GP" in body
        assert "Field: Medicine" in body
        assert "type: identity" in body
        # Preset's how_i_work lines must appear verbatim
        for line in preset["identity"]["how_i_work"]:
            assert line in body

    def test_skips_existing_unless_forced(self, isolated_brain):
        (isolated_brain / "identity").mkdir(parents=True)
        existing = isolated_brain / "identity" / "who-i-am.md"
        existing.write_text("MY OWN NOTES")
        preset = load_preset("doctor")
        init_mod._render_who_i_am(preset, {"name": "x", "role": "y", "field": "z"}, force=False)
        assert existing.read_text() == "MY OWN NOTES"

    def test_force_overwrites_existing(self, isolated_brain):
        (isolated_brain / "identity").mkdir(parents=True)
        existing = isolated_brain / "identity" / "who-i-am.md"
        existing.write_text("MY OWN NOTES")
        preset = load_preset("developer")
        init_mod._render_who_i_am(preset, {"name": "Son", "role": "Dev", "field": "SE"}, force=True)
        assert existing.read_text() != "MY OWN NOTES"
        assert "Name: Son" in existing.read_text()


class TestEntityDirs:
    def test_creates_each_preset_folder(self, isolated_brain):
        preset = load_preset("lawyer")
        init_mod._create_entity_dirs(preset["entity_types"])
        for t in preset["entity_types"]:
            assert (isolated_brain / "entities" / t["name"]).is_dir()


# ─────────────────────────────────────────────────────────────────────────
# Non-interactive end-to-end (--yes, --no-install)
# ─────────────────────────────────────────────────────────────────────────
class TestMainEntryPoint:
    def test_yes_with_no_install_runs_clean(self, isolated_brain):
        rc = init_mod.main(["--yes", "--no-install", "--preset", "doctor"])
        assert rc == 0
        cfg = yaml.safe_load(init_mod.CONFIG_PATH.read_text())
        assert cfg["preset"] == "doctor"
        assert "patients" in cfg["entity_types"]
        # Identity file must exist with doctor's field substituted
        body = (isolated_brain / "identity" / "who-i-am.md").read_text()
        assert "Field: Medicine" in body

    def test_yes_default_preset_is_developer(self, isolated_brain):
        rc = init_mod.main(["--yes", "--no-install"])
        assert rc == 0
        cfg = yaml.safe_load(init_mod.CONFIG_PATH.read_text())
        assert cfg["preset"] == "developer"

    def test_rerun_preserves_user_identity_edits(self, isolated_brain):
        init_mod.main(["--yes", "--no-install", "--preset", "developer"])
        identity = isolated_brain / "identity" / "who-i-am.md"
        identity.write_text("HAND-EDITED CONTENT THE USER WROTE")
        # Second run should NOT overwrite.
        init_mod.main(["--yes", "--no-install", "--preset", "doctor"])
        assert identity.read_text() == "HAND-EDITED CONTENT THE USER WROTE"

    def test_force_identity_flag_overwrites_on_rerun(self, isolated_brain):
        init_mod.main(["--yes", "--no-install", "--preset", "developer"])
        identity = isolated_brain / "identity" / "who-i-am.md"
        identity.write_text("STALE")
        init_mod.main(["--yes", "--no-install", "--preset", "doctor", "--force-identity"])
        assert identity.read_text() != "STALE"
        assert "Field: Medicine" in identity.read_text()
