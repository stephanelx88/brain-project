"""Recipient resolution: UUID, name in-project, name cross-project, errors."""
from __future__ import annotations

import pytest

from brain.runtime import names, resolve


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_uuid_resolves_to_itself_fire_and_forget():
    out = resolve.resolve_recipient(
        to="ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
        sender_project="acme",
        live_uuids={"ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"},
    )
    assert out.ok and out.uuid == "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    assert out.name_at_send == ""


def test_uuid_dead_still_resolves_for_uuid_send():
    out = resolve.resolve_recipient(
        to="ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
        sender_project="acme",
        live_uuids=set(),
    )
    assert out.ok and out.uuid == "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"


def test_cursor_uuid_rejected_in_mvp():
    out = resolve.resolve_recipient(
        to="cursor:ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
        sender_project="acme",
        live_uuids=set(),
    )
    assert not out.ok and out.error == "cursor_recipient_unsupported"


def test_name_in_sender_project_resolves():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    out = resolve.resolve_recipient(
        to="planner",
        sender_project="acme",
        live_uuids={"u1"},
    )
    assert out.ok and out.uuid == "u1"
    assert out.name_at_send == "planner"


def test_name_dead_session_fails_loud():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    out = resolve.resolve_recipient(
        to="planner",
        sender_project="acme",
        live_uuids=set(),
    )
    assert not out.ok and out.error == "recipient_dead"


def test_name_not_found():
    out = resolve.resolve_recipient(
        to="ghost",
        sender_project="acme",
        live_uuids=set(),
    )
    assert not out.ok and out.error == "name_not_found"


def test_cross_project_qualified():
    names.register("u1", "planner", "other", "/tmp/o", 1)
    out = resolve.resolve_recipient(
        to="other/planner",
        sender_project="acme",
        live_uuids={"u1"},
    )
    assert out.ok and out.uuid == "u1"


def test_invalid_recipient():
    out = resolve.resolve_recipient(
        to="!!not-valid!!",
        sender_project="acme",
        live_uuids=set(),
    )
    assert not out.ok and out.error == "invalid_recipient"


def test_ambiguous_name_when_two_entries_match():
    """Two registry entries with the same (project, name) -> ambiguous_name.

    Can occur if `register()` (the forceful bootstrap path) was called
    twice for the same slot from different UUIDs, leaving two JSON
    files. lookup_by_name's linear scan would otherwise return a
    nondeterministic first match; this guard makes the failure mode
    explicit instead of silently picking one.
    """
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    names.register("u2", "planner", "acme", "/tmp/b", 2)
    out = resolve.resolve_recipient(
        to="planner",
        sender_project="acme",
        live_uuids={"u1", "u2"},
    )
    assert not out.ok and out.error == "ambiguous_name"


def test_lowercase_normalization_for_name():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    out = resolve.resolve_recipient(
        to="PLANNER",
        sender_project="acme",
        live_uuids={"u1"},
    )
    assert out.ok and out.uuid == "u1"
