"""Git operations for the brain repository.

Important: automated jobs (extract, dedupe, autoresearch) MUST commit
with an explicit `paths=` allowlist. The legacy `git add -A` behaviour
silently swept user-deleted root notes (e.g. `where-is-son.md`) into
unrelated automated commits, masking when/who deleted what — see the
2026-04-20 dedupe commit `2d3f195` that ate `where-is-son.md` along
with 11 entity merges. `commit_all()` is the opt-in escape hatch for
user-initiated cleanup; never call it from a scheduled job.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

import brain.config as config


# Auto-managed paths automated jobs are allowed to stage when no
# explicit `paths=` list is given. Everything outside this list is user
# territory (root vault notes like `son is working.md`, ad-hoc canvases,
# Obsidian sidecars) and must never be touched by a scheduled commit.
#
# Order matters only for readability — git treats these as pathspecs.
AUTO_MANAGED_PATHS: tuple[str, ...] = (
    "entities",
    "playground",
    "timeline",
    "identity/corrections.md",
    "log.md",
    "index.md",
    "research-log.md",
    "recall-ledger.jsonl",
)


def _normalise_paths(paths: Iterable[str | Path]) -> list[str]:
    """Return paths as repo-relative strings, dropping anything outside BRAIN_DIR."""
    out: list[str] = []
    root = config.BRAIN_DIR.resolve()
    for p in paths:
        pp = Path(p)
        if pp.is_absolute():
            try:
                rel = pp.resolve().relative_to(root)
            except ValueError:
                continue  # outside the brain — refuse silently
            out.append(str(rel))
        else:
            out.append(str(pp))
    return out


def commit(
    message: str,
    paths: Iterable[str | Path] | None = None,
) -> bool:
    """Stage `paths` (or AUTO_MANAGED_PATHS) and commit.

    Why this signature: the previous implementation did `git add -A`,
    which staged every uncommitted change in the vault — including
    user-deleted root notes — under whatever automated commit happened
    to run next. That made it impossible to tell what dedupe (or any
    scheduled job) actually changed vs. what the user changed.

    Now: callers pass the exact paths they touched. When `paths` is
    None we fall back to AUTO_MANAGED_PATHS, which still excludes
    root-level user notes. Use `commit_all()` for the rare case where
    you really want the old behaviour (user-initiated cleanup only).
    """
    try:
        if paths is None:
            stage = list(AUTO_MANAGED_PATHS)
        else:
            stage = _normalise_paths(paths)
            if not stage:
                return False  # nothing to stage; don't run `git add` empty
        # Add paths one at a time so a missing pathspec (e.g. `playground`
        # in a fresh vault, or a tracked-but-already-removed file) doesn't
        # abort the whole commit. `git add -- <nonexistent>` exits 128
        # with "pathspec did not match any files"; we treat that as
        # "nothing to stage here" and move on.
        for p in stage:
            subprocess.run(
                ["git", "add", "--", p],
                cwd=config.BRAIN_DIR,
                capture_output=True,
                check=False,
            )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=config.BRAIN_DIR,
            capture_output=True,
        )
        if result.returncode == 0:
            return False  # nothing to commit
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=config.BRAIN_DIR,
            capture_output=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def entity_history(path: str, limit: int = 10) -> list[dict] | dict:
    """Return git commit history for one entity/note path.

    `path` is relative to BRAIN_DIR. Returns a list of
    {sha, date, author, subject, insertions, deletions} or {error: ...}.
    """
    import subprocess
    limit = max(1, min(int(limit), 50))
    p = config.BRAIN_DIR / path
    try:
        p.resolve().relative_to(config.BRAIN_DIR.resolve())
    except (ValueError, OSError):
        return {"error": f"path outside vault: {path}"}
    try:
        out = subprocess.check_output(
            ["git", "log",
             f"-{limit}",
             "--pretty=format:%H\t%aI\t%an\t%s",
             "--shortstat",
             "--", path],
            cwd=str(config.BRAIN_DIR),
            stderr=subprocess.STDOUT,
            timeout=10,
        ).decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        return {"error": f"git failed: {e.output.decode(errors='replace')}"}
    except subprocess.TimeoutExpired:
        return {"error": "git timed out"}
    except FileNotFoundError:
        return {"error": "git not on PATH"}

    commits: list[dict] = []
    cur: dict | None = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line and len(line.split("\t", 3)) == 4:
            if cur:
                commits.append(cur)
            sha, date, author, subject = line.split("\t", 3)
            cur = {"sha": sha[:12], "date": date, "author": author,
                   "subject": subject, "insertions": 0, "deletions": 0}
        elif cur and ("insertion" in line or "deletion" in line):
            n = 0
            for tok in line.replace(",", "").split():
                if tok.isdigit():
                    n = int(tok)
                elif tok.startswith("insertion"):
                    cur["insertions"] = n
                elif tok.startswith("deletion"):
                    cur["deletions"] = n
    if cur:
        commits.append(cur)
    return commits


def commit_all(message: str) -> bool:
    """Escape hatch: stage everything (legacy `git add -A`) and commit.

    Only use this from user-initiated cleanup commands. Scheduled jobs
    must use `commit(paths=...)` so they don't accidentally bundle
    unrelated user changes into automated commits.
    """
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=config.BRAIN_DIR,
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=config.BRAIN_DIR,
            capture_output=True,
        )
        if result.returncode == 0:
            return False
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=config.BRAIN_DIR,
            capture_output=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
