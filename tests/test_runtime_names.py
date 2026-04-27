"""Name registry — set, get, lookup, validation, collision."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

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


def test_set_name_collision_under_concurrent_writes():
    """Two sessions in the same project race to claim the same name.

    Contract: exactly one winner; the other gets `name_taken`. The
    O_EXCL reservation file under
    `paths.name_reservations_dir()/<project>__<name>.lock` is the
    linearization point that makes this race-safe.
    """
    target = "shared-name"

    def _claim(uuid: str) -> object:
        return names.set_name(uuid, target)

    results: list[tuple[str, ...]] = []
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

    expected = ("", "name_taken")
    assert all(r == expected for r in results), (
        f"expected exactly-one-winner per trial; got "
        f"{[r for r in results if r != expected][:5]}"
    )


def test_set_name_collision_under_4_thread_concurrent_writes():
    """Four sessions in the same project race for the same name.

    Stronger version of the 2-thread race: 4 sessions hammer one name
    across 50 trials. Contract: exactly ONE winner per trial, the other
    three get `name_taken`. Catches reservation primitives that only
    happen to serialise cleanly at low contention.
    """
    target = "shared-name"
    uuids = ("u1", "u2", "u3", "u4")

    def _claim(uuid: str) -> object:
        return names.set_name(uuid, target)

    trials = 50
    bad_trials: list[tuple] = []
    for _ in range(trials):
        # Reset every uuid to a unique non-conflicting name.
        for i, u in enumerate(uuids, start=1):
            names.register(u, f"honey-{i}", "acme", f"/tmp/{u}", i)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_claim, u) for u in uuids]
            trial_results = [f.result() for f in as_completed(futures)]
        winners = [r for r in trial_results if r is None]
        losers = [r for r in trial_results if r == "name_taken"]
        if not (len(winners) == 1 and len(losers) == 3):
            bad_trials.append(tuple(sorted(
                "" if r is None else str(r) for r in trial_results
            )))

    assert not bad_trials, (
        f"{len(bad_trials)}/{trials} trials had != 1 winner; "
        f"first few: {bad_trials[:5]}"
    )


def test_delete_releases_reservation_so_name_is_reclaimable():
    """After delete(uuid), the (project, name) slot must be free again."""
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    # u2 cannot claim 'planner' while u1 holds it.
    names.register("u2", "honey-2", "acme", "/tmp/b", 2)
    assert names.set_name("u2", "planner") == "name_taken"
    # Once u1 is deleted, the reservation is released and u2 can claim it.
    names.delete("u1")
    assert names.set_name("u2", "planner") is None
    assert names.get("u2")["name"] == "planner"
