---
title: SOTA Memory — State of the Art in AI / "Second-Brain" Systems
date: 2026-04-19
audience: brain project maintainers (Son)
status: living document
sources: web search + hands-on read of brain v0 source
---

# SOTA Memory — State of the Art in AI / "Second-Brain" Systems

> **Thesis.** As of April 2026, the AI-memory field has converged on a small set
> of working architectural primitives: **(1)** an LLM-driven extract→consolidate→retrieve
> pipeline, **(2)** hybrid retrieval (BM25 + dense vector + optional graph traversal,
> fused with RRF, optionally cross-encoder reranked), **(3)** bi-temporal facts with
> validity windows, **(4)** an episodic / semantic / procedural memory split, and
> **(5)** sleep-time / off-line consolidation. Almost every credible system —
> mem0, Letta, Zep/Graphiti, Cognee, Pieces, Basic Memory — implements some
> permutation of these. The differentiators are *capture surface* (how data
> enters), *storage substrate* (markdown vs DB vs graph), and *trust model*
> (cloud vs local). `brain` is already on the right side of three of those five
> axes; this document maps what's missing.

This document has five parts:

- **A.** Survey of 14 production / research systems, with mechanism-level detail.
- **B.** Architectural patterns extracted across the field, with implementation notes.
- **C.** Capability matrix: where `brain` sits today vs the field, vs best-in-class.
- **D.** Concrete, ranked recommendations for `brain` (10 items, S/M/L effort).
- **E.** Things to deliberately *not* adopt, with justification.
- **References.** All cited URLs, with retrieval date.

---

## Reader's note on `brain` today

For grounding, the current architecture is:

- **Capture.** A `SessionStart` hook scans `~/.claude/projects/**/*.jsonl`, writes
  per-session summaries to `~/.brain/raw/`. Free-text mid-session "ingest this
  file" command for explicit injection. (`harvest_session.py`, `ingest.py`.)
- **Extract.** Haiku (or claude CLI fallback) turns each raw session into JSON
  `{entities[], corrections[]}` against a free-form schema. Batched up to 10
  sessions per LLM call. (`auto_extract.py`, `prompts/extract_*.md`.)
- **Apply.** A single mutator writes/updates entity markdown files under
  `~/.brain/entities/<type>/<slug>.md`, rebuilds `index.md`, commits to git.
  (`apply_extraction.py`, `entities.py`, `git_ops.py`.)
- **Index.** SQLite mirror with FTS5 over facts and entity names. Lazy
  rebuildable from markdown. (`db.py`.)
- **Semantic.** Numpy + `sentence-transformers/all-MiniLM-L6-v2` (384-d), brute
  cosine, RRF fusion with BM25. (`semantic.py`.)
- **Surface.** MCP server exposes `brain_search`, `brain_recall` (hybrid),
  `brain_semantic`, `brain_get`, `brain_recent`, `brain_identity`,
  `brain_stats`. Sub-50 ms reads. (`mcp_server.py`.)
- **Substrate.** Markdown is the source of truth; SQLite + numpy are
  rebuildable caches. The whole vault is a git repo at `~/.brain/`.

This puts `brain` architecturally closest to **Basic Memory** (markdown +
MCP, local-first), with retrieval mechanics closer to a stripped-down **mem0**
(LLM extract → hybrid recall) and capture mechanics closer to **claude-mem**
(SessionStart hook → compressed summary). It has none of the bi-temporal /
graph / consolidation machinery that distinguishes Zep/Graphiti or A-MEM.

---

## A. Survey of Systems

Each entry: **stack | retrieval | capture | killer feature | weakness | license/repo**.

### 1. mem0 (mem0.ai)

- **Stack.** Three-stage streaming pipeline: *Extraction* (LLM over last ~10 messages), *Update/Consolidation* (LLM decides ADD / UPDATE / DELETE / NOOP against semantically similar prior memories), *Retrieval* (multi-signal). Storage is a triple: vector store + graph store (entities/edges) + SQL/KV (audit + rolling window). [mem0-arch] [mem0-eval]
- **Retrieval.** Multi-signal fusion of semantic (vector), keyword (BM25 normalized), and entity-graph search. Reports avg <7 K tokens/retrieval and sub-second p50. [mem0-arch]
- **Capture.** Library is framework-agnostic; you call `mem.add(messages, user_id)` after each turn. Cloud product also offers connectors.
- **Killer feature.** The four-op "decision engine" on write — explicit conflict resolution baked into the consolidation step rather than left to retrieval. Strong LoCoMo numbers (66.9 % dense, 68.4 % graph, vs OpenAI 52.9 %, full-context 72.9 %) at 2 K – 4 K tokens vs full-context's 26 K. [mem0-bench]
- **Weakness.** "Memory" is essentially per-(user,session) chat-history compression. No native filesystem, multimodal, or temporal-graph reasoning beyond entity edges. Graph variant requires Neo4j or similar.
- **License / repo.** Apache-2.0, [github.com/mem0ai/mem0](https://github.com/mem0ai/mem0). Docs at [docs.mem0.ai](https://docs.mem0.ai). Hosted SaaS available.

### 2. Letta (formerly MemGPT)

- **Stack.** Originally MemGPT's "OS for LLMs" pattern — `core_memory` blocks (always-in-context), `recall_memory` (paginated chat history), `archival_memory` (vector store), and tool calls to page between tiers. As of 2026, the codebase has refactored toward a "MemFS" model where memory is **a git-backed directory of files** that the agent reads/writes via standard `bash`-style tools, with sleep-time compute as a client-side process. [letta-next] [letta-v1]
- **Retrieval.** Tool-mediated: the model decides when to swap blocks in/out of context, when to grep archival, etc. Works better with frontier models that can plan tool calls cleanly.
- **Capture.** Not a capture system — Letta is the *runtime* into which you wire capture.
- **Killer feature.** Sleep-time compute: between user turns the agent re-runs reflection/consolidation passes over its own memory, persisting improvements. [letta-sleep]
- **Weakness.** Heavy infra (server, postgres, optional Redis). The pivot to MemFS effectively concedes that "the file system is all you need" for memory primitives — which is exactly what `brain` already exploits. [letta-bench]
- **License / repo.** Apache-2.0, [github.com/letta-ai/letta](https://github.com/letta-ai/letta).

### 3. Zep + Graphiti

- **Stack.** Graphiti is the OSS engine; Zep is the hosted product. Three-layer **temporal knowledge graph**: episodes (raw events) → semantic-entity subgraph → community subgraph (clustered topics). Every edge is bi-temporal: `valid_from` / `valid_to` (event time) plus `recorded_at` / `invalidated_at` (transaction time). Pluggable graph backend: Neo4j, FalkorDB, Kuzu (now Apple-owned, archived), Amazon Neptune. [graphiti-os] [zep-paper]
- **Retrieval.** Hybrid: cosine on entity/edge embeddings + BM25 on episode text + breadth-first graph traversal from seed nodes. Sub-200 ms typical. No LLM at query time.
- **Capture.** SDK methods for adding episodes (chat messages, JSON business records, free text). Includes an MCP server so Claude/Cursor can be wired directly. [graphiti-os]
- **Killer feature.** **Bi-temporal facts.** When you say "I no longer work at Acme," the old `WORKS_AT(me, Acme)` edge gets `invalidated_at = now`, but is preserved — so "where did I work last March?" still answers correctly. This is the architectural difference no other OSS memory layer ships.
- **Weakness.** Operating a Neo4j-class DB locally is heavyweight; FalkorDB Lite improves this but is still a Redis-module. Cypher-style queries are not what most application code wants to write.
- **License / repo.** Apache-2.0 (Graphiti), [github.com/getzep/graphiti](https://github.com/getzep/graphiti). Paper: [arXiv:2501.13956](https://arxiv.org/abs/2501.13956).

### 4. Cognee

- **Stack.** ECL pipeline — **Extract** from 38+ sources, **Cognify** (chunk, embed, NER, optional RDF/OWL ontology validation), **Load** into three stores: relational (provenance), vector (LanceDB by default), graph (Kuzu / Neo4j). A `memify` post-process refines the graph (merge stale, reweight). [cognee-arch] [cognee-lance]
- **Retrieval.** Vector + graph traversal hybrid. Multi-hop reasoning is the marketing pitch.
- **Capture.** Files, APIs, DBs via "data sources" abstraction.
- **Killer feature.** Optional **ontology grounding** — feed it RDF/OWL and entity extraction is constrained to your domain schema. Useful for regulated/structured domains.
- **Weakness.** Heavier moving parts than the brain ethos prefers; ontology setup is real work; their benchmarks are self-reported.
- **License / repo.** Apache-2.0, [github.com/topoteretes/cognee](https://github.com/topoteretes/cognee).

### 5. ChatGPT Memory (OpenAI built-in)

- **Stack.** Two surfaces: **Saved Memories** (`bio` tool — a notepad of explicit facts, ~human-readable) and **Reference Chat History** (vector embeddings over past chats, summarized into a profile). Surfaced as system-prompt injection at the top of every conversation. [chatgpt-mem-faq] [embracethered]
- **Retrieval.** Embedding-based + recency/frequency weighting; no live search of full chat corpus per turn.
- **Capture.** Auto when the model "decides" something is worth saving, plus explicit "remember that…".
- **Killer feature.** Zero friction. Most users never configure anything and it just works.
- **Weakness.** Black-box. No export of the actual retrieval logic. **Loses on LoCoMo to mem0 by ~14–26 percentage points** [mem0-bench]. Privacy concerns: by default training-eligible; "incognito" / temporary chat is opt-in.

### 6. Claude memory (Anthropic)

- **Stack.** Two layers, both released through Q1 2026: (a) the **`memory` tool** in the API (beta header `context-management-2025-06-27`) — a filesystem-shaped tool with `view / create / str_replace / insert / delete / rename` over a `/memories` directory, host-side implementation up to you; (b) the consumer **Memory feature** in claude.com (preferences + conversation references, plus a `claude.com/import-memory` route that ingests ChatGPT/Gemini exports). [claude-mem-tool] [aimaker-q1]
- **Retrieval.** Tool-driven: Claude decides when to read memory files. Combined with **context editing / compaction** the model can offload aging context into memory files mid-task.
- **Capture.** API path: you decide. Consumer path: automatic via heuristics.
- **Killer feature.** Memory is *files Claude can read/write/grep*, which composes naturally with Skills, Code, and any other tool surface. This is functionally identical to `brain`'s markdown-vault model — and is the same direction Letta pivoted to.
- **Weakness.** No cross-product memory layer (yet); Claude Desktop's memory ≠ Claude Code's memory ≠ API memory tool. Each app has to wire its own backend.

### 7. Notion AI

- **Stack.** Workspace-scoped RAG over your pages + connected apps (Slack, Drive, GitHub, Jira). "Agent Instructions Pages" act as persistent style/preference memory. AI Meeting Notes captures + summarizes meetings, indexes into Q&A. [notion-mem]
- **Retrieval.** Federated search across workspace + connectors, returns cited answers.
- **Killer feature.** Best-in-class **enterprise context**: Slack / Drive / GitHub indexed natively, citations per claim.
- **Weakness.** Closed; lives inside Notion. No relevance for `brain`'s personal-vault use case beyond inspiration on the "show citations" UX.

### 8. Rewind.ai / Limitless

- **Status.** Acquired by Meta December 2025; Mac app sunsetted Dec 19 2025; pendant sales discontinued; service shut down in EU/UK/Korea/etc. [9to5-rewind] [tc-meta-limitless]
- **Lesson.** Continuous-recording personal memory is a market hazard — privacy-class lawsuits, geographic regulation, and an acquihire endpoint. Open-source successors picking up the slack: **Screenpipe** (local-only screen+audio→SQLite), **Plaud NotePin** (push-to-flag, not always-on), **Omi** (open hardware). [plaud-2026]
- **Killer feature (lost).** Episodic-grade ground truth — you really can search "what was on my screen at 3pm Tuesday."
- **Why not adopt for brain.** See section E.

### 9. Personal.ai

- **Stack.** A **Personal Language Model** (PLM, ~120 M params) trained continuously on the user's "Memory Stack" (independent Memory Blocks: time + source + content). At inference, a unified ranker measures relevance / fluency / style / accuracy and grounds output in the stack. Optional fall-through to OpenAI/Anthropic when PLM lacks coverage. [personal-ai-plm]
- **Killer feature.** Style imprinting — the small model learns *how you write*, not just what you know. "Personal Score" surfaces how grounded each response is.
- **Weakness.** Requires continual fine-tuning infra; a proprietary platform; the actual gain over a frontier LLM + good RAG is unproven.
- **For brain.** The Memory Block schema (time, source, content) is essentially what `brain.facts` already stores. The PLM-fine-tune step is out of scope for a local-first Python project.

### 10. Pieces for Developers (LTM-2)

- **Stack.** OS-level capture across IDE / browser / terminal / chat / meeting audio (LTM Audio added in v5.0.3). 9-month rolling retention. Local-first (no Ollama dependency since v5.1.0, Mar 2026). MCP server exposes 39 tools including `ask_pieces_ltm`, `create_pieces_memory`. [pieces-ltm] [pieces-protips]
- **Retrieval.** Time-anchored natural language ("what was I working on 3 months ago"), workstream timeline view.
- **Killer feature.** **Cross-tool capture**. Pieces is the only mainstream system that captures from IDE *and* browser *and* terminal *and* meetings, and exposes it through a single MCP surface. That breadth is the model `brain` should aspire to.
- **Weakness.** Closed source; "always on" capture has the same trust questions as Rewind, mitigated by being local-only.

### 11. Reflect / Mem.ai / Tana

- **Reflect.** Networked notes + GPT-4/Whisper layer; chat-with-notes; backlinks form an implicit knowledge graph. Mobile-first.
- **Mem.ai.** "Heads up" surfacing — proactively shows related notes as you write. Workspace-wide chat.
- **Tana.** "Supertags" = structured schemas over notes. Tana AI uses workspace as RAG context.
- **Common pattern.** All three are **PKM apps with an AI overlay** — semantic chat + automatic linking. None publishes the retrieval mechanics; treat as inspiration for UX (e.g. proactive related-note surfacing) rather than architecture.

### 12. Graphiti (standalone, see §3)

Already covered as Zep's engine. Worth flagging that Graphiti is now installable as a standalone package (`pip install graphiti-core` with `[falkor] / [kuzu] / [neptune]` extras) and ships an MCP server. If `brain` ever grows a graph layer, Graphiti is the most credible OSS adoption target — but see §E for the "don't" case.

### 13. A-MEM and MemoryBank (academic)

- **A-MEM (NeurIPS 2025, Xu et al.).** Zettelkasten-inspired agentic memory: each new note gets LLM-generated context/keywords/tags + embedding, then the LLM proposes links to similar past notes; storing the new note triggers re-tagging of linked old notes (autonomous evolution). Outperforms baselines on LoCoMo at lower token cost. Repo: [github.com/agiresearch/A-mem](https://github.com/agiresearch/A-mem). Paper: [arXiv:2502.12110](https://arxiv.org/abs/2502.12110). [amem-paper]
- **MemoryBank (AAAI 2024, Zhong et al.).** Three pillars: storage (conversations + summaries + personality), dual-tower FAISS retriever, **Ebbinghaus-curve updater** that decays unused memories and reinforces re-accessed ones. Application: SiliconFriend chatbot. [memorybank]
- **Why these matter for brain.** A-MEM's *write-time link proposal* and MemoryBank's *strength-based forgetting* are the two academic ideas with the cleanest mapping to a markdown-and-SQLite system.

### 14. OpenAI Apps SDK & Agents SDK memory

- **Apps SDK.** Apps in ChatGPT can read the user's Memory via the host; capture is host-mediated. MCP-compatible at the connection layer. [openai-apps]
- **Agents SDK.** Two layers: `Session` (short-term context window management, trimming/compression) and `Memory()` capability (sandboxed `memories/` directory with `MEMORY.md` index, progressive disclosure, optional `MemoryLayoutConfig` for per-task isolation). [openai-agents-mem]
- **Lesson.** Even OpenAI's reference design is now **markdown files on disk** with a summary index — convergent evolution toward the same pattern as Letta MemFS, Anthropic memory tool, claude-mem, and `brain`.

### Honourable mentions

- **Basic Memory** (basicmachines-co/basic-memory). Local markdown + MCP + bidirectional notes; the system architecturally closest to `brain` already shipped. Exposes "Entities" / "Observations" / "Relations" parsed from `[[wikilinks]]`. [basic-mem]
- **claude-mem** (thedotmack/claude-mem). Claude Code plugin that mirrors the brain's hook pattern: capture tool usage during sessions, compress with Haiku, inject context next session. [claude-mem-plugin]
- **Backtrack Core**, **MemMachine**, **MemU**, **Hindsight**, **Memvid**, **Cortex**: a long tail of 2025/2026 memory frameworks, mostly variations on the same Extract/Consolidate/Retrieve theme with different stacks. [devgenius-2026]

---

## B. Architectural Patterns (extracted across the field)

For each: **definition → who does it well → minimum viable implementation in `brain`**.

### B1. Episodic / Semantic / Procedural split

- **What.** Three memory kinds, mapped from cognitive science (Tulving): **episodic** = "what happened" (event + time + place), **semantic** = "what I know" (timeless facts, preferences), **procedural** = "how I do things" (skills, learned heuristics, corrections). Park's Generative Agents (2023) and Soar/CoALA (2023) both ground LLM agent designs in this taxonomy. [coala] [genagents]
- **Done well.** Generative Agents (memory stream + reflection); Letta (core / recall / archival); Cognee (separate entity vs episode stores). Treating memory as one undifferentiated vector store is consistently called out as the #1 architectural anti-pattern. [tianpan-3-memories]
- **For brain.** You already split: identity/ ≈ semantic+procedural, raw/ ≈ episodic-in-flight, entities/ ≈ semantic, corrections.md ≈ procedural. The split is not labelled as such, and there's no episodic store keyed by *(when, what)*. Adding a `timeline/` of dated episodes as first-class entities (or a `facts.fact_date` index that's actually populated) closes this gap.

### B2. Hot / Warm / Cold tiers + working memory

- **What.** MemGPT's contribution: a tiered memory model where the *active context window* is "working memory" (RAM), **recall** is paginated chat history (warm storage), **archival** is vector-indexed long-term (cold storage). The agent moves blocks across tiers via tool calls. [letta-v1]
- **Done well.** Letta, OpenAI Agents SDK (`Session` + `Memory`).
- **For brain.** `brain` has no working-memory abstraction — everything is cold. The MCP `brain_identity()` tool is the closest thing to "always-warm." A small `working_memory.md` that the SessionStart hook always writes into the system prompt, holding the last N high-confidence facts the model touched, would map this pattern at zero architectural cost.

### B3. Memory consolidation / sleep-time compute

- **What.** A periodic offline pass that re-reads recent memories, merges duplicates, computes higher-order summaries / "reflections," updates link structure, prunes noise. Modeled on hippocampal replay during sleep. [consolidation-pmc] [letta-sleep]
- **Done well.** Generative Agents (reflection when importance-sum exceeds threshold); Letta (sleep-time compute); A-MEM (link re-proposal on every write); Cognee (memify pipeline). Anthropic's "Auto Dream" maintenance cycle in Claude Code's auto-memory is this pattern at a consumer level. [aimaker-q1]
- **For brain.** This is the single biggest gap. Right now extraction is real-time per session and `reconcile.py` exists but isn't invoked on a schedule. A nightly `brain consolidate` job (launchd) that: (1) re-reads facts touched in the last week, (2) deduplicates with `llm_dedup.py`, (3) writes a `weekly/YYYY-WW.md` reflection summarizing what changed, (4) decays unused-fact weights, would be a 100-line PR with outsized impact.

### B4. Recency + frequency + importance weighting (Ebbinghaus)

- **What.** Generative Agents formalized this as `score = α·relevance + β·recency + γ·importance` where importance is LLM-rated 1–10 at write time. MemoryBank applies an Ebbinghaus exponential decay `S(t) = e^(-t/strength)` and re-strengthens on each retrieval. [memorybank]
- **Done well.** Generative Agents (importance), MemoryBank (decay), mem0 (implicit via consolidation update step).
- **For brain.** Add three columns to `facts`: `importance INTEGER`, `last_accessed TEXT`, `access_count INTEGER`. At write time the extractor LLM rates importance 1–5; at retrieval time `db.search` re-ranks by `bm25 / (decay_factor) + importance_weight`. Cheap, principled, no infra change.

### B5. Hybrid retrieval (BM25 + dense + graph) + reranker

- **What.** Industry consensus: BM25 wins on rare tokens, IDs, error codes; dense wins on paraphrase; graph wins on multi-hop. Fuse with **Reciprocal Rank Fusion** (parameter-free, scale-invariant), top-K → cross-encoder rerank for last-mile precision. [hybrid-2026] [rrf-2026]
- **Done well.** mem0, Zep/Graphiti, Cognee — all three signals fused. ChatGPT and Claude do not (publicly) rerank.
- **For brain.** RRF is implemented in `semantic.hybrid_search` (good). What's missing: a small cross-encoder rerank pass on the top 20 RRF results before returning the top 8. `BAAI/bge-reranker-v2-m3` is MIT-licensed, ~250 MB, ~10–30 ms per pair on CPU; capping rerank at top-20 keeps a query under ~500 ms. This is the highest-precision-per-engineering-hour upgrade available.

### B6. Bi-temporal facts (valid-time × transaction-time)

- **What.** Every fact carries `valid_from / valid_to` (when true in the world) **and** `recorded_at / invalidated_at` (when *we knew*). Updates never destroy — they invalidate. SQL:2011 standardized this; Graphiti applies it to LLM agent memory. [bitemporal-wiki] [martin-fowler-bitemporal]
- **Done well.** Zep/Graphiti is the only widely-used OSS implementation.
- **For brain.** `facts` table currently has only `fact_date` (valid-time approximation, free-form). Schema upgrade: `valid_from TEXT, valid_to TEXT, recorded_at TEXT NOT NULL, invalidated_at TEXT, supersedes_id INTEGER`. On contradiction, **don't UPDATE — INSERT a new row and set the old row's `invalidated_at`**. This is the foundation for queries like "what did I think about X in March vs now." Implementation cost: medium; design discipline cost: ongoing.

### B7. Entity resolution / coreference

- **What.** "Sang Yoon Lee" / "Son" / "@songg" must resolve to one entity. mem0's update step does this with an LLM. Newer ATOM (arXiv 2510.22590) does it with LLM-independent parallel merging via distance metrics. [atom-paper]
- **Done well.** mem0, Graphiti, ATOM. Critically all use a **persistent alias table**, not just embeddings.
- **For brain.** `aliases` table already exists in `db.py` ✅ but extraction prompts don't populate it richly. Add an `aliases:` field to the extractor JSON schema; bias `apply_extraction.py` toward matching on aliases before slugs.

### B8. Memory write-back / self-correction loop

- **What.** When a retrieved fact gets contradicted in conversation, the system *writes back* a correction. mem0's DELETE/UPDATE ops, MemoryBank's update mechanism, and Anthropic's memory tool's `str_replace` all enable this. The agent's own use is the highest-quality training signal it gets.
- **Done well.** mem0, Letta. Most "memory layers" don't do this — they're one-way pipes.
- **For brain.** The capture path is one-way: hook → extractor → write. The MCP surface is read-only. Add `brain_correct(entity, fact_id, new_text, reason)` and `brain_supersede(fact_id)` MCP tools so Claude can fix mistakes during a session, with provenance preserved.

### B9. Privacy: local-first + redaction

- **What.** Encrypt-before-sync, PII redaction at gateway, no telemetry by default. Inkandswitch's local-first manifesto remains the canonical guide. [inkandswitch-lf]
- **Done well.** Pieces (local-only by default), Basic Memory (markdown stays local), Screenpipe (auditable local DB).
- **For brain.** You're already local-first. The next step is a **redaction pass** in `prefilter.py` for outbound LLM calls — strip API keys, secrets in env files, personal identifiers of *third parties* the user hasn't consented to share with Anthropic. `gitleaks`/`trufflehog`-style regex set is enough to start.

### B10. Conflict / contradiction handling

- **What.** Detect (NLI or LLM judge), then either (a) keep both with timestamps (bi-temporal), (b) ask the user, (c) prefer the more recent / higher-confidence. Multi-agent designs (LegalWiz) decouple generation from evaluation. [legalwiz]
- **Done well.** Zep/Graphiti (bi-temporal preserves both); mem0 (DELETE op resolves at write).
- **For brain.** Today, contradictions silently overwrite (or pile up as bullets in the same file). With B6 in place, a `reconcile_merge.py` extension can flag contradictions per entity, write a `## Contradictions` section to the entity file, and let the user resolve via Obsidian.

### B11. Forgetting policies (TTL, decay, manual prune)

- **What.** Soft (relevance decay) vs hard (TTL/explicit delete). Pieces enforces 9-month TTL; ChatGPT memory has manual pruning UI; MemoryBank uses Ebbinghaus decay.
- **For brain.** Combine with B4. Add a `brain prune --older-than 365d --confidence-below 0.5 --dry-run` CLI. Never auto-delete without dry-run; treat any prune as a git commit so it's recoverable.

### B12. Multi-modal memory

- **What.** Storing images, audio, PDFs alongside text and retrieving them with the same surface. LoCoMo includes image-grounded conversation; Pieces ingests meeting audio.
- **For brain.** Stretch goal. Lowest-friction first step: when `ingest.py` sees an image, run captioning (Haiku-vision or local) and store the caption as a fact with `source: <image-path>`.

### B13. Cross-tool capture

- **What.** Pulling memory from every tool the user touches: Cursor, Claude Code, Claude Desktop, ChatGPT, Gmail, Calendar, Slack. Pieces is the breadth leader; Notion is the workspace leader.
- **For brain.** The SessionStart hook covers Claude Code. Logical next captures (each is a small adapter that drops a `raw/*.md` for the existing extractor to pick up):
  1. **ChatGPT export.** Periodic ZIP export → adapter splits into per-conversation summaries.
  2. **Claude Desktop.** Similar; conversations exportable from settings.
  3. **Cursor chat.** Cursor stores chat in `~/Library/Application Support/Cursor/User/workspaceStorage/.../chat.jsonl` — same JSONL pattern as Claude Code, so a sister harvester is ~30 LOC.
  4. **Calendar / Mail.** macOS `EventKit` and `MailKit` (or simply `.ics` / `.mbox` exports). Once a day → `raw/cal-YYYY-MM-DD.md`.
- The capture surface is the *moat*. Every system above is bottlenecked by it.

### B14. Provenance everywhere

- **What.** Every fact retains a pointer to its source: which session, which file, which line. Graphiti's "every derived fact traces back to episodes" is the strongest stance. [graphiti-os]
- **For brain.** Already half-implemented: `(source: <session-id>, <date>)` is parsed by `db._SOURCE_RE`. Make it mandatory in the extraction schema (currently optional), surface it in MCP tool outputs (already done), and let the user click through in Obsidian via a tiny script that resolves session-id → original `raw/` snapshot before deletion (so don't delete `raw/` after extraction; archive into `raw/.archive/YYYY-MM/`).

---

## C. Capability matrix: brain vs the field

One-sentence verdict per cell. "✅" = production-grade. "◑" = present but partial. "✗" = absent.

| Capability | brain (today) | mem0 | Letta | Zep/Graphiti | Cognee | ChatGPT Mem | Best in class |
|---|---|---|---|---|---|---|---|
| **Capture: chat sessions** | ✅ SessionStart hook for Claude Code | ◑ SDK call per turn | ◑ runtime captures own loop | ◑ SDK | ◑ data sources | ✅ built-in | brain (zero-friction for CC) |
| **Capture: cross-tool** | ✗ Claude Code only | ✗ | ✗ | ✗ | ◑ 38 sources | ✗ | Pieces (IDE+browser+meet) |
| **Capture: files (manual)** | ✅ `ingest.py` md/txt/csv/json | ✗ | ◑ MemFS | ◑ episodes | ✅ ECL | ✅ uploads | Cognee |
| **Storage substrate** | ✅ markdown + SQLite cache | vector + graph + KV | postgres + MemFS | graph DB | tri-store | proprietary | brain (for personal scale) |
| **BM25 lexical retrieval** | ✅ FTS5 | ✅ | ✗ | ✅ | ✗ | ✗ | brain & mem0 tie |
| **Dense semantic retrieval** | ✅ MiniLM + numpy | ✅ | ✅ | ✅ | ✅ | ✅ | mem0 (multi-signal) |
| **Graph traversal** | ✗ | ◑ optional | ✗ | ✅ | ✅ | ✗ | Graphiti |
| **Hybrid fusion (RRF)** | ✅ in `semantic.py` | ✅ | n/a | ✅ | ✅ | ✗ | tie |
| **Cross-encoder rerank** | ✗ | ◑ | ✗ | ✗ | ◑ | ✗ | (gap across the field) |
| **Bi-temporal facts** | ✗ (`fact_date` only) | ✗ | ✗ | ✅ | ✗ | ✗ | Graphiti |
| **Entity resolution / aliases** | ◑ schema present, prompt under-uses | ✅ LLM-driven | ◑ | ✅ | ✅ ontology | ✗ | mem0 |
| **Conflict/contradiction handling** | ✗ silent overwrite | ✅ explicit DELETE | ◑ | ✅ via bi-temp | ◑ | ✗ | Graphiti |
| **Forgetting / decay / TTL** | ✗ | ◑ via consolidation | ◑ | ✗ | ◑ memify | ◑ manual UI | MemoryBank (Ebbinghaus) |
| **Episodic / semantic / procedural split** | ◑ implicit via dirs | ✗ flat | ✅ tiers | ◑ episodes vs entities | ◑ | ✗ | Letta |
| **Sleep-time consolidation** | ✗ (`reconcile.py` not scheduled) | ◑ at write | ✅ | ✗ | ✅ memify | ✗ | Letta |
| **Importance weighting** | ✗ | ✗ | ✗ | ✗ | ✗ | ◑ implicit | Generative Agents |
| **Provenance per fact** | ✅ `(source: …)` | ✅ | ✅ | ✅ | ✅ | ✗ | tie (brain explicit) |
| **Local-first / privacy** | ✅ markdown + git, no cloud | ◑ self-host opt | ◑ self-host | ◑ self-host | ✅ local mode | ✗ cloud-only | brain & Basic Memory |
| **Human-editable** | ✅ Obsidian-native | ✗ | ✗ | ✗ | ✗ | ◑ | brain (uniquely) |
| **MCP surface for any client** | ✅ 8 tools | ✅ | ✅ | ✅ | ✅ | ✗ proprietary | brain & Graphiti |
| **Self-correction (write-back from agent)** | ✗ MCP read-only | ✅ | ✅ | ✅ | ◑ | ◑ | mem0 |
| **Multi-modal** | ✗ | ◑ | ◑ | ◑ | ✅ | ✅ | Cognee/ChatGPT |
| **Benchmarked on LoCoMo** | ✗ | ✅ 66.9% | ◑ | ✅ ~94% (DMR) | ◑ | ✅ 52.9% | Graphiti |

**Reading.** `brain` is competitive on substrate, BM25, semantic, RRF, provenance, local-first, MCP, and human-editability — i.e. the boring infra. It is missing the things that make a memory layer *intelligent rather than indexed*: rerank, bi-temporal, contradiction, decay, importance, consolidation, write-back, and cross-tool capture.

---

## D. Recommendations for `brain` (ranked by impact / effort)

Each: **idea → source → why → sketch → effort (S/M/L) → risk**.

### D1. Add a cross-encoder reranker on top of RRF — **S, very high impact**
- **Source.** Industry consensus (BGE, Jina, Qwen3 rerankers); ZeroEntropy's 2026 guide. [reranker-2026]
- **Why.** Your RRF returns ~20 candidates; an MS-MARCO-finetuned cross-encoder reorders them with cross-attention. Empirically lifts P@1 by 10–20 points on noisy corpora. The brain's facts are *short* (1–3 sentences), which is the regime where cross-encoders dominate.
- **Sketch.** New `brain/rerank.py` exposing `rerank(query, candidates) -> list[dict]`. Lazy-load `BAAI/bge-reranker-v2-m3` via `sentence_transformers.CrossEncoder`. Wire `semantic.hybrid_search` to call it on the post-fusion top-20 and return the top-K. Gate on env `BRAIN_RERANK=1`.
- **Effort.** S — ~50 lines + one model download. CPU latency ~150 ms for 20 pairs.
- **Risk.** Adds startup time (mitigated by lazy load); model is ~250 MB on disk. Set a fallback to identity rerank if the model can't load.

### D2. Bi-temporal fact schema — **M, structural**
- **Source.** Graphiti / Zep ([arXiv 2501.13956]); SQL:2011 bi-temporal standard.
- **Why.** Without it, "what did I believe last quarter" is unanswerable, and contradictions destroy history. With it, you get the foundation for *every* contradiction-, decay-, and timeline-related feature below at marginal extra cost.
- **Sketch.** Migrate `facts` to add `valid_from, valid_to, recorded_at NOT NULL DEFAULT now, invalidated_at, supersedes_id`. `apply_extraction.py` *never* updates a fact in place — it inserts a new row and sets the old row's `invalidated_at`. `db.search` filters `WHERE invalidated_at IS NULL` by default, with a `as_of` parameter for time travel. Add `brain_search(..., as_of='2026-01-01')` to the MCP surface.
- **Effort.** M — schema migration + extractor prompt update + retrieval default + a backfill (every existing fact gets `recorded_at = file mtime`).
- **Risk.** All consumers must learn to query `WHERE invalidated_at IS NULL`. Embedding rebuild needs to filter the same way or the semantic store will return ghost facts.

### D3. Sleep-time consolidation job — **M, high leverage**
- **Source.** Letta sleep-time compute, Generative Agents reflection, A-MEM autonomous evolution, Anthropic Auto Dream.
- **Why.** Real-time per-session extraction is good for *capture* but bad for *coherence*: dups accumulate, low-confidence facts persist, no higher-order summary forms. A nightly pass that re-reads recent activity and writes back a denser, deduped state is what makes a memory "feel" intelligent.
- **Sketch.** New `brain/consolidate.py` with three sub-passes: (1) **dedupe** — for entities updated in last 7d, run `llm_dedup.py` over their facts; (2) **reflect** — feed the week's new facts to Haiku with prompt "What patterns / themes / contradictions / open questions emerged this week? Output a `weekly/YYYY-WW.md` reflection."; (3) **decay** — decrement importance on facts not retrieved in N days. Schedule via launchd `~/Library/LaunchAgents/co.brain.consolidate.plist`, daily at 03:00.
- **Effort.** M — ~200 lines + plist + tests.
- **Risk.** Cost: ~$0.05/day at Haiku rates. Make sure the job is idempotent and can resume after partial failure (commit per pass).

### D4. Cross-tool capture — **M per source, but the moat**
- **Source.** Pieces LTM-2 breadth playbook.
- **Why.** Every system reviewed is throttled by *what it sees*. The brain's substrate (markdown + git + extraction pipeline) is *agnostic to source* — adding a new capture is a small adapter, not a rewrite. The marginal value of source #2 is huge (you correlate Cursor and Claude Code work); source #5 (Calendar) lets the model say "I notice you're meeting Adi at 4 — last time you wanted to revisit the Honeywell escalation."
- **Sketch.** Generic `brain/capture/<tool>.py` adapter that emits `raw/<tool>-YYYY-MM-DD-HHMMSS.md` in the existing format. First three to build:
  1. `capture/cursor.py` — scan `~/Library/Application Support/Cursor/User/workspaceStorage/**/chat.jsonl` analogously to `harvest_session.py`.
  2. `capture/chatgpt.py` — accept the standard ChatGPT export ZIP, split per conversation.
  3. `capture/calendar.py` — read `~/Library/Calendars/*.ics` since last run.
- **Effort.** M each, S to add the framework once; the harvest pattern is already there.
- **Risk.** Privacy — Calendar/Mail capture must be opt-in, with a redaction allowlist. Source-tag every fact (`source: cursor:project-foo`, `source: cal:meeting-xyz`) so the user can trace and prune by origin.

### D5. Write-back MCP tools (`brain_correct`, `brain_supersede`, `brain_remember`) — **S, behavior-changing**
- **Source.** mem0's UPDATE/DELETE; Anthropic memory tool's `str_replace`.
- **Why.** Today the model can *read* the brain but can't *fix* it. The single highest-quality memory signal you ever get is "no, that's wrong, here's the correct version" — and you currently don't capture it. Adding write-back tools turns the brain from a read replica into a living substrate.
- **Sketch.** Three new tools in `mcp_server.py`:
  - `brain_remember(text, type, name, source='session')` — quick fact insert without going through extraction.
  - `brain_correct(entity_path, old_text, new_text, reason)` — supersedes a fact (uses D2's `invalidated_at`).
  - `brain_forget(entity_path, fact_id, reason)` — soft-delete (sets `invalidated_at`, never destroys row).
  - Each writes to markdown via `apply_extraction` for source-of-truth consistency, then fires a git commit `brain: claude wrote-back …`.
- **Effort.** S — ~150 lines + tests. Depends on D2 being in flight (degrades cleanly without it).
- **Risk.** Hallucinated writes. Mitigate by (a) always commit per write, (b) per-tool rate limit, (c) require `reason` param so the audit trail is human-readable.

### D6. Importance + decay scoring on facts — **S, requires D2**
- **Source.** Generative Agents (importance 1–10 at write); MemoryBank (Ebbinghaus decay).
- **Why.** Right now BM25 and cosine treat every fact equally; in reality "Son's spouse's birthday" beats "Son once preferred 2-space indent in 2024" by orders of magnitude. Importance + access-recency lets retrieval surface *what matters*, not just *what matches*.
- **Sketch.** Add `importance INTEGER DEFAULT 3`, `access_count INTEGER DEFAULT 0`, `last_accessed TEXT` to `facts`. Extractor prompt asks for `importance: 1-5`. `db.search` final ORDER BY becomes `bm25(...) - 0.1*importance + 0.05*age_days`. `mcp_server.brain_search` and `brain_recall` increment `access_count` and stamp `last_accessed` on every hit.
- **Effort.** S.
- **Risk.** LLMs are bad at calibrated importance scores. Use the score as a tie-breaker, not a primary ranker, for the first few months.

### D7. Working memory primer at session start — **XS, immediate UX win**
- **Source.** MemGPT `core_memory`; Letta memory blocks; Anthropic system-prompt memory injection.
- **Why.** Today, Claude has to *call* `brain_identity()` to even know who you are. A SessionStart-injected primer of (identity + N most-important facts touched in last 7 days + active corrections + active issues) means the model walks in oriented.
- **Sketch.** Extend the SessionStart hook to also write `~/.brain/.session-primer.md` (≤ 2 K tokens) and have the CLAUDE.md load it on session start. Content: identity files + `brain_recent` + top 5 corrections + the 10 highest-importance facts overall.
- **Effort.** XS — extend `auto_extract.py` or a separate `prime_session.py`.
- **Risk.** Token bloat; cap the primer hard at 2 K tokens.

### D8. Migrate semantic store to `sqlite-vec` (when corpus >50 K) — **S today, M migration**
- **Source.** asg017/sqlite-vec, current state-of-art SQLite vector ext (sqlite-vss is deprecated). [sqlite-vec]
- **Why.** Today you brute-force cosine over numpy at ~3 K facts. At your growth rate this works for years. But: (a) `sqlite-vec` co-locates vectors with FTS5 in *one* DB — fewer moving parts; (b) it supports metadata filtering in the same query (no Python-side post-filter); (c) it's the path others (memweave, Basic Memory variants) are converging on. The current note in `semantic.py` flags this exact migration. The blocker is that pyenv Python lacks `enable_load_extension` — fixable by recompiling Python with `--enable-loadable-sqlite-extensions` or shipping a custom sqlite3 build via `pysqlite3-binary`.
- **Sketch.** Add `pysqlite3-binary` to deps. New `db.connect_with_vec()` uses `pysqlite3` and loads `sqlite-vec`. New table `vec_facts` virtual `vec0(embedding float[384])`. `semantic.build` writes there; `semantic.search_facts` becomes a SQL query joining facts + vec_facts.
- **Effort.** S to land the option behind a flag; M to make it the default.
- **Risk.** Distribution: `pysqlite3-binary` is well-maintained but adds a ~5 MB wheel. Worth it.

### D9. Contradiction surfacing (not auto-resolution) — **S after D2**
- **Source.** LegalWiz multi-agent contradiction mining, Graphiti's "preserve, don't destroy" stance.
- **Why.** Auto-resolving contradictions is dangerous (LLM picks wrong); surfacing them for the human is safe and useful. Obsidian-native UI is a perfect home.
- **Sketch.** Extend `reconcile.py` to detect contradictions per entity (NLI via a small `cross-encoder/nli-deberta-v3-small` model, or LLM judge with Haiku at consolidation time). When found, append a `## ⚠ Contradictions` section to the entity's markdown linking the conflicting fact ids and dates. The user resolves in Obsidian; a CLI `brain resolve <entity> <fact_id> --keep <id> --reason "…"` writes back via D5.
- **Effort.** S (depends on D2 + D5).
- **Risk.** False-positive contradictions are noisy; tune the threshold conservatively.

### D10. Auto-link / "related entities" via write-time graph proposal — **M, A-MEM-style**
- **Source.** A-MEM (Zettelkasten-inspired). [amem-paper]
- **Why.** Your entity files use `[[wikilinks]]` (Obsidian-native), but they're proposed only when the extractor "happens to mention" another entity by name. A-MEM's insight: at write time, embed the new note, find K nearest existing notes, and have the LLM propose explicit links with relationship labels — *and* update the linked notes to point back. This builds a real graph in your existing markdown without ever needing a graph DB.
- **Sketch.** In `apply_extraction.py`, after writing/updating an entity, fire `propose_links(entity_id)`: top-K by entity-embedding similarity → Haiku call "for each candidate, is there a meaningful relationship? if yes, what label?" → emit `[[wiki-link|label]]` lines into a `## Related` section of both entities. Idempotent: same proposal twice = no-op. Also: a `brain_neighbors(entity, k=5)` MCP tool that walks one hop in the markdown link graph for "what's connected to X" queries.
- **Effort.** M — ~250 lines + careful prompt design + a per-entity LRU so you don't re-propose every run.
- **Risk.** LLM cost grows with corpus size — cap candidates and only run on changed entities. The "label" can be hallucinated; use a controlled vocabulary `{related, supersedes, blocks, mentions, contradicts, instance-of, member-of, synonyms}`.

### Bonus (lower priority, worth flagging):

- **D11. Episodic timeline as first-class.** Promote `timeline/` from incidental to core: every session, calendar event, and decision becomes a dated episode with its own retrieval channel (`brain_when(start, end)`).
- **D12. Outbound redaction in `prefilter.py`.** Strip secrets and third-party PII from extraction prompts before they hit Anthropic. (Defensive privacy; cost: $0.)
- **D13. Anonymous benchmark vs LoCoMo.** Subset the benchmark, ingest into a throwaway `~/.brain-bench/`, run the QA tasks via the MCP surface. Expect mid-50s F1 today; the recommendations above plausibly push to 70+. Even self-reported numbers expose regressions.
- **D14. Image captions on ingest.** When `ingest.py` sees `.png/.jpg/.pdf`, run a Haiku-vision call and store the caption as a fact. Bare-minimum multi-modal.
- **D15. `brain://timeline/<date>` MCP resource.** Surface specific days as resources (already have `brain://entity/...` pattern).

---

## E. What to deliberately NOT adopt

### E1. Heavy graph DBs (Neo4j, Memgraph, even FalkorDB server)

- **Why tempting.** Graphiti shows real wins from temporal-graph reasoning; Cypher is expressive; "knowledge graph" is the buzzword.
- **Why wrong for brain.** A personal memory at single-user scale tops out around 10⁵ facts and 10⁴ entities. You can compute graph traversal in pure Python on a SQLite-mirrored adjacency list at that scale in <50 ms. Adding Neo4j (server, JVM, port, license edges) or even FalkorDB (Redis module) is a Pareto regression on the brain's *only* unfair advantage: zero operational footprint. Kuzu was the only embedded option that fit, and Apple acquired and archived it in October 2025. [arcadedb-2026]
- **Right move.** Recommendation D10 builds a graph **in markdown** via `[[wikilinks]]` and computes hops in SQLite. If you outgrow that (you won't), revisit FalkorDB Lite or a Graphiti integration.

### E2. Full vector DBs (Pinecone / Weaviate / Qdrant server / Chroma server)

- **Why tempting.** "Real" vector DBs ship HNSW, metadata filtering, hybrid query out of the box.
- **Why wrong.** At 3 K facts, brute-force cosine on a 384-d numpy matrix is ~5 ms — *faster* than a network round-trip to Pinecone. Even at 100 K facts, `sqlite-vec` (D8) keeps you in-process. Pinecone/Weaviate add cloud dependency, monthly cost, vendor lock, and an export problem on the day they pivot — all for a search-quality delta of zero at this scale.
- **Right move.** Stay on numpy until ~50 K facts; then in-process `sqlite-vec`.

### E3. Agent frameworks (LangGraph, LangChain, AutoGen, CrewAI) for the brain itself

- **Why tempting.** "Agentic memory" is a hot phrase; LangGraph has nice diagrams.
- **Why wrong.** The brain is not an agent. It's a memory substrate that an agent (Claude) consults via MCP. Wrapping the brain itself in LangGraph adds runtime, abstraction, and breakage paths without giving the user anything. Letta tried being-the-agent, and the lesson of their 2026 pivot was: *the runtime should be the model's harness, not your library.* You already use the right composition: brain = data, MCP = interface, Claude = agent.
- **Right move.** Stay framework-free Python. The total dependencies should remain `mcp`, `anthropic`, `sentence-transformers`, `numpy`, optional `pysqlite3-binary` and a reranker.

### E4. Continuous recording (Rewind / Limitless model)

- **Why tempting.** Highest-fidelity episodic memory imaginable. "Search anything you ever saw."
- **Why wrong.** (a) **Privacy disaster** — captures third-party content (others on calls, things on screen) without their consent; legally radioactive in EU/CA/IL. (b) **Storage**: full-fidelity OS recording is GBs/day. (c) **Existential risk on the platform** — Rewind's path from cult favorite to Meta-acquihire-and-shutdown in 18 months is the warning. [9to5-rewind] [tc-meta-limitless] (d) **Brain's design ethos is durable artifacts**, not raw streams.
- **Right move.** Capture *artifacts* (chats, notes, calendar entries, decisions) not *streams* (screen, audio). If you ever want screen-grade fidelity, point at Screenpipe and let it dump into `raw/screen-*.md` summaries via its own pipeline.

### E5. Proprietary memory APIs as the source of truth (mem0 SaaS, Zep cloud, Personal.ai)

- **Why tempting.** Hosted means no ops; mem0 has the best benchmarks; Personal.ai promises a personalized model.
- **Why wrong.** Markdown + git is *forever exportable*. Cloud APIs are not. The brain's value compounds for years; every cloud dependency is a 3-year liability with a non-zero probability of acquihire / shutdown / pivot (cf. Rewind, Mem.ai's bumpy ride, every YC memory startup). Use these systems as *inspiration* and read their papers; never as substrate.
- **Right move.** Periodically re-read the leader's papers (mem0, Graphiti) and steal mechanisms, not infra.

### E6. PLM / personal fine-tuning (Personal.ai model)

- **Why tempting.** "An AI that writes like me."
- **Why wrong.** The win from a 120 M-param PLM over `Claude + good RAG` is unproven. Continual fine-tuning infra (data prep, eval harness, drift monitoring, weight storage, serving) is a project larger than the brain itself. And Anthropic / OpenAI ship better base models faster than any personal fine-tune can keep up with.
- **Right move.** Lean into *prompts as personalization*: the identity files + corrections + working-memory primer (D7) approximate "writes like Son" at near-zero cost.

### E7. Heavy ontologies / RDF / OWL (Cognee's grounding mode)

- **Why tempting.** "Structured" is comforting. Disambiguates entities. Plays well with existing knowledge bases.
- **Why wrong for personal use.** Personal life is messy and the ontology shifts faster than you can edit RDF. The free-form `type` slot in `extract_session.md` is the right call — let the LLM invent `quotes`, `recipes`, `arguments`, `rituals` as needed; converge later via D9 + D10. Rigid schemas prematurely freeze a brain that's still learning what it is.
- **Right move.** Soft conventions (existing-types reuse) > hard schemas. The current prompt strikes that balance.

### E8. Multi-agent contradiction frameworks (LegalWiz-style)

- **Why tempting.** Sounds rigorous.
- **Why wrong at this scale.** A single LLM judge in the consolidation pass (D9) catches most of what a multi-agent system would, at a tenth the complexity and cost. Keep this in the "if benchmarks demand it" file.

---

## References (with retrieval date)

All accessed 2026-04-19 via WebSearch / WebFetch.

- [mem0-arch] mem0.ai blog, *The Architecture of Remembrance*. https://mem0.ai/blog/what-is-ai-agent-memory
- [mem0-eval] mem0 docs, *Memory Evaluation*. https://docs.mem0.ai/core-concepts/memory-evaluation
- [mem0-bench] mem0 blog, *AI Memory Benchmark: Mem0 vs OpenAI vs LangMem vs MemGPT*. https://mem0.ai/blog/benchmarked-openai-memory-vs-langmem-vs-memgpt-vs-mem0-for-long-term-memory-here-s-how-they-stacked-up
- [mem0-paper] *Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory*. arXiv:2504.19413. https://arxiv.org/html/2504.19413v1
- [letta-next] Letta blog, *Letta's next phase*. https://www.letta.com/blog/our-next-phase
- [letta-v1] Letta blog, *Rearchitecting Letta's Agent Loop*. https://www.letta.com/blog/letta-v1-agent
- [letta-sleep] Letta blog, *Sleep-time Compute*. https://www.letta.com/blog/sleep-time-compute
- [letta-bench] Letta blog, *Benchmarking AI Agent Memory: Is a Filesystem All You Need?* https://www.letta.com/blog/benchmarking-ai-agent-memory
- [letta-blocks] Letta blog, *Memory Blocks*. https://www.letta.com/blog/memory-blocks
- [graphiti-os] Zep, *Graphiti Open Source*. https://www.getzep.com/product/open-source/
- [graphiti-gh] github.com/getzep/graphiti
- [zep-paper] *Zep: A Temporal Knowledge Graph Architecture for Agent Memory*. arXiv:2501.13956. https://arxiv.org/abs/2501.13956
- [graphiti-neo4j] Neo4j blog, *Graphiti: Knowledge Graph Memory for an Agentic World*. https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/
- [cognee-arch] Cognee docs, *Architecture*. https://docs.cognee.ai/core-concepts/architecture
- [cognee-lance] LanceDB blog, *How Cognee Builds AI Memory Layers with LanceDB*. https://www.lancedb.com/blog/case-study-cognee
- [cognee-onto] Cognee blog, *AI Memory with Ontologies*. https://www.cognee.ai/blog/deep-dives/grounding-ai-memory
- [chatgpt-mem-faq] OpenAI Help Center, *Memory FAQ*. https://help.openai.com/en/articles/8590148-memory-faq
- [embracethered] Embrace The Red, *How ChatGPT Remembers You*. https://embracethered.com/blog/posts/2025/chatgpt-how-does-chat-history-memory-preferences-work/
- [claude-mem-tool] Anthropic, *Memory tool — Claude API Docs*. https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- [claude-context-edit] Anthropic, *Context editing*. https://platform.claude.com/docs/en/build-with-claude/context-editing
- [aimaker-q1] aimaker.substack.com, *Complete Guide to Every Claude Update in Q1 2026*. https://aimaker.substack.com/p/anthropic-claude-updates-q1-2026-guide
- [skills] Anthropic engineering, *Equipping agents for the real world with Agent Skills*. https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- [notion-mem] Notion, *Use Notion AI to give teams perfect memory*. https://www.notion.com/help/guides/use-notion-ai-to-give-teams-perfect-memory-and-save-time
- [9to5-rewind] 9to5Mac, *Rewind Mac app shutting down*. https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/
- [tc-meta-limitless] TechCrunch, *Meta acquires AI device startup Limitless*. https://techcrunch.com/2025/12/05/meta-acquires-ai-device-startup-limitless/
- [plaud-2026] Plaud blog, *Best Wearable Device for AI Note Taking (2026)*. https://www.plaud.ai/blogs/articles/whats-the-best-wearable-device-for-ai-note-taking-2026
- [personal-ai-plm] Personal.ai, *Differences Between PLMs and LLMs*. https://www.personal.ai/plm-personal-and-large-language-models
- [pieces-ltm] Pieces, *Long-Term Memory*. https://pieces.app/features/long-term-memory
- [pieces-protips] github.com/pieces-app/pro_tips
- [amem-paper] *A-MEM: Agentic Memory for LLM Agents*. arXiv:2502.12110. https://arxiv.org/abs/2502.12110
- [amem-gh] github.com/agiresearch/A-mem
- [memorybank] *MemoryBank: Enhancing LLMs with Long-Term Memory*. arXiv:2305.10250 / AAAI-24. https://arxiv.org/abs/2305.10250
- [openai-apps] OpenAI Developers, *Apps SDK*. https://developers.openai.com/apps-sdk
- [openai-agents-mem] OpenAI Agents SDK, *Agent memory*. https://openai.github.io/openai-agents-python/sandbox/memory/
- [hybrid-2026] gopenai blog, *Hybrid Search in RAG (Mar 2026)*. https://blog.gopenai.com/hybrid-search-in-rag-dense-sparse-bm25-splade-reciprocal-rank-fusion-and-when-to-use-which-fafe4fd6156e
- [rrf-2026] Medium / Ashutosh Singh, *Hybrid Search Done Right*. https://ashutoshkumars1ngh.medium.com/hybrid-search-done-right-fixing-rag-retrieval-failures-using-bm25-hnsw-reciprocal-rank-fusion-a73596652d22
- [reranker-2026] ZeroEntropy, *Ultimate Guide to Choosing the Best Reranking Model 2026*. https://zeroentropy.dev/articles/ultimate-guide-to-choosing-the-best-reranking-model-in-2025/
- [bitemporal-wiki] Wikipedia, *Bitemporal modeling*. https://en.wikipedia.org/wiki/Bitemporal_modeling
- [martin-fowler-bitemporal] Martin Fowler, *Bitemporal History*. https://martinfowler.com/articles/bitemporal-history.html
- [atom-paper] *ATOM: Adaptive and Optimized dynamic temporal knowledge graph construction*. arXiv:2510.22590. https://arxiv.org/html/2510.22590v1
- [coala] *Cognitive Architectures for Language Agents*. arXiv:2309.02427. https://arxiv.org/html/2309.02427v3
- [genagents] Park et al., *Generative Agents*. arXiv:2304.03442. https://arxiv.org/abs/2304.03442
- [tianpan-3-memories] tianpan.co, *The Three Memory Systems Every Production AI Agent Needs*. https://tianpan.co/blog/long-term-memory-types-ai-agents
- [consolidation-pmc] *Memory Consolidation*. PMC. https://pmc.ncbi.nlm.nih.gov/articles/PMC4526749/
- [sleepgate] *Learning to Forget: Sleep-Inspired Memory Consolidation*. arXiv:2603.14517. https://arxiv.org/abs/2603.14517
- [locomo-paper] Maharana et al., *Evaluating Very Long-Term Conversational Memory of LLM Agents*. arXiv:2402.17753. https://arxiv.org/abs/2402.17753
- [locomo-flaws] r/AIMemory, *Serious flaws in two popular AI Memory Benchmarks*. https://www.reddit.com/r/AIMemory/comments/1s1jlnd/serious_flaws_in_two_popular_ai_memory_benchmarks/
- [legalwiz] *LegalWiz: A Multi-Agent Generation Framework for Contradiction Detection*. arXiv:2510.03418. https://arxiv.org/html/2510.03418v2
- [sqlite-vec] github.com/asg017/sqlite-vec
- [memweave] Towards Data Science, *memweave: Zero-Infra AI Agent Memory*. https://towardsdatascience.com/memweave-zero-infra-ai-agent-memory-with-markdown-and-sqlite-no-vector-database-required/
- [basic-mem] github.com/basicmachines-co/basic-memory
- [claude-mem-plugin] github.com/thedotmack/claude-mem
- [arcadedb-2026] ArcadeDB blog, *Neo4j Alternatives in 2026*. https://arcadedb.com/blog/neo4j-alternatives-in-2026-a-fair-look-at-the-open-source-options/
- [kuzu-eol] HN thread, *We will no longer be actively supporting KuzuDB*. https://news.ycombinator.com/item?id=45560036
- [inkandswitch-lf] Ink & Switch, *Local-first software*. https://www.inkandswitch.com/essay/local-first/
- [devgenius-2026] Dev Genius, *AI Agent Memory Systems in 2026 Compared*. https://blog.devgenius.io/ai-agent-memory-systems-in-2026-mem0-zep-hindsight-memvid-and-everything-in-between-compared-96e35b818da8

---

*End of document.*
