"""Fact supersession: collapse contradictions within an entity.

When an entity accumulates multiple facts about the same attribute
(e.g. Thuha's location extracted from three different sessions plus
the user's own note), the newest + highest-priority fact wins and the
losers are marked as superseded. Losers stay visible in the entity
markdown as `~~strikethrough~~` so the user can see history, but the
MCP read path (FTS index, semantic vectors) only surfaces winners.

Priority rule (first wins):
  1. source starts with `note:` (user-authored)
  2. newer `fact_date` (ISO yyyy-mm-dd)
  3. higher rowid (newest insert)

Predicate buckets use cheap regex heuristics so we avoid a hot-path
LLM call. Two facts in the same bucket are a contradiction candidate;
facts with no bucket are left alone (e.g. "Previously traveled through
Switzerland" never conflicts with "Currently in Cần Thơ").

Public API:
  recompute_for_entity(path)   — run supersession on one entity file
  recompute_all()              — walk every entity; used by db.rebuild
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import brain.config as config
from brain import db


# Predicate groups — additive, start conservative. Add more when real
# contradictions show up in the audit surface.
_PREDICATE_GROUPS: list[tuple[str, re.Pattern]] = [
    (
        "location",
        re.compile(
            r"\b(currently\s+(in|at)|located\s+in|is\s+(in|at|located)|"
            r"lives\s+in|based\s+in|đang\s+ở|hiện\s+ở|ở\s+tại)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "employer",
        re.compile(
            r"\b(works\s+at|employed\s+by|is\s+employed\s+at|"
            r"làm\s+(việc\s+)?(ở|tại|cho))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role",
        re.compile(
            r"\b(role\s+is|title\s+is|position\s+is|is\s+a\s+|serves\s+as|"
            r"chức\s+vụ|vị\s+trí)\b",
            re.IGNORECASE,
        ),
    ),
]


def classify_predicate(fact_text: str) -> str | None:
    """Return the predicate-bucket key for a fact, or None if uncategorised."""
    for key, pat in _PREDICATE_GROUPS:
        if pat.search(fact_text):
            return key
    return None


def _is_note_source(source: str | None) -> bool:
    return bool(source) and source.startswith("note:")


def _parse_date(fact_date: str | None) -> datetime:
    if not fact_date:
        return datetime.min.replace(tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(fact_date, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _priority_key(fact_row: tuple) -> tuple:
    """Sort key: higher tuple == higher priority. Use `max(bucket, key=…)`.

    fact_row: (id, text, source, fact_date, status)
    """
    _id, _text, source, fact_date, _status = fact_row
    return (
        1 if _is_note_source(source) else 0,
        _parse_date(fact_date),
        _id,
    )


def _load_entity_facts(rel_path: str) -> list[tuple]:
    """Return all facts for an entity as (id, text, source, fact_date, status)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM entities WHERE path = ?", (rel_path,)
        ).fetchone()
        if not row:
            return []
        entity_id = row[0]
        return list(conn.execute(
            "SELECT id, text, source, fact_date, status "
            "FROM facts WHERE entity_id = ?",
            (entity_id,),
        ))


def _mark_superseded_in_markdown(
    entity_path: Path,
    loser_hashes: set[str],
    winner_source: str | None,
    today: str,
) -> int:
    """Wrap losing fact lines in `~~…~~` with an audit tag. Idempotent."""
    try:
        text = entity_path.read_text(errors="replace")
    except OSError:
        return 0

    winner_tag = winner_source or "higher-priority fact"
    new_lines: list[str] = []
    changed = 0
    for raw in text.split("\n"):
        line = raw
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if not stripped.startswith("- "):
            new_lines.append(line)
            continue
        body_text = stripped[2:]
        if body_text.lstrip().startswith("~~"):
            new_lines.append(line)
            continue
        h = db.canonical_fact_hash(body_text)
        if h not in loser_hashes:
            new_lines.append(line)
            continue
        m = re.search(r"\(source:[^)]*\)", body_text)
        if m:
            head = body_text[: m.start()].rstrip()
            tail = body_text[m.start():]
            new_body = (
                f"~~{head}~~ {tail} "
                f"[superseded {today}: winner={winner_tag}]"
            )
        else:
            new_body = (
                f"~~{body_text.rstrip()}~~ "
                f"[superseded {today}: winner={winner_tag}]"
            )
        new_lines.append(f"{indent}- {new_body}")
        changed += 1

    if changed:
        entity_path.write_text("\n".join(new_lines))
    return changed


def recompute_for_entity(path: Path | str) -> dict:
    """Run supersession on one entity file.

    Reads all current facts for the entity, groups by predicate bucket,
    picks a winner per bucket, and rewrites the markdown so losers are
    `~~strikethroughed~~`. Re-upserts the file so the DB `status`
    column and the FTS index reflect the new state.

    Returns {"facts_superseded": int, "buckets_resolved": int}.
    """
    path = Path(path)
    if not path.exists():
        return {"facts_superseded": 0, "buckets_resolved": 0}
    try:
        rel_path = str(path.relative_to(config.BRAIN_DIR))
    except ValueError:
        return {"facts_superseded": 0, "buckets_resolved": 0}

    rows = _load_entity_facts(rel_path)
    if len(rows) < 2:
        return {"facts_superseded": 0, "buckets_resolved": 0}

    # Only current (non-superseded) rows compete. Already-superseded
    # facts stay superseded — we don't resurrect them.
    current = [r for r in rows if (r[4] or "current") != "superseded"]
    buckets: dict[str, list[tuple]] = {}
    for r in current:
        key = classify_predicate(r[1])
        if key is None:
            continue
        buckets.setdefault(key, []).append(r)

    loser_hashes_by_winner: dict[str | None, set[str]] = {}
    buckets_resolved = 0
    for key, bucket in buckets.items():
        if len(bucket) < 2:
            continue
        winner = max(bucket, key=_priority_key)
        losers = [r for r in bucket if r[0] != winner[0]]
        if not losers:
            continue
        buckets_resolved += 1
        winner_source = winner[2]
        hashes = loser_hashes_by_winner.setdefault(winner_source, set())
        for loser in losers:
            hashes.add(db.canonical_fact_hash(loser[1]))

    if not loser_hashes_by_winner:
        return {"facts_superseded": 0, "buckets_resolved": 0}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_changed = 0
    for winner_source, hashes in loser_hashes_by_winner.items():
        total_changed += _mark_superseded_in_markdown(
            path, hashes, winner_source, today
        )

    if total_changed:
        # Re-upsert so `_facts_from_body` picks up `~~…~~` markers and
        # writes `status='superseded'` + skips FTS.
        db.upsert_entity_from_file(path)

    return {
        "facts_superseded": total_changed,
        "buckets_resolved": buckets_resolved,
    }


def recompute_all() -> dict:
    """Walk every entity file and recompute supersession.

    Called from `db.rebuild` after the per-file upsert loop completes.
    Safe to re-run — no-op when no new contradictions exist.
    """
    totals = {"entities_touched": 0, "facts_superseded": 0, "buckets_resolved": 0}
    for type_dir in config.ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            if f.name.startswith("_"):
                continue
            res = recompute_for_entity(f)
            if res["facts_superseded"]:
                totals["entities_touched"] += 1
                totals["facts_superseded"] += res["facts_superseded"]
                totals["buckets_resolved"] += res["buckets_resolved"]
    return totals
