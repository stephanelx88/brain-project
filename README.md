# Brain

Persistent memory for Claude Code & Cursor. Captures what you learn across sessions — people, projects, decisions, insights — and exposes it back as MCP tools so the LLM can recall, search, and reason over your own history.

Works for any field. Pick a preset (developer, doctor, lawyer, researcher, student) or bring your own entity types.

A SessionStart hook (auto-wired into both Claude Code and Cursor) prepends a `🧠 Brain audit —` block to every new session so the LLM sees what's pending review before it answers your first message. An autonomous research loop runs every 30 min in the background, generating hypotheses, contradictions, and insights that get auto-promoted into the vault when high-confidence.

## Quick start

```bash
git clone https://github.com/<you>/brain-project ~/code/brain-project
cd ~/code/brain-project
pip install -e '.[init]'
brain init
```

`brain init` asks **two** things — pick a profile, point it at a vault. Everything else is automatic.

```
? Pick your profile:
  ❯ Software developer   — people, projects, decisions, insights
    Doctor               — patients, conditions, treatments, studies
    Lawyer               — clients, cases, statutes, decisions
    Researcher           — papers, experiments, hypotheses, datasets
    Student              — courses, professors, assignments, notes
    Custom               — pick your own folders

? Vault path (your Obsidian vault, or any folder):
  ▸ /Users/you/Documents/MyVault
```

Then it auto-configures everything else:

- writes `<vault>/brain-config.yaml` and seeds entity folders for the preset
- renders `identity/who-i-am.md` (name pulled from `git config user.name`)
- exports `BRAIN_DIR` into your shell rc (`~/.zshrc` etc.)
- registers the MCP server with Claude Code **and** Cursor
- wires SessionStart hooks into both `~/.claude/settings.json` and `~/.cursor/hooks.json` (audit block injected at every session start)
- loads the launchd watchers (auto-extract + semantic worker + autoresearch tick)
- downloads the embedding model (~120 MB, one-time)
- runs `bin/doctor.sh` to verify everything green

Restart Claude Code / Cursor afterwards to pick up the brain MCP tools.

The vault you point at can be a brand-new folder **or an existing Obsidian vault** — brain creates its own `entities/`, `raw/`, `identity/`, `logs/` dirs alongside whatever notes are already there. They coexist; brain even indexes your hand-written notes via `brain_recall` / `brain_notes`.

> macOS only for the auto-extract launchd watcher. The Python package itself is portable; PRs for `systemd-user` are welcome.

## What you get

```
<your vault>/                # path you supplied to `brain init`
  brain-config.yaml          # preset, identity, llm provider
  identity/                  # who-i-am, preferences, corrections
  entities/<type>/           # one .md per entity, folders match your preset
  raw/                       # transient session summaries waiting for extraction
  playground/                # autoresearch sandbox — articles/hypotheses/
                             # contradictions/insights/questions before promotion
  index.md                   # auto-generated entity catalog
  log.md                     # extraction log
  eval-queries.md            # Question Coverage Score eval set (one query/line)
  recall-ledger.jsonl        # every brain_recall call logged for live coverage
  .dedupe.ledger.json        # canonical dedupe verdicts (audit reads this)
  .extract.lock.d/           # flock dir — singleton guard for the watcher
  bin/                       # rendered watcher + doctor scripts
  logs/                      # auto-extract + autoresearch logs
  (your existing notes…)     # untouched — brain reads but never deletes them
```

The pipeline:

```
Claude / Cursor session  ──► SessionStart hook  ──► brain audit (3 items, ≤10 lines)
                              │                       injected into agent context
                              ▼
                        launchd watcher (every 5 min)
                              │
                              ▼
                        harvest_session.py   (transcripts → raw/)
                              │
                              ▼
                        auto_extract.py      (LLM → entities/, index, log)
                              │
                              ▼
                        reconcile + clean    (dedup, tidy, dedupe ledger)

                              ╭── parallel ──╮
                              ▼              ▼
                  semantic worker       autoresearch (every 30 min)
                  (embeddings, RRF)        │
                                           ▼
                                    playground/articles|hypotheses|
                                    contradictions|insights|questions
                                           │
                                           ▼
                                    promote.py (≥2 refs, ≤14d, conf=high)
                                           │
                                           ▼
                                    entities/insights|hypotheses|contradictions

                Anywhere in your conversation, the LLM calls
                  brain_recall · brain_search · brain_get · brain_audit ·
                  brain_status · brain_recent · brain_identity · brain_history ·
                  brain_notes · brain_note_get · brain_entities · brain_semantic ·
                  brain_stats · brain_live_sessions · brain_live_tail ·
                  brain_live_coverage
                via the brain MCP server, which reads the same vault back
                as a SQLite + FTS5 + semantic mirror.
```

No slash commands. No memory of "did I save that?". You work normally; the brain captures, recalls, and surfaces what needs your attention.

## CLI

| Command | What it does |
|---|---|
| `brain init` | Interactive setup wizard (profile + vault → install) |
| `brain init --preset doctor --vault ~/MyVault --yes` | Non-interactive setup |
| `brain status` | Operational dashboard (launchd jobs, in-flight locks, ledgers, coverage) |
| `brain status --json` | Same data as JSON for scripting / MCP consumers |
| `brain status -v` | Adds per-type entity counts |
| `brain audit` | Walk top-N items needing review (keep / contest / open / skip) |
| `brain audit --list` | Just print the audit block, no walker |
| `brain doctor` | Run `bin/doctor.sh` health check |
| `brain config` | Print resolved `brain-config.yaml` |

`brain audit` stamps `reviewed: YYYY-MM-DD` into the entity's frontmatter when you pick `keep`, suppressing it from the audit surface for 90 days (decay window — facts genuinely do go stale). `contest` flips it into the higher-priority contested bucket so it stays surfaced until you resolve it.

Lower-level entry points are still exposed for scripting:

```bash
python3 -m brain.harvest_session    # one-shot harvest of pending Claude/Cursor sessions
python3 -m brain.auto_extract       # one-shot extraction of pending raw files
python3 -m brain.ingest_notes       # one-shot pickup of user-authored markdown
python3 -m brain.autoresearch       # one cycle of the autonomous research loop
python3 -m brain.promote            # promote high-confidence playground items into entities/
python3 -m brain.reconcile          # full duplicate / conflict report
python3 -m brain.recall_metric      # Question Coverage Score over the eval set
python3 -m brain.audit --walk       # same as `brain audit` (legacy entry point)
python3 -m brain.mcp_server         # MCP stdio server (auto-registered by install.sh)
```

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

The discovery logic in `brain/config.py` picks up any folder under
`entities/`, so manual edits work too.

### Identity

Edit `<vault>/identity/who-i-am.md` (and `preferences.md`,
`corrections.md`). The MCP `brain_identity` tool returns these on
demand, and the install also wires them into `~/.claude/CLAUDE.md` so
the LLM sees them at session start.

### LLM provider

`brain init` writes `llm_provider: claude` by default. Switch by
editing `brain-config.yaml`:

```yaml
llm_provider: openai   # or: ollama
```

The extraction pipeline picks this up on the next run.

## Architecture

```
src/brain/
  init.py             interactive wizard
  cli.py              top-level `brain` dispatcher
  install_hooks.py    JSON-merge wirer for Claude + Cursor SessionStart hooks
  presets/*.yaml      persona definitions
  prompts/*.md        LLM prompts for extract / autoresearch
  config.py           paths + entity-type discovery
  harvest_session.py  scans Claude/Cursor session JSONLs → raw/
  auto_extract.py     batched LLM extraction        (raw/ → entities/)
  apply_extraction.py single source of truth for brain mutations
  ingest_notes.py     auto-pickup of user-authored markdown
  autoresearch.py     30-min autonomous loop — reads brain, writes playground/
  round_robin.py      picker-driven concrete subjects for autoresearch slots
  promote.py          playground/ → entities/ promotion (per-kind rules)
  recall_metric.py    Question Coverage Score (eval + live ledger)
  reconcile.py        conflict / duplicate sweep
  clean.py            stale-data sweep + MOC regen
  prefilter.py        strips low-signal tool noise from transcripts
  audit.py            top-N surface + interactive walker (`brain audit`)
  status.py           operational dashboard (`brain status` / brain_status MCP)
  db.py               SQLite + FTS5 mirror of the vault
  semantic.py         sentence-transformer embeddings + RRF fusion
  mcp_server.py       MCP stdio server exposing brain_* tools
  entities.py         entity CRUD
  index.py            rebuilds index.md
  slugify.py          name → filesystem slug
  log.py              append-only log
  git_ops.py          stage + commit
```

## Testing

```bash
pip install -e '.[dev]'
pytest -v
```

## Cost

- Extraction uses your Claude SDK (sonnet by default — ~$0.001 per session).
- Autoresearch loop runs every 30 min, capped at 8 LLM calls / 10-min wall-clock per cycle (~$0.001-$0.01 per cycle depending on vault size; ~$0.05-$0.50 / day).
- Interactive sessions use whatever model you already pay for (Claude Max / Pro / API).
- Local embedding model is `paraphrase-multilingual-MiniLM-L12-v2` (~120 MB, downloaded once).

## License

MIT.
