"""SystemReminder formatting for surfaced messages."""
from __future__ import annotations

from brain.runtime import surface


def _msg(body: str, sender: str = "planner", at: str = "2026-04-25T17:05:11.342Z") -> dict:
    return {
        "id": "01JBXY7K9RZNG7M2XKSZP4Q3VC",
        "from_uuid": "u1",
        "from_name_at_send": sender,
        "to_uuid": "u2",
        "to_name_at_send": "executor",
        "body": body,
        "sent_at": at,
    }


def test_empty_returns_empty_string():
    assert surface.format_pending([]) == ""


def test_single_message_format():
    out = surface.format_pending([_msg("GO — read spec.md")])
    assert "<system-reminder>" in out
    assert "</system-reminder>" in out
    assert "1 new message" in out
    assert "planner" in out
    assert "GO — read spec.md" in out
    assert "17:05" in out


def test_body_truncated_at_default_limit():
    long_body = "x" * 1500
    out = surface.format_pending([_msg(long_body)])
    assert "x" * 800 in out
    assert "…" in out
    assert "x" * 801 not in out


def test_multi_message_count_in_header():
    msgs = [_msg(f"body {i}") for i in range(3)]
    out = surface.format_pending(msgs)
    assert "3 new messages" in out


def test_more_than_5_summarises_overflow():
    msgs = [_msg(f"body {i}") for i in range(7)]
    out = surface.format_pending(msgs)
    assert "2 older message" in out
