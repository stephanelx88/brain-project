# Ontologist Spec — Adaptive Predicate Vocabulary + Typed Relation Contract

> **Scope.** Implementation spec for two improvements to the brain's ontologist:
> (1) replace the hardcoded predicate whitelist with a learned, audit-promoted
> registry; (2) extend `triple_rules.py` from per-predicate confidence to
> per-`(from_type, predicate, to_type)` confidence.
>
> **Complements** `docs/ontology-improvement-plan.md` — that doc owns the M0/M1/M2
> milestone framing; this doc owns the file layouts, function signatures,
> migration steps, and tests for the two changes specifically.
>
> **Status.** Draft, 2026-04-22. Author: pair work with the agent. No code
> written yet — review this spec before implementing.

---

## 0. Why these two and not the others

From the meta-design comparison (skill `ontology` / LaminDB / brain), the brain's
killer feature is `triple_rules.py` — confidence calibration learned from user
audit. Neither competitor has it. Both proposed changes **double down on the
existing strength** (extend the learning loop) rather than bolt on something
foreign:

| Change | Surface area | Risk | Reuses brain's existing strength? |
|---|---|---|---|
| Predicate discovery loop | `graph.py`, `triple_audit.py`, new registry file | Low (additive) | Yes — same pending-queue + audit-walker pattern |
| Typed relation contract | `triple_rules.py` schema extension | Low-Medium | Yes — same JSONL ledger, just wider key |

The two are **independent** but compose: with discovery in place, typed-relation
stats can be collected for the new predicates from day one. Recommend shipping
discovery first, then typing.

---

## 1. Direction 1 — Predicate Discovery Loop

### 1.1 Problem statement

`graph.py:33` ships a hardcoded set:

```
VALID_PREDICATES = frozenset({
    "worksAt", "workedAt", "knows", "manages", "reportsTo",
    "partOf", "locatedIn", "builds", "uses", "involves",
    "relatedTo", "about", "decidedOn", "learnedFrom", "contradicts",
})
```

When LLM extraction proposes a predicate outside this set (e.g. `wrote`,
`presentedAt`, `dependsOn`), `add_triple()` returns `False` silently and the
fact is dropped. The brain has no path to learn new vocabulary except by editing
Python and shipping a release.

### 1.2 Goal

Predicates become **first-class, audit-gated entities**, mirroring how triples
themselves are gated. New predicates earn their place in the registry by
surviving 3 user confirmations within 30 days; rejected ones are remembered so
they don't re-appear in the audit queue.

### 1.3 Data model

New file: `~/.brain/identity/predicates.jsonl` (append-only).

Each row:

```json
{
  "predicate": "presentedAt",
  "status": "active",
  "confirmed": 5,
  "rejected": 0,
  "first_seen": "2026-04-22",
  "promoted_at": "2026-05-04",
  "examples": ["Son presentedAt PyVietnam2026", "..."],
  "aliases": ["presented_at", "spoke_at"]
}
```

**Status state machine:**

```
proposed ──confirm 1──> proposed ──confirm 3 within 30d──> active
   │                                          │
   │                                          └──reject 3 within 30d──> retired
   │
   └──reject 1──> proposed (still — single rejection ≠ kill)
```

`active` means `add_triple` accepts it. `proposed` means triples using it route
to the audit queue regardless of confidence. `retired` means triples using it
are silently dropped + logged to `failures.jsonl` with source=`retired_predicate`.

### 1.4 Code changes

#### `graph.py`

```python
# DELETE the hardcoded VALID_PREDICATES set.

# ADD:
def is_valid_predicate(pred: str) -> bool:
    """Active predicates only — proposed/retired are not write-allowed."""
    from brain import predicate_registry
    return predicate_registry.status(pred) == "active"

def add_triple(subject, predicate, obj, source="") -> bool:
    if not is_valid_predicate(predicate):
        # Don't drop — route to discovery queue.
        from brain import predicate_registry
        predicate_registry.observe(predicate, basis=f"{subject} {predicate} {obj}")
        return False
    # ... rest unchanged
```

#### New module `predicate_registry.py` (~120 LOC, mirrors `triple_rules.py` style)

```python
"""Registry of predicates the brain has learned to trust.

API surface (all silent-fail on disk errors, like triple_rules.py):
  observe(predicate, basis)        — first sighting → status='proposed'
  status(predicate) -> 'active'|'proposed'|'retired'|'unknown'
  record_decision(predicate, decision: 'y'|'n')  — y/n from audit walker
  list_proposed() -> list[dict]    — feed for `brain audit predicates`
  promote(predicate)               — manual override
  retire(predicate)                — manual override
  bootstrap_from_legacy()          — one-time: seed all 15 hardcoded as active
"""
```

**Promotion rule** (in `record_decision`):
- if `confirmed >= 3` and `(today - first_seen) <= 30 days` → `active`
- if `rejected >= 3` and `(today - first_seen) <= 30 days` → `retired`
- otherwise stay `proposed`

#### `triple_audit.py`

Add a second audit category. Today the walker only handles triples; extend to
also walk `proposed` predicates:

```python
def walk_predicates(limit=10, *, _input=None) -> dict:
    """Walk proposed predicates with example triples and a 'used N times' count.
    Each y/n updates predicate_registry.record_decision().
    """
```

Hook into the existing CLI: `brain audit` (currently triples-only) gains a
`brain audit predicates` subcommand. Default `brain audit` walks both, triples
first.

### 1.5 Migration

One-time bootstrap on first run after deployment:

```python
predicate_registry.bootstrap_from_legacy()
# Writes 15 rows with status=active, confirmed=0, first_seen=today,
# promoted_at=today. confirmed=0 is honest — they're grandfathered, not earned.
```

After bootstrap, behavior is identical to today (15 predicates accepted). New
ones accumulate in `proposed`.

### 1.6 Tests (`tests/test_predicate_registry.py`)

| Test | Asserts |
|---|---|
| `test_observe_creates_proposed_row` | First `observe("foo", basis)` → status `proposed`, confirmed=0 |
| `test_three_confirms_within_30d_promotes` | 3 `record_decision("foo", "y")` → status `active` |
| `test_three_confirms_outside_window_does_not_promote` | Confirms spread > 30d → still `proposed` |
| `test_three_rejects_retires` | 3 `n` decisions → status `retired` |
| `test_active_predicate_passes_add_triple` | After promotion, `graph.add_triple` returns True |
| `test_proposed_predicate_routes_to_audit` | `add_triple` with proposed predicate → False, observation logged |
| `test_retired_predicate_drops_silently_and_records_failure` | Triple with retired predicate → `failures.jsonl` row, `source=retired_predicate` |
| `test_bootstrap_seeds_legacy_15_as_active` | After `bootstrap_from_legacy`, all 15 hardcoded names are `active` |
| `test_alias_collapses_predicates` | `presented_at` and `presentedAt` resolve to same row (slug-normalized) |
| `test_walker_handles_predicates` | `walk_predicates(limit=2)` consumes 2 rows, updates ledger |

### 1.7 Acceptance

1. All 15 hardcoded predicates remain accepted post-migration (no behavior
   regression on day one).
2. After 1 week of real use, `predicates.jsonl` contains > 5 proposed rows from
   actual extraction (proves the discovery loop catches new vocabulary).
3. At least 1 promotion + 1 retirement happen organically within 30 days
   (proves the gate works).
4. `failures.jsonl` shows zero `silently_dropped_predicate` rows after week 2
   (proves nothing falls through the cracks).

---

## 2. Direction 2 — Typed Relation Contract

### 2.1 Problem statement

`triple_rules.py` calibrates confidence per *predicate* only:

```
{ "predicate": "partOf", "confirmed": 12, "rejected": 4, ... }
```

But `partOf` may be 95% accurate for `(Project, partOf, Project)` and only 40%
for `(Person, partOf, Project)`. Today both share one accuracy number (~73%) and
the LLM gets a single calibration signal.

Per the OntoKG insight already in the brain
(`brain-recall-ranking-architecture-...`), the **typed triple** `(s_type, p, o_type)`
is the right unit for statistical schema discovery.

### 2.2 Goal

Same audit ledger, wider key. Confidence calibration becomes per-typed-relation;
schema (which type combos exist for which predicate) is **discovered from data**
rather than written by hand.

### 2.3 Data model — extension, not replacement

Today's `triple_rules.jsonl` row:

```json
{ "predicate": "manages", "confirmed": 8, "rejected": 1, "examples": [...] }
```

New row format (additive — old rows stay readable):

```json
{
  "predicate": "manages",
  "from_type": "people",
  "to_type": "people",
  "confirmed": 8,
  "rejected": 1,
  "examples": ["Madhav manages Son's project review"],
  "updated": "2026-04-22"
}
```

**Type values use the brain's existing folder taxonomy** (`people`, `projects`,
`decisions`, `insights`, `issues`, `domains`, `locations`, `techniques`,
`tools`, `infrastructure`). A special token `*` means "type not extracted" —
covers legacy rows + cases where the LLM didn't tag.

### 2.4 Code changes

#### `triple_audit.py`

Pending triples already carry `subject` and `object` slugs. Extend `add_pending`
to also resolve and store types:

```python
def add_pending(triples, source=""):
    from brain.entities import resolve_type  # new helper
    for t in triples:
        s_type = resolve_type(t["subject"]) or "*"
        o_type = resolve_type(t["object"]) or "*"
        # ... existing logic, plus:
        existing.append({
            ...,
            "from_type": s_type,
            "to_type": o_type,
        })
```

`resolve_type(slug)` looks at `~/.brain/entities/<type>/<slug>.md` existence. If
multiple types match (slug collision), returns the most recently updated.

#### `triple_rules.py` — extend the key

```python
# OLD: rules are keyed by predicate alone
def record_decision(predicate, basis, decision): ...

# NEW: keyed by (predicate, from_type, to_type) triplet
def record_decision(predicate, from_type, to_type, basis, decision): ...

def adjusted_confidence(
    predicate: str,
    from_type: str,
    to_type: str,
    raw_confidence: float,
) -> float:
    """Hierarchical lookup:
       1. Exact (predicate, from_type, to_type) → use that accuracy
       2. (predicate, from_type, *) → fall back to from-type-only
       3. (predicate, *, to_type) → fall back to to-type-only
       4. (predicate, *, *) → fall back to predicate-only (legacy rows)
       5. nothing → return raw_confidence unchanged
    Only return adjusted value when total samples >= 3 at that level.
    """
```

Hierarchical fallback means **legacy rows keep working** — they read as
`(*, *)` and serve as the predicate-only baseline.

#### Migration

Legacy rows stay where they are. They're treated as `(predicate, *, *)`
rules. New audit decisions write the wider-key rows. After ~30 days the wider
rules dominate and legacy rows become base-case fallbacks. No destructive
migration needed.

### 2.5 Tests (`tests/test_triple_rules.py` — extend existing)

| Test | Asserts |
|---|---|
| `test_record_decision_keyed_by_triplet` | Two decisions for `(manages, people, people)` and `(manages, people, projects)` create two rows |
| `test_adjusted_confidence_exact_match` | Calibration uses the exact-triplet row when sample >= 3 |
| `test_adjusted_confidence_falls_back_to_partial` | Missing exact row → uses `(predicate, from_type, *)` |
| `test_adjusted_confidence_falls_back_to_predicate_only` | Missing partial row → uses legacy `(predicate, *, *)` row |
| `test_adjusted_confidence_unchanged_below_sample_threshold` | < 3 samples at every level → return raw |
| `test_legacy_rows_still_parse` | Old rows without `from_type/to_type` keys still load and serve as `(*, *)` |
| `test_md_renderer_groups_by_predicate` | Generated `triple_rules.md` groups typed variants under each predicate header (LLM prompt clarity) |

### 2.6 LLM prompt impact

`triple_rules.md` (auto-generated, injected into extraction prompt) gets a
nested format:

```
## High-confidence patterns

### manages — 89% accurate (8✓ 1✗) [aggregate]
- (people manages people) — 100% (5✓ 0✗)  e.g. "Madhav manages Son"
- (people manages projects) — 75% (3✓ 1✗)  e.g. "Son manages brain-project"

### worksAt — 94% accurate (16✓ 1✗)
- (people worksAt *) — 94% (16✓ 1✗)  ← still aggregate, not enough type data
```

This gives the LLM a **typed schema sketch** to extract against, without anyone
writing the schema by hand.

### 2.7 Acceptance

1. After 30 days of real audits, `triple_rules.jsonl` contains at least 3
   predicates with multiple typed variants (proves typed signal accumulates).
2. At least one predicate shows accuracy split > 20 percentage points between
   typed variants (proves typing changes the calibration meaningfully — if
   nothing splits, the typing layer has no value).
3. No regression on existing triple-extraction benchmark (`pytest -k triple`
   passes; mean adjusted confidence within ±5% of pre-change baseline).

---

## 3. Sequencing & rollout

| Order | Item | Why this order |
|---|---|---|
| 1 | Direction 1 (predicate discovery) | Strictly additive. No existing behavior changes for the 15 grandfathered predicates. Ship + observe for 1 week. |
| 2 | Direction 2 (typed relations) | Once new predicates can flow in, typed stats need somewhere to land. Discovery without typing wastes the new vocabulary; typing without discovery only enriches 15 predicates. |
| 3 | Re-evaluate | With both shipped, look at `predicates.jsonl` + the typed `triple_rules.jsonl` together. Decide whether to add `from_count`/`to_count` cardinality stats next (skill `ontology` pattern) or pause. |

Estimated effort: ~1 day for Direction 1 (small surface, mostly new files);
~1.5 days for Direction 2 (touches `triple_rules.py` schema, `triple_audit.py`,
extraction prompt format, plus migration tests).

---

## 4. Out of scope (deliberately)

So scope creep doesn't sneak in:

- **External vocabulary mounting** (GeoNames, Wikidata QIDs) — this is the
  Direction 4 from the meta-design discussion. Larger, conflicts with
  local-only privacy stance, defer.
- **Entity-existence guard** for triples (Direction 3) — write-path change with
  ledger-corruption risk. Worth doing later, not bundled.
- **Schema YAML file** (skill `ontology` style) — the brain's stance is
  "discover from data, don't pre-declare". Adding a YAML schema would compete
  with the discovery loop. Skip.
- **Predicate aliases beyond simple slug normalization** — if `presented_at` and
  `gave_talk_at` should merge, that's a semantic-similarity question, not a
  string question. Defer to a future autoresearch pass.
- **Cross-predicate constraints** ("if `manages`, then `knows`") — implication
  rules are SHACL-territory; out of scope until the typed-relation table has
  enough data to even propose them.

---

## 5. Open questions to decide before implementation

1. **Storage location for `predicates.jsonl`**: under `identity/` (alongside
   `triple_rules.jsonl`) or under a new `schema/` folder? Lean: `identity/` for
   consistency, but `schema/` if we later add more registry-style files.
2. **Promotion threshold (3 confirms / 30 days)**: arbitrary defaults. Should be
   `BRAIN_PREDICATE_PROMOTE_N` and `BRAIN_PREDICATE_PROMOTE_DAYS` env-overridable
   from day one (cheap), to allow tuning without code edits.
3. **Backward compat for `is_valid_predicate`**: any external caller assuming
   the 15-predicate set? Quick `rg "VALID_PREDICATES"` showed only `graph.py`
   uses it — safe to delete.
4. **Audit walker UI**: should `brain audit` show triples + predicates in one
   stream (interleaved by recency) or two passes? Lean: two passes (predicates
   first — they gate triples).

---

## 6. Cross-references

- `docs/ontology-improvement-plan.md` §M1 (Ontologist agent loop) — this spec
  fulfills the "schema mining" half of PlanSage without needing a full
  multi-agent FSM yet.
- Brain insight `eval-metric-saturation-at-060-threshold-hides-real-signal` —
  same anti-pattern this spec avoids: don't pick one number for everything.
- Brain decision `brain-fact-supersession-system-implementation` — pending
  queue pattern reused here for predicates.
- Brain insight `real-pain-point-is-ranking-gap-not-vocabulary-gap` — note: this
  spec **does add vocabulary**, but only the kind the user has implicitly
  signaled they want by surviving audit. It does not contradict the insight;
  it operationalizes the audit-gated vocabulary growth that insight implies.
