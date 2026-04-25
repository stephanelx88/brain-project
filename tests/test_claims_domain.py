"""Claim domain dataclasses + status enum."""
from __future__ import annotations

import dataclasses

import pytest

from brain.claims import domain


def test_claim_status_values():
    assert domain.ClaimStatus.CURRENT.value == "current"
    assert domain.ClaimStatus.SUPERSEDED.value == "superseded"


def test_claim_dataclass_frozen():
    c = domain.Claim(
        id=1,
        subject_slug="son",
        predicate="locatedIn",
        predicate_key="locatedin",
        predicate_group="location",
        object_text="long xuyen",
        object_slug=None,
        object_type="string",
        text="son currently in long xuyen",
        fact_time=None,
        observed_at=1700000000.0,
        source_kind="note",
        source_path="journal/2026-04-25.md",
        confidence=0.5,
        salience=0.3,
        status="current",
        superseded_by=None,
        claim_key="abc123",
    )
    assert c.subject_slug == "son"
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.subject_slug = "other"  # type: ignore


def test_claim_hit_minimal_shape():
    h = domain.ClaimHit(
        path="entities/people/son.md",
        text="son currently in long xuyen",
        name="Son",
        score=0.85,
        claim_id=42,
    )
    assert h.kind == "claim"
    assert h.path == "entities/people/son.md"
    assert h.score == 0.85
