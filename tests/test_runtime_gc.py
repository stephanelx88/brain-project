"""Runtime GC: delivered TTL, pending TTL for dead UUIDs, name TTL."""
from __future__ import annotations

import os
import time

import pytest

from brain.runtime import gc, inbox, names, paths


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def _age_file(p, days):
    old = time.time() - days * 86400
    os.utime(p, (old, old))


def test_gc_prunes_old_delivered():
    msg = inbox.send("u1", "snd", "a", "b", "hi")
    inbox.mark_delivered("u1", [msg["id"]])
    delivered = paths.inbox_delivered_dir("u1") / f"{msg['id']}.json"
    _age_file(delivered, days=10)
    n = gc.run(live_uuids={"u1"})
    assert n["delivered_pruned"] == 1
    assert not delivered.exists()


def test_gc_keeps_recent_delivered():
    msg = inbox.send("u1", "snd", "a", "b", "hi")
    inbox.mark_delivered("u1", [msg["id"]])
    n = gc.run(live_uuids={"u1"})
    assert n["delivered_pruned"] == 0


def test_gc_prunes_pending_for_dead_uuid_after_ttl():
    msg = inbox.send("dead", "snd", "a", "b", "hi")
    pending_file = paths.inbox_pending_dir("dead") / f"{msg['id']}.json"
    _age_file(pending_file, days=40)
    n = gc.run(live_uuids=set())
    assert n["pending_pruned"] == 1
    assert not pending_file.exists()


def test_gc_keeps_pending_for_live_uuid_even_if_old():
    msg = inbox.send("alive", "snd", "a", "b", "hi")
    pending_file = paths.inbox_pending_dir("alive") / f"{msg['id']}.json"
    _age_file(pending_file, days=40)
    n = gc.run(live_uuids={"alive"})
    assert n["pending_pruned"] == 0
    assert pending_file.exists()


def test_gc_prunes_name_for_long_dead_uuid():
    names.register("ghost", "planner", "acme", "/tmp/g", 99)
    name_file = paths.name_file("ghost")
    _age_file(name_file, days=40)
    n = gc.run(live_uuids=set())
    assert n["names_pruned"] == 1
    assert not name_file.exists()


def test_gc_prune_dead_names_releases_reservation_so_name_is_reclaimable():
    """After GC removes a dead UUID's name JSON, the (project, name) slot
    must be reclaimable by a fresh session.

    Pre-fix bug: _prune_dead_names called Path.unlink() directly,
    bypassing names._release_reservations_for(). The orphaned reservation
    file under _name_reservations/ kept O_EXCL claim on (project, name),
    so a follow-up brain_set_name in the same project came back
    `name_taken` even though no live session held the slot. The session
    couldn't reclaim its own name after a long absence — silent dead end.
    """
    # 1. Long-dead session was once registered as `planner` in `acme`.
    names.register("ghost-uuid", "planner", "acme", "/tmp/g", 99)
    name_file = paths.name_file("ghost-uuid")
    reservation = paths.name_reservation_file("acme", "planner")
    assert name_file.exists()
    assert reservation.exists(), "register() should have placed the lock"
    _age_file(name_file, days=40)

    # 2. GC fires for an empty live-set — should remove BOTH the JSON
    #    entry AND the (project, name) reservation.
    counts = gc.run(live_uuids=set())
    assert counts["names_pruned"] == 1
    assert not name_file.exists()
    assert not reservation.exists(), (
        "GC must release the (project, name) reservation when it removes "
        "a dead UUID's name JSON; otherwise the slot is permanently locked"
    )

    # 3. End-to-end: a fresh session in the same project can now claim
    #    the freed alias. This is what was broken before — name_taken
    #    even on an empty registry.
    names.register("new-uuid", "honey-2", "acme", "/tmp/n", 100)
    err = names.set_name("new-uuid", "planner")
    assert err is None, f"new session should reclaim freed name; got {err!r}"
    assert names.get("new-uuid")["name"] == "planner"


def test_gc_prunes_orphan_inbox_no_name_no_live_after_ttl():
    """UUID with no names/<uuid>.json AND not in live_uuids -> pruned."""
    inbox.send("orphan", "snd", "a", "b", "hi")
    sid_dir = paths.inbox_dir() / "orphan"
    assert sid_dir.exists()
    _age_file(sid_dir, days=2)
    n = gc.run(live_uuids=set())
    assert n["orphans_pruned"] == 1
    assert not sid_dir.exists()


def test_gc_keeps_orphan_inbox_if_live_even_without_name():
    """Live UUID without a name registry entry -> NOT pruned (live overrides)."""
    inbox.send("newborn", "snd", "a", "b", "hi")
    sid_dir = paths.inbox_dir() / "newborn"
    _age_file(sid_dir, days=2)
    n = gc.run(live_uuids={"newborn"})
    assert n["orphans_pruned"] == 0
    assert sid_dir.exists()


def test_gc_does_not_orphan_prune_uuid_with_name_file():
    """Dead UUID WITH name registry entry -> NOT pruned by orphan rule
    (only the existing dead-pending rule applies)."""
    names.register("named-dead", "planner", "acme", "/tmp/g", 99)
    inbox.send("named-dead", "snd", "a", "b", "hi")
    sid_dir = paths.inbox_dir() / "named-dead"
    _age_file(sid_dir, days=2)
    n = gc.run(live_uuids=set())
    assert n["orphans_pruned"] == 0
    assert sid_dir.exists()


def test_gc_keeps_recent_orphan_inbox_under_ttl():
    """Orphan UUID with dir mtime younger than TTL -> NOT pruned."""
    inbox.send("fresh-orphan", "snd", "a", "b", "hi")
    sid_dir = paths.inbox_dir() / "fresh-orphan"
    _age_file(sid_dir, days=0.5)
    n = gc.run(live_uuids=set())
    assert n["orphans_pruned"] == 0
    assert sid_dir.exists()


def test_maybe_run_runs_when_no_stamp_exists():
    """First call (no stamp file) executes a GC pass and writes the stamp."""
    msg = inbox.send("u1", "snd", "a", "b", "hi")
    inbox.mark_delivered("u1", [msg["id"]])
    delivered = paths.inbox_delivered_dir("u1") / f"{msg['id']}.json"
    _age_file(delivered, days=10)

    counts = gc.maybe_run(live_uuids={"u1"})
    assert counts is not None
    assert counts["delivered_pruned"] == 1
    assert gc._stamp_path().exists()


def test_maybe_run_skips_when_stamp_recent():
    """Within throttle window: returns None and does NOT prune."""
    # Seed a stale delivered file so we'd notice if GC ran.
    msg = inbox.send("u1", "snd", "a", "b", "hi")
    inbox.mark_delivered("u1", [msg["id"]])
    delivered = paths.inbox_delivered_dir("u1") / f"{msg['id']}.json"
    _age_file(delivered, days=10)

    # Touch stamp 'now' so throttle blocks the call.
    stamp = gc._stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()

    counts = gc.maybe_run(live_uuids={"u1"}, min_interval_sec=3600)
    assert counts is None
    assert delivered.exists()  # not pruned


def test_maybe_run_runs_when_stamp_older_than_interval():
    """Stamp older than min_interval -> GC runs and stamp is refreshed."""
    msg = inbox.send("u1", "snd", "a", "b", "hi")
    inbox.mark_delivered("u1", [msg["id"]])
    delivered = paths.inbox_delivered_dir("u1") / f"{msg['id']}.json"
    _age_file(delivered, days=10)

    stamp = gc._stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    _age_file(stamp, days=1)  # well past 1-hour throttle
    pre_mtime = stamp.stat().st_mtime

    counts = gc.maybe_run(live_uuids={"u1"}, min_interval_sec=3600)
    assert counts is not None
    assert counts["delivered_pruned"] == 1
    assert stamp.stat().st_mtime > pre_mtime  # stamp refreshed


def test_main_invokes_run_and_prints_summary(monkeypatch, capsys):
    """`python -m brain.runtime.gc` discovers live UUIDs, runs all GC
    passes, and prints a one-line `runtime-gc:` summary with the
    pruned counts."""
    # Seed: one stale delivered (live UUID, so pending/orphan rules
    # don't fire) and one stale dead-name in the registry. Skip
    # orphan inbox to avoid clobbering the delivered file under
    # "u-alive". The two non-zero counts let us validate the print
    # format end-to-end.
    msg = inbox.send("u-alive", "snd", "a", "b", "hi")
    inbox.mark_delivered("u-alive", [msg["id"]])
    delivered = paths.inbox_delivered_dir("u-alive") / f"{msg['id']}.json"
    _age_file(delivered, days=10)

    names.register("u-ghost", "planner", "acme", "/tmp/g", 99)
    name_file = paths.name_file("u-ghost")
    _age_file(name_file, days=40)

    # Stub live_sessions.list_live_sessions to a controlled set
    # (u-alive is "live", u-ghost is dead).
    import brain.live_sessions as ls
    monkeypatch.setattr(
        ls, "list_live_sessions",
        lambda include_self=True: [{"session_id": "u-alive"}],
    )

    rc = gc.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "runtime-gc:" in out
    assert "delivered=1" in out
    assert "names=1" in out
    assert "pending=" in out
    assert "orphans=" in out
