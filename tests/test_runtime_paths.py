"""Runtime root + subdirectory resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.runtime import paths


def test_default_root_is_home_dot_brain_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert paths.runtime_root() == tmp_path / ".brain-runtime"


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path / "custom"))
    assert paths.runtime_root() == tmp_path / "custom"


def test_inbox_dir_for_uuid(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))
    uuid = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    assert paths.inbox_pending_dir(uuid) == tmp_path / "inbox" / uuid / "pending"
    assert paths.inbox_delivered_dir(uuid) == tmp_path / "inbox" / uuid / "delivered"


def test_names_file_for_uuid(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))
    uuid = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    assert paths.name_file(uuid) == tmp_path / "names" / f"{uuid}.json"


def test_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))
    assert paths.hook_log_path() == tmp_path / "log" / "inbox-hook.log"
