"""Per-session name registry.

One JSON file per session under ~/.brain-runtime/names/<uuid>.json.
Names are scoped per-project; the same name can exist in two
different projects without collision.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from brain.io import atomic_write_text
from brain.runtime import paths

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_RESERVED = frozenset({"peer", "self", "all", "me"})
_PROJECT_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

_DIACRITIC_MAP = str.maketrans({
    "à": "a", "á": "a", "ạ": "a", "ả": "a", "ã": "a",
    "â": "a", "ầ": "a", "ấ": "a", "ậ": "a", "ẩ": "a", "ẫ": "a",
    "ă": "a", "ằ": "a", "ắ": "a", "ặ": "a", "ẳ": "a", "ẵ": "a",
    "è": "e", "é": "e", "ẹ": "e", "ẻ": "e", "ẽ": "e",
    "ê": "e", "ề": "e", "ế": "e", "ệ": "e", "ể": "e", "ễ": "e",
    "ì": "i", "í": "i", "ị": "i", "ỉ": "i", "ĩ": "i",
    "ò": "o", "ó": "o", "ọ": "o", "ỏ": "o", "õ": "o",
    "ô": "o", "ồ": "o", "ố": "o", "ộ": "o", "ổ": "o", "ỗ": "o",
    "ơ": "o", "ờ": "o", "ớ": "o", "ợ": "o", "ở": "o", "ỡ": "o",
    "ù": "u", "ú": "u", "ụ": "u", "ủ": "u", "ũ": "u",
    "ư": "u", "ừ": "u", "ứ": "u", "ự": "u", "ử": "u", "ữ": "u",
    "ỳ": "y", "ý": "y", "ỵ": "y", "ỷ": "y", "ỹ": "y",
    "đ": "d",
})


def normalize_project(project: str) -> str:
    """Lowercase + replace non-alphanumerics with '-', collapse runs, strip ends.

    Vietnamese diacritics are folded to ASCII first (đôi → doi) so
    cwd-derived project labels in non-ASCII paths still produce
    sensible names.
    """
    s = (project or "").lower().translate(_DIACRITIC_MAP)
    s = _PROJECT_NORMALIZE_RE.sub("-", s).strip("-")
    return s or "unknown"


def default_name(project: str, short_id: str) -> str:
    """Build the default-name format `<normalized-project>-<short_id>`."""
    return f"{normalize_project(project)}-{short_id}"


def validate_user_name(name: str) -> Optional[str]:
    """Return error code (str) or None if valid.

    Error codes: "lowercase", "length", "chars", "reserved".
    """
    if not name:
        return "length"
    if name in _RESERVED:
        return "reserved"
    if name != name.lower():
        return "lowercase"
    if len(name) > 64:
        return "length"
    if not _NAME_RE.match(name):
        return "chars"
    return None


def _write(uuid: str, payload: dict) -> None:
    atomic_write_text(paths.name_file(uuid), json.dumps(payload, indent=2) + "\n")


def register(
    uuid: str,
    name: str,
    project: str,
    cwd: str,
    pid: int | None,
) -> dict:
    """Write a name registry entry. Overwrites any prior entry for `uuid`."""
    entry = {
        "uuid": uuid,
        "name": name,
        "project": normalize_project(project),
        "cwd": cwd,
        "pid": pid,
        "set_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    _write(uuid, entry)
    return entry


def get(uuid: str) -> Optional[dict]:
    p = paths.name_file(uuid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def all_entries() -> list[dict]:
    d = paths.names_dir()
    if not d.exists():
        return []
    out: list[dict] = []
    for p in d.iterdir():
        if not p.suffix == ".json":
            continue
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def lookup_by_name(name: str, project: str) -> Optional[str]:
    """Return the UUID matching (name, project) or None."""
    project = normalize_project(project)
    name = name.lower()
    for entry in all_entries():
        if entry.get("name") == name and entry.get("project") == project:
            return entry["uuid"]
    return None


def set_name(uuid: str, new_name: str) -> Optional[str]:
    """Rename the entry for `uuid`. Returns error code or None.

    Error codes: validation codes from validate_user_name + "name_taken",
    "no_entry".
    """
    err = validate_user_name(new_name)
    if err:
        return err
    current = get(uuid)
    if not current:
        return "no_entry"
    project = current["project"]
    other_uuid = lookup_by_name(new_name, project)
    if other_uuid and other_uuid != uuid:
        return "name_taken"
    current["name"] = new_name
    current["set_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    _write(uuid, current)
    return None


def delete(uuid: str) -> None:
    p = paths.name_file(uuid)
    if p.exists():
        p.unlink()
