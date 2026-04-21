"""Structured failure ledger — the substrate for brain's self-correction loop.

Today, failure modes (extraction bugs, recall false-positives, template
drift, subject-conflation hallucinations like the 2026-04-21 "đôi dép
tôi đâu" incident) are recorded only as prose paragraphs in CLAUDE.md /
USER_RULES.md templates. There is no schema, no queryable history, and
no mechanism to drive patches or verify fixes.

This module is the WRITE substrate. Consumers — extraction DLQ, recall
correction classifier, template drift detector — are separate waves;
they all call `record_failure(...)` and read via `list_failures(...)`.

Schema (one JSONL row per failure in `BRAIN_DIR/failures.jsonl`):

    {
      "id":              "<uuid4-hex, 12 chars>",
      "ts":              "<iso-8601 UTC>",
      "source":          "extraction" | "recall" | "template_drift" | "manual" | ...,
      "tool":            "brain_recall" | "note_extract" | ... | null,
      "query":           str | null,
      "result_digest":   str | null,     # short hash / prose of what brain returned
      "user_correction": str | null,
      "tags":            [str],
      "session_id":      str | null,
      "extra":           { ... },        # open-ended per-source metadata
      "resolution":      null | {
          "patch_ref":   str,            # commit sha / PR url / file path
          "outcome":     str,            # "fixed" | "wontfix" | "duplicate" | ...
          "verified_at": "<iso-8601 UTC>",
      }
    }

Writes are O_APPEND with a trailing newline — POSIX atomically serialises
`write()` calls up to PIPE_BUF (4096+ bytes), which is comfortably larger
than a JSONL row. Concurrent appends from multiple processes don't
interleave. A `flush() + fsync()` hardens the line against crash.

Resolutions require a full rewrite (JSONL isn't mutable-in-place). We
use tmp-file + `os.replace()` for atomicity.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import brain.config as config


def _ledger_path() -> Path:
    """Resolve the ledger path from the *current* BRAIN_DIR.

    We deliberately recompute this on every call rather than caching at
    import time, so tests that monkeypatch `brain.config.BRAIN_DIR` see
    the override. (A module-level constant bound at import would freeze
    the path to wherever the first importer ran.)
    """
    return config.BRAIN_DIR / "failures.jsonl"


# Public module-level alias for introspection / backward compat. Callers
# should prefer `_ledger_path()` or go through `record_failure` /
# `list_failures`, both of which honour runtime BRAIN_DIR changes.
LEDGER_PATH = _ledger_path()


def _now_iso() -> str:
    """UTC ISO-8601 timestamp with 'Z' suffix, microsecond precision.

    Microseconds (rather than seconds) matter for two reasons:
      1. Newest-first sorting by `ts` needs to break ties when multiple
         rows are appended in the same second — common in tests and in
         batch imports from extraction DLQs.
      2. It still sorts lexicographically (ISO-8601 fixed-width).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _short_id() -> str:
    """12-hex-char slice of a uuid4 — unique enough for a local ledger."""
    return uuid.uuid4().hex[:12]


def record_failure(
    *,
    source: str,
    tool: str | None = None,
    query: str | None = None,
    result_digest: str | None = None,
    user_correction: str | None = None,
    tags: Iterable[str] = (),
    session_id: str | None = None,
    extra: dict | None = None,
) -> str:
    """Append one failure row to the ledger. Returns the generated id.

    All fields except `source` are optional — real failures rarely have
    every field. Callers should provide whatever context they have; the
    downstream consumers (patch generator, drift detector) gracefully
    handle sparse rows.

    `source` is the only required field because without it we can't
    route a row to the right consumer.
    """
    if not source or not isinstance(source, str):
        raise ValueError("source is required and must be a non-empty string")

    row: dict[str, Any] = {
        "id": _short_id(),
        "ts": _now_iso(),
        "source": source,
        "tool": tool,
        "query": query,
        "result_digest": result_digest,
        "user_correction": user_correction,
        "tags": list(tags) if tags else [],
        "session_id": session_id,
        "extra": dict(extra) if extra else {},
        "resolution": None,
    }

    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # json.dumps with ensure_ascii=False keeps non-ASCII queries legible
    # (the 2026-04-21 incident was Vietnamese). We enforce single-line
    # output so the JSONL invariant holds.
    line = json.dumps(row, ensure_ascii=False) + "\n"

    #  O_APPEND guarantees atomic write up to PIPE_BUF (typically 4 KiB
    #  on Linux, 512 B on macOS pipes but much larger on regular files).
    #  JSONL rows are well under that, so concurrent writers from
    #  different processes won't interleave bytes. fsync() hardens it
    #  against an OS crash before the page cache flushes.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())

    return row["id"]


def _read_all_rows(path: Path) -> list[dict]:
    """Read every row from the ledger, skipping malformed lines silently.

    A bad line shouldn't poison the ledger — this is an append-only
    log with no schema migration story. We prefer "best-effort read"
    over "crash on corruption" so the tooling stays useful when a row
    is truncated by an fs hiccup.
    """
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def list_failures(
    *,
    source: str | None = None,
    tag: str | None = None,
    unresolved_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Read the ledger, filter, return newest-first, capped at `limit`.

    Filters compose (AND). An absent ledger file returns [] — not an
    error; the first `record_failure` creates it.
    """
    rows = _read_all_rows(_ledger_path())

    if source is not None:
        rows = [r for r in rows if r.get("source") == source]
    if tag is not None:
        rows = [r for r in rows if tag in (r.get("tags") or [])]
    if unresolved_only:
        rows = [r for r in rows if r.get("resolution") is None]

    # Newest-first by timestamp. `ts` is ISO-8601 so lexicographic
    # sort == chronological sort. Missing `ts` sinks to the bottom.
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)

    limit = max(0, int(limit))
    return rows[:limit]


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically via tmpfile + os.replace.

    We keep this private to failures.py rather than promoting it to a
    shared `brain.io` module — Wave 1a may introduce `brain.io.atomic_write_text`
    later, and this function can become a thin call-through at that
    point. For now: don't create shared infra consumers don't need yet.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    #  Write to a tempfile in the same directory so os.replace is a
    #  rename-in-place (cross-device renames are NOT atomic). tempfile
    #  names are unique so we won't collide with another in-flight
    #  rewrite on the same ledger.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup — don't leak tempfiles on rewrite failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def resolve_failure(
    failure_id: str,
    *,
    patch_ref: str,
    outcome: str,
    verified_at: str | None = None,
) -> bool:
    """Mark one failure as resolved. Returns True if the row was found.

    `patch_ref`: commit sha, PR url, or file path that fixes the failure.
    `outcome`:   short label ("fixed", "wontfix", "duplicate", ...).
    `verified_at`: caller-supplied timestamp, or auto-generated.

    The ledger is rewritten atomically: we read all rows, patch the
    target in-memory, and write a tmp-file + rename. A concurrent
    `record_failure` append between our read and rename would be lost;
    resolutions are low-volume human-initiated operations, so the race
    window is tolerable. A cross-process lock can be added later if
    the flow becomes automated.
    """
    if not failure_id:
        raise ValueError("failure_id is required")
    if not patch_ref or not outcome:
        raise ValueError("patch_ref and outcome are required")

    path = _ledger_path()
    rows = _read_all_rows(path)
    hit = None
    for row in rows:
        if row.get("id") == failure_id:
            hit = row
            break
    if hit is None:
        return False

    hit["resolution"] = {
        "patch_ref": patch_ref,
        "outcome": outcome,
        "verified_at": verified_at or _now_iso(),
    }

    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    _atomic_write_text(path, text)
    return True
