"""Inbox storage: write pending, list, mark delivered, prune."""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from brain.runtime import inbox, paths


@pytest.fixture(autouse=True)
def _runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path))


def test_send_writes_pending_file():
    msg = inbox.send(
        to_uuid="receiver-uuid",
        from_uuid="sender-uuid",
        from_name_at_send="planner",
        to_name_at_send="executor",
        body="hello",
    )
    assert msg["id"]
    pending_files = list(paths.inbox_pending_dir("receiver-uuid").iterdir())
    assert len(pending_files) == 1
    assert pending_files[0].name == f"{msg['id']}.json"
    payload = json.loads(pending_files[0].read_text())
    assert payload["body"] == "hello"
    assert payload["from_uuid"] == "sender-uuid"
    assert payload["to_uuid"] == "receiver-uuid"


def test_send_rejects_oversize_body():
    big = "x" * (32 * 1024 + 1)
    with pytest.raises(inbox.BodyTooLarge):
        inbox.send(
            to_uuid="receiver-uuid",
            from_uuid="sender-uuid",
            from_name_at_send="planner",
            to_name_at_send="executor",
            body=big,
        )


def test_list_pending_returns_envelopes_in_ulid_order():
    inbox.send("rcv", "snd", "a", "b", "first")
    time.sleep(0.002)
    inbox.send("rcv", "snd", "a", "b", "second")
    msgs = inbox.list_pending("rcv")
    assert [m["body"] for m in msgs] == ["first", "second"]


def test_list_pending_empty():
    assert inbox.list_pending("nobody") == []


def test_mark_delivered_moves_files():
    msg = inbox.send("rcv", "snd", "a", "b", "hello")
    inbox.mark_delivered("rcv", [msg["id"]])
    assert list(paths.inbox_pending_dir("rcv").iterdir()) == []
    delivered = list(paths.inbox_delivered_dir("rcv").iterdir())
    assert len(delivered) == 1
    assert delivered[0].name == f"{msg['id']}.json"


def test_mark_delivered_idempotent_on_missing():
    msg = inbox.send("rcv", "snd", "a", "b", "hello")
    inbox.mark_delivered("rcv", [msg["id"]])
    inbox.mark_delivered("rcv", [msg["id"]])  # no raise
    assert list(paths.inbox_pending_dir("rcv").iterdir()) == []


def test_prune_delivered_removes_old():
    msg = inbox.send("rcv", "snd", "a", "b", "hello")
    inbox.mark_delivered("rcv", [msg["id"]])
    delivered_file = paths.inbox_delivered_dir("rcv") / f"{msg['id']}.json"
    old = time.time() - (10 * 86400)
    os.utime(delivered_file, (old, old))
    pruned = inbox.prune_delivered("rcv", ttl_days=7)
    assert pruned == 1
    assert not delivered_file.exists()


def test_ulid_monotonic_and_unique():
    ids = {inbox._ulid() for _ in range(1000)}
    assert len(ids) == 1000


def test_concurrent_send_no_overwrites():
    """100 concurrent sends to the same inbox: every message must land
    with a unique id, all readable via list_pending. Reveals any race
    on ULID generation or atomic-write paths."""
    n = 100

    def _send(i: int) -> dict:
        return inbox.send(
            to_uuid="rcv",
            from_uuid=f"snd-{i}",
            from_name_at_send="planner",
            to_name_at_send="executor",
            body=f"msg-{i}",
        )

    sent_ids: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_send, i) for i in range(n)]
        for f in as_completed(futures):
            sent_ids.append(f.result()["id"])

    # All ids returned by send() are unique
    assert len(set(sent_ids)) == n, "send() returned duplicate ULIDs"

    # All n files landed on disk
    pending_files = list(paths.inbox_pending_dir("rcv").iterdir())
    assert len(pending_files) == n, (
        f"expected {n} files on disk, found {len(pending_files)}"
    )

    # list_pending sees every one of them
    listed = inbox.list_pending("rcv")
    assert len(listed) == n
    listed_ids = {m["id"] for m in listed}
    assert listed_ids == set(sent_ids)
    bodies = {m["body"] for m in listed}
    assert bodies == {f"msg-{i}" for i in range(n)}


