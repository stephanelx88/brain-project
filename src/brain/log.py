"""Append-only brain log operations."""

from datetime import datetime, timezone

import brain.config as config


def append_log(operation: str, detail: str) -> None:
    """Append an entry to the brain log.

    Example: append_log("extract", "Session in project-x → updated Sarah Chen, NovaMind")
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    entry = f"## [{now}] {operation} | {detail}\n"
    with open(config.LOG_FILE, "a") as f:
        f.write(entry)
