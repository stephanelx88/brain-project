"""TTL-based cleanup for the runtime transport directory.

Run periodically (launchd or cron) and lazily on each `brain_send`.
Hard-deletes only — there's no undo. Caller passes `live_uuids` so we
know which inboxes belong to abandoned sessions.
"""
from __future__ import annotations

import shutil
import time
from typing import Set

from brain.runtime import inbox, paths

DEFAULT_DELIVERED_TTL_DAYS = 7
DEFAULT_PENDING_TTL_DAYS = 30
DEFAULT_NAME_TTL_DAYS = 30
DEFAULT_ORPHAN_TTL_DAYS = 1


def run(
    live_uuids: Set[str],
    *,
    delivered_ttl_days: int = DEFAULT_DELIVERED_TTL_DAYS,
    pending_ttl_days: int = DEFAULT_PENDING_TTL_DAYS,
    name_ttl_days: int = DEFAULT_NAME_TTL_DAYS,
    orphan_ttl_days: int = DEFAULT_ORPHAN_TTL_DAYS,
) -> dict:
    """Run all GC passes. Returns counts dict."""
    delivered_pruned = _prune_all_delivered(delivered_ttl_days)
    pending_pruned = _prune_dead_pending(live_uuids, pending_ttl_days)
    names_pruned = _prune_dead_names(live_uuids, name_ttl_days)
    orphans_pruned = _prune_orphan_inboxes(live_uuids, orphan_ttl_days)
    return {
        "delivered_pruned": delivered_pruned,
        "pending_pruned": pending_pruned,
        "names_pruned": names_pruned,
        "orphans_pruned": orphans_pruned,
    }


def _prune_all_delivered(ttl_days: int) -> int:
    inbox_root = paths.inbox_dir()
    if not inbox_root.exists():
        return 0
    total = 0
    for sid_dir in inbox_root.iterdir():
        if not sid_dir.is_dir():
            continue
        total += inbox.prune_delivered(sid_dir.name, ttl_days=ttl_days)
    return total


def _prune_dead_pending(live_uuids: Set[str], ttl_days: int) -> int:
    inbox_root = paths.inbox_dir()
    if not inbox_root.exists():
        return 0
    cutoff = time.time() - ttl_days * 86400
    total = 0
    for sid_dir in inbox_root.iterdir():
        if not sid_dir.is_dir():
            continue
        if sid_dir.name in live_uuids:
            continue  # don't prune pending for live recipients
        pdir = sid_dir / "pending"
        if not pdir.exists():
            continue
        for p in pdir.iterdir():
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    total += 1
            except (OSError, FileNotFoundError):
                continue
    return total


def _prune_orphan_inboxes(live_uuids: Set[str], ttl_days: int) -> int:
    """Remove inbox/<uuid>/ subtrees whose UUID has no name registry entry
    and isn't a live session — i.e. messages addressed to a UUID nobody
    can ever pick up. Uses a shorter TTL than dead-pending because such
    inboxes are unaddressable by definition.
    """
    inbox_root = paths.inbox_dir()
    if not inbox_root.exists():
        return 0
    cutoff = time.time() - ttl_days * 86400
    total = 0
    for sid_dir in inbox_root.iterdir():
        if not sid_dir.is_dir():
            continue
        uuid = sid_dir.name
        if uuid in live_uuids:
            continue  # live session — not orphan
        if paths.name_file(uuid).exists():
            continue  # has a name registry entry — not orphan (handled by dead-pending rule)
        try:
            if sid_dir.stat().st_mtime < cutoff:
                shutil.rmtree(sid_dir)
                total += 1
        except (OSError, FileNotFoundError):
            continue
    return total


def _prune_dead_names(live_uuids: Set[str], ttl_days: int) -> int:
    ndir = paths.names_dir()
    if not ndir.exists():
        return 0
    cutoff = time.time() - ttl_days * 86400
    total = 0
    for p in ndir.iterdir():
        if not p.suffix == ".json":
            continue
        uuid = p.stem
        if uuid in live_uuids:
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                total += 1
        except (OSError, FileNotFoundError):
            continue
    return total


def main() -> int:
    """CLI entry: `python -m brain.runtime.gc` — discover live UUIDs from brain.live_sessions."""
    from brain import live_sessions as _ls
    live = {row["session_id"] for row in _ls.list_live_sessions(include_self=True)}
    counts = run(live)
    print(
        f"runtime-gc: delivered={counts['delivered_pruned']} "
        f"pending={counts['pending_pruned']} "
        f"names={counts['names_pruned']} "
        f"orphans={counts['orphans_pruned']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
