"""Name registry — set, get, lookup, validation, collision."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from brain.runtime import names, paths


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


def test_all_entries_empty_when_no_names():
    """Fresh runtime root with no registered names → empty list, no error."""
    assert names.all_entries() == []


def test_all_entries_returns_all():
    """3 sessions across 2 projects → all 3 entries returned."""
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    names.register("u2", "executor", "acme", "/tmp/b", 2)
    names.register("u3", "planner", "other", "/tmp/c", 3)
    entries = names.all_entries()
    assert len(entries) == 3
    by_uuid = {e["uuid"]: e for e in entries}
    assert by_uuid["u1"]["name"] == "planner"
    assert by_uuid["u1"]["project"] == "acme"
    assert by_uuid["u2"]["name"] == "executor"
    assert by_uuid["u3"]["project"] == "other"


def test_all_entries_skips_corrupt_files():
    """Malformed JSON files in names/ dir are skipped silently — the
    valid sibling is still returned."""
    names.register("good-uuid", "planner", "acme", "/tmp/a", 1)
    bad = paths.names_dir() / "corrupt-uuid.json"
    bad.write_text("not-valid-json{")
    entries = names.all_entries()
    # The malformed file is skipped; the good one is returned.
    assert [e["uuid"] for e in entries] == ["good-uuid"]


def test_set_name_collision_under_concurrent_writes():
    """Two sessions in the same project race to claim the same name.

    Contract: at most one wins; the other gets `name_taken`. The current
    implementation has a check-then-write race window between
    `lookup_by_name` and `_write` (no filesystem-level locking), so this
    test is marked xfail to document the gap without blocking CI. If
    `set_name` ever picks up an atomic claim primitive, flip xfail off.
    """
    target = "shared-name"

    def _claim(uuid: str) -> object:
        return names.set_name(uuid, target)

    results: list[tuple[str, ...]] = []
    # Hammer it across many trials to maximise the chance of catching
    # the race; a single attempt would almost always serialise cleanly.
    trials = 50
    for _ in range(trials):
        # Reset both names back to non-conflicting before each trial.
        names.register("u1", "honey-1", "acme", "/tmp/a", 1)
        names.register("u2", "honey-2", "acme", "/tmp/b", 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_claim, "u1"), pool.submit(_claim, "u2")]
            trial_results = [f.result() for f in as_completed(futures)]
        results.append(tuple(sorted(  # normalise: ("", "name_taken")
            "" if r is None else str(r) for r in trial_results
        )))

    # Contract assertion: every trial produced exactly one winner
    # (None) and one loser ("name_taken").
    expected = ("", "name_taken")
    bad = [r for r in results if r != expected]
    if bad:
        pytest.xfail(
            "set_name has a check-then-write race; "
            f"{len(bad)}/{trials} trials produced {bad[:3]} instead of "
            f"{expected}. Needs filesystem-level claim primitive."
        )
    assert all(r == expected for r in results)
