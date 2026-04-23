<h1 align="center">🧠 Brain</h1>

<div align="center">

<i>Persistent memory for Claude Code &amp; Cursor — captures what you learn across sessions and exposes it back as MCP tools.</i>

<br><br>

<a href="https://github.com/stephanelx88/brain-project/stargazers"><img src="https://img.shields.io/github/stars/stephanelx88/brain-project" alt="Stars Badge"/></a>
<a href="https://github.com/stephanelx88/brain-project/network/members"><img src="https://img.shields.io/github/forks/stephanelx88/brain-project" alt="Forks Badge"/></a>
<a href="https://github.com/stephanelx88/brain-project/pulls"><img src="https://img.shields.io/github/issues-pr/stephanelx88/brain-project" alt="Pull Requests Badge"/></a>
<a href="https://github.com/stephanelx88/brain-project/issues"><img src="https://img.shields.io/github/issues/stephanelx88/brain-project" alt="Issues Badge"/></a>
<a href="https://github.com/stephanelx88/brain-project/graphs/contributors"><img alt="GitHub contributors" src="https://img.shields.io/github/contributors/stephanelx88/brain-project?color=2b9348"></a>
<img src="https://img.shields.io/badge/license-MIT-2b9348" alt="License Badge"/>
<img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python Badge"/>
<img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey" alt="Platform Badge"/>

<br><br>

<i>Works for any field — pick a preset (developer, doctor, lawyer, researcher, student) or bring your own entity types.</i>

</div>

---

### Contents

- [Quick start](#quick-start)
- [What you get](#what-you-get)
- [Pipeline](#pipeline)
- [MCP tools](#mcp-tools)
- [CLI](#cli)
- [Customising](#customising)
- [Architecture](#architecture)
- [Testing](#testing)
- [Cost](#cost)
- [License](#license)

---

## Quick start

```bash
git clone https://github.com/stephanelx88/brain-project ~/code/brain-project
cd ~/code/brain-project
pip install -e '.[init]'
brain init
```

`brain init` is an interactive wizard — pick a profile, point it at a vault. Everything else is automatic.

```
? Found existing vault at ~/.brain. Keep using it? (Y/n)

? Vault path (your Obsidian vault, or any folder):
  ▸ ~/Documents/MyVault

? Pick your profile:
  ❯ Software developer   — people, projects, codebases, technical decisions
    Doctor               — patients, conditions, treatments, studies
    Lawyer               — clients, cases, statutes, decisions
    Researcher           — papers, experiments, hypotheses, datasets
    Student              — courses, professors, assignments, notes
    Custom               — pick your own folders
```

Then it auto-configures everything else:

- writes `<vault>/brain-config.yaml` and seeds entity folders for the preset
- renders `identity/who-i-am.md` (name pulled from `git config user.name`)
- copies the default auto-clean rules into `<vault>/auto_clean.yaml`
- exports `BRAIN_DIR` into your shell rc (`~/.zshrc` etc.)
- registers the MCP server with Claude Code **and** Cursor
- wires SessionStart hooks into both `~/.claude/settings.json` and `~/.cursor/hooks.json`
- loads two launchd agents: `brain-auto-extract`, `brain-semantic-worker`
- downloads the embedding model (`paraphrase-multilingual-MiniLM-L12-v2`, ~120 MB, one-time)
- runs `bin/doctor.sh` to verify everything is green

Restart Claude Code / Cursor afterwards to pick up the brain MCP tools.

The vault you point at can be a brand-new folder **or an existing Obsidian vault** — brain creates its own `entities/`, `raw/`, `identity/`, `logs/` dirs alongside whatever notes are already there. Brain reads your hand-written notes via `brain_recall` / `brain_notes` but never deletes them.

> **Platform:** macOS (launchd) and Linux (systemd `--user` timers). The installer dispatches automatically on `uname -s`. On headless Linux servers run `loginctl enable-linger $USER` once so user units run outside a login session.

---

## What you get

```
<your vault>/                # path you supplied to `brain init`
  brain-config.yaml          # preset, identity, llm provider
  auto_clean.yaml            # pre-audit deletion rules (copied from defaults)
  identity/                  # who-i-am, preferences, corrections
  entities/<type>/           # one .md per entity, folders match your preset
  raw/                       # transient session summaries waiting for extraction
  playground/                # research sandbox — articles/hypotheses/
                             # contradictions/insights/questions before promotion
  .brain.rdf/                # Oxigraph RDF triple store (typed relationships)
  index.md                   # auto-generated entity catalog
  log.md                     # extraction log
  eval-queries.md            # Question Coverage Score eval set (one query/line)
  recall-ledger.jsonl        # every brain_recall call logged for live coverage
  .dedupe.ledger.json        # canonical dedupe verdicts (audit reads this)
  .extract.lock.d/           # flock dir — singleton guard for the watcher
  bin/                       # rendered watcher + doctor scripts
  logs/                      # auto-extract + semantic-worker logs
  (your existing notes…)     # untouched — brain reads but never deletes them
```

---

## Pipeline

```
Claude / Cursor session  ──► SessionStart hook  ──► brain audit (3 items, ≤10 lines)
                              │                       injected into agent context
                              ▼
                        launchd watcher (WatchPaths + 5-min safety poll)
                              │
                              ▼
                        harvest_session.py   (transcripts → raw/)
                              │
                              ▼
                        prefilter.py         (strip low-signal tool noise)
                              │
                              ▼
                        auto_extract.py      (LLM → entities/, index, log)
                        note_extract.py      (LLM → entities/ from user notes)
                              │
                              ▼
                        reconcile + clean    (lexical dedupe, tidy)
                              │
                              ▼
                        dedupe.py            (semantic dedupe, LLM-judged)

                              ▼
                        semantic worker
                        (embeddings, RRF)

                  [optional, manual]
                        playground/articles|hypotheses|
                        contradictions|insights|questions
                              │
                              ▼
                        promote.py (≥2 refs, ≤14d, conf=high)
                              │
                              ▼
                        entities/insights|hypotheses|contradictions

                Anywhere in your conversation, the LLM calls brain_*
                tools via the MCP server, which reads the same vault
                back as a SQLite + FTS5 + semantic + RDF mirror.
```

No slash commands. No memory of "did I save that?". You work normally; the brain captures, recalls, and surfaces what needs your attention.

---

## MCP tools

20 tools exposed by `brain.mcp_server`, grouped by purpose.

**Recall & search**

| Tool | What it does |
|---|---|
| `brain_recall` | **Default** — hybrid BM25 + semantic recall with RRF fusion |
| `brain_search` | Pure BM25 fact search |
| `brain_semantic` | Pure dense-vector semantic search |
| `brain_entities` | Entity-name search (when you want the entity, not facts) |
| `brain_get` | Full markdown of one entity by `type + name` |
| `brain_recent` | Entities updated in the last N hours |
| `brain_history` | Git commit history for an entity/note path |

**Notes**

| Tool | What it does |
|---|---|
| `brain_notes` | Search user-written notes anywhere in the vault |
| `brain_note_get` | Full body of a vault note by path |

**Identity**

| Tool | What it does |
|---|---|
| `brain_identity` | Identity + recent corrections (session-start payload) |

**Audit & introspection**

| Tool | What it does |
|---|---|
| `brain_audit` | Top-N items needing review (contested / dedupe candidates / low-confidence) |
| `brain_status` | Operational dashboard — launchd jobs, locks, ledgers, coverage |
| `brain_stats` | High-level counts |
| `brain_live_coverage` | Rolling recall coverage from the live `brain_recall` ledger |

**Live peer sessions**

| Tool | What it does |
|---|---|
| `brain_live_sessions` | List Claude/Cursor sessions alive right now |
| `brain_live_tail` | Last N turns of one peer session |

**Failure ledger (self-correction substrate)**

| Tool | What it does |
|---|---|
| `brain_failure_record` | Append a failure row (recall miss, extraction bug, etc.) |
| `brain_failure_list` | List recorded failures (optionally filter by source/tag/unresolved) |

**Graph (Oxigraph RDF triple store)**

| Tool | What it does |
|---|---|
| `brain_graph_query` | Execute a SPARQL SELECT against typed relationships |
| `brain_graph_neighbors` | Return neighbors of an entity in the graph |

---

## CLI

| Command | What it does |
|---|---|
| `brain init` | Interactive setup wizard (vault + profile → install) |
| `brain init --preset developer --vault ~/MyVault --yes` | Non-interactive setup |
| `brain status` | Operational + vault dashboard |
| `brain status --json` | Same data as JSON for scripting / MCP consumers |
| `brain status -v` | Adds per-type entity counts |
| `brain doctor` | Run `bin/doctor.sh` health check |
| `brain config` | Print resolved `brain-config.yaml` |
| `brain auto-clean [--dry-run]` | Apply auto-clean rules to entities |
| `brain failure record --source … [--tool …] [--query …] [--correction …]` | Append a failure row |
| `brain failure list [--source …] [--tag …] [--unresolved] [-n N] [--json]` | List recorded failures |
| `brain failure resolve <id> --patch … --outcome …` | Mark a failure resolved |

The audit walker (`python3 -m brain.audit --walk`) stamps `reviewed: YYYY-MM-DD` into the entity's frontmatter when you pick `keep`, suppressing it from the audit surface for 90 days (decay window — facts genuinely do go stale). `contest` flips it into the higher-priority contested bucket so it stays surfaced until you resolve it.

Lower-level module entry points, for scripting or cron:

```bash
python3 -m brain.harvest_session    # one-shot harvest of pending Claude/Cursor sessions
python3 -m brain.auto_extract       # one-shot LLM extraction from raw/
python3 -m brain.note_extract       # one-shot LLM extraction from user notes
python3 -m brain.ingest_notes       # one-shot pickup of user-authored markdown
python3 -m brain.promote            # promote high-confidence playground items
python3 -m brain.reconcile          # lexical duplicate / conflict sweep
python3 -m brain.dedupe              # semantic dedupe (LLM-judged)
python3 -m brain.auto_clean         # apply auto-clean rules
python3 -m brain.recall_metric      # Question Coverage Score over the eval set
python3 -m brain.audit --walk       # interactive audit walker (keep / contest / open / skip)
python3 -m brain.audit              # print the compact audit block (same as SessionStart hook)
python3 -m brain.mcp_server         # MCP stdio server (auto-registered by install.sh)
```

---

## Customising

### Pick different folders later

Re-run `brain init` and pick a different preset, or edit
`<vault>/brain-config.yaml`'s `entity_types:` list and create the
matching folders:

```yaml
entity_types:
  - patients
  - conditions
  - treatments
```

`brain/config.py`'s discovery logic picks up any folder under
`entities/`, so manual edits work too.

### Identity

Edit `<vault>/identity/who-i-am.md` (and `preferences.md`,
`corrections.md`). The MCP `brain_identity` tool returns these on
demand, and the install also wires them into `~/.claude/CLAUDE.md` so
the LLM sees them at session start.

### LLM provider

`brain init` writes `llm_provider: claude` by default. The Anthropic
SDK is auto-detected at runtime if `pip install -e '.[sdk]'` is
installed; otherwise the pipeline shells out to `claude --print`.
Switch providers by editing `brain-config.yaml`:

```yaml
llm_provider: openai   # or: ollama
```

### Auto-clean rules

`<vault>/auto_clean.yaml` declares regex patterns for entity types
that should be deleted *before* the audit surface shows them. After
each interactive audit, the deleted-item list is fed back into
this file so future audits self-reinforce. See `src/brain/presets/auto_clean.yaml`
for the factory defaults.

---

## Architecture

```
src/brain/
  # CLI + setup
  init.py             interactive wizard
  cli.py              top-level `brain` dispatcher
  install_hooks.py    JSON-merge wirer for Claude + Cursor SessionStart hooks
  presets/*.yaml      persona definitions (developer, doctor, lawyer, …)
  prompts/*.md        LLM prompts for extract / dedupe / reconcile
  config.py           paths + entity-type discovery

  # Harvest + extraction
  harvest_session.py  scans Claude/Cursor session JSONLs → raw/
  prefilter.py        strips low-signal tool noise from transcripts
  auto_extract.py     batched LLM extraction        (raw/ → entities/)
  note_extract.py     LLM extraction from user-authored vault notes
  ingest_notes.py     auto-pickup of user-authored markdown
  apply_extraction.py single source of truth for brain mutations

  # Promotion (manual)
  promote.py          playground/ → entities/ promotion (per-kind rules)

  # Audit + cleanup
  audit.py            top-N surface + interactive walker
  auto_clean.py       auto-clean rules engine (pre-audit deletion by regex)
  reconcile.py        lexical duplicate / conflict sweep
  dedupe.py           semantic dedupe — LLM-judged near-duplicates
  dedupe_judge.py     LLM judge + JSON parser (extracted from dedupe)
  dedupe_ledger.py    persistent dedupe verdicts (skip already-judged pairs)
  clean.py            stale-data sweep + MOC regen
  failures.py         structured failure ledger (self-correction substrate)
  recall_metric.py    Question Coverage Score (eval + live ledger)

  # Graph (RDF triple store)
  graph.py            Oxigraph wrapper — typed relationships
  triple_audit.py     pending-triple queue + audit walker (< 0.8 confidence)
  triple_rules.py     learned extraction rules from user audit decisions

  # Storage + search
  db.py               SQLite + FTS5 mirror of the vault
  semantic.py         sentence-transformer embeddings + RRF fusion
  semantic_worker.py  persistent embedding worker (avoids 10 s cold-load)
  entities.py         entity CRUD
  index.py            rebuilds index.md
  log.py              append-only log
  slugify.py          name → filesystem slug
  io.py               atomic-write primitives (crash-safe)
  git_ops.py          stage + commit

  # MCP + live
  mcp_server.py       MCP stdio server exposing brain_* tools
  live_sessions.py    live Claude/Cursor peer session discovery + tail
  status.py           operational dashboard (`brain status` / brain_status)
  resource_guard.py   adaptive clearance levels for background jobs
```

---

## Testing

```bash
pip install -e '.[dev]'
pytest -v
```

---

## Cost

- **Extraction** uses your Claude SDK (sonnet by default — ~$0.001 per session).
- **Autoresearch loop** runs every 30 min, capped at 8 LLM calls / 10-min wall-clock per cycle (~$0.001–$0.01 per cycle depending on vault size; ~$0.05–$0.50 / day).
- **Interactive sessions** use whatever model you already pay for (Claude Max / Pro / API).
- **Local embedding model** is `paraphrase-multilingual-MiniLM-L12-v2` (~120 MB, downloaded once).

---

## License

MIT.
