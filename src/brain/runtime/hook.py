"""Entry point invoked by the UserPromptSubmit and Stop hooks.

Two modes (selected by `--stop` flag):

UserPromptSubmit (default): reads pending peer messages for the
calling session, prints a <system-reminder> block to stdout. Claude
Code prepends that block to the user's prompt. Atomically moves the
surfaced messages from pending/ to delivered/.

Stop (--stop): same read, but emits Claude Code's Stop-hook decision
JSON `{"decision": "block", "reason": <block>}`, which makes the
agent CONTINUE its loop with the inbox messages as the next "user"
turn instead of going idle. Solves the "peer just replied while I
was about to stop — but user has to type 'go' for me to notice"
problem documented in entity Brain-Session-Communication.

Stop mode also pre-polls briefly (BRAIN_STOP_POLL_SEC, default 2s)
so a peer reply that lands within the poll window catches the chain.

Never raises to the caller — Claude Code treats hook nonzero exit as
an error and would interrupt the user. Errors go to the log file.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

from brain.runtime import inbox, paths, session_id, surface


def run(stop_mode: bool = False) -> int:
    try:
        return _run(stop_mode=stop_mode)
    except Exception:  # noqa: BLE001 — broad on purpose; this is the safety net
        _log_exception()
        return 0


def _run(stop_mode: bool = False) -> int:
    own = session_id.detect_own_uuid()
    if not own:
        return 0
    if stop_mode:
        _poll_for_pending(own)
    pending = inbox.list_pending(own)
    if not pending:
        return 0
    block = surface.format_pending(pending)
    if block:
        if stop_mode:
            sys.stdout.write(json.dumps({"decision": "block", "reason": block}))
        else:
            sys.stdout.write(block)
        sys.stdout.flush()
    inbox.mark_delivered(own, [m["id"] for m in pending])
    return 0


def _poll_for_pending(uuid: str) -> None:
    """Wait briefly for a peer reply to land before declaring "stop".

    Bounded by BRAIN_STOP_POLL_SEC (default 2.0s, clamped [0, 30]). 0
    disables polling — Stop hook acts on whatever's already in pending/.
    Intentionally short: longer than this and the user perceives a
    visible stall at every assistant-turn end.
    """
    try:
        wait = float(os.environ.get("BRAIN_STOP_POLL_SEC", "2"))
    except ValueError:
        wait = 2.0
    wait = max(0.0, min(wait, 30.0))
    if wait <= 0:
        return
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if inbox.list_pending(uuid):
            return
        time.sleep(0.25)


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
    stop_mode = "--stop" in sys.argv[1:]
    return run(stop_mode=stop_mode)


if __name__ == "__main__":
    sys.exit(main())
