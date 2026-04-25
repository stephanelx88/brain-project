"""Claim read API — pure SQL queries on fact_claims.

NO imports from brain.entities, brain.semantic, brain.graph,
brain.consolidation. Read-only — never mutates.
"""
from __future__ import annotations

import math
import time
from typing import Optional

from brain import db
from brain.claims.domain import Claim, ClaimHit


_RECENCY_HALFLIFE_DAYS = 30.0


_SELECT_COLUMNS = """
    id, subject_slug, predicate, predicate_key, predicate_group,
    object_text, object_slug, object_type,
    text, fact_time, observed_at,
    source_kind, source_path,
    confidence, salience,
    status, superseded_by, claim_key
"""


def _row_to_claim(row) -> Claim:
    return Claim(
        id=row[0],
        subject_slug=row[1],
        predicate=row[2],
        predicate_key=row[3],
        predicate_group=row[4],
        object_text=row[5],
        object_slug=row[6],
        object_type=row[7],
        text=row[8],
        fact_time=row[9],
        observed_at=row[10],
        source_kind=row[11],
        source_path=row[12],
        confidence=row[13],
        salience=row[14],
        status=row[15],
        superseded_by=row[16],
        claim_key=row[17],
    )


def current(subject_slug: str, predicate_key: Optional[str] = None) -> list[Claim]:
    """All current (status='current') claims for a subject, optionally
    filtered by predicate_key."""
    sql = (
        f"SELECT {_SELECT_COLUMNS} FROM fact_claims "
        "WHERE subject_slug=? AND status='current'"
    )
    params: list = [subject_slug]
    if predicate_key:
        sql += " AND predicate_key=?"
        params.append(predicate_key)
    sql += " ORDER BY observed_at DESC"
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_claim(r) for r in rows]


def lookup(claim_id: int) -> Optional[Claim]:
    """Fetch one claim by primary key. Returns None if not found."""
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM fact_claims WHERE id=?",
            (claim_id,),
        ).fetchone()
    return _row_to_claim(row) if row else None


def search_text(query: str, k: int = 8) -> list[ClaimHit]:
    """Lexical search over current claims' text + subject_slug.

    MVP: SQLite LIKE-based scoring with token-overlap, recency boost,
    salience. No FTS5 yet — at <10k claims, LIKE is sub-100ms.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    tokens = [t for t in q.split() if t]
    if not tokens:
        return []

    where_parts = []
    params: list = []
    for tok in tokens:
        where_parts.append("(LOWER(fc.text) LIKE ? OR LOWER(fc.subject_slug) LIKE ?)")
        params.extend([f"%{tok}%", f"%{tok}%"])
    where_clause = " OR ".join(where_parts)

    sql = f"""
        SELECT
            fc.id, fc.subject_slug, fc.text, fc.observed_at, fc.salience,
            fc.predicate_key, fc.object_text,
            e.name, e.path
        FROM fact_claims fc
        JOIN entities e ON e.id = fc.entity_id
        WHERE fc.status='current' AND ({where_clause})
        LIMIT 200
    """

    now = time.time()
    raw_hits: list[tuple[float, ClaimHit]] = []
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    for r in rows:
        cid, subj, text, observed_at, salience, _pred_key, _obj_text, name, path = r
        score = _score_claim(
            tokens=tokens,
            text=(text or "").lower(),
            subject_slug=(subj or "").lower(),
            observed_at=observed_at or 0.0,
            salience=salience or 0.0,
            now=now,
        )
        if score <= 0:
            continue
        raw_hits.append((
            score,
            ClaimHit(
                path=path or f"entities/.../{subj}.md",
                text=text or "",
                name=name,
                score=score,
                claim_id=cid,
            ),
        ))

    raw_hits.sort(key=lambda x: -x[0])
    return [hit for _, hit in raw_hits[:max(1, min(int(k), 100))]]


def _score_claim(
    *,
    tokens: list[str],
    text: str,
    subject_slug: str,
    observed_at: float,
    salience: float,
    now: float,
) -> float:
    """Composite score: token overlap + subject match + recency + salience."""
    if not tokens:
        return 0.0
    text_hits = sum(1 for t in tokens if t in text)
    overlap = text_hits / len(tokens)
    subject_match = 1.0 if any(t == subject_slug for t in tokens) else 0.0
    age_days = max(0.0, (now - observed_at) / 86400.0)
    recency = math.exp(-age_days / _RECENCY_HALFLIFE_DAYS)
    return overlap + subject_match + 0.1 * recency + 0.5 * salience
