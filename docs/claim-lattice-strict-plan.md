# Claim Lattice — Strict Mode Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `brain.claims` package + strict recall mode + extract latency tuning, behind `BRAIN_USE_CLAIMS=1` + `BRAIN_STRICT_CLAIMS=1` flags. Default (flags off) = zero behavior change; 803 existing tests continue to pass.

**Architecture:** Three-layer model (notes → claims → projections). Strict mode reads only from `fact_claims` table. No backfill. No daemon. Reuses existing `_insert_fact_claim`, `use_claims_enabled`, dual-write.

**Tech Stack:** Python 3.11+, SQLite (existing), pytest. No new deps.

**Spec:** `docs/claim-lattice-strict-design.md`.

---

## File Map

**Create:**
- `src/brain/claims/__init__.py`
- `src/brain/claims/domain.py` — Claim, ClaimStatus, ClaimHit dataclasses
- `src/brain/claims/read.py` — `current()`, `lookup()`, `search_text()`
- `tests/test_claims_domain.py`
- `tests/test_claims_read.py`
- `tests/test_claims_strict_recall.py`
- `tests/test_claims_isolation.py`

**Modify:**
- `src/brain/mcp_server.py` — add `_strict_claims_enabled()`, `_recall_strict_claims()`, branch in `brain_recall`
- `src/brain/status.py` — add `claims_health()`
- `templates/scripts/auto-extract.sh.tmpl` — lower Level 2 idle threshold to 20s, add per-file cooldown
- `tests/test_status.py` — append claim health tests

---

## Conventions

- All file writes via `brain.io.atomic_write_text` (existing).
- Tests use `tmp_path` + `monkeypatch.setenv("BRAIN_DIR", ...)`. Reuse existing `conftest.py` fixtures where possible.
- Each task ends with one commit. Format: `feat(claims): ...`, `feat(recall): ...`, `chore(extract): ...`.
- Strict mode flag combinations:
  - `BRAIN_USE_CLAIMS=0` (default) → existing behavior, no claims involvement
  - `BRAIN_USE_CLAIMS=1` → dual-write enabled, recall still uses RRF over entities+notes
  - `BRAIN_USE_CLAIMS=1 + BRAIN_STRICT_CLAIMS=1` → recall reads claim DB only

---

## Task 1: Bootstrap `brain.claims` package + domain model

**Files:**
- Create: `src/brain/claims/__init__.py`
- Create: `src/brain/claims/domain.py`
- Test: `tests/test_claims_domain.py`

- [ ] **Step 1: Write failing tests**

`tests/test_claims_domain.py`:

```python
"""Claim domain dataclasses + status enum."""
from __future__ import annotations

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
    import dataclasses
    # Frozen: assigning raises FrozenInstanceError
    import pytest
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claims_domain.py -v`
Expected: ModuleNotFoundError or ImportError

- [ ] **Step 3: Implement domain module**

`src/brain/claims/__init__.py`:

```python
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
```

`src/brain/claims/domain.py`:

```python
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
    """Recall hit for claim-mode reads. Mirrors brain_recall envelope."""
    path: str
    text: str
    name: str | None
    score: float
    claim_id: int
    kind: str = "claim"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claims_domain.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/claims/__init__.py src/brain/claims/domain.py tests/test_claims_domain.py
git commit -m "feat(claims): bootstrap brain.claims package + domain model

Frozen Claim dataclass + ClaimStatus enum + ClaimHit recall shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Claim read API — pure SQL queries on fact_claims

**Files:**
- Create: `src/brain/claims/read.py`
- Test: `tests/test_claims_read.py`

- [ ] **Step 1: Write failing tests**

`tests/test_claims_read.py`:

```python
"""Claim read API — current(), lookup(), search_text()."""
from __future__ import annotations

import time

import pytest

from brain.claims import read
from brain import db


@pytest.fixture
def vault_with_claims(tmp_brain):
    """Insert a few claims directly via the dual-write helper."""
    # Create a couple of entities first so fact_claims FK satisfies.
    with db.connect() as conn:
        conn.execute("INSERT INTO entities (slug, name, type, path) "
                     "VALUES (?, ?, ?, ?)", ("son", "Son", "people", "entities/people/son.md"))
        son_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO entities (slug, name, type, path) "
                     "VALUES (?, ?, ?, ?)", ("aitomatic", "Aitomatic", "organizations",
                                              "entities/organizations/aitomatic.md"))
        # Insert two current claims about son
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son currently in long xuyen",
            source="note:journal/2026-04-25.md", fact_date=None, status="current",
        )
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son works at Aitomatic",
            source="note:journal/2026-04-25.md", fact_date=None, status="current",
        )
        # Insert one superseded claim
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son was in saigon",
            source="note:journal/2026-04-23.md", fact_date=None, status="superseded",
        )
    return tmp_brain


def test_current_returns_only_current_status(vault_with_claims):
    claims = read.current(subject_slug="son")
    statuses = {c.status for c in claims}
    assert statuses == {"current"}
    assert len(claims) == 2


def test_current_filtered_by_predicate_key(vault_with_claims):
    claims = read.current(subject_slug="son", predicate_key="locatedin")
    assert len(claims) == 1
    assert "long xuyen" in claims[0].text


def test_lookup_by_id(vault_with_claims):
    all_claims = read.current(subject_slug="son")
    cid = all_claims[0].id
    fetched = read.lookup(cid)
    assert fetched is not None
    assert fetched.id == cid


def test_lookup_returns_none_for_missing(vault_with_claims):
    assert read.lookup(99999) is None


def test_search_text_finds_subject_match(vault_with_claims):
    hits = read.search_text("son long xuyen", k=8)
    assert len(hits) >= 1
    top = hits[0]
    assert "son" in top.path
    assert "long xuyen" in top.text.lower()


def test_search_text_returns_empty_on_no_match(vault_with_claims):
    hits = read.search_text("completely-unrelated-noun", k=8)
    assert hits == []


def test_search_text_excludes_superseded(vault_with_claims):
    hits = read.search_text("saigon", k=8)
    # "saigon" is in a superseded claim — should NOT appear
    assert all("saigon" not in h.text.lower() for h in hits)


def test_search_text_respects_k_limit(vault_with_claims):
    hits = read.search_text("son", k=1)
    assert len(hits) <= 1
```

- [ ] **Step 2: Check tmp_brain fixture availability**

Run: `grep -rn "tmp_brain\b" tests/conftest.py 2>/dev/null | head`

If `tmp_brain` fixture exists, reuse. If not, replace `tmp_brain` with a tmp_path-based env override (look at how other tests in this repo set up `BRAIN_DIR`).

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_claims_read.py -v`
Expected: ImportError on `read` module

- [ ] **Step 4: Implement read module**

`src/brain/claims/read.py`:

```python
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


_SELECT_COLUMNS = """
    id, subject_slug, predicate, predicate_key, predicate_group,
    object_text, object_slug, object_type,
    text, fact_time, observed_at,
    source_kind, source_path,
    confidence, salience,
    status, superseded_by, claim_key
"""


def current(subject_slug: str, predicate_key: Optional[str] = None) -> list[Claim]:
    """All current (status='current') claims for a subject, optionally
    filtered by predicate_key."""
    sql = f"SELECT {_SELECT_COLUMNS} FROM fact_claims WHERE subject_slug=? AND status='current'"
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

    # Pull every current claim that contains AT LEAST ONE token in
    # text or subject_slug. This is the cheap broad-match step;
    # ranking happens in Python.
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
        cid, subj, text, observed_at, salience, pred_key, obj_text, name, path = r
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
    # Token overlap: fraction of query tokens present in claim text
    text_hits = sum(1 for t in tokens if t in text)
    overlap = text_hits / len(tokens)
    # Subject match bonus
    subject_match = 1.0 if any(t == subject_slug for t in tokens) else 0.0
    # Recency: exponential decay on age in days
    age_days = max(0.0, (now - observed_at) / 86400.0)
    recency = math.exp(-age_days / _RECENCY_HALFLIFE_DAYS)
    # Composite — overlap dominates; subject_match is the +1 sanity
    # check; recency + salience are tie-breakers.
    return overlap + subject_match + 0.1 * recency + 0.5 * salience
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_claims_read.py -v`
Expected: 8 passed

If `tmp_brain` fixture doesn't exist, this is the failure case — adapt the test fixture to use `tmp_path` + `monkeypatch.setenv("BRAIN_DIR", str(tmp_path))` + manual schema init via `db.connect()`.

- [ ] **Step 6: Commit**

```bash
git add src/brain/claims/read.py tests/test_claims_read.py
git commit -m "feat(claims): read API — current(), lookup(), search_text()

LIKE-based composite scoring (token overlap + subject + recency +
salience). FTS5 deferred until claim count exceeds 10k.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Strict-mode env flag + recall integration

**Files:**
- Modify: `src/brain/mcp_server.py`
- Test: `tests/test_claims_strict_recall.py`

- [ ] **Step 1: Write failing tests**

`tests/test_claims_strict_recall.py`:

```python
"""Strict-mode brain_recall reads only fact_claims, not entities/notes."""
from __future__ import annotations

import json

import pytest

from brain import db, mcp_server


def _setup_claim(tmp_brain):
    """Insert one current claim about 'son'."""
    with db.connect() as conn:
        conn.execute("INSERT INTO entities (slug, name, type, path) "
                     "VALUES (?, ?, ?, ?)",
                     ("son", "Son", "people", "entities/people/son.md"))
        son_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son currently in long xuyen",
            source="note:journal/2026-04-25.md", fact_date=None, status="current",
        )


def test_strict_without_use_claims_raises(tmp_brain, monkeypatch):
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "0")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_recall("anything")
    parsed = json.loads(out)
    # Should surface as configuration error, not silent fallback
    assert parsed.get("error") == "configuration_error"
    assert "BRAIN_USE_CLAIMS" in parsed.get("detail", "")


def test_strict_returns_claim_only_hits(tmp_brain, monkeypatch):
    _setup_claim(tmp_brain)
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_recall("son long xuyen")
    parsed = json.loads(out)
    assert parsed["weak_match"] is False
    assert len(parsed["hits"]) >= 1
    assert all(h.get("kind") == "claim" for h in parsed["hits"])


def test_strict_empty_returns_weak_match_with_strict_guidance(tmp_brain, monkeypatch):
    _setup_claim(tmp_brain)
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    monkeypatch.setenv("BRAIN_STRICT_CLAIMS", "1")
    out = mcp_server.brain_recall("completely-unknown-topic-xyz123")
    parsed = json.loads(out)
    assert parsed["weak_match"] is True
    assert parsed["hits"] == []
    assert "claim store" in (parsed.get("guidance") or "")
    assert "brain_notes" in (parsed.get("guidance") or "")


def test_default_mode_uses_existing_path(tmp_brain, monkeypatch):
    """With flags off, brain_recall behavior is unchanged from today."""
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    monkeypatch.delenv("BRAIN_STRICT_CLAIMS", raising=False)
    out = mcp_server.brain_recall("test-query-default-path")
    parsed = json.loads(out)
    # Existing envelope shape preserved; we don't assert specific hits
    # because the legacy path's behavior is independent of this change.
    assert "query" in parsed
    assert "weak_match" in parsed
    assert "hits" in parsed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claims_strict_recall.py -v`
Expected: AttributeError or wrong shape

- [ ] **Step 3: Modify `mcp_server.py`**

Find the `def brain_recall(` definition (around line 616). Add helpers above it and a strict branch at the top of the function body.

Insert above `def brain_recall(`:

```python
# ─────────────────────────────────────────────────────────────────────────
# Strict claim-mode recall — see docs/claim-lattice-strict-design.md
# ─────────────────────────────────────────────────────────────────────────
def _strict_claims_enabled() -> bool:
    """Return True iff BRAIN_USE_CLAIMS=1 AND BRAIN_STRICT_CLAIMS=1."""
    use = os.environ.get("BRAIN_USE_CLAIMS", "0") == "1"
    strict = os.environ.get("BRAIN_STRICT_CLAIMS", "0") == "1"
    return use and strict


def _strict_claims_misconfigured() -> bool:
    """BRAIN_STRICT_CLAIMS=1 without BRAIN_USE_CLAIMS=1 is a config error."""
    use = os.environ.get("BRAIN_USE_CLAIMS", "0") == "1"
    strict = os.environ.get("BRAIN_STRICT_CLAIMS", "0") == "1"
    return strict and not use


def _claim_miss_threshold() -> float:
    try:
        return float(os.environ.get("BRAIN_CLAIM_MISS_THRESHOLD", "0.5"))
    except (ValueError, TypeError):
        return 0.5


def _recall_strict_claims(query: str, k: int, verbose: bool) -> str:
    """Query claim store only. No entity-file or note fallback."""
    from brain.claims import read as _claim_read
    hits = _claim_read.search_text(query, k=k)
    threshold = _claim_miss_threshold()
    weak = (not hits) or (hits[0].score < threshold)
    if not hits:
        guidance = (
            "the brain has no current claim matching this query in the "
            "strict claim store. Notes layer is not consulted in strict "
            "mode — call `brain_notes(query)` to search free-form note text."
        )
    elif weak:
        guidance = (
            "weak match in claim store; top score below threshold. "
            "Treat hits as topical hints, not authoritative answers."
        )
    else:
        guidance = None

    formatted_hits = []
    for h in hits:
        item: dict = {
            "kind": h.kind,
            "path": h.path,
            "text": h.text if verbose else (h.text or "")[:240],
            "name": h.name,
            "claim_id": h.claim_id,
        }
        if verbose:
            item["score"] = h.score
        formatted_hits.append(item)

    return json.dumps({
        "query": query,
        "weak_match": weak,
        "guidance": guidance,
        "hits": formatted_hits,
    }, ensure_ascii=False, indent=2)
```

Then modify the `brain_recall` function. Find the line right after the docstring (before `if _projection.default_verbose() ...`). Insert:

```python
    if _strict_claims_misconfigured():
        return json.dumps({
            "error": "configuration_error",
            "detail": "BRAIN_STRICT_CLAIMS=1 requires BRAIN_USE_CLAIMS=1; "
                      "set both flags or unset both.",
        }, ensure_ascii=False, indent=2)
    if _strict_claims_enabled():
        return _recall_strict_claims(query, max(1, min(int(k), 25)), verbose)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claims_strict_recall.py -v`
Expected: 4 passed

- [ ] **Step 5: Verify default-mode tests still pass**

Run: `pytest tests/test_mcp_server.py -v 2>&1 | tail -30`
Expected: existing brain_recall tests still pass (we didn't break the legacy path).

- [ ] **Step 6: Commit**

```bash
git add src/brain/mcp_server.py tests/test_claims_strict_recall.py
git commit -m "feat(recall): strict-mode brain_recall reads claim store only

BRAIN_USE_CLAIMS=1 + BRAIN_STRICT_CLAIMS=1 → recall queries
fact_claims directly, no entity/note fallback. Misconfiguration
(strict without use_claims) returns explicit config_error envelope.

Default mode (flags off) unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Doctor — `claims_health()` integration

**Files:**
- Modify: `src/brain/status.py`
- Test: `tests/test_status.py` (append)

- [ ] **Step 1: Write failing test**

Append to `tests/test_status.py`:

```python


# ─── Claim layer health ──────────────────────────────────────────


def test_claims_health_default_off(tmp_brain, monkeypatch):
    monkeypatch.delenv("BRAIN_USE_CLAIMS", raising=False)
    monkeypatch.delenv("BRAIN_STRICT_CLAIMS", raising=False)
    out = status.claims_health()
    assert "Claims" in out["section"]
    assert out["use_claims"] is False
    assert out["strict_mode"] is False


def test_claims_health_counts_claims(tmp_brain, monkeypatch):
    from brain import db as _db
    monkeypatch.setenv("BRAIN_USE_CLAIMS", "1")
    with _db.connect() as conn:
        conn.execute("INSERT INTO entities (slug, name, type, path) "
                     "VALUES ('son', 'Son', 'people', 'entities/people/son.md')")
        son_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son in long xuyen",
            source="note:foo.md", fact_date=None, status="current",
        )
        _db._insert_fact_claim(
            conn, entity_id=son_id, subject_slug="son",
            text="son was in saigon",
            source="note:foo.md", fact_date=None, status="superseded",
        )
    out = status.claims_health()
    assert out["use_claims"] is True
    assert out["fact_claims_total"] == 2
    assert out["fact_claims_current"] == 1
    assert out["fact_claims_superseded"] == 1


def test_claims_health_extract_idle_threshold(tmp_brain, monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_IDLE_LEVEL2_SEC", "30")
    out = status.claims_health()
    assert out["extract_idle_threshold_sec"] == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_status.py -v -k claims`
Expected: AttributeError

- [ ] **Step 3: Add `claims_health()` to status.py**

Append to `src/brain/status.py`:

```python


def claims_health() -> dict:
    """Doctor check for the claim store (knowledge layer).

    Reports whether claim flags are set, claim counts by status, age
    of newest claim (proxy for extraction pipeline health), and
    effective extract idle threshold.
    """
    use = os.environ.get("BRAIN_USE_CLAIMS", "0") == "1"
    strict = os.environ.get("BRAIN_STRICT_CLAIMS", "0") == "1"
    try:
        idle = int(os.environ.get("BRAIN_EXTRACT_IDLE_LEVEL2_SEC", "20"))
    except (ValueError, TypeError):
        idle = 20

    total = current = superseded = 0
    newest_age: float | None = None
    try:
        from brain import db as _db
        with _db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN status='current' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='superseded' THEN 1 ELSE 0 END), "
                "MAX(observed_at) "
                "FROM fact_claims"
            ).fetchone()
            if row:
                total = row[0] or 0
                current = row[1] or 0
                superseded = row[2] or 0
                if row[3]:
                    newest_age = max(0.0, time.time() - float(row[3]))
    except Exception:  # noqa: BLE001 — best-effort doctor read
        pass

    return {
        "section": "Claims (knowledge layer)",
        "use_claims": use,
        "strict_mode": strict,
        "fact_claims_total": total,
        "fact_claims_current": current,
        "fact_claims_superseded": superseded,
        "newest_claim_age_sec": newest_age,
        "extract_idle_threshold_sec": idle,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_status.py -v -k claims`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/brain/status.py tests/test_status.py
git commit -m "feat(doctor): claims_health() reports claim store + extract config

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Extract latency tuning — auto-extract.sh.tmpl

**Files:**
- Modify: `templates/scripts/auto-extract.sh.tmpl`

- [ ] **Step 1: Read the current template + identify Level 2 idle threshold**

Run: `grep -n "Level\|idle\|min_idle\|60\|180" templates/scripts/auto-extract.sh.tmpl | head`

Find where Level 2 is gated. The template uses `brain.resource_guard` to compute LEVEL based on CPU + idle. The actual threshold likely lives in `src/brain/resource_guard.py`.

- [ ] **Step 2: Inspect resource_guard for the threshold**

Run: `grep -n "60\|180\|idle\|level\|LEVEL" src/brain/resource_guard.py`

The threshold is hardcoded in resource_guard. We'll add an env override.

- [ ] **Step 3: Modify resource_guard.py to honor env override**

Find the Level 2 idle threshold constant (likely `60`). Replace with:

```python
def _level2_idle_sec() -> int:
    """Level 2 (LLM extract) idle threshold. Default 20s — lowered
    from 60s in 2026-04-25 to reduce note→claim lag. Env override:
    BRAIN_EXTRACT_IDLE_LEVEL2_SEC."""
    try:
        return max(5, int(os.environ.get("BRAIN_EXTRACT_IDLE_LEVEL2_SEC", "20")))
    except (ValueError, TypeError):
        return 20
```

Replace the hardcoded `60` (or whatever) usage with `_level2_idle_sec()`.

For Level 3 (the heavier dedupe pass), keep at 180s since it's batched and not user-facing.

- [ ] **Step 4: Add a test for the env override**

Append to `tests/test_resource_guard.py` (create file if missing):

```python
"""Resource guard env override for Level 2 idle threshold."""
from __future__ import annotations

from brain import resource_guard


def test_level2_idle_default_is_20(monkeypatch):
    monkeypatch.delenv("BRAIN_EXTRACT_IDLE_LEVEL2_SEC", raising=False)
    assert resource_guard._level2_idle_sec() == 20


def test_level2_idle_env_override(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_IDLE_LEVEL2_SEC", "45")
    assert resource_guard._level2_idle_sec() == 45


def test_level2_idle_floor_at_5(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_IDLE_LEVEL2_SEC", "1")
    assert resource_guard._level2_idle_sec() == 5


def test_level2_idle_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_IDLE_LEVEL2_SEC", "abc")
    assert resource_guard._level2_idle_sec() == 20
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_resource_guard.py -v`
Expected: 4 passed (or adapt to existing resource_guard tests if any)

- [ ] **Step 6: Update auto-extract.sh.tmpl comment for clarity**

Update the comment block at the top of `templates/scripts/auto-extract.sh.tmpl` to mention:

> Level 2 idle threshold defaults to 20s (was 60s pre-2026-04-25).
> Override via BRAIN_EXTRACT_IDLE_LEVEL2_SEC env var.

- [ ] **Step 7: Commit**

```bash
git add src/brain/resource_guard.py tests/test_resource_guard.py templates/scripts/auto-extract.sh.tmpl
git commit -m "chore(extract): lower Level 2 idle threshold 60s -> 20s

Reduces note->claim extraction lag for the strict-mode claim store.
Env-overridable via BRAIN_EXTRACT_IDLE_LEVEL2_SEC. Floor at 5s to
prevent runaway extraction.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Isolation enforcement test for `brain.claims`

**Files:**
- Create: `tests/test_claims_isolation.py`

- [ ] **Step 1: Write the test**

`tests/test_claims_isolation.py`:

```python
"""Architectural test: brain.claims must not depend on entities/semantic/graph layers.

Claims are knowledge layer. Importing entities or semantic from
claims would couple the knowledge layer to the projection or
indexing layer — violates 3-layer separation.
"""
from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

FORBIDDEN_PREFIXES = (
    "brain.entities",
    "brain.semantic",
    "brain.graph",
    "brain.consolidation",
    "brain.dedupe",
    "brain.dedupe_judge",
    "brain.note_extract",
    "brain.auto_extract",
    "brain.apply_extraction",
    "brain.reconcile",
)


def _claims_modules():
    pkg = importlib.import_module("brain.claims")
    pkg_path = Path(pkg.__file__).parent
    for info in pkgutil.iter_modules([str(pkg_path)]):
        if info.ispkg:
            continue
        yield f"brain.claims.{info.name}", pkg_path / f"{info.name}.py"


def _imports(file_path: Path) -> list[str]:
    tree = ast.parse(file_path.read_text())
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module)
    return out


def test_claims_modules_dont_import_other_layers():
    violations: list[tuple[str, str]] = []
    for mod_name, file_path in _claims_modules():
        for imp in _imports(file_path):
            for forbidden in FORBIDDEN_PREFIXES:
                if imp == forbidden or imp.startswith(forbidden + "."):
                    violations.append((mod_name, imp))
    assert not violations, (
        "brain.claims modules must not import from entities/semantic/graph/etc:\n"
        + "\n".join(f"  {mod} imports {imp}" for mod, imp in violations)
    )
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_claims_isolation.py -v`
Expected: PASS (since Tasks 1-2 only import `brain.db` and `brain.claims.domain`)

- [ ] **Step 3: Commit**

```bash
git add tests/test_claims_isolation.py
git commit -m "test(claims): enforce isolation from entities/semantic/graph layers

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

After all tasks, run the full suite:

```bash
pytest 2>&1 | tail -10
```

Expected: 803 + ~25 new = ~828 passed, 1 deselected (existing integration test marker).

If any existing test fails, the strict-mode branch leaked to the default path. Fix in `mcp_server.brain_recall` — the `_strict_claims_enabled()` branch must be a *guard*, not a side effect.

---

## Acceptance check vs spec §5

- ✅ `brain.claims.{__init__, domain, read}` modules ≤250 LOC each — Task 1, 2
- ✅ `BRAIN_STRICT_CLAIMS=1` without `BRAIN_USE_CLAIMS=1` raises config error — Task 3
- ✅ `claims.read.search_text` returns ranked ClaimHits, sub-100ms — Task 2
- ✅ `mcp_server.brain_recall` strict envelope identical shape — Task 3
- ✅ Empty claim hits → weak_match=True, strict guidance — Task 3
- ✅ `templates/scripts/auto-extract.sh.tmpl` Level 2 idle = 20s, env override — Task 5
- ✅ `brain.status.claims_health()` reports documented dict — Task 4
- ✅ All 803 existing tests pass with default env — verified in Self-Review
- ✅ `brain.claims` isolation enforced — Task 6
