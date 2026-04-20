# Brain — Project Requirements & Decision Log

Living record of decisions and feature commitments. Append-only by convention;
edit prior entries only to mark status changes (e.g. `pending → shipped`).
Each entry has: date, decision, rationale, status.

Maintained autonomously by the assistant during planning/discussion turns.

---

## Conventions

- **Status**: `proposed` → `accepted` → `in-progress` → `shipped` → `superseded`
- **Source**: which session/conversation the decision came from
- **Linked code**: commit SHA(s), file paths, or `n/a`
- One entry per discrete decision; group related sub-decisions under one header.

---

## Shipped (baseline as of 2026-04-20)

These predate the requirements log; captured here for context only.

- **Entity-first markdown vault** (`~/.brain/entities/<type>/<slug>.md`) — human-readable, git-diffable, Obsidian-renderable.
- **Harvest pipeline** — `harvest → prefilter → batch-extract → reconcile → clean` (Claude + Cursor agent transcripts).
- **Hybrid recall** — BM25 + dense + RRF fused via `brain_recall` MCP.
- **SQLite + FTS5 mirror** — fast index over the markdown source-of-truth.
- **MCP tool surface** — `brain_recall`, `brain_get`, `brain_recent`, `brain_identity`, `brain_stats`, `brain_audit`, `brain_history`, `brain_semantic`, `brain_status`.
- **Idle-gated launchd watcher** — `flock` singleton, skips LLM stages while a Claude/Cursor session is actively typing.
- **Persona-aware `brain init`** — onboarding wizard with developer/researcher/student/lawyer/doctor/custom presets.
- **Autoresearch loop** — `python -m brain.autoresearch` with fixed cycle budget (10-min wall-clock, 8 LLM calls), `playground/` agent sandbox, `program.md` spec. Now runs autonomously via launchd every 30 min (see Phase 0.5 entry below).
- **X crawler toolkit** — `~/.brain/bin/x/` (timeline, user_tweets, search, conversation) using authenticated Playwright session.

---

## Decision Log

### 2026-04-20 — Phase 1 (first win): promote closes the autoresearch feedback loop

- **Decision:** Ship `brain.promote` with synthesized `## Key Facts` sections so playground items reach the `entities/` fact index on promotion — closing the last open wire in the autoresearch feedback loop. Before this, promoted entities lived on disk but had zero rows in `facts` (the renderer copied prose without extracting bullets), so fact-search stayed blind to every promotion and the brain couldn't build on its own reasoning.
- **Pieces shipped:**
  - `src/brain/promote.py` — scans `playground/insights|hypotheses` for `confidence: high`, `len(refs) ≥ 2`, `created_at ≤ 14d` items; writes canonical `entities/insights/*.md` with synthesized Key Facts that match `db._SOURCE_RE` so every bullet lands in the `facts` table; annotates source with `status: promoted`; re-runs `semantic.build()` in one pass.
  - `_synthesize_key_facts()` / `_extract_fact_paragraphs()` — deterministic, no-LLM extraction that turns paragraphs or bullet lists into sourced fact bullets. Drops scaffolding (`testable_via:`, `status:`) and falls back to the title so empty bodies still leave a row behind.
  - `--rerender` CLI — regenerates already-promoted entities against the current render (needed when the renderer itself changes, as it did here). Keeps playground `status: promoted` annotations intact.
  - `entities/techniques/playground-to-entities-promotion-via-brain-promote.md` — canonical doc entity written into the live vault so "how do playground items reach entities" is answerable from the brain itself.
- **Metric (live):** miss rate 6.7% → 0.0% on the 15-query eval set after rerender + upsert — the query "playground promotion to entities" flipped from 0.569 (miss at thr 0.60) to 0.716 (ok). `brain status` now shows `coverage: miss 0.0% (Δ↓6.7pp) · avg-top 0.705 @ thr 0.60`.
- **Status:** shipped — 29 promote tests pass, 162 total. Full suite green.
- **Explicitly not done (Phase 1 still has):**
  - Live recall-ledger mode (every real `brain_recall` logged, rolling 7-day coverage).
  - Realtime (≤10s) Obsidian sync — ingest still runs on the 5-min auto-extract tick.
  - `brain reconcile --promote` integration — promote is a separate command for now; wiring it into the reconcile flow is cleaner but adds coupling we don't need yet.

### 2026-04-20 — Phase 0.5 shipped: autonomous autoresearch + Question Coverage Score

- **Decision:** Promote autoresearch from "manual `python -m brain.autoresearch`" to a launchd-driven background loop, and bolt on the first honest measurement harness so "did this cycle help?" is answerable without a human in the loop.
- **Pieces shipped:**
  - `templates/launchd/brain-autoresearch.plist.tmpl` + `templates/scripts/autoresearch-tick.sh.tmpl` — 30-min tick, `Nice=15` (yields to auto-extract + semantic-worker), `RunAtLoad=false`, flock + pgrep + `program.md` guards to avoid the Mac dual-instance freeze (incident 2026-04-11).
  - `bin/install.sh` / `bin/uninstall.sh` / `bin/doctor.sh` — render + load + verify the new plist alongside the existing two (`com.son.brain-auto-extract`, `com.son.brain-semantic-worker`, `com.son.brain-autoresearch`).
  - `src/brain/recall_metric.py` — new module implementing `program.md`'s Question Coverage Score. Loads an eval set from `~/.brain/eval-queries.md` (one `- query` line per prompt, 16-query default seeded on first run), scores each via semantic.search_facts + semantic.search_notes top-k, persists every run to `~/.brain/recall-ledger.jsonl`. Miss threshold **0.60** (tuned for the multilingual-MiniLM encoder the brain actually ships; Karpathy's spec of 0.35 was for English MiniLM and overfits on this encoder).
  - `src/brain/autoresearch.py` — dedicated `call_claude()` with `--system-prompt` + `--tools ""` so the CLI can't wander into MCP lookups mid-synthesis; tougher `_parse_response()` that walks balanced braces so prose preambles don't break JSON parse; `run_cycle()` now measures pre/post coverage, re-ingests notes after writes so playground items are visible in the "after" score, logs a delta block per cycle, surfaces trajectory to stderr.
  - `src/brain/audit.py` — dedupe items read `~/.brain/.dedupe.ledger.json` + cross-check file status (skip applied merges, missing files, `status: superseded`), and brain-related items get `BRAIN_PRIORITY_BOOST = +30` so fixing the brain itself surfaces first.
  - `src/brain/status.py` — new `coverage` section on the dashboard:  `miss 6.7% (Δ↓6.7pp) · avg-top 0.695 @ thr 0.60  [17 eval runs logged]`.
  - `src/brain/harvest_session.py` — Cursor active window 60s → 10s (byte-offset ledger makes partial harvests safe); `templates/scripts/auto-extract.sh.tmpl` swaps the 180s-mtime session guard for a pgrep-only check (the mtime gate never opened while Cursor was open all day, starving the LLM stages).
- **Metric (live):** miss rate dropped from 13.3% → 6.7% across the baseline eval set during the build session itself; 1 miss remains (Vietnamese-tone preferences query).
- **Status:** shipped — commits `dbcf5fa` (launchd) · `93dd9ec` (recall metric + autoresearch) · `5512f30` (audit ledger + brain boost) · `27031a2` (harvest + auto-extract guard) · `62ca71e` (audit detail path) · `632562e` (status coverage line). 133 unit tests pass.
- **Explicitly not done (Phase 1 candidates):**
  - Live recall-ledger mode — every real `brain_recall` call logged with top-k scores. The current harness is eval-set only; live mode needs an MCP middleware hook.
  - Playground → `entities/` promotion CLI. Human still eyeballs `playground/hypotheses|insights|contradictions` before merging into the canonical vault.
  - Realtime (≤10s) Obsidian sync (Goal 4). `harvest_session.CURSOR_ACTIVE_WINDOW_SEC` is now 10s; the ingest path still runs only on the 5-min auto-extract tick.
- **Source:** this session (2026-04-20 night)

### 2026-04-20 — `brain status` becomes the operational dashboard

- **Decision:** Extend `brain status` (currently vault-stats only) into a single-shot operational dashboard answering *"is the brain doing anything in the background right now?"*. Backed by a new `brain.status` module exposing `gather() → StatusReport`, `format_text(report)`, `to_json(report)`. Same data exposed as the `brain_status` MCP tool so agents can decide whether to nudge the user (e.g. "dedupe pass in flight, hold off on heavy edits") without parsing log lines.
- **Surfaces:** launchd job state (loaded? PID? interval?), in-flight lock (`~/.brain/.extract.lock.d/`), last-run timestamp + `skipped_streak`, ETA to next run, currently-spawned brain/LLM subprocesses (`ps -A` filtered by pattern), ledger sizes (harvested + dedupe verdicts), pending audit count, vault counts.
- **Constraints:** read-only (no LLM calls, no mutation), tolerant of every component being missing (fresh installs), one `launchctl list` + one `ps -A` per call (cheap enough to be safe in hot paths). No new third-party deps — `subprocess`, `re`, `os` only.
- **CLI:** `brain status` (text), `brain status --json`, `brain status -v` (adds per-type entity table).
- **Status:** shipped — `src/brain/status.py`, wired into `cli.py` + `mcp_server.py`, 10 unit tests in `tests/test_status.py`. All 110 tests pass.
- **Source:** this session

### 2026-04-20 — Project requirements doc lives at `docs/project_requirements.md`

- **Decision:** Maintain a single append-only requirements/decision log at `docs/project_requirements.md`. The assistant updates it autonomously whenever a feature/architecture decision is reached during a session, without being asked each time.
- **Rationale:** Conversational decisions evaporate into chat history; commits capture *what shipped* but not *why this over alternatives*. A single in-repo log gives `git blame` for product intent.
- **Format:** Reverse-chronological under `## Decision Log`, each entry ≤ ~10 lines, with status, rationale, and linked code/commits.
- **Status:** shipped
- **Source:** this session

### 2026-04-20 — Commit hygiene: split mixed work into per-feature commits

- **Decision:** When staged changes span multiple unrelated features, split into one commit per logical change before pushing. Use `git add -p` only when a single file truly mixes features; otherwise group by file.
- **Rationale:** Squashed mega-commits hide intent in `git log` / `git blame` / `git revert`. The 18-file v0.2 push was split into 5 commits (db hardening, cursor harvest, audit MCP, autoresearch, dedupe) for this reason.
- **Status:** shipped (applied retroactively to the v0.2 push, commits `a2e5e3d…05496d8`)
- **Source:** this session

### Open / pending

The following are documented in `docs/100x-autoresearch.md` as "next likely upgrades" but **not yet decided/scheduled**. Promote to a numbered decision entry once committed to.

1. **Reconcile-with-promote** — `brain reconcile --promote` walks `playground/` and pulls high-confidence items into `entities/`.
2. **Question Coverage Score logger** — log every `brain_recall` from real sessions with top-3 scores so the metric in `program.md` becomes measurable.
3. **Output renderers** — Marp slides + matplotlib figures from playground articles.
4. **Multi-agent collaboration** — Karpathy's stated next step (SETI@home for personal brains; negative-result protocol).
5. **Synthetic data + finetuning** — once playground has ~1000 articles, distill into a tiny LM that knows Son.
6. **Second X crawl batch** — diversify the source pool beyond Karpathy (kepano, gwern, andy_matuschak, swyx, brennoferrari + `context engineering` / `claude.md best practices` searches). Currently weighing options A/B/C in this session.
