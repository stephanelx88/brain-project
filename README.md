# Brain

Persistent memory for Claude Code & Cursor. Captures what you learn across sessions — people, projects, decisions, insights — and exposes it back as MCP tools so the LLM can recall, search, and reason over your own history.

Works for any field. Pick a preset (developer, doctor, lawyer, researcher, student) or bring your own entity types.

## Quick start

```bash
git clone https://github.com/<you>/brain-project ~/code/brain-project
cd ~/code/brain-project
pip install -e '.[init]'
brain init
```

`brain init` is one interactive prompt:

```
? What's your field?
  ❯ Software developer  — people, projects, decisions, insights
    Doctor              — patients, conditions, treatments, studies
    Lawyer              — clients, cases, statutes, decisions
    Researcher          — papers, experiments, hypotheses, datasets
    Student             — courses, professors, assignments, notes
    Custom              — pick your own folders
```

It then collects your name + role, writes `~/.brain/brain-config.yaml`, seeds entity folders for the preset, renders `identity/who-i-am.md`, and delegates to `bin/install.sh` for the mechanical work — vault git init, MCP server registration, launchd watcher, embedding model download, and a final `bin/doctor.sh` health check.

Restart Claude Code / Cursor afterwards to pick up the brain MCP tools.

> macOS only for the auto-extract launchd watcher. The Python package itself is portable; PRs for `systemd-user` are welcome.

## What you get

```
~/.brain/
  brain-config.yaml      # preset, identity, llm provider
  identity/              # who-i-am, preferences, corrections
  entities/<type>/       # one .md per entity, folders match your preset
  raw/                   # transient session summaries waiting for extraction
  index.md               # auto-generated entity catalog
  log.md                 # extraction log
  bin/                   # rendered watcher + doctor scripts
  logs/                  # auto-extract logs
```

The pipeline:

```
Claude / Cursor session  ──► launchd watcher
                              │
                              ▼
                        harvest_session.py   (transcripts → ~/.brain/raw/)
                              │
                              ▼
                        auto_extract.py      (LLM → entities/, index, log)
                              │
                              ▼
                        reconcile + clean    (dedup, tidy)

                Anywhere in your conversation, the LLM calls
                  brain_recall · brain_search · brain_get · ...
                via the brain MCP server, which reads the same
                vault back as a SQLite + FTS5 + semantic mirror.
```

No slash commands. No memory of "did I save that?". You work normally; the brain captures and recalls.

## CLI

| Command | What it does |
|---|---|
| `brain init` | Interactive setup wizard (presets + identity + install) |
| `brain init --preset doctor --yes` | Non-interactive setup |
| `brain status` | Vault location + per-type entity counts |
| `brain doctor` | Run `bin/doctor.sh` health check |
| `brain config` | Print resolved `brain-config.yaml` |

Lower-level entry points are still exposed for scripting:

```bash
python3 -m brain.harvest_session    # one-shot harvest of pending Claude sessions
python3 -m brain.auto_extract       # one-shot extraction of pending raw files
python3 -m brain.ingest_notes       # one-shot pickup of user-authored markdown
python3 -m brain.mcp_server         # MCP stdio server (auto-registered by install.sh)
```

## Customising

### Pick different folders later

Re-run `brain init` and pick a different preset, or edit
`~/.brain/brain-config.yaml`'s `entity_types:` list and create the
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

Edit `~/.brain/identity/who-i-am.md` (and `preferences.md`,
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
  init.py             interactive wizard            (NEW)
  cli.py              top-level `brain` dispatcher  (NEW)
  presets/*.yaml      persona definitions           (NEW)
  config.py           paths + entity-type discovery
  harvest_session.py  scans Claude/Cursor session JSONLs → raw/
  auto_extract.py     batched LLM extraction        (raw/ → entities/)
  apply_extraction.py single source of truth for brain mutations
  ingest_notes.py     auto-pickup of user-authored markdown
  reconcile.py        conflict / duplicate sweep
  clean.py            stale-data sweep + MOC regen
  prefilter.py        strips low-signal tool noise from transcripts
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
- Interactive sessions use whatever model you already pay for (Claude Max / Pro / API).
- Local embedding model is `paraphrase-multilingual-MiniLM-L12-v2` (~120 MB, downloaded once).

## License

MIT.
