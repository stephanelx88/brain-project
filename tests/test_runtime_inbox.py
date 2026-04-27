"""Inbox storage: write pending, list, mark delivered, prune."""
from __future__ import annotations

import json
import os
import stat
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


def test_list_delivered_empty():
    """Fresh inbox with no delivered/ dir → empty list, no error."""
    assert inbox.list_delivered("nobody") == []


def test_list_delivered_returns_in_ulid_order():
    """Send 3 messages, mark delivered, list → chronological (ULID) order."""
    m1 = inbox.send("rcv", "snd", "a", "b", "first")
    time.sleep(0.002)
    m2 = inbox.send("rcv", "snd", "a", "b", "second")
    time.sleep(0.002)
    m3 = inbox.send("rcv", "snd", "a", "b", "third")
    inbox.mark_delivered("rcv", [m1["id"], m2["id"], m3["id"]])
    delivered = inbox.list_delivered("rcv")
    assert [m["body"] for m in delivered] == ["first", "second", "third"]
    assert [m["id"] for m in delivered] == [m1["id"], m2["id"], m3["id"]]


def test_list_delivered_skips_malformed_json():
    """Corrupt JSON file in delivered/ must be silently skipped, not raise."""
    good = inbox.send("rcv", "snd", "a", "b", "good")
    inbox.mark_delivered("rcv", [good["id"]])
    ddir = paths.inbox_delivered_dir("rcv")
    bad = ddir / "01ABCDEFGHJKMNPQRSTVWXYZ12.json"
    bad.write_text("not-valid-json{")
    delivered = inbox.list_delivered("rcv")
    # The malformed one is skipped; the good one is returned.
    assert [m["id"] for m in delivered] == [good["id"]]


def test_list_pending_skips_malformed_json():
    """Corrupt JSON file in pending/ must be silently skipped, not raise."""
    # Seed one valid envelope, then drop a malformed sibling.
    good = inbox.send("rcv", "snd", "a", "b", "good")
    pdir = paths.inbox_pending_dir("rcv")
    bad = pdir / "01ABCDEFGHJKMNPQRSTVWXYZ12.json"  # ULID-shaped fake name
    bad.write_text("not-valid-json{")
    msgs = inbox.list_pending("rcv")
    # The malformed one is skipped; the good one is returned.
    assert [m["id"] for m in msgs] == [good["id"]]


def test_list_pending_skips_unreadable_file(tmp_path):
    """chmod 000 file in pending/ must be skipped silently (OSError swallowed)."""
    if os.geteuid() == 0:
        pytest.skip("running as root: chmod 000 has no effect")
    good = inbox.send("rcv", "snd", "a", "b", "good")
    pdir = paths.inbox_pending_dir("rcv")
    locked = pdir / "01ZZZZZZZZZZZZZZZZZZZZZZZZ.json"
    locked.write_text(json.dumps({"id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ", "body": "x"}))
    os.chmod(locked, 0)
    try:
        msgs = inbox.list_pending("rcv")
    finally:
        # restore so pytest can clean up
        os.chmod(locked, stat.S_IRUSR | stat.S_IWUSR)
    assert [m["id"] for m in msgs] == [good["id"]]
