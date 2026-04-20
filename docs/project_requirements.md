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
- **Autoresearch loop** — `python -m brain.autoresearch` with fixed cycle budget (10-min wall-clock, 8 LLM calls), `playground/` agent sandbox, `program.md` spec.
- **X crawler toolkit** — `~/.brain/bin/x/` (timeline, user_tweets, search, conversation) using authenticated Playwright session.

---

## Decision Log

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
