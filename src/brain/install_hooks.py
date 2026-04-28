"""Idempotent install/remove of brain SessionStart hooks.

Wires (or unwires) the brain harvest+audit pair into:
  - `~/.claude/settings.json`        (Claude Code SessionStart)
  - `~/.cursor/hooks.json`           (Cursor sessionStart)

Both files are JSON-merged, both back up before mutating, both no-op
when the parent app dir is missing — so a machine that only runs one
of Claude/Cursor doesn't get noisy warnings about the other.

Why a Python module instead of inline shell-heredoc:
  - the merge logic is non-trivial (preserve siblings, drop just our
    entries on uninstall, handle malformed JSON, back up on every
    mutation). Easier to unit-test as a module than as a heredoc.
  - same code drives both install and uninstall, so the "what counts
    as ours" predicates live in one place.

CLI entrypoints (called from `bin/install.sh` and `bin/uninstall.sh`):
    python -m brain.install_hooks install <settings_src> <hooks_src>
    python -m brain.install_hooks remove
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# Predicates marking a hook entry as brain-owned. Both install and
# remove use these so what we drop is exactly what we wrote.
CLAUDE_BRAIN_MARKERS = ("brain.harvest_session", "brain.audit")
CURSOR_BRAIN_MARKER = "cursor-session-start.sh"


# ─────────────────────────────────────────────────────────────────────────
# JSON helpers — tolerate every "this file already existed" failure mode
# we've actually hit on real machines.
# ─────────────────────────────────────────────────────────────────────────
def _load_json(path: Path) -> dict[str, Any] | None:
    """Return parsed dict, `{}` if missing, or `None` if file exists but
    isn't a JSON object (caller should bail with a warning, not clobber)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _save_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic-ish write with timestamped backup of the prior contents."""
    if path.exists():
        ts = time.strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak.{ts}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# ─────────────────────────────────────────────────────────────────────────
# Claude Code (~/.claude/settings.json)
# ─────────────────────────────────────────────────────────────────────────
def _claude_settings_path(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def install_claude(home: Path, brain_block: dict[str, Any]) -> str | None:
    """Merge `brain_block`'s SessionStart entry into Claude settings.

    Replaces any prior `SessionStart` whole — Claude's schema treats
    SessionStart as a list of matcher groups (not a free-form bag), so
    surgical inner-merging would risk producing a malformed group. If
    the user had their own SessionStart hooks, the .bak file written
    before mutation is their hand-merge starting point.

    Returns the path written, or None if the hook was skipped (Claude
    not installed, or the existing settings file is malformed).
    """
    if not (home / ".claude").is_dir():
        return None
    target = _claude_settings_path(home)
    existing = _load_json(target)
    if existing is None:
        return None
    hooks = existing.setdefault("hooks", {})
    hooks["SessionStart"] = brain_block["hooks"]["SessionStart"]
    _save_json(target, existing)
    return str(target)


def _is_brain_claude_hook(entry: dict[str, Any]) -> bool:
    """A Claude hook entry is brain-owned iff its command runs one of
    our modules. Matching on the module name (not the full path) so this
    keeps working when PYTHONPATH or PYTHON change between installs."""
    cmd = entry.get("command") or ""
    return any(marker in cmd for marker in CLAUDE_BRAIN_MARKERS)


def remove_claude(home: Path) -> str | None:
    """Drop only brain-owned hook entries from Claude settings.

    Preserves sibling hooks the user added by hand (or that other tools
    installed). Empty groups and an empty `SessionStart` are pruned so
    the surrounding JSON stays clean.
    """
    target = _claude_settings_path(home)
    existing = _load_json(target)
    if not existing:
        return None
    bag = existing.get("hooks") or {}
    starters = bag.get("SessionStart") or []
    cleaned: list[dict[str, Any]] = []
    for group in starters:
        inner = [h for h in (group.get("hooks") or []) if not _is_brain_claude_hook(h)]
        if inner:
            cleaned.append({**group, "hooks": inner})
    if cleaned == starters:
        return None
    if cleaned:
        bag["SessionStart"] = cleaned
    else:
        bag.pop("SessionStart", None)
    if not bag:
        existing.pop("hooks", None)
    _save_json(target, existing)
    return str(target)


# ─────────────────────────────────────────────────────────────────────────
# Cursor (~/.cursor/hooks.json)
# ─────────────────────────────────────────────────────────────────────────
def _cursor_hooks_path(home: Path) -> Path:
    return home / ".cursor" / "hooks.json"


def install_cursor(home: Path, brain_block: dict[str, Any]) -> str | None:
    """Merge `brain_block`'s sessionStart entry into Cursor hooks.json.

    Replaces just the `sessionStart` array — preserves any other event
    hooks (preToolUse, beforeShellExecution, etc) the user has wired.
    """
    if not (home / ".cursor").is_dir():
        return None
    target = _cursor_hooks_path(home)
    existing = _load_json(target)
    if existing is None:
        return None
    existing.setdefault("version", brain_block.get("version", 1))
    bag = existing.setdefault("hooks", {})
    bag["sessionStart"] = brain_block["hooks"]["sessionStart"]
    _save_json(target, existing)
    return str(target)


def _is_brain_cursor_hook(entry: dict[str, Any]) -> bool:
    return CURSOR_BRAIN_MARKER in (entry.get("command") or "")


def remove_cursor(home: Path) -> str | None:
    target = _cursor_hooks_path(home)
    existing = _load_json(target)
    if not existing:
        return None
    bag = existing.get("hooks") or {}
    starters = bag.get("sessionStart") or []
    cleaned = [h for h in starters if not _is_brain_cursor_hook(h)]
    if cleaned == starters:
        return None
    if cleaned:
        bag["sessionStart"] = cleaned
    else:
        bag.pop("sessionStart", None)
    if not bag:
        existing.pop("hooks", None)
    _save_json(target, existing)
    return str(target)


# ─────────────────────────────────────────────────────────────────────────
# Claude Code (~/.claude/settings.json) — UserPromptSubmit (inbox surface)
# ─────────────────────────────────────────────────────────────────────────
INBOX_HOOK_MARKER = "inbox-surface-hook"


def install_claude_user_prompt_submit(
    home: Path, brain_block: dict[str, Any]
) -> str | None:
    """Merge `brain_block`'s UserPromptSubmit entry into Claude settings.

    Preserves any existing SessionStart wiring (and any sibling
    UserPromptSubmit groups the user has installed). Replaces the
    UserPromptSubmit array as a whole — symmetric with how
    `install_claude` handles SessionStart.
    """
    if not (home / ".claude").is_dir():
        return None
    target = _claude_settings_path(home)
    existing = _load_json(target)
    if existing is None:
        return None
    hooks = existing.setdefault("hooks", {})
    hooks["UserPromptSubmit"] = brain_block["hooks"]["UserPromptSubmit"]
    _save_json(target, existing)
    return str(target)


def _is_brain_inbox_hook(entry: dict[str, Any]) -> bool:
    cmd = entry.get("command") or ""
    return INBOX_HOOK_MARKER in cmd or STOP_HOOK_MARKER in cmd


def remove_claude_user_prompt_submit(home: Path) -> str | None:
    target = _claude_settings_path(home)
    existing = _load_json(target)
    if not existing:
        return None
    bag = existing.get("hooks") or {}
    starters = bag.get("UserPromptSubmit") or []
    cleaned: list[dict[str, Any]] = []
    for group in starters:
        inner = [h for h in (group.get("hooks") or []) if not _is_brain_inbox_hook(h)]
        if inner:
            cleaned.append({**group, "hooks": inner})
    if cleaned == starters:
        return None
    if cleaned:
        bag["UserPromptSubmit"] = cleaned
    else:
        bag.pop("UserPromptSubmit", None)
    if not bag:
        existing.pop("hooks", None)
    _save_json(target, existing)
    return str(target)


# ─────────────────────────────────────────────────────────────────────────
# Claude Code (~/.claude/settings.json) — Stop (auto-continue on peer reply)
# ─────────────────────────────────────────────────────────────────────────
STOP_HOOK_MARKER = "stop-inbox-hook"


def install_claude_stop(home: Path, brain_block: dict[str, Any]) -> str | None:
    """Merge `brain_block`'s Stop entry into Claude settings.

    Replaces the Stop array as a whole — symmetric with how
    UserPromptSubmit and SessionStart are handled. The .bak file
    written before mutation preserves any prior Stop hooks the user
    had set up.
    """
    if not (home / ".claude").is_dir():
        return None
    target = _claude_settings_path(home)
    existing = _load_json(target)
    if existing is None:
        return None
    hooks = existing.setdefault("hooks", {})
    hooks["Stop"] = brain_block["hooks"]["Stop"]
    _save_json(target, existing)
    return str(target)


def remove_claude_stop(home: Path) -> str | None:
    target = _claude_settings_path(home)
    existing = _load_json(target)
    if not existing:
        return None
    bag = existing.get("hooks") or {}
    starters = bag.get("Stop") or []
    cleaned: list[dict[str, Any]] = []
    for group in starters:
        inner = [h for h in (group.get("hooks") or []) if not _is_brain_inbox_hook(h)]
        if inner:
            cleaned.append({**group, "hooks": inner})
    if cleaned == starters:
        return None
    if cleaned:
        bag["Stop"] = cleaned
    else:
        bag.pop("Stop", None)
    if not bag:
        existing.pop("hooks", None)
    _save_json(target, existing)
    return str(target)


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m brain.install_hooks {install|remove} [args...]",
              file=sys.stderr)
        return 2
    home = Path.home()
    action = argv[0]

    if action == "install":
        if len(argv) < 3:
            print("usage: install <settings_src> <hooks_src> "
                  "[<inbox_hook_src>] [<stop_hook_src>]",
                  file=sys.stderr)
            return 2
        settings_block = json.loads(Path(argv[1]).read_text())
        hooks_block = json.loads(Path(argv[2]).read_text())
        inbox_block = None
        stop_block = None
        # Two optional hook srcs in argv[3:]. --no-inbox-hook skips both
        # (Stop hook depends on the same plumbing). Positional otherwise.
        rest = argv[3:]
        if rest and rest[0] == "--no-inbox-hook":
            rest = []  # explicit skip — neither inbox nor stop
        if len(rest) >= 1:
            inbox_block = json.loads(Path(rest[0]).read_text())
        if len(rest) >= 2:
            stop_block = json.loads(Path(rest[1]).read_text())
        for label, fn, payload in (
            ("Claude SessionStart hook installed",  install_claude, settings_block),
            ("Cursor sessionStart hook installed",  install_cursor, hooks_block),
        ):
            res = fn(home, payload)
            if res:
                print(f"      ✓ {label} ({res})")
            else:
                kind = "Claude" if "Claude" in label else "Cursor"
                dot = ".claude" if kind == "Claude" else ".cursor"
                if not (home / dot).is_dir():
                    print(f"      - ~/{dot} not found — {kind} hook skipped.")
                else:
                    print(f"      ! {kind} config malformed — {kind} hook skipped (fix manually).")
        if inbox_block is not None:
            res = install_claude_user_prompt_submit(home, inbox_block)
            if res:
                print(f"      ✓ Claude UserPromptSubmit (inbox surface) installed ({res})")
            elif (home / ".claude").is_dir():
                print("      ! Claude UserPromptSubmit install failed — inbox-hook skipped.")
        if stop_block is not None:
            res = install_claude_stop(home, stop_block)
            if res:
                print(f"      ✓ Claude Stop (peer-reply auto-continue) installed ({res})")
            elif (home / ".claude").is_dir():
                print("      ! Claude Stop install failed — stop-hook skipped.")
        return 0

    if action == "remove":
        for label, fn in (
            ("Claude SessionStart hook removed", remove_claude),
            ("Claude UserPromptSubmit hook removed", remove_claude_user_prompt_submit),
            ("Claude Stop hook removed", remove_claude_stop),
            ("Cursor sessionStart hook removed", remove_cursor),
        ):
            res = fn(home)
            if res:
                print(f"      ✓ {label} ({res})")
        return 0

    print(f"unknown action: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
