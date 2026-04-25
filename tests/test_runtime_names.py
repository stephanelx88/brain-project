"""Name registry — set, get, lookup, validation, collision."""
from __future__ import annotations

import pytest

from brain.runtime import names


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_default_name_format_lowercase():
    assert names.default_name("Honeywell-Forge-Cognition", "68293") == \
        "honeywell-forge-cognition-68293"


def test_default_name_with_slashes_normalized():
    assert names.default_name("RICHARDSON/master/0425", "42139") == \
        "richardson-master-0425-42139"


def test_default_name_strips_unicode():
    # Non-[a-z0-9-] chars get replaced with '-', diacritics folded
    assert names.default_name("Đôi Dép", "1234") == "doi-dep-1234"


def test_validate_user_name_ok():
    assert names.validate_user_name("planner") is None


def test_validate_user_name_lowercase_required():
    err = names.validate_user_name("Planner")
    assert err and "lowercase" in err


def test_validate_user_name_reserved():
    for reserved in ("peer", "self", "all", "me"):
        err = names.validate_user_name(reserved)
        assert err and "reserved" in err


def test_validate_user_name_too_long():
    err = names.validate_user_name("a" * 65)
    assert err and "length" in err


def test_validate_user_name_bad_chars():
    err = names.validate_user_name("plan/ner")
    assert err and "chars" in err


def test_register_and_get():
    uuid = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    entry = names.register(
        uuid=uuid,
        name="planner",
        project="acme",
        cwd="/tmp/acme",
        pid=1234,
    )
    assert entry["name"] == "planner"
    got = names.get(uuid)
    assert got["uuid"] == uuid
    assert got["name"] == "planner"
    assert got["project"] == "acme"


def test_lookup_by_name_in_project():
    uuid = "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2"
    names.register(uuid, "planner", "acme", "/tmp/a", 1)
    assert names.lookup_by_name("planner", project="acme") == uuid


def test_lookup_by_name_not_found_returns_none():
    assert names.lookup_by_name("ghost", project="acme") is None


def test_lookup_by_name_wrong_project_returns_none():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    assert names.lookup_by_name("planner", project="other") is None


def test_set_name_collision_in_same_project():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    names.register("u2", "honey-2", "acme", "/tmp/b", 2)
    err = names.set_name("u2", "planner")
    assert err == "name_taken"


def test_set_name_same_name_different_project_ok():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    names.register("u2", "honey-2", "other", "/tmp/b", 2)
    err = names.set_name("u2", "planner")
    assert err is None
    assert names.get("u2")["name"] == "planner"


def test_delete_clears_entry():
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    names.delete("u1")
    assert names.get("u1") is None
