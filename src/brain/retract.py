"""User-driven fact retraction and correction.

Provides two operations:

  retract_fact(entity_type, entity_name, fact_query)
      Find a fact line matching `fact_query` (substring, case-insensitive)
      in the entity markdown, wrap it in ~~strikethrough~~, re-upsert DB.
      Returns the exact fact text that was retracted.

  correct_fact(entity_type, entity_name, wrong_fact, correct_fact, source)
      retract_fact + append the corrected fact as a new bullet.
      Returns {"retracted": str, "appended": str}.

Both operations are idempotent — calling retract on an already-superseded
fact is a no-op.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import brain.config as config
from brain import db
from brain.entities import entity_path, append_to_entity
from brain.slugify import slugify


_SOURCE_RE = re.compile(r"\s*\(source:[^)]*\)\s*$")
_STRIKE_RE = re.compile(r"^~~")


def _strip_source(text: str) -> str:
    return _SOURCE_RE.sub("", text).strip()


def _match_fact_line(body_text: str, fact_query: str) -> bool:
    """True if `body_text` (fact bullet without leading `- `) contains
    `fact_query` as a case-insensitive substring, ignoring the trailing
    `(source: …)` annotation."""
    clean = _strip_source(body_text).lower()
    return fact_query.lower().strip() in clean


def _retract_in_markdown(
    path: Path,
    fact_query: str,
    retracted_by: str,
) -> str:
    """Apply strikethrough to first matching fact in `path`. Returns fact text.
    Does NOT touch the DB — caller decides when to upsert."""
    text = path.read_text(errors="replace")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_lines: list[str] = []
    retracted: str | None = None

    for raw in text.split("\n"):
        stripped = raw.lstrip()
        indent = raw[: len(raw) - len(stripped)]
        if not stripped.startswith("- "):
            new_lines.append(raw)
            continue
        body_text = stripped[2:]
        if _STRIKE_RE.match(body_text.lstrip()):
            new_lines.append(raw)
            continue
        if retracted is None and _match_fact_line(body_text, fact_query):
            retracted = _strip_source(body_text)
            m = re.search(r"\(source:[^)]*\)", body_text)
            if m:
                head = body_text[: m.start()].rstrip()
                tail = body_text[m.start():]
                new_body = f"~~{head}~~ {tail} [retracted {today}: {retracted_by}]"
            else:
                new_body = f"~~{body_text.rstrip()}~~ [retracted {today}: {retracted_by}]"
            new_lines.append(f"{indent}- {new_body}")
        else:
            new_lines.append(raw)

    if retracted is None:
        raise ValueError(
            f"no matching fact for {fact_query!r} in entity at {path}"
        )
    path.write_text("\n".join(new_lines))
    return retracted


def retract_fact(
    entity_type: str,
    entity_name: str,
    fact_query: str,
    *,
    retracted_by: str = "user-correction",
) -> str:
    """Retract (supersede) a fact matching `fact_query` in the entity.

    Returns the exact fact text that was retracted.
    Raises ValueError if the entity does not exist or no matching fact is found.

    Also writes a global tombstone so re-extraction from a fresh session
    cannot resurrect the claim. Without the tombstone, retract was lossy:
    the strikethrough lived in the entity markdown only, and the next
    LLM extraction that happened to mention the retracted claim would
    append it back as a brand-new fact.
    """
    path = entity_path(entity_type.strip().lower(), entity_name.strip())
    if not path.exists():
        raise ValueError(f"entity not found: {entity_type}/{entity_name}")
    retracted = _retract_in_markdown(path, fact_query, retracted_by)
    db.upsert_entity_from_file(path)
    db.add_tombstone(
        retracted,
        entity_type=entity_type,
        entity_name=entity_name,
        reason=f"retract:{retracted_by}",
        created_by="retract",
    )
    return retracted


def correct_fact(
    entity_type: str,
    entity_name: str,
    wrong_fact: str,
    correct_fact_text: str,
    *,
    source: str = "user-correction",
) -> dict:
    """Retract `wrong_fact` and append `correct_fact_text` as a new bullet.

    Returns {"retracted": str, "appended": str}.
    """
    path = entity_path(entity_type.strip().lower(), entity_name.strip())
    if not path.exists():
        raise ValueError(f"entity not found: {entity_type}/{entity_name}")
    # Retract in markdown (no DB upsert yet)
    retracted = _retract_in_markdown(path, wrong_fact, source)
    # Append corrected fact directly to markdown (no DB upsert yet)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fact_line = f"\n- {correct_fact_text.strip()} (source: {source}, {now})"
    existing = path.read_text(errors="replace")
    path.write_text(existing.rstrip() + fact_line + "\n")
    # Single upsert after both edits are done.
    db.upsert_entity_from_file(path)
    # Tombstone the wrong phrasing so a later extraction can't re-create
    # it. Scoped to this entity so unrelated entities with a coincidentally
    # similar claim text are unaffected.
    db.add_tombstone(
        retracted,
        entity_type=entity_type,
        entity_name=entity_name,
        reason=f"correction:{source}",
        created_by="correct",
    )
    return {"retracted": retracted, "appended": correct_fact_text.strip()}
