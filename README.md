# brain

Persistent memory for Claude Code & Cursor. Captures everything you write — Claude session transcripts, Obsidian notes, manual edits — and exposes it as MCP tools so the LLM has perfect recall in every future conversation.

```
You write a note in Obsidian, or finish a Claude session.
                ↓
launchd watches ~/.brain/ and ~/.claude/projects/ (1 s throttle)
                ↓
harvest_session  →  ingest_notes  →  auto_extract (LLM)  →  reconcile
                ↓
SQLite + FTS5 (BM25) + numpy semantic vectors (multilingual MiniLM)
                ↓
brain MCP server  →  Claude/Cursor calls brain_recall("where is son")
                ↓
Hybrid BM25 + dense ranking with path-density re-ranking → instant answer
```

No commands to run. No slash prompts. Just install once.

## Install

Requires macOS, Python ≥ 3.11, and the [Claude Code](https://claude.ai/code) CLI.

```bash
git clone https://github.com/stephanelx88/brain-project ~/code/brain-project
cd ~/code/brain-project
bin/install.sh
```

That's it. `install.sh` is idempotent — re-run it any time you edit a template or pull updates.

What it does:

1. Picks the right Python (≥ 3.11), refuses to install if the project sits in `~/Desktop` / `~/Documents` / `~/Downloads` (macOS TCC blocks launchd from reading those).
2. `pip install -e .` (editable, so source edits take effect immediately).
3. Renders `templates/` into `~/.brain/bin/`, `~/Library/LaunchAgents/`, and `~/.claude/CLAUDE.md`.
4. Seeds `~/.brain/identity/{who-i-am,preferences,corrections}.md` (only if missing — existing files are never overwritten).
5. Downloads the multilingual embedding model (~120 MB, one-time).
6. Registers the `brain` MCP server with Claude Code (`claude mcp add brain ...`).
7. Builds the SQLite + semantic indexes.
8. Loads the launchd watcher (1 s throttle on the vault + Claude project dir).
9. Runs `bin/doctor.sh` — the install only succeeds if doctor reports green.

After install: **restart your Claude Code / Cursor session** so the MCP tools load.

## Verify

```bash
~/.brain/bin/doctor.sh
```

Expected: `11 passed, 0 warnings, 0 failures`. Doctor catches every silent failure mode (stale install, launchd not loaded, MCP can't boot, semantic index missing, deletions not propagating).

## Daily use

Open Obsidian on `~/.brain/`. Write notes anywhere in there. They're searchable from your next prompt to Claude/Cursor within ~2 s.

In Claude Code:

```
> where am I?
[brain_recall called automatically]
> Long Xuyen — per ~/.brain/Untitled 2.md, modified today.
```

The LLM knows to use the brain because `~/.claude/CLAUDE.md` (installed by `install.sh`) mandates `brain_recall` as the first action for any factual query.

## MCP tools

| Tool | Use for |
|---|---|
| `brain_recall` | **Default** — hybrid BM25+semantic across everything |
| `brain_semantic` | Paraphrase / concept queries |
| `brain_notes` | User-authored markdown notes only |
| `brain_note_get` | Fetch full note body by path |
| `brain_entities` | List extracted entities by type |
| `brain_get` | Fetch one entity file by slug |
| `brain_recent` | Last N entities/facts |
| `brain_identity` | Who-I-am snapshot |
| `brain_stats` | Counts |

## Architecture

| Component | What it does | Trigger |
|---|---|---|
| `harvest_session` | Tails `~/.claude/projects/*.jsonl`, writes `~/.brain/raw/session-*.md` | launchd, 1 s throttle |
| `ingest_notes` | Walks `~/.brain/**/*.md`, mirrors to SQLite + FTS5 + semantic | launchd, 1 s throttle |
| `auto_extract` | Sends raw sessions to Claude Haiku, extracts entities/facts | launchd, skipped during active session |
| `reconcile` | Merges duplicate entities, regenerates `index.md` | launchd, skipped during active session |
| `clean` | Removes empty entity files, dedups facts | launchd, 1 s throttle |
| `mcp_server` | FastMCP server exposing tools, with synchronous embedding warm-up | spawned by Claude/Cursor on session start |

State lives in:

- `~/.brain/`              — your vault (markdown, git repo)
- `~/.brain/.brain.db`      — SQLite cache (rebuildable from markdown)
- `~/.brain/.vec/`          — numpy semantic vectors (rebuildable)
- `~/.brain/.harvest.db`    — incremental session-byte-offset ledger
- `~/.brain/raw/`           — pending session captures + new content
- `~/.brain/logs/auto-extract.log` — what launchd has been doing

## Troubleshooting

| Symptom | First thing to check |
|---|---|
| Claude doesn't call `brain_recall` | `~/.claude/CLAUDE.md` exists and contains "Personal Brain — Mandatory Use". Restart session. |
| `brain_recall` returns nothing | `~/.brain/.brain.db` exists, `doctor.sh` shows non-zero notes count. |
| New note isn't searchable | `tail -f ~/.brain/logs/auto-extract.log` — should see `ingest_notes` runs every ~1–2 s. |
| Deleted note still appears | Same as above — `ingest_notes` cleans the index on the next launchd cycle (~1–3 s). |
| MCP tools missing in Claude UI | `claude mcp list` should show `brain ✔ connected`. If not: `bin/install.sh` re-runs the registration step. |
| `ModuleNotFoundError: brain` in launchd log | Project is under `~/Desktop`/`~/Documents`/`~/Downloads`. Move to `~/code/` and re-run install. |
| Slow first query (~10 s) | Embedding model wasn't cached. Re-run `install.sh` step 4, or set `BRAIN_WARMUP=1` (default). |

When in doubt: `~/.brain/bin/doctor.sh`.

## Uninstall

```bash
~/code/brain-project/bin/uninstall.sh           # keeps your vault
~/code/brain-project/bin/uninstall.sh --purge   # also deletes ~/.brain (asks confirmation)
```

## Layout

```
brain-project/
├── bin/
│   ├── install.sh        # 8-step idempotent installer
│   ├── uninstall.sh      # symmetric teardown
│   └── doctor.sh         # health check (also symlinked into ~/.brain/bin/)
├── templates/            # rendered into per-machine paths by install.sh
│   ├── scripts/auto-extract.sh.tmpl
│   ├── launchd/brain-auto-extract.plist.tmpl
│   ├── claude/CLAUDE.md.tmpl
│   └── identity/{who-i-am,preferences,corrections}.md.tmpl
├── src/brain/            # the actual python package
│   ├── mcp_server.py     # FastMCP tools (brain_recall, brain_semantic, …)
│   ├── semantic.py       # multilingual MiniLM + RRF + path-density re-rank
│   ├── ingest_notes.py   # vault → SQLite mirror
│   ├── harvest_session.py# Claude transcripts → ~/.brain/raw/
│   └── auto_extract.py   # raw → entities via Claude Haiku
├── tests/
└── pyproject.toml
```

## License

MIT.
