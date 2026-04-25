"""Inbox storage primitives.

File-tree layout under BRAIN_RUNTIME_DIR/inbox/<receiver-uuid>/:
    pending/<ulid>.json    — unread, FIFO by ULID
    delivered/<ulid>.json  — read; pruned at TTL
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Iterable

from brain.io import atomic_write_text
from brain.runtime import paths

MAX_BODY_BYTES = 32 * 1024  # 32 KiB
DEFAULT_DELIVERED_TTL_DAYS = 7

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class BodyTooLarge(ValueError):
    """Raised when a message body exceeds MAX_BODY_BYTES."""


def _ulid(now_ms: int | None = None) -> str:
    """Generate a Crockford-base32 ULID — 48 bits time + 80 bits randomness."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    rand = secrets.randbits(80)
    encoded_time = _b32encode(now_ms, 10)
    encoded_rand = _b32encode(rand, 16)
    return encoded_time + encoded_rand


def _b32encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def send(
    to_uuid: str,
    from_uuid: str,
    from_name_at_send: str,
    to_name_at_send: str,
    body: str,
) -> dict:
    """Write a message into receiver's pending/. Returns the envelope dict."""
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise BodyTooLarge(
            f"body exceeds {MAX_BODY_BYTES} bytes "
            f"({len(body.encode('utf-8'))} given)"
        )
    msg_id = _ulid()
    envelope = {
        "id": msg_id,
        "from_uuid": from_uuid,
        "from_name_at_send": from_name_at_send,
        "to_uuid": to_uuid,
        "to_name_at_send": to_name_at_send,
        "body": body,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    target = paths.inbox_pending_dir(to_uuid) / f"{msg_id}.json"
    atomic_write_text(target, json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    return envelope


def list_pending(to_uuid: str) -> list[dict]:
    """Return all pending envelopes for `to_uuid`, sorted by id (ULID = time-ordered)."""
    pdir = paths.inbox_pending_dir(to_uuid)
    if not pdir.exists():
        return []
    out: list[dict] = []
    for p in sorted(pdir.iterdir()):
        if not p.suffix == ".json":
            continue
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def list_delivered(to_uuid: str) -> list[dict]:
    ddir = paths.inbox_delivered_dir(to_uuid)
    if not ddir.exists():
        return []
    out: list[dict] = []
    for p in sorted(ddir.iterdir()):
        if not p.suffix == ".json":
            continue
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def mark_delivered(to_uuid: str, message_ids: Iterable[str]) -> int:
    """Atomic-rename matching pending/<id>.json to delivered/. Idempotent."""
    pdir = paths.inbox_pending_dir(to_uuid)
    ddir = paths.inbox_delivered_dir(to_uuid)
    ddir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for mid in message_ids:
        src = pdir / f"{mid}.json"
        dst = ddir / f"{mid}.json"
        try:
            os.replace(src, dst)
            moved += 1
        except FileNotFoundError:
            continue
    return moved


def prune_delivered(to_uuid: str, ttl_days: int = DEFAULT_DELIVERED_TTL_DAYS) -> int:
    """Delete delivered/ files older than ttl_days. Returns count removed."""
    ddir = paths.inbox_delivered_dir(to_uuid)
    if not ddir.exists():
        return 0
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    for p in ddir.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except (OSError, FileNotFoundError):
            continue
    return removed
