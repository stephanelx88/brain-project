# Brain v0.2 — Autoresearch (the 100x lift)

This doc records the design + implementation of the autoresearch lift,
driven by an X crawl of @karpathy's "LLM Knowledge Bases" + autoresearch
threads (Mar–Apr 2026) plus the surrounding builder community.

> The crawl's raw output lives in `~/.brain/raw/x-crawl/` (10 conversation
> threads, 10 keyword searches, ~1100 unique tweets). The synthesized
> findings live in `~/.brain/raw/session-2026-04-20-karpathy-brain-research.md`
> and `~/.brain/Karpathy AutoResearch Pattern.md`.

## What was already great

- Entity-first markdown vault → human-readable + git-diffable + Obsidian-renderable. Karpathy's spec arrived at the same shape.
- `harvest → prefilter → batch-extract → reconcile → clean` pipeline is a clean compiler from raw sessions to entities.
- BM25 + dense + RRF hybrid recall in one MCP tool (`brain_recall`).
- Idle-time-gated launchd watcher with `flock` singleton — no dual-instance freezes.
- SQLite + FTS5 mirror as a fast index over the markdown source-of-truth.
- Direct Anthropic SDK call when API key is present (3-5× faster than CLI subprocess).

## What was missing vs Karpathy's spec

| Karpathy capability | Brain (before) | Status |
|---|---|---|
| `raw/` source ingest | only Claude/Cursor sessions | ✅ added: X crawler + `bin/x/ingest.py` adapter; future adapters trivial |
| LLM-compiled cross-entity articles | atomic facts only | ✅ added: `playground/articles/` via autoresearch |
| Output rendering (Marp slides, matplotlib) | none | ⏭ not yet — agent could write `playground/outputs/`; defer until needed |
| Queries "add up" — answers filed back | none | ✅ added: every cycle's findings land in `playground/` |
| LLM linting / health checks | structural only | ✅ partial: contradictions/hypotheses queues; full lint pass next |

| Autoresearch primitive | Brain (before) | Status |
|---|---|---|
| Single human-edited spec | none | ✅ `~/.brain/program.md` (v0.1) |
| Single agent-edited surface | n/a (agent had no surface) | ✅ `~/.brain/playground/` sandbox |
| Fixed cycle budget | n/a | ✅ 10-min wall-clock, 8 LLM calls, 5 outputs/cycle |
| Crisp metric | n/a | ✅ Question Coverage Score (defined in `program.md`) |
| Cycle log | only the always-on `log.md` | ✅ `~/.brain/research-log.md` + per-cycle `playground/cycle-NNNN.md` |
| Idle-time gating | shared with auto-extract | ✅ same `IDLE_THRESHOLD` mechanism |

| Community-validated patterns | Brain (before) | Status |
|---|---|---|
| Vault separation (kepano) | one vault, no sandbox | ✅ `playground/` is the agent sandbox |
| O(n^k) cross-entity synthesis (elvissun) | per-query only | ✅ articles cycle does cross-entity narratives |
| Negative-result memory (kathysyock) | corrections.md only | ✅ `playground/hypotheses/` + reconcile auto-promotes to `entities/hypotheses/` |
| Time-decay weighting (Karpathy 03-25) | none | ✅ `_recency_factor` in `semantic.hybrid_search` (env-tunable halflife) |
| Git-as-episodic-memory (shikhr_) | not exposed | ✅ `brain_history` MCP tool |
| Meta-iteration on `program.md` (kristoph) | n/a | ⏭ deliberately deferred — intent drift risk |

## What changed in the codebase

```
src/brain/autoresearch.py     [new, 380 lines] — the loop
src/brain/semantic.py         [+50 lines]      — recency factor in hybrid_search
src/brain/mcp_server.py       [+60 lines]      — brain_history tool
~/.brain/program.md           [new]            — the autoresearch spec
~/.brain/research-queue.md    [empty, created on first cycle]
~/.brain/research-log.md      [append-only]    — cycle history
~/.brain/playground/          [agent sandbox]
~/.brain/Karpathy AutoResearch Pattern.md [filled in]
~/.brain/bin/x/_session.py    [new]            — auth Playwright context
~/.brain/bin/x/extract_chrome_cookies.py [new] — cookie pull from local Chrome
~/.brain/bin/x/whoami.py      [new]            — login verification
~/.brain/bin/x/timeline.py    [new]            — home/following timeline
~/.brain/bin/x/user_tweets.py [new]            — profile crawl
~/.brain/bin/x/search.py      [new]            — keyword search crawl
~/.brain/bin/x/conversation.py [new]           — thread crawl
~/.brain/bin/x/crawl_karpathy_brain.py [new]   — orchestrator (one-off)
~/.brain/bin/x/ingest.py      [new]            — JSONL → raw/session-*.md adapter
```

No new third-party dependencies (Playwright + pycryptodome already present
on the dev box; `bin/x/` is OUTSIDE the brain-project source tree, so the
brain pkg stays clean).

## How to use it

```bash
# One autoresearch cycle (respects idle gate; bypass with --no-idle-check):
python -m brain.autoresearch --cycles 1

# Force a specific question, skip the queue:
python -m brain.autoresearch --question "decision-audit: ..." --no-idle-check

# Dry-run to inspect the prompt without spending tokens:
python -m brain.autoresearch --dry-run --no-idle-check --question "..."

# Crawl X for a topic and feed it into the brain:
python ~/.brain/bin/x/search.py "your query" --count 60 > /tmp/x.jsonl
python ~/.brain/bin/x/ingest.py your-topic /tmp/x.jsonl
# next launchd auto-extract pass picks it up automatically
```

## Recommended schedule (launchd)

Add to `~/Library/LaunchAgents/com.son.brain-autoresearch.plist`:

```xml
<key>StartCalendarInterval</key>
<array>
  <dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer></dict>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
</array>
<key>ProgramArguments</key>
<array>
  <string>/Users/son/.pyenv/versions/3.12.12/bin/python</string>
  <string>-m</string>
  <string>brain.autoresearch</string>
  <string>--cycles</string>
  <string>2</string>
</array>
```

Three cycles a night, 2 questions per run = 6 cycles/morning. Per `program.md` 10-min budget, that's ~60 min total nightly compute, well under the 8-hour idle window.

## Open knobs

- **`BRAIN_AR_BUDGET_S`** (default 600) — cycle wall-clock.
- **`BRAIN_AR_MAX_LLM`** (default 8) — LLM calls per cycle.
- **`BRAIN_TIME_DECAY`** (default 1) — set 0 to disable recency factor.
- **`BRAIN_TIME_HALFLIFE_D`** (default 180) — recency halflife in days.
- **`BRAIN_AR_IDLE_S`** (default 180) — seconds of inactivity before a cycle is allowed.

## Next likely upgrades (NOT yet built)

1. **Reconcile-with-promote** — `brain reconcile --promote` walks `playground/` and pulls high-confidence items into `entities/` (currently the human does this by hand).
2. **Question Coverage Score logger** — log every `brain_recall` call from real sessions with the top-3 scores so the metric in `program.md` becomes measurable rather than aspirational.
3. **Output renderers** — Marp slides + matplotlib figures from articles (per Karpathy's "render answers as markdown/Marp/png" pattern).
4. **Multi-agent collaboration** — Karpathy's stated next step. SETI@home for personal brains. A negative-result protocol so multiple Sons (or Son's variants) don't repeat dead ends.
5. **Synthetic data + finetuning** — once the playground has ~1000 high-quality articles, distill into a tiny LM that knows Son.

## Where the work came from

X conversations crawled on 2026-04-20 (logged in as @Minimalist882):

- @karpathy threads: `LLM Knowledge Bases` (56k likes), `autoresearch` repo intro (28k likes), `idea file` (26k likes), `memory distraction` (21k likes), `autoresearch tuning nanochat` (19k likes), `autoresearch must be collaborative` (7k), `DeepWiki/malleability` (7k), `Farzapedia` (6k).
- Highest-signal community replies: @lexfridman, @kepano (Obsidian), @omarsar0 (DAIR.AI), @elvissun, @dharmesh, @kathysyock, @shikhr_, @brennoferrari (independent obsidian-mind builder), @kristoph, @chadwahl.
- Karpathy's `autoresearch` repo README (74k stars) for the program.md/train.py/fixed-budget/single-metric architecture.
