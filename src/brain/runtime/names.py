"""Per-session name registry.

One JSON file per session under ~/.brain-runtime/names/<uuid>.json.
Names are scoped per-project; the same name can exist in two
different projects without collision.
"""
from __future__ import annotations

import json
import os
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


def _try_reserve(uuid: str, project: str, name: str) -> bool:
    """Atomically claim (project, name) for `uuid`.

    Uses O_CREAT|O_EXCL to make the reservation file the linearization
    point for concurrent set_name/register calls. Returns True if this
    call now holds the reservation (either because it just created it,
    or because it already owned it). Returns False if a different
    session already holds the reservation.
    """
    reserve_path = paths.name_reservation_file(project, name)
    reserve_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            str(reserve_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
        try:
            os.write(fd, uuid.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        # Reservation already held — check whether it's ours.
        try:
            holder = reserve_path.read_text().strip()
        except OSError:
            return False
        return holder == uuid


def _release_reservations_for(uuid: str) -> None:
    """Remove every reservation file whose holder is `uuid`.

    Called from delete() so a session releasing its name doesn't
    permanently lock that (project, name) pair. Safe under races —
    if a file disappears between scan and unlink, that's fine.
    """
    rdir = paths.name_reservations_dir()
    if not rdir.exists():
        return
    for p in rdir.iterdir():
        if p.suffix != ".lock":
            continue
        try:
            holder = p.read_text().strip()
        except OSError:
            continue
        if holder != uuid:
            continue
        try:
            p.unlink()
        except OSError:
            pass


def _release_reservation(project: str, name: str, uuid: str) -> None:
    """Release a single (project, name) reservation if `uuid` holds it."""
    reserve_path = paths.name_reservation_file(project, name)
    try:
        holder = reserve_path.read_text().strip()
    except OSError:
        return
    if holder != uuid:
        return
    try:
        reserve_path.unlink()
    except OSError:
        pass


def register(
    uuid: str,
    name: str,
    project: str,
    cwd: str,
    pid: int | None,
) -> dict:
    """Write a name registry entry. Overwrites any prior entry for `uuid`.

    Also claims the (project, name) atomic reservation so this name is
    treated as held by `uuid` for subsequent set_name calls. If the
    reservation is currently held by a different uuid, the old holder's
    reservation is left untouched and the existing JSON entry on disk
    is what `lookup_by_name` continues to see — register() is the
    "I'm taking this slot for myself" path used by tests/bootstrap and
    must not silently fail; for race-correct rename, use set_name().
    """
    norm_project = normalize_project(project)
    entry = {
        "uuid": uuid,
        "name": name,
        "project": norm_project,
        "cwd": cwd,
        "pid": pid,
        "set_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    # Drop any reservations this uuid previously held under a different
    # name in any project, so the registry stays in sync with the JSON
    # entry on disk (one live name per uuid).
    _release_reservations_for(uuid)
    _try_reserve(uuid, norm_project, name)
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
    """Return the UUID matching (name, project) or None.

    If the registry contains multiple entries with the same (project,
    name) — possible if `register()` was called forcefully past the
    reservation guard — the first match in `iterdir()` order wins.
    Callers that need to detect that ambiguity should use
    `lookup_uuids_by_name` instead.
    """
    matches = lookup_uuids_by_name(name, project)
    return matches[0] if matches else None


def lookup_uuids_by_name(name: str, project: str) -> list[str]:
    """Return all UUIDs whose registry entry matches (name, project).

    Normally length 0 or 1 — `set_name()` is race-safe via the
    reservation lock. Length > 1 indicates the registry got into an
    ambiguous state (forceful `register()` call after a crash, or two
    bootstrap-path registrations colliding); `resolve._resolve_name`
    surfaces this to the user as `ambiguous_name` rather than picking
    a winner non-deterministically.
    """
    project = normalize_project(project)
    name = name.lower()
    return [
        entry["uuid"]
        for entry in all_entries()
        if entry.get("name") == name and entry.get("project") == project
    ]


def set_name(
    uuid: str,
    new_name: str,
    live_uuids: Optional[set[str]] = None,
) -> Optional[str]:
    """Rename the entry for `uuid`. Returns error code or None.

    Error codes: validation codes from validate_user_name + "name_taken",
    "no_entry".

    Race semantics: the (project, new_name) reservation file under
    `paths.name_reservations_dir()` is created with O_CREAT|O_EXCL so it
    acts as the linearization point. Concurrent calls racing for the
    same name resolve to exactly one winner; losers see "name_taken".

    Dead-holder takeover: when `live_uuids` is supplied and the slot is
    held by a UUID NOT in that set, the dead holder's reservation and
    name entry are silently reclaimed instead of returning "name_taken".
    Without 30-day TTL pressure, sessions that crash or are killed would
    otherwise lock their name forever — the live caller's claim wins.
    `live_uuids=None` preserves the strict pre-2026-04-28 behavior.
    """
    err = validate_user_name(new_name)
    if err:
        return err
    current = get(uuid)
    if not current:
        return "no_entry"
    project = current["project"]
    old_name = current.get("name")
    # Atomic claim — this is the linearization point. Cheap collision
    # check first so callers don't churn reservation files for an
    # obviously taken name, but the O_EXCL claim below is what makes
    # the operation race-safe.
    other_uuid = lookup_by_name(new_name, project)
    if other_uuid and other_uuid != uuid:
        if live_uuids is not None and other_uuid not in live_uuids:
            # Holder is dead — free its slot so the live caller wins.
            _release_reservation(project, new_name, other_uuid)
            other_entry = get(other_uuid)
            if other_entry and other_entry.get("name") == new_name:
                other_entry["name"] = None
                _write(other_uuid, other_entry)
        else:
            return "name_taken"
    if not _try_reserve(uuid, project, new_name):
        return "name_taken"
    # Reservation now held by `uuid`. Release the old name's
    # reservation (if any and different) before persisting the rename
    # so future renames-back-to-old-name aren't permanently blocked.
    if old_name and old_name != new_name:
        _release_reservation(project, old_name, uuid)
    current["name"] = new_name
    current["set_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    _write(uuid, current)
    return None


def delete(uuid: str) -> None:
    p = paths.name_file(uuid)
    if p.exists():
        p.unlink()
    # Free any (project, name) reservations this uuid still owned so
    # the slot is reclaimable by other sessions.
    _release_reservations_for(uuid)
