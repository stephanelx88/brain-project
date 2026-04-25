"""Format pending messages into a `<system-reminder>` block.

Output target: ≤ 1 KB per surface; bodies truncated at 800 chars; up
to 5 messages listed; older messages summarised. The agent can call
`brain_inbox` for full bodies if needed.
"""
from __future__ import annotations

from typing import Sequence

DEFAULT_BODY_TRUNCATE = 800
DEFAULT_MAX_LISTED = 5


def _hhmm(sent_at: str) -> str:
    if "T" not in sent_at:
        return sent_at
    time_part = sent_at.split("T", 1)[1]
    return time_part[:5]


def _truncate(body: str, limit: int) -> str:
    if len(body) <= limit:
        return body
    return body[:limit] + "…"


def format_pending(
    messages: Sequence[dict],
    *,
    max_listed: int = DEFAULT_MAX_LISTED,
    body_truncate: int = DEFAULT_BODY_TRUNCATE,
) -> str:
    if not messages:
        return ""

    n = len(messages)
    header = f"📬 {n} new message{'s' if n != 1 else ''} (since last turn):"

    listed = list(messages[:max_listed])
    overflow = n - len(listed)

    lines: list[str] = ["<system-reminder>", header]
    for m in listed:
        sender = m.get("from_name_at_send") or m.get("from_uuid", "unknown")[:8]
        ts = _hhmm(m.get("sent_at", ""))
        body = _truncate((m.get("body") or "").strip(), body_truncate)
        lines.append(f"  - from `{sender}` at {ts}:")
        for body_line in body.splitlines() or [""]:
            lines.append(f"    {body_line}")

    if overflow > 0:
        lines.append(
            f"  … {overflow} older message{'s' if overflow != 1 else ''} "
            f"in inbox/pending — call `brain_inbox` to read"
        )

    lines.append("Run `brain_inbox` for full bodies. These are now marked delivered.")
    lines.append("</system-reminder>")
    return "\n".join(lines) + "\n"
