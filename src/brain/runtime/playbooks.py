"""Playbook self-improvement loop.

LLMs that run a brain playbook learn things — usually because something
broke, or because the playbook's "Steps" section glossed over an edge
case the LLM had to figure out. Without a write path back into the
playbook, that knowledge dies with the session.

This module is the write path: append a dated lesson to the playbook's
"## Lessons learned" section, atomically, and bump audit fields in the
frontmatter so future sessions can tell at a glance how often the
playbook has been touched.

Brain stays read-only re: scripts. Only the .md doc is modified.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from brain import config
from brain.io import atomic_write_text

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_LESSONS_HEADING_RE = re.compile(r"^## Lessons learned\s*$", re.MULTILINE)


def find_playbook_path(slug: str) -> Optional[Path]:
    """Return the .md file under <vault>/playbooks/ matching `slug`.

    Matches in priority order:
      1. `playbooks/<slug>.md`
      2. `playbooks/<slug>/README.md` (multi-script playbook layout)
      3. any `playbooks/**/<slug>.md` recursive match (last-resort)
    """
    root = config.BRAIN_DIR / "playbooks"
    if not root.exists():
        return None
    direct = root / f"{slug}.md"
    if direct.is_file():
        return direct
    readme = root / slug / "README.md"
    if readme.is_file():
        return readme
    for path in root.rglob(f"{slug}.md"):
        if path.is_file():
            return path
    return None


def record_lesson(slug: str, lesson: str, source_uuid: Optional[str] = None) -> dict:
    """Append a lesson to a playbook. Returns a result dict.

    Result shape:
      success:  {ok: True, path, lessons_count, last_updated}
      missing:  {ok: False, error: "not_found", detail}
      empty:    {ok: False, error: "empty_lesson"}

    The lesson is wrapped with date + optional source-session
    attribution so future readers can audit where the line came from.
    Atomic write — concurrent record_lesson calls last-write-wins on
    the body; the file is never left half-rewritten.
    """
    lesson = (lesson or "").strip()
    if not lesson:
        return {"ok": False, "error": "empty_lesson"}
    target = find_playbook_path(slug)
    if not target:
        return {
            "ok": False,
            "error": "not_found",
            "detail": f"no playbooks/**/{slug}.md or playbooks/{slug}/README.md",
        }

    text = target.read_text()
    fm_text, body = _split_frontmatter(text)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_count = _read_int_field(fm_text, "lessons_count", 0) + 1

    fm_text = _set_field(fm_text, "last_updated", now_iso)
    fm_text = _set_field(fm_text, "lessons_count", str(new_count))
    body = _append_lesson(body, lesson, now_iso, source_uuid)

    if fm_text:
        new_text = f"---\n{fm_text}\n---\n{body}"
    else:
        # Playbook had no frontmatter — emit a minimal one so future
        # tools can rely on at least the audit fields existing.
        seed = f"last_updated: {now_iso}\nlessons_count: {new_count}\n"
        new_text = f"---\n{seed}---\n{body}"

    atomic_write_text(target, new_text)
    return {
        "ok": True,
        "path": str(target.relative_to(config.BRAIN_DIR)),
        "lessons_count": new_count,
        "last_updated": now_iso,
    }


# ─── private helpers ───────────────────────────────────────────


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_inner_text, body). Empty fm if missing."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def _read_int_field(fm: str, key: str, default: int) -> int:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    m = pattern.search(fm)
    if not m:
        return default
    try:
        return int(m.group(1).strip())
    except ValueError:
        return default


def _set_field(fm: str, key: str, value: str) -> str:
    """In-place update of `key: value` line, or append if missing.

    Preserves all surrounding lines, comments, and formatting. We do
    NOT round-trip through a YAML parser because (a) brain's existing
    parser is line-based and would lose nested structure, and (b)
    stable diffs matter — a YAML reformat on every lesson would create
    git noise that drowns the actual change.
    """
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    new_line = f"{key}: {value}"
    if pattern.search(fm):
        return pattern.sub(new_line, fm)
    if fm and not fm.endswith("\n"):
        fm = fm + "\n"
    return fm + new_line


def _append_lesson(
    body: str,
    lesson: str,
    iso_ts: str,
    source_uuid: Optional[str],
) -> str:
    """Insert a bullet under `## Lessons learned`. Section is created
    at end of body if it doesn't exist."""
    date = iso_ts[:10]
    src = f" (session {source_uuid[:8]})" if source_uuid else ""
    bullet = f"- {date}{src}: {lesson}"

    if _LESSONS_HEADING_RE.search(body):
        # Insert the bullet immediately after the existing heading,
        # so newest-first is the on-disk default.
        return _LESSONS_HEADING_RE.sub(
            lambda m: m.group(0) + "\n\n" + bullet, body, count=1
        )
    # No section yet — append at end.
    body = body.rstrip() + "\n\n## Lessons learned\n\n" + bullet + "\n"
    return body
