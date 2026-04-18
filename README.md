# Brain

Persistent memory system for Claude Code. Automatically captures what you learn across sessions — people, projects, decisions, corrections, insights — and makes it available in every future conversation.

## How it works

```
You work in Claude Code normally
         |
Session ends → next session starts
         |
SessionStart hook fires automatically:
  1. harvest_session.py  — scans ended session transcripts, writes summaries to ~/.brain/raw/
  2. auto_extract.py     — sends summaries to Haiku, extracts entities, writes to ~/.brain/entities/
         |
Your brain grows. Every session, Claude reads it back.
```

No commands. No slash prompts. Just work.

## Setup

### 1. Install the brain vault

```bash
mkdir -p ~/.brain
cd ~/.brain && git init
```

The brain stores everything as markdown files in `~/.brain/`:

```
~/.brain/
  identity/        # who-i-am.md, preferences.md, corrections.md
  entities/
    people/         # one .md per person
    clients/
    projects/
    domains/
    decisions/
    issues/
    insights/
    evolutions/
  raw/              # temporary — session summaries waiting for extraction
  index.md          # auto-generated entity catalog
  log.md            # extraction log
```

### 2. Wire the SessionStart hook

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd /path/to/brain-project/src && python3 -m brain.harvest_session && python3 -m brain.auto_extract",
            "timeout": 60000
          }
        ]
      }
    ]
  }
}
```

Replace `/path/to/brain-project` with where you cloned this repo.

### 3. Add brain instructions to CLAUDE.md

Copy the brain section from this project's reference into your `~/.claude/CLAUDE.md`. This tells Claude to:
- Read identity files at session start
- Check the brain before searching the codebase
- Write to `~/.brain/raw/` every ~30 minutes during sessions
- Process pending extractions

### 4. Create identity files

```bash
mkdir -p ~/.brain/identity
```

Create `~/.brain/identity/who-i-am.md`:
```markdown
---
type: identity
---

# Who I Am

- Name: Your Name
- Role: Your role
- How you work, what matters to you
```

Create `~/.brain/identity/preferences.md` and `~/.brain/identity/corrections.md` similarly.

## Usage

### Automatic (default)

Just work. The hook captures everything between sessions.

### Inject a file mid-session

Tell Claude naturally:
> "ingest this file into the brain: /path/to/notes.md"

Or from the terminal:
```bash
cd /path/to/brain-project/src
python3 -m brain.ingest /path/to/file.md
```

Supports: `.md`, `.txt`, `.csv`, `.tsv`, `.log`, `.json`, `.yaml`, `.yml`

### View in Obsidian

Open `~/.brain/` as an Obsidian vault. Entity files use `[[wiki-links]]` so relationships are clickable. No plugins needed — Obsidian is just the viewer.

## Architecture

```
harvest_session.py  — Scans ~/.claude/projects/**/*.jsonl for ended sessions
                      Writes structured summaries to ~/.brain/raw/
                      Rotates .harvested tracker (max 2000 entries)

auto_extract.py     — Reads raw files, sends to Haiku for entity extraction
                      Delegates to apply_extraction.py for all brain writes
                      Retries 3x on failure, then gives up

apply_extraction.py — Single source of truth for brain mutations
                      Creates/updates entities, rebuilds index, logs, git commits

ingest.py           — Manual file injection into the brain
                      Same extraction pipeline as auto_extract

entities.py         — Entity CRUD (create, read, append, list)
index.py            — Rebuilds index.md from all entity files
slugify.py          — Name → filesystem-safe slug conversion
log.py              — Append-only brain log
git_ops.py          — Stage and commit brain changes
reconcile.py        — Scan for conflicts, duplicates, low-confidence facts
config.py           — All paths and directory structure
```

## Testing

```bash
cd /path/to/brain-project
python3 -m pytest tests/ -v
```

30 tests covering entity CRUD, harvesting, extraction parsing, slug validation, reconciliation, file ingestion, and end-to-end integration.

## Cost

- **Haiku** for extraction (~$0.001 per session)
- **Opus** for your interactive sessions (reads brain at start)
- Extraction happens at session start, not during your work
