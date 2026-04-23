# Brain 10x Plan

Canonical engineering plan for the 10x initiative. Source of truth for workstream IDs, gates, owners, dependencies. Team coordination and open seams live in `~/.brain/.teams/2026-04-23-brain-100x/` (not checked in).

**Status**: PM-signed 2026-04-23 13:30.
**Axes**: accuracy (A), speed (S), token efficiency (T), ingest autonomy (I).

## TL;DR

Ten workstreams. **WS1 is the hard gate** — no retrieval-side PR merges without a benchmark delta. WS2–WS5 are independently shippable quick wins (token, speed, ingest autonomy, security fence). WS6 ships the reified-fact substrate. WS7a/b/c are the recall-quality levers with distinct gates. WS8 is the brain-likeness capstone.

Ship order: **WS1 this week → WS2 immediately after**. WS4 (scrubber) and WS5 (MCP split) run in parallel with WS1. WS7b ships earliest of the WS7 family (WS1-gated only, no WS6 dependency).

## Workstreams

### WS1 — Golden set + benchmark in CI   `[A,S]`
**Owner**: Architect
`tests/golden/recall.yaml` ≥ 50 queries checked into the repo. `pytest -m bench` runs `brain.benchmark.run_benchmark`; merge fails if `p@1` regresses > 2 pp vs. main. Nightly cron writes `~/.brain/bench/YYYY-MM-DD.json`; `brain_status` surfaces the latest headline. Seeded from `recall_metric.DEFAULT_EVAL_QUERIES`, `top_miss_queries()` over the last 14 days, and the CLAUDE.md regression corpus (đôi-dép class, Thuha subject-conflation). Also extends `run_benchmark` to score `expected_weak_match: true` anchors for WS7a.
**Depends on**: none.
**Blocks**: WS6, WS7a, WS7b, WS7c, WS8.

### WS2 — Compact MCP envelope + canonical-hash dedup   `[T,S]`
**Owner**: Ontologist (field spec) + Architect (wire-format)
Default projection of `brain_recall / search / semantic / entities / notes` reduced to `{id, kind, name, path, text, entity_summary, score}`. No indent. `text` capped at `BRAIN_RECALL_SNIPPET=240`. `rrf / sem_score / lexical_rank / semantic_rank / source / date / status / top_score / threshold` move behind `verbose=true` / `debug=true`. Canonical-fact-hash dedup runs before truncation to `k`; duplicates collapse to a single hit with `seen_in:[entity,…]`. `id` = `kind:type/slug` (matches `benchmark.hit_identifier`).
**Impact**: ≥ 50% byte cut at k=8; honest `k` (no duplicates); Security's three-condition redaction landed without a separate allowlist.
**Depends on**: none (orthogonal to storage). Re-scored by WS1 after it lands.

### WS3 — Freshness: watermark + watcher daemon   `[S,I]`
**Owner**: Architect
(a) Replace `mcp_server._ensure_fresh()`'s unconditional triple-sweep with an mtime watermark per source dir (`raw/`, notes root, `entities/`), persisted in `.brain/.freshness.json`. (b) `inotifywait` (Linux) / `fswatch` (macOS) daemon calls `ingest_notes.ingest_one` + incremental `semantic.index_new` within ~1 s of a note/entity change. Fold under the existing scheduler: `brain-watcher.service` / `com.son.brain-watcher.plist`. Expensive paths (session extract) stay on the `auto_extract` tick.
**Impact**: warm-path recall FS overhead 10–40 ms → sub-ms. Note-save → queryable 60–180 s → ≤ 2 s.
**Depends on**: none. Audit-event shape agreed with Security in WS5.

### WS4 — Pre-LLM scrubber + injection tripwire   `[I,A]`
**Owner**: Security
`brain.sanitize` module called from `prefilter.filter_session_text`, `note_extract.extract_from_note`, `ingest_notes.upsert_note`. Three passes: regex+entropy secret scrub → `[redacted:KIND:sha8]`; prompt-injection tripwire (`ignore previous`, `system:`, `<|`, identity-rewrite attempts); per-line length budget with elision.
**Impact**: eliminates drive-by fact-poisoning; 2–3x smaller extractor input on heavy tool-traffic days.
**Depends on**: none.
**Blocks**: WS8.

### WS5 — Read/write MCP split + audit ledger   `[I,A]`
**Owner**: Security (+ Architect for `brain install` wiring)
Split the MCP surface into two servers.
- **Read**: `brain_recall / search / semantic / entities / notes / identity / recent / stats / status / history / live_sessions / live_tail / live_coverage / audit / learning_gaps / get / note_get / failure_list / tombstones`.
- **Write**: `brain_remember / brain_note_add / brain_retract_fact / brain_correct_fact / brain_forget / brain_mark_reviewed / brain_mark_contested / brain_resolve_contested / brain_failure_record`.

Read server registers on every Claude/Cursor host. Write server registers only where `BRAIN_WRITE=1`. Every write tool appends a hash-chained entry to `.brain/.audit/ledger.jsonl`. No compat shim; `brain doctor` refuses to proceed if wiring is absent on a host that needs it.
**Depends on**: none.
**Breaking**: hosts wired to the old single server must be re-wired; migration note in `brain install`.

### WS6 — Reified `fact_claims` + `facts` VIEW   `[A]`
**Owner**: Ontologist (schema) + Architect (migration, VIEW, read-path)
Additive SQLite migration.
- `fact_claims(id, predicate, subject_slug, object_text, object_slug, fact_time, observed_at, confidence, salience, episode_id, trust_level, source{kind,path,sha,scrub_tag}, status, superseded_by)`.
- Dual-write from `apply_extraction` for ≥ 1 week.
- Flip read path under `BRAIN_USE_CLAIMS=1`; `facts` becomes a `VIEW` over `fact_claims WHERE status='current'` so `db.py / semantic.py / mcp_server.py` stay unchanged.
- Predicate `group` lives as an optional field on existing `predicate_registry.jsonl` rows. Cold-path LLM classifier fills `group` at extract time, replacing the three regexes in `supersede.py`.

**Impact**: predicate-aware supersession, canonical-key dedup, `salience` decay, and unblocks WS7a + WS8.
**Depends on**: WS1 (no read-path regression after the flip).

### WS7a — Subject-filter hard reject at recall   `[A]`
**Owner**: Ontologist (parse spec) + Architect (integration in `hybrid_search`)
Parse the query for possessives (`tôi`, `my`, `của tôi`, `nhà tôi`, plus proper nouns) → resolve to subject slug(s) → drop hits whose `fact_claims.subject_slug` ≠ query subject. Hard filter, not score.
**Impact**: `weak_match=True` on ≥ 90% of subject-mismatch queries (vs. today's ~0%). Kills the đôi-dép-2026-04-21 class structurally.
**Depends on**: WS1 (bench must score `expected_weak_match` anchors), WS6 (`subject_slug` column).

### WS7b — Rewriter + reranker default-on   `[A]`
**Owner**: Architect
Flip `BRAIN_QUERY_REWRITE=1` and `BRAIN_RERANK=1` as defaults iff WS1 shows `p@1 ≥ baseline + 3 pp` AND `p50 latency ≤ baseline + 200 ms`. Per-query timeout; silent hybrid fallback on any LLM failure.
**Impact**: realises already-built but never-measured recall uplift.
**Depends on**: WS1 only. Ships **earliest** of the WS7 family.

### WS7c — sqlite-vec HNSW backend   `[S]`
**Owner**: Architect
Swap `semantic.py`'s brute-force numpy cosine for `sqlite-vec` HNSW behind `BRAIN_VEC_BACKEND=hnsw`. Numpy remains the automatic fallback. If WS1 shows HNSW recall drifts > 1 pp, auto-fallback.
**Impact**: retrieval scales past ~100K facts without visible P95 growth.
**Depends on**: WS1 only.

### WS8 — Idle consolidation + alias canonicalisation   `[A, brain-likeness]`
**Owner**: Ontologist (promotion/decay rubric) + Architect (worker, scheduler, budget)
Low-priority LLM worker off the existing `semantic_worker` socket.
- (a) Episodic→semantic promotion on (i) ≥ 2 independent episodes, (ii) matching `(subject_slug, predicate)`, (iii) no contested sibling. Episodic `salience` decays; semantic doesn't.
- (b) Alias canonicalisation: cheap LLM groups recent `object_text` values, writes aliases, requeues facts.

Token budget: `BRAIN_CONSOLIDATE_DAILY_BUDGET_TOK=25000` (≈ $0.05/day Haiku; env-var so it's tunable without a deploy).
**Impact**: consolidation during idle, forgetting curve, cue-based reconstruction. ≥ 20% MRR lift on repeat queries expected.
**Depends on**: WS4 (scrubber must be in-path), WS6.

## Build order

```
Phase 0  [GATE]
  WS1   Golden set + bench in CI

Phase 1  [parallel — independently shippable]
  WS2   Compact MCP envelope + dedup
  WS3   Freshness (watermark + watcher)
  WS4   Pre-LLM scrubber + tripwire
  WS5   Read/write MCP split + audit ledger

Phase 2  [substrate + recall-path levers]
  WS6   Reified fact_claims + VIEW
  WS7b  Rewriter + reranker default-on     (WS1-gated only)
  WS7c  sqlite-vec HNSW                    (WS1-gated only)
  WS7a  Subject-filter hard reject         (WS1 + WS6)

Phase 3  [capstone]
  WS8   Idle consolidation + aliases       (WS4 + WS6)
```

Critical path: **WS1 → WS6 → WS8**.

## Tag coverage

| Axis | Workstreams |
|---|---|
| Accuracy | WS1, WS2 (dedup), WS4, WS6, WS7a, WS7b, WS8 |
| Speed | WS1, WS2, WS3, WS7c |
| Token | WS2 (primary); WS4 (side-effect) |
| Ingest autonomy | WS3b, WS4, WS5 |
| Brain-likeness | WS8 |
