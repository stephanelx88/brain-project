"""Registry of predicates the brain has learned to trust.

Replaces the hardcoded ``graph.VALID_PREDICATES`` frozenset with an
audit-gated, learning ledger. New predicates seen by the extractor land
as ``proposed``; after N confirmations from the audit walker they
graduate to ``active`` and become write-allowed. N rejections retire
them (future triples with a retired predicate get dropped and logged
to ``failures.jsonl`` so silent drops stay discoverable).

Storage: ``identity/predicates.jsonl`` (JSONL, rewritten atomically on
every change — mirrors ``triple_rules.py``). Silent-fail on disk errors.

Status state machine::

    proposed ──N confirms within WINDOW days──> active
    proposed ──N rejects  within WINDOW days──> retired
    proposed ──anything else────────────────> stays proposed

Env overrides for the promotion gate:

    BRAIN_PREDICATE_PROMOTE_N       (default 3)
    BRAIN_PREDICATE_PROMOTE_DAYS    (default 30)

First call bootstraps the 15 legacy predicates as ``active`` so day-one
behaviour matches the old frozenset.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import brain.config as config
from brain.io import atomic_write_text


# Legacy set — grandfathered as ``active`` on first bootstrap so no
# regression on existing triples. confirmed=0 is honest: they're
# inherited, not earned through audit.
_LEGACY_PREDICATES: tuple[str, ...] = (
    "worksAt", "workedAt", "knows", "manages", "reportsTo",
    "partOf", "locatedIn", "builds", "uses", "involves",
    "relatedTo", "about", "decidedOn", "learnedFrom", "contradicts",
)

_MAX_EXAMPLES = 5


def _promote_n() -> int:
    try:
        return max(1, int(os.environ.get("BRAIN_PREDICATE_PROMOTE_N", "3")))
    except ValueError:
        return 3


def _promote_days() -> int:
    try:
        return max(1, int(os.environ.get("BRAIN_PREDICATE_PROMOTE_DAYS", "30")))
    except ValueError:
        return 30


def _path() -> Path:
    """Resolve the registry path from the *current* IDENTITY_DIR.

    Deliberately recomputed on every call so tests that monkeypatch
    ``brain.config.IDENTITY_DIR`` see the override. A module-level
    constant bound at import time would freeze the path to wherever
    the first importer ran and leak writes into the real vault from
    test suites that only patch ``IDENTITY_DIR``.
    """
    return config.IDENTITY_DIR / "predicates.jsonl"


def _norm(pred: str) -> str:
    """Lookup key collapsing case and separator variants.

    ``presented_at``, ``presentedAt``, ``PRESENTED-AT`` all map to the
    same key ``presentedat``. The ``predicate`` field keeps the
    first-seen canonical form.
    """
    return "".join(c for c in pred.lower() if c.isalnum())


def _load() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        text = p.read_text()
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def _save(rows: list[dict]) -> None:
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            _path(),
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        )
    except OSError:
        pass  # silent-fail, mirror triple_rules.py


def _find(rows: list[dict], pred: str) -> dict | None:
    key = _norm(pred)
    if not key:
        return None
    for r in rows:
        if _norm(r.get("predicate", "")) == key:
            return r
    return None


def _ensure_bootstrapped() -> None:
    """Seed the legacy 15 as ``active`` on first run. Idempotent."""
    if _path().exists() and _load():
        return
    today = date.today().isoformat()
    seed = [
        {
            "predicate": p,
            "status": "active",
            "confirmed": 0,
            "rejected": 0,
            "first_seen": today,
            "promoted_at": today,
            "examples": [],
        }
        for p in _LEGACY_PREDICATES
    ]
    _save(seed)


def bootstrap_from_legacy() -> int:
    """Explicit bootstrap hook. Returns the count seeded (0 if already done)."""
    if _path().exists() and _load():
        return 0
    _ensure_bootstrapped()
    return len(_LEGACY_PREDICATES)


def status(predicate: str) -> str:
    """Return ``'active' | 'proposed' | 'retired' | 'unknown'``."""
    _ensure_bootstrapped()
    hit = _find(_load(), predicate)
    if hit is None:
        return "unknown"
    return hit.get("status", "unknown")


def observe(predicate: str, basis: str = "") -> None:
    """Record a sighting of ``predicate``.

    First sighting creates a ``proposed`` row. Subsequent sightings only
    append the basis to ``examples`` (capped). Status is never changed
    here — that's ``record_decision``'s job.
    """
    if not predicate or not predicate.strip():
        return
    _ensure_bootstrapped()
    rows = _load()
    hit = _find(rows, predicate)
    if hit is None:
        rows.append({
            "predicate": predicate,
            "status": "proposed",
            "confirmed": 0,
            "rejected": 0,
            "first_seen": date.today().isoformat(),
            "promoted_at": None,
            "examples": [basis] if basis else [],
        })
        _save(rows)
        return
    if basis:
        examples = hit.get("examples", [])
        if basis not in examples:
            hit["examples"] = (examples + [basis])[-_MAX_EXAMPLES:]
            _save(rows)


def record_decision(predicate: str, decision: str) -> None:
    """Apply an audit ``y``/``n`` decision. Promotes or retires when the
    count threshold is met **within the promotion window**.

    Decisions outside the window still accumulate but can no longer
    trigger a transition — that's the point of the window. This keeps
    a predicate that drifted in popularity years ago from being
    promoted by stale activity.
    """
    decision = (decision or "").strip().lower()
    if decision not in ("y", "n"):
        return
    _ensure_bootstrapped()
    rows = _load()
    hit = _find(rows, predicate)
    if hit is None:
        observe(predicate)
        rows = _load()
        hit = _find(rows, predicate)
        if hit is None:
            return

    if decision == "y":
        hit["confirmed"] = hit.get("confirmed", 0) + 1
    else:
        hit["rejected"] = hit.get("rejected", 0) + 1

    _maybe_transition(hit)
    _save(rows)


def _maybe_transition(row: dict) -> None:
    if row.get("status") != "proposed":
        return
    try:
        first = date.fromisoformat(row.get("first_seen", ""))
    except (TypeError, ValueError):
        return
    if (date.today() - first) > timedelta(days=_promote_days()):
        return
    n = _promote_n()
    if row.get("confirmed", 0) >= n:
        row["status"] = "active"
        row["promoted_at"] = date.today().isoformat()
    elif row.get("rejected", 0) >= n:
        row["status"] = "retired"
        row["promoted_at"] = date.today().isoformat()


def list_proposed() -> list[dict]:
    _ensure_bootstrapped()
    return [r for r in _load() if r.get("status") == "proposed"]


def promote(predicate: str) -> bool:
    """Manual override → ``active``. Returns True on state change."""
    _ensure_bootstrapped()
    rows = _load()
    hit = _find(rows, predicate)
    if hit is None or hit.get("status") == "active":
        return False
    hit["status"] = "active"
    hit["promoted_at"] = date.today().isoformat()
    _save(rows)
    return True


def retire(predicate: str) -> bool:
    """Manual override → ``retired``. Returns True on state change."""
    _ensure_bootstrapped()
    rows = _load()
    hit = _find(rows, predicate)
    if hit is None or hit.get("status") == "retired":
        return False
    hit["status"] = "retired"
    hit["promoted_at"] = date.today().isoformat()
    _save(rows)
    return True
