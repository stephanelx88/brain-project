---
title: Claim Lattice — Strict Mode Foundation
date: 2026-04-25
status: design — ready for implementation plan
---

# Claim Lattice — Strict Mode Foundation

## 0. Problem statement

Brain today has **dual sources** for the same fact: extracted entity
files (`entities/<type>/*.md`) and free-form notes (`journal/*.md`,
root-level `.md`). `brain_recall` does RRF over both. Same fact "son
is in long xuyen" can exist in 3 places:

1. Raw note user just wrote in Obsidian (BM25 hit immediately)
2. Extracted entity `entities/people/son.md` (after auto_extract runs)
3. (When `BRAIN_USE_CLAIMS=1`) `fact_claims` table

When (1) and (2) disagree (note says `long xuyen`, entity still says
`saigon` because extract hasn't run yet), agent gets whichever scores
higher in RRF. That's the **dép-class risk** institutionalised — fact
inferred from weak match, not authoritative claim.

## 1. Goal

Single source of truth for **fact-intent queries**: the
`fact_claims` table. When `BRAIN_USE_CLAIMS=1` AND
`BRAIN_STRICT_CLAIMS=1`, `brain_recall` queries only the claim store
(not entity .md, not notes). Notes remain queryable via `brain_notes`
for content-intent.

Three layers, three responsibilities:

```
USER LAYER     — Obsidian notes, free-form text  (input + evidence)
                       ↓ extract pipeline
KNOWLEDGE LAYER — fact_claims table              (single source of truth)
                       ↓ existing upsert_entity_from_file (dual-write)
PROJECTION     — entities/<type>/*.md             (read-only Obsidian view)
```

Read contract:
- **Fact intent** (`brain_recall("where is son")`): claim layer only in strict mode.
- **Content intent** (`brain_notes("what did i write today")`): notes layer (unchanged).
- **Browse intent** (`brain_get`, opening entity file): projection layer (unchanged).

## 2. Scope decisions (already settled with user)

| Decision | Choice |
|---|---|
| Backfill existing 412 facts? | **No.** Fresh writes only. Legacy entity files stay; new claims accumulate. |
| Strict vs soft mode | **Strict.** No `unverified_hints` shape. Better failure mode (loud) over silent inaccuracy. |
| Cost mitigation for strict (extract lag) | **Tune extract idle threshold** from 60-180s to ~15-30s; no streaming daemon. |
| Projector (entity .md ← claims) | **Defer.** Existing `upsert_entity_from_file` already keeps .md in sync via dual-write. Fully claim-authoritative .md = phase 2. |
| Predicate type registry (subject_type, cardinality, ...) | **Defer.** Existing 3-regex `supersede.classify_predicate` works at MVP scale (412 facts). Type system = phase 2. |
| Watchdog / NLI / manifold from 100x roadmap | **Defer.** Foundation first. |

→ Net-new code in this MVP: claim read API + strict mode flag + extract tuning + doctor check.

## 3. Architecture

### 3.1 New env flag — `BRAIN_STRICT_CLAIMS`

```
BRAIN_USE_CLAIMS=0  → no claims (current default; dual-write off)
BRAIN_USE_CLAIMS=1  → dual-write claims AND legacy facts; recall reads BOTH
BRAIN_USE_CLAIMS=1 + BRAIN_STRICT_CLAIMS=1  → recall reads claims ONLY
```

`BRAIN_STRICT_CLAIMS=1` without `BRAIN_USE_CLAIMS=1` is illegal —
strict requires claim-write to have happened. `recall_with_claims()`
raises `ConfigurationError` on that combination.

### 3.2 New module — `brain.claims`

```
src/brain/claims/
  __init__.py
  domain.py        ← Claim dataclass, ClaimStatus enum, RecallEnvelope
  read.py          ← current(), lookup(), search_text()
  doctor.py        ← health check for status.py integration
```

LOC budget per file: ≤ 250.

### 3.3 Claim domain (`claims/domain.py`)

```python
@dataclass(frozen=True)
class Claim:
    id: int
    subject_slug: str
    predicate: str
    predicate_key: str
    predicate_group: str | None
    object_text: str | None
    object_slug: str | None
    object_type: str           # "string" | "entity"
    text: str                  # surface form
    fact_time: str | None
    observed_at: float
    source_kind: str           # "note" | "session" | "user" | "correction" | "import"
    source_path: str | None
    confidence: float
    salience: float
    status: str                # "current" | "superseded"
    superseded_by: int | None
    claim_key: str             # dedup hash


class ClaimStatus(str, Enum):
    CURRENT = "current"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ClaimHit:
    """Recall hit shape for claim-mode reads. Mirrors the existing
    brain_recall envelope contract (kind, path, text, name?,
    entity_summary?) so callers in mcp_server need minimal change."""
    kind: str = "claim"        # always "claim" for now
    path: str                  # "entities/<type>/<slug>.md" — for navigation
    text: str                  # claim.text (truncated to BRAIN_RECALL_SNIPPET_CHARS)
    name: str | None           # entity display name
    score: float               # composite ranking score
    claim_id: int              # primary key — agent can fetch full row if needed
```

### 3.4 Claim read API (`claims/read.py`)

Three pure-SQL functions:

```python
def current(subject_slug: str, predicate_key: str | None = None) -> list[Claim]:
    """All current (status='current') claims for a subject, optionally
    filtered by predicate. Used for "what does brain know about X right
    now"."""

def lookup(claim_id: int) -> Claim | None:
    """Fetch one claim by id."""

def search_text(query: str, k: int = 8) -> list[ClaimHit]:
    """Lexical search over current claims' text + subject_slug.

    MVP implementation: SQLite LIKE on `text` and `subject_slug`,
    weighted by:
      - exact subject match: +1.0
      - text token-overlap ratio: 0.0-1.0
      - recency boost: 0.1 * exp(-age_days / 30)
      - salience: claim.salience * 0.5

    Returns top-k. Future: FTS5 over fact_claims.text (separate work).
    """
```

Why LIKE not FTS5 for MVP: no FTS5 virtual table over fact_claims
exists yet; building one is ≥1 day of separate work. At 412→4k
claims, LIKE-on-text is sub-100ms — acceptable. Upgrade path: when
claim count >10k, add FTS5 virtual table mirroring the existing one
on `facts`.

### 3.5 Strict recall integration

`mcp_server.brain_recall` gets a strict-mode branch:

```python
def brain_recall(query, k=8, type=None, verbose=False, debug=False):
    if _strict_claims_enabled():
        return _recall_strict_claims(query, k, type, verbose, debug)
    # else: existing RRF-over-entities-and-notes path (unchanged)
```

`_strict_claims_enabled()`:
```python
def _strict_claims_enabled() -> bool:
    use = os.environ.get("BRAIN_USE_CLAIMS", "0") == "1"
    strict = os.environ.get("BRAIN_STRICT_CLAIMS", "0") == "1"
    if strict and not use:
        raise ConfigurationError(
            "BRAIN_STRICT_CLAIMS=1 requires BRAIN_USE_CLAIMS=1"
        )
    return use and strict
```

`_recall_strict_claims(...)`:
```python
def _recall_strict_claims(query, k, type, verbose, debug):
    hits = claims.read.search_text(query, k=k)
    envelope = {
        "query": query,
        "weak_match": _is_weak_match(hits),
        "guidance": _strict_guidance(hits),  # claim-aware text
        "hits": [_format_claim_hit(h, verbose) for h in hits],
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2)
```

The envelope shape is **identical** to today's brain_recall — agents
calling brain_recall don't see the difference. Difference is
entirely server-side (where we read from).

### 3.6 Weak-match contract (preserved)

Today's `brain_recall` returns `weak_match: bool` based on RRF top
score below threshold. In strict mode:

```python
def _is_weak_match(hits: list[ClaimHit]) -> bool:
    if not hits:
        return True
    return hits[0].score < float(os.environ.get("BRAIN_CLAIM_MISS_THRESHOLD", "0.5"))
```

When weak_match, guidance is:
```
"the brain has no current claim matching this query in the strict
claim store. Notes layer is not consulted in strict mode — call
`brain_notes(query)` if you want to search free-form note text."
```

This is the LOUD failure mode user agreed to: strict mode tells the
agent "I don't know, and I'm not guessing".

### 3.7 Extract latency tuning

`templates/scripts/auto-extract.sh.tmpl` has a Level 2 idle threshold
of 60s for LLM extraction (the `note_extract`, `auto_extract`,
`reconcile` stages). Lower this to **20s** with the following
safeguards:

1. **Throttle per file**: `note_extract` already hashes notes and
   skips unchanged. Per-file cooldown: 30s minimum between extracts
   on the same file path. Prevents thrash when user is actively
   typing.

2. **`claude --print` mutex remains**: Level 2 is still gated by
   `pgrep -f "claude --print"` returning empty. The dual-instance
   freeze incident (2026-04-11) protection is unchanged.

3. **Resource guard remains**: `brain.resource_guard` CPU/load checks
   stay. Lowering idle threshold from 60s to 20s does not bypass
   resource gating.

4. **Configurable**: env override `BRAIN_EXTRACT_IDLE_LEVEL2_SEC`
   (default 20). User can raise back to 60 if their machine struggles.

Trade-off: extraction triggers more often, ~3-4× per hour vs ~1× per
hour today. CPU cost: ~3-4× as well. On a 2-core machine that's
borderline; on M-series Mac it's noise. Acceptable per design intent.

### 3.8 Doctor integration

`brain.status.claims_health()`:

```python
def claims_health() -> dict:
    return {
        "section": "Claims (knowledge layer)",
        "use_claims": os.environ.get("BRAIN_USE_CLAIMS", "0") == "1",
        "strict_mode": os.environ.get("BRAIN_STRICT_CLAIMS", "0") == "1",
        "fact_claims_total": _count_claims(),
        "fact_claims_current": _count_current_claims(),
        "fact_claims_superseded": _count_superseded(),
        "newest_claim_age_sec": _newest_claim_age(),
        "extract_idle_threshold_sec": int(
            os.environ.get("BRAIN_EXTRACT_IDLE_LEVEL2_SEC", "20")
        ),
    }
```

Reports:
- Whether flags are set
- Claim store stats (total, current, superseded)
- Newest claim age — proxy for "is extraction running?"
- Effective idle threshold

If `use_claims=True` and `newest_claim_age_sec > 600` (10 minutes),
something is likely wrong with the extraction pipeline. Doctor can
warn.

## 4. What's NOT in this MVP

| Deferred | Reason | Phase |
|---|---|---|
| Predicate type registry (subject_type, cardinality, temporality) | Existing 3-regex classifier sufficient at 412 facts | 2 |
| Projector (entity .md ← claims as authoritative source) | Existing `upsert_entity_from_file` dual-write keeps .md in sync | 2 |
| FTS5 virtual table over fact_claims | LIKE is fine at MVP scale; add when >10k claims | 2 |
| Watchdog daemon (NLI contradiction detection) | Out-of-band correction is a separate feature | 3 |
| Manifold push surface | Reactive recall is the foundation; push is built on top | 3 |
| Streaming extract daemon | Idle threshold tune from 60s → 20s captures 80% of value at 5% of complexity | revisit if 20s still too slow |
| Backfill of existing 412 facts | User explicit decision: fresh-only | (never) |

## 5. Acceptance criteria

- [ ] `brain.claims` package with `domain.py`, `read.py`, `doctor.py`
      modules, each ≤ 250 LOC.
- [ ] `BRAIN_STRICT_CLAIMS=1` without `BRAIN_USE_CLAIMS=1` raises
      `ConfigurationError` on first `brain_recall` call.
- [ ] `claims.read.search_text(query, k)` returns ranked `ClaimHit`s
      from `fact_claims WHERE status='current'` only; sub-100ms at
      1000 claims.
- [ ] `mcp_server.brain_recall` in strict mode returns the same
      envelope shape (`query`, `weak_match`, `guidance`, `hits`) as
      today's path; only `hits[].kind == "claim"` differs.
- [ ] When `claims.read.search_text` returns empty, brain_recall
      envelope reports `weak_match=True` with strict guidance text
      (mentions notes layer, no fallback). **Does NOT** consult
      entity files or notes.
- [ ] `templates/scripts/auto-extract.sh.tmpl` Level 2 idle threshold
      is 20s, env-overridable via `BRAIN_EXTRACT_IDLE_LEVEL2_SEC`.
      Per-file 30s cooldown prevents thrash.
- [ ] `brain.status.claims_health()` returns the documented dict;
      added to `brain status` output via `gather()`.
- [ ] All 803 existing tests continue to pass with default env
      (`BRAIN_USE_CLAIMS=0`, `BRAIN_STRICT_CLAIMS=0`). Strict-mode
      tests live in new test files.
- [ ] No code in `brain.claims` imports from `brain.entities`,
      `brain.semantic`, `brain.graph`, `brain.consolidation` —
      enforced by isolation test.

## 6. Non-goals + risks

**Non-goals:**
- Provide identical recall coverage to today's RRF-over-everything
  path. Strict mode is **strictly less** information until claim DB
  catches up. That's the design intent (loud failure > silent
  inaccuracy).
- Migrate users automatically. `BRAIN_STRICT_CLAIMS=1` is opt-in.

**Risks:**
- **Cold-start strict mode UX**: user enables strict, asks question
  about an entity that has 0 claims (because no new write has
  happened) → empty result. Mitigation: doctor reports current
  claim count; if low, user keeps strict off until claim DB has
  meaningful coverage.
- **Extract latency tuning false starts**: lowering 60s → 20s might
  trigger CPU spikes on slower machines. Mitigation: env override
  exists; doctor can recommend raising if CPU saturation detected.
- **fact_claims FTS scaling**: at >10k claims, LIKE may slow. We'll
  see this in `claims_health` newest-claim-age telemetry; upgrade
  to FTS5 then.

## 7. References

- Existing `_insert_fact_claim` dual-write: `src/brain/db.py:938-1011`
- Existing `use_claims_enabled()` gate: `src/brain/db.py:932-935`
- Existing extract idle gating: `templates/scripts/auto-extract.sh.tmpl`
- Existing dép-class incident lesson: `~/.claude/CLAUDE.md` "Brain grounding"
- Companion spec (foundation that this builds on): `docs/realtime-named-sessions-design.md`
