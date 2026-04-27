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
