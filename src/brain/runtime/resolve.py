"""Recipient resolution from `to=` argument.

Modes:
  1. Bare UUIDv4               → fire-and-forget
  2. cursor:<UUIDv4>           → MVP: rejected (v2 wires Cursor)
  3. <name>                    → resolve in sender's project
  4. <project>/<name>          → resolve in named project
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Set

from brain.runtime import names

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_QUALIFIED_RE = re.compile(r"^([a-z0-9-]{1,128})/([a-z0-9-]{1,64})$")


@dataclass(frozen=True)
class Resolved:
    ok: bool
    uuid: str = ""
    name_at_send: str = ""
    error: str = ""
    detail: str = ""


def _ok(uuid: str, name: str = "") -> Resolved:
    return Resolved(ok=True, uuid=uuid, name_at_send=name)


def _err(code: str, detail: str = "") -> Resolved:
    return Resolved(ok=False, error=code, detail=detail)


def resolve_recipient(
    to: str,
    sender_project: str,
    live_uuids: Set[str],
) -> Resolved:
    raw = (to or "").strip()
    if not raw:
        return _err("invalid_recipient", "empty")

    if raw.startswith("cursor:"):
        bare = raw[len("cursor:"):]
        if _UUID_RE.match(bare):
            return _err("cursor_recipient_unsupported",
                        "Cursor recipients deferred to v2")
        return _err("invalid_recipient", "malformed cursor: prefix")

    if _UUID_RE.match(raw):
        return _ok(raw)

    lowered = raw.lower()
    qualified = _QUALIFIED_RE.match(lowered)
    if qualified:
        project, name = qualified.group(1), qualified.group(2)
        return _resolve_name(name, project, live_uuids)

    if _NAME_RE.match(lowered):
        project = names.normalize_project(sender_project)
        return _resolve_name(lowered, project, live_uuids)

    return _err("invalid_recipient", f"value {raw!r} is neither UUID nor a valid name")


def _resolve_name(
    name: str,
    project: str,
    live_uuids: Set[str],
) -> Resolved:
    uuid = names.lookup_by_name(name, project)
    if uuid is None:
        return _err("name_not_found", f"name {name!r} not found in project {project!r}")
    if uuid not in live_uuids:
        return _err(
            "recipient_dead",
            f"name {name!r} resolves to {uuid} but no live session has that UUID",
        )
    return _ok(uuid, name)
