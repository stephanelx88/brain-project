"""Entry point invoked by the UserPromptSubmit hook.

Reads pending messages for the calling session, formats a
SystemReminder block to stdout, and atomically moves the surfaced
messages from pending/ to delivered/.

Never raises to the caller — Claude Code treats hook nonzero exit as
an error and would interrupt the user. Errors go to the log file.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

from brain.runtime import inbox, paths, session_id, surface


def run() -> int:
    try:
        return _run()
    except Exception:  # noqa: BLE001 — broad on purpose; this is the safety net
        _log_exception()
        return 0


def _run() -> int:
    own = session_id.detect_own_uuid()
    if not own:
        return 0
    pending = inbox.list_pending(own)
    if not pending:
        return 0
    block = surface.format_pending(pending)
    if block:
        sys.stdout.write(block)
        sys.stdout.flush()
    inbox.mark_delivered(own, [m["id"] for m in pending])
    return 0


def _log_exception() -> None:
    try:
        log = paths.hook_log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with log.open("a") as f:
            f.write(f"\n=== {ts} ===\n")
            traceback.print_exc(file=f)
    except Exception:  # noqa: BLE001
        # If even logging fails, swallow — never crash the hook
        pass


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
