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
