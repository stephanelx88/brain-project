"""Runtime transport layer for inter-session messaging.

Strictly separate from the vault (BRAIN_DIR / entities / journal / etc.).
This package MUST NOT import brain.entities, brain.graph, brain.semantic,
or any module that touches curated knowledge — see
tests/test_runtime_isolation.py for the enforcement check.
"""
from __future__ import annotations
