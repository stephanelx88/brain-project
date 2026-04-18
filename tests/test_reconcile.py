"""Tests for brain reconciliation."""

from datetime import datetime, timezone, timedelta
from brain.reconcile import get_recent_log
from brain import config


def test_get_recent_log_filters_by_hours(tmp_brain, monkeypatch):
    """Only entries within the time window should be returned."""
    monkeypatch.setattr(config, "LOG_FILE", tmp_brain / "log.md")
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=5)
    recent = now - timedelta(minutes=30)

    log_content = (
        f"## [{old.strftime('%Y-%m-%d %H:%M')}] extract | old-session → 2 entities\n"
        f"## [{recent.strftime('%Y-%m-%d %H:%M')}] extract | new-session → 3 entities\n"
    )
    (tmp_brain / "log.md").write_text(log_content)

    result = get_recent_log(hours=2)
    assert "new-session" in result
    assert "old-session" not in result


def test_get_recent_log_returns_message_when_empty(tmp_brain, monkeypatch):
    monkeypatch.setattr(config, "LOG_FILE", tmp_brain / "log.md")
    (tmp_brain / "log.md").write_text("")
    result = get_recent_log(hours=2)
    assert result == "No log entries."
