"""Claim domain model — frozen dataclasses + status enum."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ClaimStatus(str, Enum):
    CURRENT = "current"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Claim:
    id: int
    subject_slug: str
    predicate: str
    predicate_key: str
    predicate_group: str | None
    object_text: str | None
    object_slug: str | None
    object_type: str
    text: str
    fact_time: str | None
    observed_at: float
    source_kind: str
    source_path: str | None
    confidence: float
    salience: float
    status: str
    superseded_by: int | None
    claim_key: str


@dataclass(frozen=True)
class ClaimHit:
    """Recall hit for claim-mode reads. Mirrors brain_recall envelope.

    Per docs/claim-lattice-strict-design.md §3.3 the hit shape is
    `{kind, path, text, name?, entity_summary?}` — `entity_summary`
    emits only on the FIRST hit per entity (suppressed on duplicates).
    """
    path: str
    text: str
    name: str | None
    score: float
    claim_id: int
    entity_summary: str | None = None
    kind: str = "claim"
