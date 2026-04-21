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

## Shipped (baseline as of 2026-04-21)

These predate the requirements log; captured here for context only.

- **Entity-first markdown vault** (`~/.brain/entities/<type>/<slug>.md`) — human-readable, git-diffable, Obsidian-renderable.
- **Harvest pipeline** — `harvest → prefilter → batch-extract → reconcile → clean` (Claude + Cursor agent transcripts).
- **Hybrid recall** — BM25 + dense + RRF fused via `brain_recall` MCP.
- **SQLite + FTS5 mirror** — fast index over the markdown source-of-truth.
- **MCP tool surface** (17 tools) — `brain_recall`, `brain_search`, `brain_semantic`, `brain_get`, `brain_recent`, `brain_identity`, `brain_stats`, `brain_audit`, `brain_history`, `brain_status`, `brain_notes`, `brain_note_get`, `brain_entities`, `brain_live_sessions`, `brain_live_tail`, `brain_live_coverage`. (Plus `brain_audit` interactive walker via the `brain audit` CLI — see 2026-04-21 entry below.)
- **Idle-gated launchd watcher** — `flock` singleton, skips LLM stages while a Claude/Cursor session is actively typing.
- **Persona-aware `brain init`** — onboarding wizard with developer/researcher/student/lawyer/doctor/custom presets.
- **Autoresearch loop** — `python -m brain.autoresearch` with fixed cycle budget (10-min wall-clock, 8 LLM calls), `playground/` agent sandbox, `program.md` spec. Runs autonomously via launchd every 30 min (see Phase 0.5 entry below). Auto-promotes high-confidence items per cycle, picker-driven concrete subjects per slot (see Phase 1 entries below).
- **SessionStart hooks auto-wired in both Claude Code and Cursor** — audit block injected at every session start (see 2026-04-21 entry below).
- **Question Coverage Score** — eval-set metric + live recall-ledger (rolling 7-day miss rate from real `brain_recall` calls).
- **X crawler toolkit** — `~/.brain/bin/x/` (timeline, user_tweets, search, conversation) using authenticated Playwright session.

---

## Decision Log

### 2026-04-21 — `brain audit` walker + `reviewed` decay fix the unbounded-queue bug

**Decision:** Add interactive `brain audit` CLI walker and a
`reviewed: YYYY-MM-DD` frontmatter field that suppresses single-source
low-confidence items from the SessionStart audit surface for
`REVIEW_DECAY_DAYS = 90` after the user explicitly confirms them.
After the decay window the item re-surfaces (a fact that was true 90
days ago is no longer guaranteed true). `contest` flips the same item
into the higher-priority contested bucket so it stays surfaced until
resolved; `resolve` clears the contested flag.

**Rationale:** The audit block told users to `Run `brain audit` to
walk merges interactively`, but (a) the `brain audit` subcommand did
not exist (only `python -m brain.audit`, which just re-printed the
same block — no walker), and (b) low-confidence single-source items
had **no removal mechanism at all** other than waiting for a second
source to bump `source_count` to 2. The same three insights from
2026-04-11 had been re-surfacing in every Claude/Cursor session for
ten days; the user reviewed them, decided they were correct, and the
brain just kept nagging. This was a real "the audit queue grows
forever" bug, not a UX preference.

**Pieces shipped:**
- `src/brain/audit.py` — added `AuditItem.path` + `AuditItem.extra`
  fields so the walker can act on items directly without re-parsing
  detail strings; `_reviewed_recently()` checks the decay window;
  `_low_confidence_items()` skips recently-reviewed items;
  `_set_frontmatter_field()` / `_drop_frontmatter_field()` are
  insert-or-update mutators that preserve all other YAML lines + the
  body verbatim; `mark_reviewed()`, `mark_contested()`,
  `resolve_contested()` are the public mutators; `walk()` is the
  interactive driver (`(k)eep / (c)ontest / (o)pen / (s)kip / (q)uit`
  for low-confidence; `(r)esolve / (o)pen / (s)kip / (q)uit` for
  contested; dedupe items hand off cleanly to `python -m brain.reconcile
  --apply` since merge-from-walker is non-trivial).
- `src/brain/cli.py` — `brain audit [--limit N] [--list]` subcommand.
  `--list` matches the legacy print-only behaviour; default drops into
  the walker.
- `tests/test_audit.py` — 15 new tests covering: recently-reviewed
  suppression, decay window expiry, malformed-date fail-open,
  end-to-end mark→top_n round-trip, idempotency, contest routing,
  resolve clearing, frontmatter preservation, and the walker
  (keep/contest/resolve/quit/EOF paths + `main --walk`).

**Edge cases handled:**
- Malformed `reviewed:` date (typo / hand-edit) fails *open* — better
  to nag than to silently disappear an item forever.
- `mark_reviewed` is idempotent same-day (returns False, no write) so
  walking twice in one day is a no-op.
- EOF / Ctrl-D in the walker is treated as `quit` so piped/empty
  stdin doesn't spin.
- `_input` parameter resolves `builtins.input` at call time (not
  import time) so `monkeypatch.setattr("builtins.input", …)` actually
  reaches the walker — a real test failure caught the import-time
  default-arg footgun before merge.
- Empty frontmatter after `_drop_frontmatter_field` removes the `---`
  fences entirely instead of leaving a `---\n---\n` stub.

**Status:** shipped — 269/269 tests pass (15 new in
`tests/test_audit.py`). Smoke-tested on real `~/.brain` via
`python -m brain.cli audit --list`.
**Source:** this session (2026-04-21) — user observed
"toi audit roi thi kien thuc duoc audit phai duoc remove ra khoi
queue chu phai khong" (audited items should leave the queue, right?).
**Linked code:** `src/brain/audit.py`, `src/brain/cli.py`,
`tests/test_audit.py`, `README.md` (CLI table + behaviour note).

---

### 2026-04-21 — SessionStart hooks auto-wired in BOTH Claude Code and Cursor

**Decision:** `bin/install.sh` now installs the brain SessionStart hook
into Cursor (`~/.cursor/hooks.json` → `sessionStart` event) in addition
to Claude Code (`~/.claude/settings.json` → `SessionStart` event), and
manages both files via the same idempotent merge module
(`brain.install_hooks`). Onboarding requires zero manual edits to
either app's config — `brain init` → `bin/install.sh` is the single
entry point and both surfaces light up.

**Rationale:** Cursor's hook API (introduced after the brain shipped)
exposes a `sessionStart` event that returns `additional_context` to be
prepended to the agent's initial system context — semantically
identical to what Claude's `SessionStart` already does for us. Before
this push:
- Cursor sessions never saw the `🧠 Brain audit —` block.
- Cursor harvest only ran via the launchd watcher every 5 min, so a
  fresh Cursor session had no chance of seeing freshly-ended Claude
  work without a multi-minute lag.
- The `~/.claude/settings.json` block itself was hand-edited on first
  install (install.sh never touched it), so a fresh-clone install on a
  new machine would silently skip the audit surface until the user
  noticed and copied the JSON by hand.

**Pieces shipped:**
- `templates/cursor/hooks.json.tmpl` — `sessionStart` entry pointing
  at `{{BRAIN_DIR}}/bin/cursor-session-start.sh` (10 s timeout).
- `templates/cursor/hooks/session-start.sh.tmpl` — the script Cursor
  invokes. Reads stdin (Cursor passes `session_id`,
  `is_background_agent`, `composer_mode`), runs harvest in the
  background (never blocks), runs `brain.audit` synchronously, emits
  `{"additional_context": "<block>"}` (or `{}` when clean). Always
  exits 0 — a noisy hook would block session startup. Background
  agents skip the surface (no human reading) but still trigger
  harvest as a side-effect.
- `templates/claude/settings.json.tmpl` — the previously-hand-written
  Claude block, now templated and machine-rendered. Adds explicit
  `BRAIN_DIR=…` env vars (the hand-edit had relied on shell rc),
  closing a subtle bug where Claude sessions launched outside an
  interactive shell would harvest into the wrong vault.
- `src/brain/install_hooks.py` — idempotent JSON-merge module for
  both files. Replaces only brain-owned entries on uninstall (matched
  by command substring), preserves siblings, backs up on every
  mutation, silently no-ops when the parent app dir is missing (so a
  Cursor-only or Claude-only machine doesn't get noisy warnings about
  the other). Single source of truth for what counts as "brain-owned"
  via `CLAUDE_BRAIN_MARKERS` + `CURSOR_BRAIN_MARKER`.
- `bin/install.sh` — renders both JSON templates + the Cursor hook
  script, then delegates the merge to `python -m brain.install_hooks
  install`. Step renumbered: `[5/8]` is now "register MCP server +
  SessionStart hooks (Claude Code + Cursor)".
- `bin/uninstall.sh` — symmetric teardown via `python -m
  brain.install_hooks remove`. Falls back gracefully if the brain
  package was already pip-uninstalled (PYTHONPATH points at the
  source tree as a last resort).
- `bin/doctor.sh` — two new green-line checks: "Claude SessionStart
  hook wired" and "Cursor sessionStart hook wired", with a `bad` line
  if the Cursor hooks.json points at a missing/non-executable
  `cursor-session-start.sh` (regression guard for future template
  renames).
- `templates/cursor/USER_RULES.md.tmpl` + `templates/claude/CLAUDE.md.tmpl`
  — agent-facing rules updated to drop the "Cursor has no SessionStart
  hook, call brain_audit yourself on first message" fallback. Both
  tools now follow the same surface-once contract on the same hook
  output. The fallback rule remains as a doctor-flagged degraded
  mode, so the agent still does the right thing on a half-installed
  machine.
- `src/brain/audit.py` + `src/brain/harvest_session.py` — module
  docstrings updated to reflect that both tools call them.

**Edge cases handled (locked in by `tests/test_install_hooks.py`, 20
cases):**
- Re-running install never duplicates entries (3× install → still
  exactly one SessionStart group).
- Sibling hooks (other `SessionStart` entries the user added by hand,
  other event hooks like `beforeShellExecution`) survive both install
  and remove.
- Unrelated top-level keys (`skipDangerousModePermissionPrompt`,
  custom user keys) survive.
- Malformed target JSON degrades to skip — we never clobber an
  in-progress hand-edit. Doctor flags it as not-wired.
- Empty groups are pruned on remove so the surrounding JSON stays
  clean.
- Missing app dirs (~/.claude or ~/.cursor) are silent skips, not
  warnings — many machines only run one of the two.

**Known Cursor-side limitation:** there are open Cursor forum bug
reports (Mar–Apr 2026) that `additional_context` from `sessionStart`
sometimes fails to actually reach the Agent Window. The hook still
emits valid JSON either way, and the USER_RULES degraded-mode rule
("if hook block is missing, call `brain_audit(limit=3)` yourself")
catches the case if/when it happens. When Cursor fixes the upstream
bug, no brain code changes are needed.

**Status:** shipped — 232 tests pass (20 new in
`tests/test_install_hooks.py`). Doctor green on real `~/.brain` after
end-to-end install: both hook lines tick, no warnings.
**Source:** this session (2026-04-21)
**Linked code:** `src/brain/install_hooks.py` (new),
`templates/cursor/hooks.json.tmpl` (new),
`templates/cursor/hooks/session-start.sh.tmpl` (new),
`templates/claude/settings.json.tmpl` (new),
`bin/install.sh`, `bin/uninstall.sh`, `bin/doctor.sh`,
`templates/cursor/USER_RULES.md.tmpl`,
`templates/claude/CLAUDE.md.tmpl`,
`src/brain/audit.py`, `src/brain/harvest_session.py`,
`tests/test_install_hooks.py` (new).

---

### 2026-04-21 — Phase 1 (fourth push): smarter round-robin — picker-driven concrete subjects

**Decision:** Replace `autoresearch.ROUND_ROBIN_QUESTIONS` (six static English
sentences) with a new module `brain.round_robin` that runs a tiny SQL picker
per slot to embed a *concrete* subject (a real entity name, decision, or
domain) into the question before the LLM ever sees it. The legacy generic
prompts stay as `FALLBACK_QUESTIONS`, used only when a slot's picker can't
find an eligible subject.

**Rationale:** With static prompts the LLM had to do its own retrieval to
decide *which* person/decision/issue to talk about. Once the obvious
candidate had been written about, every subsequent same-slot cycle would
print "saturated — nothing new to add" and burn ~100 s of LLM time
producing nothing. Concrete pre-targeted prompts eliminate that loop.
Added bonuses:
- **Saturation guard for slot 1 (cross-project person)** — pickers skip
  any subject already covered by an article/insight in the last 14 days,
  so the wheel naturally rotates through new people instead of resurfacing
  the same well-known one.
- **Young-vault relaxation (slots 3 + 5)** — strict cutoffs from
  `program.md` (decisions ≥30d, issues ≥14d) would starve a brand-new
  vault. Pickers try the strict cutoff first, then relax to ">3 days
  old" before giving up — so even a 10-day-old vault gets concrete
  prompts.
- **NOT-IN status filter (slots 3 + 5)** — vault uses 12+ free-text
  status values (`current`, `approved for commit`, `code_complete_pending_commits`,
  …). Picker now filters by an explicit *closed* set (`accepted`,
  `committed`, `fixed`, `resolved`, …) so the very common default
  `current` status is correctly auditable. Allow-listing `'open'` would
  have missed 99% of live decisions.

**Live verification on `~/.brain` (10-day-old vault):** 3 of 6 slots now
return concrete subjects (slot 2: a stale decision, slot 4: an unresolved
issue, slot 5: an under-covered domain). Slots 0/1/3 correctly fall back
to generic prompts (vault has nothing 60+ days old yet, the only multi-
project person was already covered in last 14 days, only 2 corrections
total). Mature vault should fire all 6 slots.

**Status:** `shipped`
**Source:** Cycle 22 follow-up — `brain status` showed round-robin firing
the same 6 generic prompts every cycle for ~10 hours; this kills the loop.
**Linked code:** `src/brain/round_robin.py` (new), `src/brain/autoresearch.py`
(`_next_round_robin` now delegates), `tests/test_round_robin.py` (25 tests
covering each picker + young-vault relaxation + slot rotation).

---

### 2026-04-21 — Phase 1 (third push): per-kind promote rules — hypotheses + contradictions go live

**Decision:** Replace `promote.py`'s flat `PROMOTE_MAP` (which silently routed
hypotheses into `entities/insights/` and ignored contradictions entirely) with
a `PROMOTE_RULES` dataclass dict that gives each playground subdir its own
target folder, frontmatter `type` + `status`, and gating thresholds. This
matches what `program.md` always promised — that hypotheses surface in
`entities/hypotheses/` with `status: unverified`, and contradictions auto-
promote to `entities/contradictions/` so they're queryable in MCP.

**Rationale:** A 24-hour audit of `~/.brain/playground/` found 6 contradictions
and 8 hypotheses backed up since the loop went live, none of them queryable.
The promote spec section in `program.md` had been written but never
implemented — a real "ghost code" gap. Closing it makes the autoresearch
agent's two highest-leverage output kinds (gap detection + falsifiable
claims) immediately useful in MCP recall instead of rotting in a sandbox
folder no one reads.

**Per-kind config:**
- `insights/` → `entities/insights/`, `status: current`, confidence=high (unchanged)
- `hypotheses/` → `entities/hypotheses/`, `status: unverified`, confidence=high|medium
- `contradictions/` → `entities/contradictions/`, `status: open`, confidence=high

The hypothesis bar drops to medium because the *whole point* of an unverified
hypothesis is to surface medium-conf claims for future evidence to verify or
refute — gating to `high` would defeat the spec. Contradictions stay strict
because a wrong contradiction wastes more attention than a missed one.

**Backward compat:** `_render_entity` accepts a `rule_override` so `rerender()`
can preserve legacy hypothesis-as-insight files in `entities/insights/`
without restamping them as `type: hypothesis` (which would leave them in the
wrong folder). New promotions land in the correct folders going forward.

**Status:** shipped — `feat(promote): per-kind rules` (`c1977e5`).
- 184/184 tests pass (added 3 new tests: hypotheses → hypotheses folder,
  contradictions → contradictions folder, contradiction medium-conf gate).
- Backfilled real `~/.brain`: 5 hypotheses + 5 contradictions promoted, both
  queryable via `brain_search` (verified end-to-end with two real queries).
- Coverage held at 0/15 miss = 0.0%.

---

### 2026-04-20 — Phase 1 (second push): autoresearch auto-promotes + live recall-ledger

Two follow-ups the moment Phase 1 first win flipped the miss rate to 0.0%.

- **Autoresearch auto-promotes every cycle.** `run_cycle()` now calls `brain.promote.run(apply=True, limit=1)` right after `_refresh_index_after_writes()` and before the post-cycle `score_coverage()`. A cap of 1/cycle keeps the bar high (promote itself still requires `confidence: high` + ≥2 refs + ≤14d), and because promotion happens *before* re-scoring, a freshly-promoted entity's Key Facts contribute to the same cycle's "after" number — the loop closes in one launchd tick instead of waiting for the next. Commit: `b89ba40`.
- **Live recall-ledger mode.** Every real MCP `brain_recall` / `brain_semantic` call now appends a `kind: "live"` row to `~/.brain/recall-ledger.jsonl` with `query`, `top_score`, and `miss` flag. `recall_metric.live_coverage(days=7)` aggregates into a rolling window; `brain status` shows it as a second line: `live recall : miss 23.1% · avg-top 0.578  [44 calls, 31 uniq, last 7d]`. Complements the eval-set score by answering "does the brain actually serve the questions Son keeps asking?" — a high eval score + high live miss rate is the signal to expand the eval set. Commit: `f055ba6`.
- **Docs note:** `docs/100x-autoresearch.md` still calls live mode "next likely upgrades" — leave that bullet there as a history of the plan and let this entry be the "shipped" marker instead of editing backwards.
- **Status:** 176 tests pass (14 new in `test_recall_metric.py`, 3 new in `test_status.py`). Doctor green.

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
