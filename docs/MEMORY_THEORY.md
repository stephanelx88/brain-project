---
title: Memory Theory for the Brain Project
audience: brain-project maintainers
date: 2026-04-19
status: design research
---

# Memory Theory for the `brain` Project

A research synthesis bridging cognitive science (how human memory actually works) and information retrieval (how machines approximate it), with concrete recommendations for the `brain` codebase at `/Users/son/Desktop/brain-project/` and the deployed vault at `/Users/son/.brain/`.

The end-goal stated in `README.md` is a persistent memory that remembers *everything* about a user and lets an LLM retrieve it the way a human accesses their own memory — **associative, contextual, temporal, episodic, zero-friction.** That goal is structurally larger than "good RAG over markdown." It requires committing to an explicit cognitive architecture and exposing each retrieval mode as a first-class tool.

This document does that in six parts.

---

## 1. Human memory model — applied to LLM brain design

Modern cognitive science treats memory not as one store but as a federation of specialised systems. The most influential decomposition comes from **Endel Tulving (1972, 1983, 2002)**, who split long-term memory into *episodic* and *semantic*; **Squire & Zola-Morgan (1991)** added *procedural* (non-declarative); and the **Atkinson–Shiffrin multi-store model (1968)** layered *sensory*, *short-term/working*, and *long-term* on top. Working memory was further refined by **Baddeley & Hitch (1974)**. The brain project today implements pieces of this taxonomy implicitly; making it explicit yields a clearer roadmap.

### 1.1 Episodic memory — "what happened, when, where, with whom"

Episodic memory stores autobiographical events with a *spatio-temporal-relational signature* (Tulving 1983). The defining quality is **mental time travel** — the ability to re-experience a past moment, not just know that it happened (Tulving 2002, "Episodic Memory: From Mind to Brain"). An episode binds together *time, place, participants, sensory cues, internal state,* and *the act of having experienced it* (autonoetic consciousness).

How this should map to the brain:

- An **episode is a row, not a file.** Where today every fact is `- text (source: session-…, YYYY-MM-DD)` parsed by `db.py:128`, an episode deserves a real table:

  ```sql
  CREATE TABLE episodes (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,            -- ISO 8601
    ended_at      TEXT,
    duration_sec  INTEGER,
    place         TEXT,                      -- 'mac/.cursor', 'iphone-notes', 'vscode-bms-will'
    project       TEXT,                      -- foreign key to entities.name where type='project'
    session_id    TEXT,                      -- harvested transcript id
    summary       TEXT,                      -- 1-3 sentence gist
    affect        TEXT                       -- optional: 'frustrated', 'productive', 'breakthrough'
  );
  CREATE TABLE episode_participants (episode_id, person_id);
  CREATE TABLE episode_facts (episode_id, fact_id);     -- which facts originated here
  CREATE TABLE episode_artifacts (episode_id, path);    -- files touched
  ```

  This gives mental time travel: any retrieval can pivot to *"the episode in which fact X was learned"*, then expand outward to the participants, place, and surrounding facts. The current `entities/insights/2026-04-18-…` files are episode-shaped but stored as semantic entities — they have lost their structured time/place/participant binding.

- The deployed vault has `~/.brain/timeline/` declared in `config.py:15` but **empty on disk** (Glob returns 0 files). Episodic memory is the missing limb.

### 1.2 Semantic memory — "facts about the world, decoupled from when learned"

Semantic memory holds context-free knowledge: meanings, concepts, and relations (Tulving 1972; Collins & Quillian 1969). It is the system that lets you know Paris is in France without remembering when or where you learned that.

This maps cleanly onto today's `entities/` folders. `entities/domains/`, `entities/clients/`, `entities/projects/` are textbook semantic memory.

**Strengths of the current implementation:**
- One markdown file per concept (good cognitive economy: a "node" has a stable address).
- Aliases (`db.py:57`) are essentially synonym links in a semantic network.
- The open-vocabulary type system (`apply_extraction.py`) lets the schema grow rather than forcing facts into a rigid ontology — a strength shared with **ACT-R's declarative chunks** (Anderson 1993).

**Gaps:**
- **No typed relations.** Facts are free text. Knowing "Hung is a person" and "Hung worked on bms-will" requires a `relations(subject_id, predicate, object_id)` table, not `- Hung worked on BMS Will (source: …)`. Without typed edges you cannot do spreading activation (§2.7) or one-hop neighbourhood queries.
- **No concept hierarchy.** "Honeywell" is a `client`, but there is nothing saying `client ⊂ organization ⊂ entity`. A semantic network needs *isa* edges (Collins & Quillian's hierarchical model).

### 1.3 Procedural memory — "how I do things"

Procedural memory stores skills and habits implicitly; it is acquired by repetition and is famously hard to verbalise (Squire 1992; Anderson's ACT-R "production rules"). In an LLM brain it covers things like *"Son writes terse one-line PR descriptions,"* *"Always run `pytest -q` before committing,"* *"Never explain code with bullet points to this user."*

Today this is encoded in:
- `identity/preferences.md`
- `identity/corrections.md` (only **2 entries** in the vault — extremely under-populated)
- `entities/corrections/_MOC.md` (3 files)

This is **the single most underdeveloped store in the brain.** Procedural memory in humans is *strengthened by use, not by retelling.* The current pipeline only captures procedural rules when the LLM explicitly emits a `corrections` block (`apply_extraction.py:109`). The richer signal — *what the user actually does, repeatedly* — is dropped on the floor by `prefilter.py` because tool calls are stripped as "noise."

Concrete enrichment path:
- Promote `corrections.md` to a structured `procedures` table: `pattern, rule, last_fired, fire_count, success_count`.
- Add **observed-routine extraction**: when 3+ episodes share a tool-sequence (e.g. "Read → Edit → run pytest → commit"), generate a candidate procedure rather than discarding the trace.
- Map to ACT-R: every procedure is an *if-then production* with a *utility* that grows with reinforced use and decays without it.

### 1.4 Working memory — "what is currently in attention"

Working memory is severely capacity-limited. **Miller (1956) "The Magical Number Seven, Plus or Minus Two"** estimated 7±2 chunks; **Cowan (2001)** revised this to ~4 chunks under genuine load; **Baddeley's** model splits it into a phonological loop, visuospatial sketchpad, and central executive.

For an LLM brain, working memory ≈ **the system prompt + conversation context + the last few MCP results.** Capacity limits matter because:

- Returning 25 facts from `brain_recall` blows past Cowan's 4-chunk effective limit; the LLM will use the first few and ignore the rest.
- The `brain_identity` tool (`mcp_server.py:120`) concatenates *all three* identity files unconditionally. As `corrections.md` grows, this will silently push older corrections out of attention.
- The `brain_get` tool returns the **entire** entity markdown — for a 20 KB project file, that consumes a quarter of Claude's available reasoning budget on one call.

The brain should treat working memory as a **token budget**, not a row count. Every retrieval tool needs an optional `max_tokens` parameter and a re-ranker that picks the highest-value subset under the budget (see §4 on cross-encoder rerankers).

### 1.5 Sensory / iconic memory — out of scope, but…

Sperling (1960) showed iconic memory holds rich visual detail for ~250 ms before decay. Echoic memory holds audio for 3–4 s. Neither is a useful target for a text brain *directly*. But the cognitive principle is: **rich, low-symbolic input that is selectively transcribed into higher stores.** The brain's analog is:

- Screenshots of dashboards / Figma / terminal panes that get OCR'd and stored as facts attached to the episode they came from.
- Voice memos (iOS Voice Memos, ChatGPT voice) transcribed to text.
- Camera roll → `gemini-vision` or `claude-3.5-sonnet` describe → episodic memory.

Recommendation: don't try to *be* sensory memory; *ingest from* sensory inputs into the episode store. The capture surface is the AUDIT.md row-3 gap.

### 1.6 Memory consolidation — sleep, hippocampus, neocortex

The dominant theory is **Standard Consolidation Theory (Squire & Alvarez 1995)** with the modern **Complementary Learning Systems** refinement (McClelland, McNaughton & O'Reilly 1995): the hippocampus rapidly encodes episodes, then *replays* them during sleep so the neocortex can integrate the regularities into stable semantic structure without catastrophic forgetting. **Diekelmann & Born (2010)** review the sleep-replay evidence.

Direct analog in the brain pipeline:

| Brain stage                   | Cognitive analog                          | Implemented? |
|------------------------------|-------------------------------------------|--------------|
| `harvest_session` → `raw/`   | Hippocampal episode trace                 | Yes          |
| `auto_extract` → entity files| Cortical integration, slow-learning store | Partial      |
| `clean.py`                   | Synaptic pruning / structural cleanup     | Yes          |
| `reconcile.py` + `_merge.py` | Schema-driven reorganisation              | Partial — duplicate slug detection only |
| **Replay-driven re-embedding**| Sleep replay reinforcing important traces | **Missing**  |
| **Forgetting curve / decay** | Synaptic depression on unused traces      | **Missing**  |

`reconcile.py` looks for *contested* and *low-confidence* facts but doesn't *do* anything with them — it only emits a markdown report. `reconcile_merge.py` only handles slug-similarity duplicates. Genuine consolidation would re-cluster facts, propose new entities when a cluster crosses a size threshold, and fold weak singletons into stronger neighbours. That is the missing nightly batch.

---

## 2. Human retrieval mechanisms — and how to expose them via MCP

Cognitive psychology distinguishes retrieval modes that today's `mcp_server.py` mostly conflates into one BM25/RRF "search." Each deserves its own tool because LLMs (like humans) pick *the wrong cue* when only one retrieval verb is offered.

The current 8 tools (`mcp_server.py:35-178`) are: `brain_search`, `brain_entities`, `brain_get`, `brain_recent`, `brain_identity`, `brain_recall`, `brain_semantic`, `brain_stats`. The mapping below shows which retrieval modes are covered, missing, or conflated.

### 2.1 Free recall — "tell me everything about X"

Free recall (Murdock 1962; Roediger & Crowder 1976) asks the system to dump everything associated with a cue with no further constraint. Quality is governed by **encoding specificity** (Tulving & Thomson 1973): the cue must overlap with how the memory was encoded.

- **Today:** `brain_recall(query, k, type)` (hybrid RRF) approximates this well.
- **Refinement:** add a *"completeness mode"* that returns *all* facts attached to matching entities, not just the top-k facts globally. Free recall is exhaustive, not ranked.

```python
brain_recall(query: str, k: int = 8, type: str | None = None,
             mode: Literal["ranked", "complete"] = "ranked") -> str
```

### 2.2 Cued recall — "the meeting where we decided…"

Cued recall provides a partial context (a date, a person, a topic) that triggers a target memory. **Tulving's encoding-specificity principle** says that the *more dimensions* the cue shares with the encoded trace, the better. A pure text query collapses cue dimensions; a structured cue does not.

- **Today:** missing. `brain_search` matches text only.
- **Proposed:**

```python
brain_cued(text: str | None = None,
           when: str | None = None,           # natural-language time range
           who: list[str] | None = None,      # people involved
           where: str | None = None,          # project / place
           kind: str | None = None,           # entity type
           k: int = 8) -> str
```

Backed by the `episodes` table (§1.1) joined with `episode_participants` and `episode_facts`.

### 2.3 Recognition — "is this already known?"

Recognition is *easier* than recall (Mandler 1980) because the cue itself is the target. In an LLM brain it answers "have we already stored this?" — vital for deduplication during ingestion and for letting Claude check before re-emitting.

- **Today:** missing as a tool. Implicitly buried in `apply_extraction._strip_existing_source_suffix` and reconcile's duplicate detector.
- **Proposed:**

```python
brain_known(fact: str, threshold: float = 0.78) -> dict
# returns {"known": bool, "nearest": [{text, score, source}], "decision": "exact|near|novel"}
```

Backed by dense-vector cosine on `facts.npy` plus an exact normalised-string check.

### 2.4 Associative chaining — "what was I thinking about right before that?"

Classical association theory (Hebb 1949: "neurons that fire together wire together") underlies the **Search of Associative Memory model (Raaijmakers & Shiffrin 1981)** and **Howard & Kahana's Temporal Context Model (2002)**, which formalise how each retrieval primes neighbours along *temporal* and *semantic* axes simultaneously.

- **Today:** missing. Nothing in the brain models *adjacency* between facts.
- **Proposed:** materialise two adjacency relations:
  - `co_occurrence(fact_a, fact_b, episode_id)` — facts that surfaced in the same session.
  - `relation(subject_entity, predicate, object_entity, source)` — typed edges.

```python
brain_associate(seed: str, hops: int = 1, k: int = 10,
                axis: Literal["temporal", "semantic", "both"] = "both") -> str
```

### 2.5 Temporal traversal — "what happened around Tuesday last week?"

Time-based retrieval has neural correlates in **time cells** in the hippocampus (Eichenbaum 2014) and is computationally captured by Howard & Kahana's TCM.

- **Today:** `brain_recent(hours, type, k)` does *forward-looking* "since N hours ago" only. There is no point-in-time, no range, no "around event X."
- **Proposed:**

```python
brain_when(start: str, end: str, focus: str | None = None,
           include: list[str] = ["episodes", "facts", "decisions"]) -> str
```

Natural-language times (`"last Tuesday"`, `"during the Honeywell sprint"`) are resolved against the `episodes` table.

### 2.6 Source monitoring — "where did I learn this?"

**Source monitoring (Johnson, Hashtroudi & Lindsay 1993)** is the cognitive operation of attributing a memory to its origin (perceived vs imagined, learnt-from-A vs learnt-from-B). It is the cognitive substrate of *citation*. It also fails in characteristic ways: people remember *facts* better than they remember *where they came from* — which is exactly the failure mode of LLM hallucination.

- **Today:** provenance lives only as a substring `(source: session-…)` inside the fact text (`db.py:81`), parsed back out by regex. There is no foreign-key relation between a fact and its source episode.
- **Proposed:** promote `source` to a real table:

```sql
CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  kind TEXT,           -- 'session', 'ingest', 'manual', 'voice', 'web'
  uri  TEXT,           -- session-id, file path, URL
  observed_at TEXT,
  confidence REAL DEFAULT 0.8
);
ALTER TABLE facts ADD COLUMN source_id INTEGER REFERENCES sources(id);
```

```python
brain_source(fact_id: int) -> dict
brain_provenance(entity: str) -> list[dict]   # all sources, sorted by recency / confidence
```

### 2.7 Tip-of-the-tongue / partial match — fuzzy recall when the cue is weak

The **TOT phenomenon (Brown & McNeill 1966)** — knowing you know without quite reaching it — is the cognitive failure mode that fuzzy and phonetic search target.

- **Today:** `db._sanitize_fts` already OR-combines tokens, which gives weak partial matching. Levenshtein appears only inside `reconcile.py:84` for slug dedup.
- **Proposed:** expose a tolerant search that combines:
  - SQLite `editdist3` extension (or pure-Python Levenshtein on entity names),
  - dense-vector recall with a low threshold,
  - character n-gram similarity on aliases.

```python
brain_fuzzy(cue: str, k: int = 8) -> str   # returns nearest entities + reasons
```

### 2.8 Spreading activation — graph-shaped recall

**Collins & Loftus (1975) "A Spreading-Activation Theory of Semantic Processing"** is the canonical model: activating a node sends decaying activation to its neighbours; intersecting waves identify the answer. It explains why "doctor → nurse" primes faster than "doctor → bread."

- **Today:** **entirely missing.** There are no edges in `db.py`. The Obsidian `graphify-out/` artefact (mentioned in AUDIT.md row-1) is read-only visualisation, not retrieval.
- **Proposed:** add a `relations` edge table (see §2.4) and a recursive CTE walk:

```python
brain_neighbors(seed: str, hops: int = 2, decay: float = 0.5,
                k: int = 15, predicate: str | None = None) -> str
```

This is the highest-value missing mechanism because it converts the brain from a *bag of cards* into a *network*.

### 2.9 Schema-based reconstruction — and how to flag confidence

**Bartlett's "War of the Ghosts" (1932)** showed that recall is *constructive* — gaps are filled with prior schemas, often confidently and wrongly. This is identical to LLM hallucination. The cognitive lesson is to **mark which parts of a memory are reconstructed vs verbatim.**

- **Today:** `source_count` and `status: contested` exist but are advisory; nothing in the retrieval path surfaces them.
- **Proposed:** every returned fact carries `confidence ∈ [0, 1]` and `evidence: ["verbatim" | "reconstructed" | "inferred"]`. The MCP wire format forces Claude to *see* uncertainty rather than assume verbatim.

### Coverage matrix vs current MCP surface

| Retrieval mode             | Cognitive ref          | Current tool                       | Status   |
|----------------------------|------------------------|------------------------------------|----------|
| Free recall                | Murdock 1962           | `brain_recall` (hybrid)            | covered  |
| Cued recall                | Tulving & Thomson 1973 | (mixed into `brain_search`)        | conflated|
| Recognition                | Mandler 1980           | —                                  | missing  |
| Associative chaining       | Raaijmakers & Shiffrin | —                                  | missing  |
| Temporal traversal         | Howard & Kahana 2002   | `brain_recent` (one-sided)         | partial  |
| Source monitoring          | Johnson et al. 1993    | regex on fact text                 | weak     |
| Tip-of-the-tongue          | Brown & McNeill 1966   | weak (FTS OR-tokens)               | partial  |
| Spreading activation       | Collins & Loftus 1975  | —                                  | missing  |
| Schema reconstruction flag | Bartlett 1932          | `source_count` (not surfaced)      | weak     |

Three modes are completely missing: **recognition, associative chaining, spreading activation.** These are the headline gaps.

---

## 3. Forgetting and salience

Most "AI memory" systems hoard. Humans don't, and that's a feature.

### 3.1 The forgetting curve

**Ebbinghaus (1885)** measured his own retention of nonsense syllables and found a roughly exponential decay: $R(t) = e^{-t/S}$, where $S$ is the strength of the trace. **Murre & Dros (2015)** replicated him with modern controls and confirmed the shape.

Modern refinements:
- **Bjork & Bjork's New Theory of Disuse (1992):** memories have separable *storage strength* (long-term durability) and *retrieval strength* (current accessibility). Forgetting reduces retrieval strength while leaving storage strength intact, which is why re-learning is faster than first learning.
- **Spaced-repetition algorithms** (SuperMemo SM-2, Anki, FSRS) operationalise this: schedule the next review near the predicted forgetting threshold to maximise the retrieval-strength gain per review.

### 3.2 Why forgetting improves retrieval

A retrieval system without forgetting suffers signal-to-noise collapse:
- **Anderson's fan effect (Anderson 1974):** the more facts associated with a cue, the slower and less accurate retrieval becomes. Pruning the irrelevant ones is *not* lossy in any practical sense — it improves the precision of the surviving ones.
- **Information theory:** if low-salience traces have $p \to 1$ (always present), they carry zero information ($-\log p \to 0$) and dilute every result.

The current brain has no decay. After 12 months it will be dominated by trivia.

### 3.3 Salience heuristics

Salience should be a derived score combining:

| Signal              | Cognitive basis                                | Computable from current schema?            |
|---------------------|------------------------------------------------|--------------------------------------------|
| **Recency**         | Forgetting curve                               | Yes — `last_updated`, `fact_date`          |
| **Frequency**       | Hebbian strengthening                          | Partial — `source_count` only at entity    |
| **Re-access**       | Testing effect (Roediger & Karpicke 2006)      | **No** — no read counter                   |
| **Surprise**        | Information gain $-\log p(\text{fact})$        | No — needs background language model       |
| **Emotional weight**| Amygdala-mediated (Cahill & McGaugh 1998)      | No — would need explicit `affect` tag      |
| **Pin (manual)**    | Deliberate rehearsal                           | No — no pin column                         |

### 3.4 Implementation

Add a `salience` column to `entities` and `facts`:

```sql
ALTER TABLE facts ADD COLUMN salience REAL DEFAULT 0.5;
ALTER TABLE facts ADD COLUMN last_accessed TEXT;
ALTER TABLE facts ADD COLUMN access_count INTEGER DEFAULT 0;
ALTER TABLE facts ADD COLUMN pinned INTEGER DEFAULT 0;
```

Maintenance loop (nightly cron, alongside `clean.py`):

```python
# pseudo
for fact in all_facts:
    age_days = days_since(fact.fact_date or fact.created_at)
    decay   = exp(-age_days / 90)              # 90-day half-life-ish
    boost   = log1p(fact.access_count) * 0.2
    pin     = 1.0 if fact.pinned else 0.0
    fact.salience = clamp(0.1*pin + 0.6*decay + boost, 0, 1)
```

Re-ranking: every retrieval multiplies its similarity score by `(0.5 + 0.5 * salience)` so high-salience facts rise without hard-truncating low-salience ones.

### 3.5 Manual controls vs automatic decay

Both. ACT-R and FSRS converge on the same conclusion: automatic decay with manual override is more robust than either alone. Tools:

```python
brain_pin(fact_id_or_query: str)       # force salience = 1.0, exempt from decay
brain_forget(fact_id_or_query: str)    # tombstone, then physically delete after grace period
```

### 3.6 Tombstoning vs hard delete

Three legal/operational regimes coexist:

1. **Soft delete (tombstone):** `facts.deleted_at = NOW()`. Excluded from retrieval; recoverable for ~30 days.
2. **Hard delete:** physical `DELETE` from SQLite *and* removal from FTS5, vector store, git history (`git filter-repo`).
3. **GDPR Art. 17 "right to erasure":** on user request, must be hard-deletable across all stores within 30 days. The brain's git history makes this non-trivial — every commit references the now-deleted file. A `brain_purge(name)` tool that runs `git filter-repo` is the cleanest answer; document it as the legally compliant path.

---

## 4. Information retrieval techniques that approximate human recall

Each IR technique should be chosen because it *cognitively* maps to a retrieval mechanism, not because it is fashionable.

| IR technique          | Cognitive analog                          | Why the mapping holds |
|-----------------------|-------------------------------------------|------------------------|
| **BM25 / TF-IDF**     | Cue-overlap free recall                   | Both reward rare-token overlap between cue and target — a direct numerical analog of Tulving's encoding-specificity. Robertson & Walker (1994). |
| **Dense embeddings** (sentence-transformers, E5, BGE) | Semantic similarity / spreading activation in concept space | Cosine in embedding space approximates conceptual proximity — the geometry that Collins & Loftus drew as a graph. Karpukhin et al. DPR (2020). |
| **Cross-encoder rerankers** (`bge-reranker`, `ms-marco-MiniLM-L-6-v2`) | Deliberation / source monitoring | Models that read query+document jointly perform the *post-recall verification* humans do when they're confident enough to commit. Nogueira & Cho (2019). |
| **HNSW / IVF-PQ**     | None — pure scaling                       | Necessary above ~50K facts; until then numpy brute-force (current `semantic.py`) wins on simplicity. Malkov & Yashunin (2018). |
| **Hybrid (RRF, weighted sum)** | Multi-trace memory (Hintzman 1986) | Multiple traces with different cue strengths combine non-linearly. RRF = $\sum 1/(K + \text{rank}_i)$ is the Bayes-optimal late fusion when score distributions differ across systems (Cormack, Clarke & Büttcher 2009). Already in `semantic.hybrid_search`. |
| **Knowledge graph traversal** | Spreading activation (literal) | Graph BFS with edge-weighted decay = Collins & Loftus, mechanically. |
| **Late interaction (ColBERT v1/v2)** | Sub-symbolic feature matching | Per-token MaxSim mimics how humans match memory traces feature-by-feature rather than gist-by-gist. Khattab & Zaharia (2020). |
| **SPLADE / learned-sparse** | Cue-and-meaning combined | Sparse vectors with neural term weighting fuse BM25's interpretability with dense recall. Formal et al. (2021). |
| **Time-decay re-ranking** | Forgetting curve                          | Multiplying score by `exp(-age/τ)` is literally Ebbinghaus. |
| **Query expansion** (RM3, HyDE, doc2query, LLM rewrite) | Priming                                  | Expanding the cue with related terms before retrieval is the engineering analog of associative priming (Meyer & Schvaneveldt 1971). |
| **Personalisation re-ranking** | Encoding specificity at the user level    | Conditioning on user profile is encoding-specificity made explicit. |

### Concrete recommendation for `brain`

The current stack — FTS5 BM25 + sentence-transformers dense + RRF hybrid — is the right *floor*. Next steps, ordered by ROI vs cost:

1. **Add a cross-encoder reranker** over the top-30 RRF candidates, returning the top-8 to the LLM. Use `BAAI/bge-reranker-base` (~280 MB, CPU-runnable in <50 ms for 30 pairs). This single addition typically lifts MRR by 5–15 points on hybrid systems and is the cognitive analog of *deliberation*. **No new heavy dep** — already pulls in via `sentence-transformers`.

2. **Add time-decay re-ranking** to `semantic.hybrid_search`. One-line change: multiply RRF score by `exp(-age_days/180)` for non-pinned facts.

3. **Add knowledge-graph edges** + a `brain_neighbors` tool. Use SQLite recursive CTEs — no new dep. Edges populated by extraction (the LLM already names co-occurring entities).

4. **Defer ColBERT/SPLADE** until corpus > 100K facts. They are excellent but demand torch-on-every-query and an HNSW index; that crosses the *minimal-deps* line set in `semantic.py:1-12`.

5. **Defer HNSW** until brute-force exceeds 50 ms (currently <5 ms at 3K facts). When it does, `hnswlib` is the right pick — a single C++ wheel, no GPU.

Cost / latency budget summary:

| Stage                | Latency on M-series | Memory  | New dep?               |
|----------------------|---------------------|---------|------------------------|
| FTS5 BM25            | <2 ms               | 0       | none                   |
| Dense (numpy cosine) | <5 ms @ 3K facts    | 5 MB    | none (have it)         |
| RRF fusion           | <1 ms               | 0       | none (have it)         |
| Cross-encoder rerank | ~30 ms / 30 pairs   | 280 MB  | bge-reranker (recommended) |
| Time-decay rescore   | <1 ms               | 0       | none                   |
| Graph neighbour walk | <10 ms / 2 hops     | 0       | none                   |

Total budget: still under 50 ms for the recommended path, well within the design target stated in `mcp_server.py:18`.

---

## 5. The "everything about a user" problem

To remember everything, the brain needs an explicit ontology — not as a rigid schema (the current open-vocabulary approach is correct) but as a **top-level taxonomy that retrieval can pivot on.** Eight top-level domains, each mappable to existing or new entity-type folders:

| Top-level domain | What it stores                                                | Today's `entities/<type>` mapping | Gap |
|------------------|---------------------------------------------------------------|-----------------------------------|-----|
| **self**         | Identity, preferences, values, goals, biographical facts      | `identity/who-i-am.md`, `identity/preferences.md` | No `goals/` or `values/`; no longitudinal self-history |
| **relationships**| People, organizations, roles, familial/professional ties      | `entities/people/`, `entities/clients/` | No `organizations/` distinct from `clients/`; no role history; no relation edges between people |
| **activity**     | Projects, tasks, decisions, events, locations                 | `entities/projects/`, `entities/decisions/`, `entities/issues/` | No `tasks/`, `events/`, or `locations/`; no episode rows |
| **knowledge**    | Domains, facts, references, sources                           | `entities/domains/`, `entities/insights/` | No `references/` (papers/books/URLs); sources only inline |
| **artifacts**    | Files, code, documents, media                                 | — | **Entirely missing** — files referenced in facts are never first-class. No `artifacts/` folder. |
| **behavior**     | Corrections, patterns, routines, habits                       | `entities/corrections/` (3 files), `identity/corrections.md` | No `routines/` or `patterns/`; tool-sequences dropped by prefilter |
| **temporal**     | Timelines, recurring events, milestones                       | `timeline/` declared in config, **empty on disk** | Episode store missing; no recurrence model |
| **meta**         | Provenance, confidence, last-verified, salience               | `source_count`, `status` in frontmatter | No `confidence`, no `salience`, no `last_verified`, no source table |

Today's vault has nine entity types: `people, projects, clients, domains, decisions, issues, insights, evolutions, corrections`. Mapping to the proposed taxonomy:

- **Well placed:** people → relationships; projects/decisions/issues → activity; domains/insights → knowledge; corrections → behavior; clients → relationships.
- **Awkwardly placed:** `evolutions/` straddles temporal+meta (it's "how things changed" — a meta-narrative). Recommend moving it under `temporal/` with a `kind: evolution` discriminator.
- **Missing folders:** `artifacts/`, `events/`, `tasks/`, `routines/`, `references/`, `goals/`, `locations/`, `organizations/` — each is a distinct cognitive bucket and each is referenced *implicitly* in current facts.

The fix is not to mandate these folders up front (open-vocabulary is right), but to **prompt the extractor to use them** — update `prompts/extract_batch.md` to suggest the taxonomy and let it choose.

---

## 6. Concrete recommendations for the `brain` project

Ranked by *cognitive leverage per unit of engineering effort.* Each item names actual files, sizes the work, and notes interaction with the in-flight FTS5 / batched-extraction / MCP work.

### R1. Episode store (`episodes` table + `brain_when` tool) — **Effort: M**

- **Principle:** Tulving's episodic/semantic split. Without this, every other temporal/cued-recall mechanism is broken.
- **Files:** new `src/brain/episodes.py`; extend `db.py` schema; emit episode rows from `harvest_session.py:271` (one per harvested session); new MCP tool in `mcp_server.py`.
- **Deps:** none.
- **Interactions:** complements batched extraction — the episode is the unit batched, not the fact. Empties out `~/.brain/timeline/` properly. Works alongside FTS5 (episodes can have their own FTS index for summary text).

### R2. Relations table + `brain_neighbors` (spreading activation) — **Effort: M**

- **Principle:** Collins & Loftus 1975. Converts the brain from a list to a network.
- **Files:** extend `db.py` with `relations(subject_id, predicate, object_id, source_id, weight)`; new `brain.semantic.expand_neighbors()`; new MCP tool. Extraction prompt change in `prompts/extract_batch.md` to emit `relations: [...]` alongside `entities: [...]`.
- **Deps:** none. SQLite recursive CTEs.
- **Interactions:** entity write-through (`db.upsert_entity_from_file`) extends to relations.

### R3. Salience column + decay loop — **Effort: S**

- **Principle:** Ebbinghaus + Bjork. Forgetting improves retrieval.
- **Files:** schema change in `db.py`; new `brain.consolidate.decay_pass()` invoked by `bin/auto-extract.sh` once per night; multiplicative re-ranking in `semantic.hybrid_search`.
- **Deps:** none.
- **Interactions:** runs in the same cron window as `clean.py`. No effect on extraction throughput.

### R4. Cross-encoder reranker — **Effort: S**

- **Principle:** post-recall deliberation; lifts precision at low k.
- **Files:** new `brain.rerank` module; called from `mcp_server.brain_recall` after RRF returns top-30.
- **Deps:** uses already-loaded `sentence_transformers`; downloads `BAAI/bge-reranker-base` on first use.
- **Interactions:** keeps the 50 ms budget. Optional via env var so it can be disabled on cold-start-sensitive paths.

### R5. Promote source to a real table — **Effort: M**

- **Principle:** source monitoring (Johnson et al. 1993). Provenance is a relation, not a substring.
- **Files:** schema change in `db.py`; backfill from existing `(source: …)` strings in facts; new MCP tool `brain_provenance`. Update `apply_extraction.py` to insert into `sources` rather than concatenating into fact text.
- **Deps:** none.
- **Interactions:** fact text becomes cleaner → BM25 indexes improve. Migration is one-shot SQL + regex on existing markdown.

### R6. Procedural-memory upgrade — **Effort: M**

- **Principle:** ACT-R productions; testing/spacing effects on rules.
- **Files:** new `entities/routines/` type; new `brain.procedures` module; modify `prefilter.py` to *retain* tool sequences rather than strip them; nightly job to detect 3+ repeated sequences and propose `routines` entries.
- **Deps:** none.
- **Interactions:** changes the prefilter's "noise" definition — coordinate with batched-extraction throughput tests (might add ~10–20 % to prompt size; mitigated by truncation rules).

### R7. Recognition tool (`brain_known`) — **Effort: S**

- **Principle:** recognition < recall in difficulty; cheap dedup primitive.
- **Files:** new MCP tool calling `semantic.search_facts` with high-threshold dense match + exact normalised-string check. Reuses existing index.
- **Deps:** none.
- **Interactions:** also callable internally from `apply_extraction` to skip near-duplicate fact appends, reducing the `(source: A) (source: B)` fix-up that `clean.collapse_double_sources` does today.

### R8. Working-memory budget on every retrieval — **Effort: S**

- **Principle:** Cowan's 4±1; LLM context ≠ infinite.
- **Files:** add `max_tokens` arg to all MCP tools; truncate or summarise within budget. `brain_get` learns to return a *digest* by default, *full* on demand.
- **Deps:** none (tiktoken-style estimator can be byte-based).
- **Interactions:** no negative interaction; mostly UX improvement for the LLM.

### R9. Schema-confidence wire format — **Effort: S**

- **Principle:** Bartlett 1932. Mark reconstruction.
- **Files:** every retrieval result returns `confidence`, `evidence`, `last_verified`. Compute confidence from `source_count`, salience, and (eventually) cross-encoder agreement.
- **Deps:** none.

### R10. Capture-surface expansion (artifacts, voice, screenshots) — **Effort: L**

- **Principle:** Sperling — sensory inputs feed episodic memory.
- **Files:** new `src/brain/capture/` package — start with one source (e.g. macOS `screencapture` cron + Vision OCR) before broadening. AUDIT.md row 2 names the gap.
- **Deps:** local OCR (`macocr` shell-out, no Python deps), Whisper for voice (heavy — make optional).
- **Interactions:** every captured artefact must land in an *episode*; depends on R1.

### R11. Consolidation-driven re-clustering — **Effort: L**

- **Principle:** McClelland-McNaughton-O'Reilly Complementary Learning Systems.
- **Files:** `reconcile.py` extended with embedding-based clustering; promote tight clusters to new entities, fold weak singletons. Nightly.
- **Deps:** uses existing dense vectors + simple agglomerative clustering (`numpy` only).
- **Interactions:** moderately destructive; gate behind dry-run + diff like `reconcile_merge.py` already does.

### R12. Personalisation / encoding-specificity layer — **Effort: M**

- **Principle:** Tulving & Thomson 1973 at the user level — re-rank by user profile.
- **Files:** small score boost when retrieved facts share entities with `who-i-am.md` / `preferences.md`; per-project bias when called inside a project context.
- **Deps:** none.

---

## Appendix A — Quick reference: cognitive sources cited

- Atkinson, R. C., & Shiffrin, R. M. (1968). Human memory: A proposed system and its control processes.
- Anderson, J. R. (1974). Retrieval of propositional information from long-term memory. *(Fan effect.)*
- Anderson, J. R. (1993). *Rules of the Mind* (ACT-R).
- Baddeley, A. D., & Hitch, G. (1974). Working memory.
- Bartlett, F. C. (1932). *Remembering*. *(Schema theory.)*
- Bjork, R. A., & Bjork, E. L. (1992). A new theory of disuse.
- Brown, R., & McNeill, D. (1966). The "tip of the tongue" phenomenon.
- Cahill, L., & McGaugh, J. L. (1998). Mechanisms of emotional arousal and lasting declarative memory.
- Collins, A. M., & Loftus, E. F. (1975). A spreading-activation theory of semantic processing.
- Collins, A. M., & Quillian, M. R. (1969). Retrieval time from semantic memory.
- Cowan, N. (2001). The magical number 4 in short-term memory.
- Diekelmann, S., & Born, J. (2010). The memory function of sleep.
- Ebbinghaus, H. (1885). *Über das Gedächtnis*.
- Eichenbaum, H. (2014). Time cells in the hippocampus.
- Hebb, D. O. (1949). *The Organization of Behavior*.
- Hintzman, D. L. (1986). MINERVA 2: A simulation model of human memory.
- Howard, M. W., & Kahana, M. J. (2002). A distributed representation of temporal context.
- Johnson, M. K., Hashtroudi, S., & Lindsay, D. S. (1993). Source monitoring.
- Mandler, G. (1980). Recognizing: The judgment of previous occurrence.
- McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems.
- Meyer, D. E., & Schvaneveldt, R. W. (1971). Facilitation in recognizing pairs of words. *(Priming.)*
- Miller, G. A. (1956). The magical number seven, plus or minus two.
- Murdock, B. B. (1962). The serial position effect of free recall.
- Murre, J. M. J., & Dros, J. (2015). Replication and analysis of Ebbinghaus' forgetting curve.
- Raaijmakers, J. G. W., & Shiffrin, R. M. (1981). Search of associative memory.
- Roediger, H. L., & Karpicke, J. D. (2006). The testing effect.
- Sperling, G. (1960). The information available in brief visual presentations.
- Squire, L. R. (1992). Declarative and nondeclarative memory.
- Squire, L. R., & Alvarez, P. (1995). Retrograde amnesia and memory consolidation.
- Tulving, E. (1972). Episodic and semantic memory.
- Tulving, E. (1983). *Elements of Episodic Memory*.
- Tulving, E. (2002). Episodic memory: From mind to brain.
- Tulving, E., & Thomson, D. M. (1973). Encoding specificity and retrieval processes.

## Appendix B — Quick reference: IR sources cited

- Robertson, S. E., & Walker, S. (1994). Some simple effective approximations to the 2-Poisson model. *(BM25.)*
- Karpukhin, V., et al. (2020). Dense Passage Retrieval for Open-Domain Question Answering.
- Khattab, O., & Zaharia, M. (2020). ColBERT: Efficient and effective passage search via contextualized late interaction.
- Santhanam, K., et al. (2022). ColBERTv2.
- Formal, T., et al. (2021). SPLADE.
- Nogueira, R., & Cho, K. (2019). Passage re-ranking with BERT.
- Cormack, G. V., Clarke, C. L. A., & Büttcher, S. (2009). Reciprocal rank fusion outperforms Condorcet and individual rank learning methods.
- Malkov, Y., & Yashunin, D. (2018). Efficient and robust approximate nearest neighbor search using HNSW.
