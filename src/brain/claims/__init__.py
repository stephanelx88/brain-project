"""Claim lattice — knowledge layer (single source of truth).

The fact_claims table is authoritative for fact-intent queries when
BRAIN_USE_CLAIMS=1 is set. Notes (free-form Obsidian text) and
entity .md files are evidence and projection layers respectively;
neither is queried by `claims.read.*`.

This package MUST NOT import from brain.entities, brain.semantic,
brain.graph, brain.consolidation, brain.dedupe — see
tests/test_claims_isolation.py.
"""
from __future__ import annotations
