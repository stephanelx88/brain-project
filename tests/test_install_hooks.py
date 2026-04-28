"""Tests for `brain.install_hooks` — the JSON merge that wires brain's
SessionStart hooks into Claude Code and Cursor.

Goals these tests lock in:
  - Re-running install is idempotent: re-installs replace only brain
    entries, never duplicate them.
  - Sibling content (other MCP servers, other hooks, unrelated keys)
    survives both install and remove.
  - Malformed target JSON degrades to "skip" — we never clobber an
    unparseable file, because that's almost always a hand-edit in
    progress.
  - Missing app dirs (~/.claude or ~/.cursor) are silent skips, not
    warnings — many machines only run one of the two.
  - `remove` only drops brain-owned entries; user-added hooks living
    in the same array are untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import install_hooks as ih


# Canonical brain blocks — match what install.sh renders from the
# templates. Kept inline so the tests don't depend on shell `sed` running.
CLAUDE_BLOCK = {
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {"type": "command",
                     "command": "PYTHONPATH=/p/src python3 -m brain.harvest_session",
                     "timeout": 10000},
                    {"type": "command",
                     "command": "PYTHONPATH=/p/src python3 -m brain.audit",
                     "timeout": 5000},
                ]
            }
        ]
    }
}

CURSOR_BLOCK = {
    "version": 1,
    "hooks": {
        "sessionStart": [
            {"command": "/v/bin/cursor-session-start.sh", "timeout": 10}
        ]
    }
}


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """An isolated $HOME so every helper mutation is sandboxed.

    `install_hooks` reads `Path.home()` only via the public functions'
    `home` argument — the CLI is the only caller that resolves it
    automatically — so passing tmp_path explicitly is enough; we still
    monkeypatch HOME for the CLI-path tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────
# Claude
# ─────────────────────────────────────────────────────────────────────────
class TestClaudeInstall:
    def test_skips_when_claude_not_installed(self, fake_home):
        # ~/.claude doesn't exist → no-op, returns None, no file created.
        assert ih.install_claude(fake_home, CLAUDE_BLOCK) is None
        assert not (fake_home / ".claude").exists()

    def test_creates_settings_when_missing(self, fake_home):
        (fake_home / ".claude").mkdir()
        result = ih.install_claude(fake_home, CLAUDE_BLOCK)
        assert result is not None
        cfg = json.loads((fake_home / ".claude" / "settings.json").read_text())
        assert cfg["hooks"]["SessionStart"] == CLAUDE_BLOCK["hooks"]["SessionStart"]

    def test_preserves_unrelated_existing_keys(self, fake_home):
        (fake_home / ".claude").mkdir()
        target = fake_home / ".claude" / "settings.json"
        target.write_text(json.dumps({
            "skipDangerousModePermissionPrompt": True,
            "hooks": {"PreToolUse": [{"hooks": [{"command": "echo other"}]}]},
            "customUserKey": ["keep", "me"],
        }))
        ih.install_claude(fake_home, CLAUDE_BLOCK)
        cfg = json.loads(target.read_text())
        assert cfg["skipDangerousModePermissionPrompt"] is True
        assert cfg["customUserKey"] == ["keep", "me"]
        # Sibling hook event preserved
        assert "PreToolUse" in cfg["hooks"]
        # Brain hook installed
        assert "SessionStart" in cfg["hooks"]

    def test_idempotent_reinstall_does_not_duplicate(self, fake_home):
        (fake_home / ".claude").mkdir()
        ih.install_claude(fake_home, CLAUDE_BLOCK)
        ih.install_claude(fake_home, CLAUDE_BLOCK)
        ih.install_claude(fake_home, CLAUDE_BLOCK)
        cfg = json.loads((fake_home / ".claude" / "settings.json").read_text())
        # Still exactly one SessionStart group, with two inner hooks.
        assert len(cfg["hooks"]["SessionStart"]) == 1
        assert len(cfg["hooks"]["SessionStart"][0]["hooks"]) == 2

    def test_backs_up_before_overwriting(self, fake_home):
        (fake_home / ".claude").mkdir()
        target = fake_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"command": "echo USER_OWNED"}
        ]}]}}))
        ih.install_claude(fake_home, CLAUDE_BLOCK)
        backups = list((fake_home / ".claude").glob("settings.json.bak.*"))
        assert backups, "expected a timestamped backup"
        # User's prior config is preserved in the backup.
        assert "USER_OWNED" in backups[0].read_text()

    def test_malformed_existing_returns_none(self, fake_home):
        (fake_home / ".claude").mkdir()
        target = fake_home / ".claude" / "settings.json"
        target.write_text("{ this is { not json")
        assert ih.install_claude(fake_home, CLAUDE_BLOCK) is None
        # We must NOT have overwritten the malformed file — the user's
        # in-progress edit is more valuable than auto-fixing.
        assert target.read_text() == "{ this is { not json"


class TestClaudeRemove:
    def test_noop_when_no_settings(self, fake_home):
        assert ih.remove_claude(fake_home) is None

    def test_drops_only_brain_entries(self, fake_home):
        (fake_home / ".claude").mkdir()
        target = fake_home / ".claude" / "settings.json"
        target.write_text(json.dumps({
            "hooks": {
                "SessionStart": [
                    {"hooks": [
                        {"command": "python -m brain.harvest_session"},
                        {"command": "python -m brain.audit"},
                        {"command": "echo USER_OWNED_HOOK"},
                    ]}
                ]
            }
        }))
        ih.remove_claude(fake_home)
        cfg = json.loads(target.read_text())
        # User's hook survives; both brain hooks gone.
        commands = [h["command"] for h in cfg["hooks"]["SessionStart"][0]["hooks"]]
        assert commands == ["echo USER_OWNED_HOOK"]

    def test_prunes_empty_groups_and_event(self, fake_home):
        (fake_home / ".claude").mkdir()
        target = fake_home / ".claude" / "settings.json"
        target.write_text(json.dumps({
            "hooks": {"SessionStart": [{"hooks": [
                {"command": "python -m brain.harvest_session"},
                {"command": "python -m brain.audit"},
            ]}]}
        }))
        ih.remove_claude(fake_home)
        cfg = json.loads(target.read_text())
        # SessionStart was 100% brain — should be gone, not left as empty list.
        assert "SessionStart" not in cfg.get("hooks", {})

    def test_remove_is_idempotent(self, fake_home):
        (fake_home / ".claude").mkdir()
        ih.install_claude(fake_home, CLAUDE_BLOCK)
        first = ih.remove_claude(fake_home)
        second = ih.remove_claude(fake_home)
        # First removal mutates → returns path; second has nothing to do.
        assert first is not None
        assert second is None


# ─────────────────────────────────────────────────────────────────────────
# Cursor
# ─────────────────────────────────────────────────────────────────────────
class TestCursorInstall:
    def test_skips_when_cursor_not_installed(self, fake_home):
        assert ih.install_cursor(fake_home, CURSOR_BLOCK) is None

    def test_creates_hooks_when_missing(self, fake_home):
        (fake_home / ".cursor").mkdir()
        ih.install_cursor(fake_home, CURSOR_BLOCK)
        cfg = json.loads((fake_home / ".cursor" / "hooks.json").read_text())
        assert cfg["version"] == 1
        assert cfg["hooks"]["sessionStart"][0]["command"].endswith(
            "cursor-session-start.sh"
        )

    def test_preserves_other_hook_events(self, fake_home):
        (fake_home / ".cursor").mkdir()
        target = fake_home / ".cursor" / "hooks.json"
        target.write_text(json.dumps({
            "version": 1,
            "hooks": {
                "beforeShellExecution": [
                    {"command": "/u/safety.sh", "matcher": "rm -rf"}
                ]
            }
        }))
        ih.install_cursor(fake_home, CURSOR_BLOCK)
        cfg = json.loads(target.read_text())
        assert "beforeShellExecution" in cfg["hooks"]
        assert "sessionStart" in cfg["hooks"]

    def test_idempotent_reinstall(self, fake_home):
        (fake_home / ".cursor").mkdir()
        for _ in range(3):
            ih.install_cursor(fake_home, CURSOR_BLOCK)
        cfg = json.loads((fake_home / ".cursor" / "hooks.json").read_text())
        assert len(cfg["hooks"]["sessionStart"]) == 1


class TestCursorRemove:
    def test_drops_only_brain_entry(self, fake_home):
        (fake_home / ".cursor").mkdir()
        target = fake_home / ".cursor" / "hooks.json"
        target.write_text(json.dumps({
            "version": 1,
            "hooks": {
                "sessionStart": [
                    {"command": "/v/bin/cursor-session-start.sh"},
                    {"command": "/u/my-other-hook.sh"},
                ]
            }
        }))
        ih.remove_cursor(fake_home)
        cfg = json.loads(target.read_text())
        cmds = [h["command"] for h in cfg["hooks"]["sessionStart"]]
        assert cmds == ["/u/my-other-hook.sh"]

    def test_preserves_unrelated_event_hooks(self, fake_home):
        (fake_home / ".cursor").mkdir()
        target = fake_home / ".cursor" / "hooks.json"
        target.write_text(json.dumps({
            "version": 1,
            "hooks": {
                "sessionStart": [{"command": "/v/bin/cursor-session-start.sh"}],
                "afterFileEdit": [{"command": "/u/format.sh"}],
            }
        }))
        ih.remove_cursor(fake_home)
        cfg = json.loads(target.read_text())
        assert "sessionStart" not in cfg["hooks"]
        # The user's afterFileEdit hook survives untouched.
        assert cfg["hooks"]["afterFileEdit"] == [{"command": "/u/format.sh"}]


# ─────────────────────────────────────────────────────────────────────────
# CLI smoke — make sure install.sh / uninstall.sh's invocations work.
# ─────────────────────────────────────────────────────────────────────────
class TestCli:
    def test_install_then_remove_round_trip(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Pretend both apps exist on this machine.
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".cursor").mkdir()

        settings_src = tmp_path / "settings.json"
        hooks_src = tmp_path / "hooks.json"
        settings_src.write_text(json.dumps(CLAUDE_BLOCK))
        hooks_src.write_text(json.dumps(CURSOR_BLOCK))

        rc = ih.main(["install", str(settings_src), str(hooks_src)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Claude SessionStart hook installed" in out
        assert "Cursor sessionStart hook installed" in out
        assert (tmp_path / ".claude" / "settings.json").exists()
        assert (tmp_path / ".cursor" / "hooks.json").exists()

        rc = ih.main(["remove"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Claude SessionStart hook removed" in out
        assert "Cursor sessionStart hook removed" in out

    def test_remove_when_nothing_installed_is_silent(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        rc = ih.main(["remove"])
        assert rc == 0
        # No "removed" lines because there was nothing to remove.
        assert "removed" not in capsys.readouterr().out

    def test_install_skips_silently_when_app_missing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Only Claude installed, no Cursor.
        (tmp_path / ".claude").mkdir()
        settings_src = tmp_path / "settings.json"
        hooks_src = tmp_path / "hooks.json"
        settings_src.write_text(json.dumps(CLAUDE_BLOCK))
        hooks_src.write_text(json.dumps(CURSOR_BLOCK))
        rc = ih.main(["install", str(settings_src), str(hooks_src)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Claude SessionStart hook installed" in out
        assert "~/.cursor not found" in out

    def test_unknown_action_returns_2(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert ih.main(["frobnicate"]) == 2


# ─── UserPromptSubmit hook (inbox surface) ──────────────────────────


def test_install_user_prompt_submit_writes_entry(tmp_path):
    from brain import install_hooks
    import json
    home = tmp_path
    (home / ".claude").mkdir()
    block = {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{
                    "type": "command",
                    "command": "/abs/path/inbox-surface-hook.sh",
                }]
            }]
        }
    }
    res = install_hooks.install_claude_user_prompt_submit(home, block)
    written = json.loads((home / ".claude" / "settings.json").read_text())
    assert "UserPromptSubmit" in written["hooks"]
    assert any(
        "inbox-surface-hook" in h["command"]
        for grp in written["hooks"]["UserPromptSubmit"]
        for h in grp["hooks"]
    )
    assert res == str(home / ".claude" / "settings.json")


def test_install_user_prompt_submit_preserves_session_start(tmp_path):
    from brain import install_hooks
    import json
    home = tmp_path
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "x brain.audit"}]}]
        }
    }))
    block = {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{"type": "command", "command": "/abs/inbox-surface-hook.sh"}]
            }]
        }
    }
    install_hooks.install_claude_user_prompt_submit(home, block)
    written = json.loads(settings.read_text())
    assert "SessionStart" in written["hooks"]
    assert "UserPromptSubmit" in written["hooks"]


def test_remove_user_prompt_submit_drops_only_brain_entry(tmp_path):
    from brain import install_hooks
    import json
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [
                    {"type": "command", "command": "/abs/inbox-surface-hook.sh"},
                    {"type": "command", "command": "/some/other/user-hook.sh"},
                ]
            }]
        }
    }))
    install_hooks.remove_claude_user_prompt_submit(home)
    written = json.loads((home / ".claude" / "settings.json").read_text())
    surviving = [
        h for grp in written["hooks"].get("UserPromptSubmit", [])
        for h in grp["hooks"]
    ]
    assert len(surviving) == 1
    assert "inbox-surface-hook" not in surviving[0]["command"]


# ─── Stop hook (peer-reply auto-continue) ────────────────────────────


def test_install_stop_writes_entry(tmp_path):
    from brain import install_hooks
    import json
    home = tmp_path
    (home / ".claude").mkdir()
    block = {
        "hooks": {
            "Stop": [{
                "hooks": [{
                    "type": "command",
                    "command": "/abs/path/stop-inbox-hook.sh",
                }]
            }]
        }
    }
    res = install_hooks.install_claude_stop(home, block)
    written = json.loads((home / ".claude" / "settings.json").read_text())
    assert "Stop" in written["hooks"]
    assert any(
        "stop-inbox-hook" in h["command"]
        for grp in written["hooks"]["Stop"]
        for h in grp["hooks"]
    )
    assert res == str(home / ".claude" / "settings.json")


def test_install_stop_preserves_user_prompt_submit_and_session_start(tmp_path):
    from brain import install_hooks
    import json
    home = tmp_path
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart":      [{"hooks": [{"type": "command", "command": "x brain.audit"}]}],
            "UserPromptSubmit":  [{"hooks": [{"type": "command", "command": "/abs/inbox-surface-hook.sh"}]}],
        }
    }))
    block = {
        "hooks": {
            "Stop": [{
                "hooks": [{"type": "command", "command": "/abs/stop-inbox-hook.sh"}]
            }]
        }
    }
    install_hooks.install_claude_stop(home, block)
    written = json.loads(settings.read_text())
    assert "SessionStart" in written["hooks"]
    assert "UserPromptSubmit" in written["hooks"]
    assert "Stop" in written["hooks"]


def test_remove_stop_drops_only_brain_entry(tmp_path):
    from brain import install_hooks
    import json
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {
            "Stop": [{
                "hooks": [
                    {"type": "command", "command": "/abs/stop-inbox-hook.sh"},
                    {"type": "command", "command": "/some/other/stop-hook.sh"},
                ]
            }]
        }
    }))
    install_hooks.remove_claude_stop(home)
    written = json.loads((home / ".claude" / "settings.json").read_text())
    surviving = [
        h for grp in written["hooks"].get("Stop", [])
        for h in grp["hooks"]
    ]
    assert len(surviving) == 1
    assert "stop-inbox-hook" not in surviving[0]["command"]
