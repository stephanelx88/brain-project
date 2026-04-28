"""Best-effort tmux poke — wake an idle peer session by sending Enter
to its tmux pane.

Why this exists: brain_send drops a JSON envelope into the recipient's
inbox; UserPromptSubmit and Stop hooks deliver it to the agent — but
both fire only on the recipient's own activity. A truly idle session
(assistant has long since stopped, terminal at empty prompt) needs an
external write channel. tmux's `send-keys` is the cleanest one we have
on this platform: as long as the recipient registered its TMUX_PANE,
we can synthesize an Enter keystroke from the sender's process and the
recipient's UserPromptSubmit hook does the rest.

What we do NOT do:
  - Type any text. Just Enter. If the user has half-typed text, it
    gets submitted along with the inbox-surface block prepended to
    it — annoying but message delivery is preserved. If the prompt
    is empty (the common idle case), Enter is a no-op for the prompt
    buffer but still triggers UserPromptSubmit.
  - Poke when the pane is in tmux copy-mode (user is reading
    scrollback) — `send-keys` to copy-mode goes to the copy-mode
    keybindings, not to the program.

Opt-out: BRAIN_TMUX_POKE=0 disables the call entirely, leaving the
old pull-only behavior. Failure of every step is silent — pull-based
hooks are still the safety net.
"""
from __future__ import annotations

import os
import subprocess

from brain.runtime import names


def poke_session(uuid: str) -> bool:
    """Deliver a wake-up Enter to `uuid`'s tmux pane. Returns True on
    successful tmux invocation (does not guarantee the agent woke)."""
    if os.environ.get("BRAIN_TMUX_POKE", "1") == "0":
        return False
    entry = names.get(uuid)
    if not entry:
        return False
    pane = entry.get("tmux_pane")
    if not pane:
        return False
    if _pane_in_copy_mode(pane):
        return False
    return _send_enter(pane)


def _send_enter(pane: str) -> bool:
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", pane, "Enter"],
            timeout=2,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _pane_in_copy_mode(pane: str) -> bool:
    """True when the recipient's pane is in tmux copy mode (user is
    reading scrollback). Sending keys there hits copy-mode bindings
    instead of the program — so we skip the poke until they exit."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_in_mode}"],
            timeout=2,
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.stdout.strip() == "1"
