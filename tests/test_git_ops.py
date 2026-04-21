"""Tests for the scoped-commit fix in `brain.git_ops`.

The pre-fix `commit()` did `git add -A`, which is what swept the
user-deleted `where-is-son.md` into the unrelated 2026-04-20 dedupe
commit `2d3f195`. These tests pin the new contract:

  - `commit(paths=[...])` stages ONLY those paths.
  - `commit()` with no paths falls back to AUTO_MANAGED_PATHS, which
    must NOT include arbitrary root-level user notes.
  - `commit_all()` is the explicit escape hatch for user-initiated
    cleanup; scheduled jobs must never call it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def git_brain(tmp_path, monkeypatch):
    """Initialised git repo wired up as the brain root."""
    brain = tmp_path / "brain"
    brain.mkdir()
    (brain / "entities" / "people").mkdir(parents=True)
    (brain / "log.md").write_text("")

    subprocess.run(["git", "init", "-q"], cwd=brain, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=brain, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=brain, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=brain, check=True
    )
    subprocess.run(["git", "add", "-A"], cwd=brain, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=brain, check=True)

    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", brain)
    return brain


def test_commit_stages_only_explicit_paths(git_brain):
    """commit(paths=[A]) must commit A and ignore any other dirty path."""
    from brain.git_ops import commit

    target = git_brain / "entities" / "people" / "alice.md"
    target.write_text("alice-fact")
    bystander = git_brain / "where-is-son.md"
    bystander.write_text("Son is in Saigon")  # uncommitted user note

    ok = commit("brain: extract alice", paths=[target])
    assert ok is True

    last = _git(["log", "-1", "--name-only", "--pretty=format:"], git_brain)
    assert "alice.md" in last
    assert "where-is-son.md" not in last  # bystander stayed dirty

    status = _git(["status", "--porcelain"], git_brain)
    assert "where-is-son.md" in status  # still untracked, still safe


def test_commit_default_does_not_sweep_root_user_notes(git_brain):
    """commit() with no paths must fall back to AUTO_MANAGED_PATHS only.

    This is the literal regression from 2026-04-20: dedupe ran `commit()`,
    which did `git add -A`, which staged the user's just-deleted
    `where-is-son.md` under a misleading "merged 11 entities" message.
    """
    from brain.git_ops import commit

    (git_brain / "entities" / "people" / "bob.md").write_text("bob-fact")
    user_note = git_brain / "free-form-thought.md"
    user_note.write_text("a thought")

    ok = commit("brain: dedupe — merged 1 entity")
    assert ok is True

    last = _git(["log", "-1", "--name-only", "--pretty=format:"], git_brain)
    assert "entities/people/bob.md" in last
    assert "free-form-thought.md" not in last

    status = _git(["status", "--porcelain"], git_brain)
    assert "free-form-thought.md" in status


def test_commit_returns_false_when_nothing_staged(git_brain):
    from brain.git_ops import commit

    target = git_brain / "entities" / "people" / "ghost.md"  # never created
    assert commit("noop", paths=[target]) is False


def test_commit_all_is_explicit_escape_hatch(git_brain):
    """commit_all() preserves the legacy `git add -A` behaviour for
    user-initiated cleanup. We don't *want* schedulers calling this,
    but it must still work when someone does — mass migrations, etc.
    """
    from brain.git_ops import commit_all

    (git_brain / "entities" / "people" / "carol.md").write_text("carol")
    (git_brain / "scratch.md").write_text("user thought")

    assert commit_all("manual cleanup") is True
    last = _git(["log", "-1", "--name-only", "--pretty=format:"], git_brain)
    assert "scratch.md" in last  # the whole point of the escape hatch
