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


def test_set_name_takes_over_from_dead_holder():
    """When live_uuids hints that the current holder is dead, set_name
    silently reclaims the slot rather than returning name_taken."""
    names.register("u1-dead", "planner", "acme", "/tmp/a", 1)
    names.register("u2-live", "honey-2", "acme", "/tmp/b", 2)
    # Without the hint, behavior is unchanged — strict name_taken.
    assert names.set_name("u2-live", "planner") == "name_taken"
    # With live_uuids excluding u1-dead, u2-live wins.
    assert names.set_name("u2-live", "planner", live_uuids={"u2-live"}) is None
    assert names.get("u2-live")["name"] == "planner"
    # The dead holder's entry was rewritten to drop the now-stolen name.
    assert names.get("u1-dead")["name"] is None


def test_report_empty_registry():
    """Fresh root → zeros across the board, no exceptions."""
    rep = names.report()
    assert rep["total"] == 0
    assert rep["with_name"] == 0
    assert rep["alive_with_name"] == 0
    assert rep["dead_with_name"] == 0
    assert rep["stale_dead_holders"] == []
    assert rep["cross_project_collisions"] == []


def test_report_counts_alive_vs_dead():
    """With a live_uuids hint, named entries split into alive vs dead."""
    names.register("u-alive", "planner", "acme", "/tmp/a", 1)
    names.register("u-dead", "executor", "acme", "/tmp/b", 2)
    rep = names.report(live_uuids={"u-alive"})
    assert rep["total"] == 2
    assert rep["with_name"] == 2
    assert rep["alive_with_name"] == 1
    assert rep["dead_with_name"] == 1


def test_report_skips_entries_with_null_name():
    """An entry whose `name` field has been cleared (e.g. by dead-holder
    takeover) is no longer "named" and shouldn't be counted in
    with_name/alive/dead — it's basically an orphan slot."""
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    # Manually clear the name to simulate post-takeover state.
    p = paths.name_file("u1")
    import json as _json
    entry = _json.loads(p.read_text())
    entry["name"] = None
    p.write_text(_json.dumps(entry))

    rep = names.report(live_uuids={"u1"})
    assert rep["total"] == 1
    assert rep["with_name"] == 0
    assert rep["alive_with_name"] == 0


def test_report_flags_cross_project_collision():
    """Same name in 2+ projects is allowed by the registry but
    ambiguous when callers omit project context. Doctor needs the
    list to surface."""
    names.register("u1", "commandor", "vulcan", "/tmp/a", 1)
    names.register("u2", "commandor", "bangalore", "/tmp/b", 2)
    names.register("u3", "designer", "acme", "/tmp/c", 3)
    rep = names.report()
    collisions = rep["cross_project_collisions"]
    assert len(collisions) == 1
    assert collisions[0]["name"] == "commandor"
    assert collisions[0]["count"] == 2
    assert collisions[0]["projects"] == ["bangalore", "vulcan"]


def test_report_does_not_flag_same_project_dupes_as_collisions():
    """Multiple entries with the same (project, name) is a registry
    integrity bug elsewhere (lookup_uuids_by_name surfaces it as
    `ambiguous_name`). It is NOT a 'cross-project collision'."""
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    # Force a second entry for the same (project, name) by writing the
    # JSON directly — the public set_name path is race-guarded so we
    # have to bypass it to construct this state.
    p2 = paths.name_file("u2")
    p2.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    p2.write_text(_json.dumps({
        "uuid": "u2", "name": "planner", "project": "acme",
        "cwd": "/tmp/b", "pid": 2, "set_at": "2026-04-30T00:00:00.000+00:00",
    }))
    rep = names.report()
    assert rep["cross_project_collisions"] == []


def test_report_lists_stale_dead_holders():
    """Dead holders whose set_at is older than the threshold get
    surfaced. The stale list is the actionable subset of dead_with_name —
    user can rm them to reclaim the slot.
    """
    from datetime import datetime, timedelta, timezone
    names.register("u-fresh-dead", "fresh", "acme", "/tmp/a", 1)
    names.register("u-stale-dead", "stale", "acme", "/tmp/b", 2)

    # Backdate the stale entry so its set_at is 60 days old.
    p = paths.name_file("u-stale-dead")
    import json as _json
    entry = _json.loads(p.read_text())
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="milliseconds")
    entry["set_at"] = old_iso
    p.write_text(_json.dumps(entry))

    rep = names.report(live_uuids=set(), stale_after_days=30)
    stale_uuids = {row["uuid"] for row in rep["stale_dead_holders"]}
    assert stale_uuids == {"u-stale-dead"}
    # Both are dead, but only one is stale.
    assert rep["dead_with_name"] == 2


def test_report_treats_no_live_hint_as_liveness_unknown():
    """If caller doesn't supply live_uuids, alive/dead counts collapse
    to 0 — we can't lie about liveness without information."""
    names.register("u1", "planner", "acme", "/tmp/a", 1)
    rep = names.report()  # no live_uuids
    assert rep["with_name"] == 1
    assert rep["alive_with_name"] == 0
    assert rep["dead_with_name"] == 0


def test_set_name_does_not_steal_from_live_holder_even_with_hint():
    """If live_uuids includes the current holder, name_taken still wins —
    we must not steal a slot from a session that's actually alive."""
    names.register("u1-alive", "planner", "acme", "/tmp/a", 1)
    names.register("u2-also-alive", "honey-2", "acme", "/tmp/b", 2)
    err = names.set_name(
        "u2-also-alive",
        "planner",
        live_uuids={"u1-alive", "u2-also-alive"},
    )
    assert err == "name_taken"
    assert names.get("u1-alive")["name"] == "planner"
