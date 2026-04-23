# Indexing & delay — how fast a note becomes searchable

Two extraction paths feed the brain index:
1. **`ingest_notes`** — any `.md` under `~/.brain/`, diffed by `mtime + sha`, upsert into SQLite FTS. Makes the note BM25-searchable (`brain_notes`, `brain_recall`).
2. **`note_extract`** (LLM) — turns note bullets into entity facts under `entities/<type>/`. Semantic-searchable with provenance.

Both are driven by `auto-extract.sh`, which gates each stage behind a resource clearance level from `brain.resource_guard`.

## Clearance levels (`resource_guard.py`)

| Level | Conditions | What runs in `auto-extract.sh` |
|---|---|---|
| 0 | always | `harvest_session` |
| 1 | CPU<60% + MEM<90% | `ingest_notes`, `clean` |
| 2 | L1 + CPU<40% + MEM<80% + `session_idle`≥60s + no `claude --print` | `auto_extract`, `note_extract`, `reconcile` |
| 3 | L2 + CPU<20% + MEM<70% + `session_idle`≥180s + AC | `dedupe` |
| 4 | L3 + CPU<15% + MEM<60% + `session_idle`≥300s + screen_idle≥120s | (declared, not wired) |

- `session_idle` = seconds since the newest mtime in `raw/` (≈ time since last Claude/Cursor session wrote output). **Not** human-HID idle.
- `screen_idle` on headless Linux is forced to `1e9` (no DISPLAY / WAYLAND_DISPLAY), so the gate trivially passes on servers. Desktop Linux needs `xprintidle`.
- AC on Linux reads `/sys/class/power_supply/*/online`; desktops / servers without a `Mains` entry default to `True`.

## Triggering

- **macOS**: launchd with `WatchPaths` + `ThrottleInterval=1s` + `StartInterval=300s` backstop. FS events fire in ~1 s.
- **Linux (this box)**: `brain-auto-extract.timer` (user systemd) at **5 min cadence + on boot**. No `.path` unit, no FS watcher. Every note has to wait for the next tick.

Both paths flock on `~/.brain/.extract.lock.d` so runs never overlap, and both skip L2/L3 stages while *any* `claude --print` is running (dual-instance Mac GPU freeze, incident 2026-04-11).

## Observed end-to-end delay

| Path | macOS | Linux (this box) |
|---|---|---|
| Note → BM25 searchable | ~1–2 s (happy path) · ≤5 min worst | **[0, ~5 min]** uniformly |
| Note → entity fact (LLM) | +60 s idle after L1 write | +60 s idle + 0 other `claude --print` running + next 5-min tick |
| Entity fact dedupe | +180 s idle + AC | same, next 5-min tick |

The delay-to-fact-extract compounds on a shared-session workstation: with N live Claude sessions writing to `raw/`, `session_idle` rarely exceeds 60 s, so L2 never clears and notes stay BM25-only until all sessions are quiet.

## Excluded from ingest

- Directories: `entities/`, `raw/`, `_archive/`, `logs/`, `.obsidian/`, `.git/`, `.vec/`, `.extract.lock.d/`, `node_modules/`, `.trash/`, any dotdir.
- Filenames starting with `_` (e.g. `_MOC.md`, `_placeholder.md`).
- Files > 256 KB (`MAX_BYTES`).
- Files outside `~/.brain/` entirely — `ingest_notes` only walks `BRAIN_DIR`.

## Gaps worth closing

- **No FS watcher on Linux.** A `systemd.path` unit on `~/.brain/` (with `PathModified=` + glob on `*.md`) would cut happy-path latency from 5 min to ~1 s, matching macOS.
- **L4 declared but not wired.** `auto-extract.sh` stops at L3; "backfill / revalidate" at L4 is dead code until something calls it.
- **Session-idle starvation on multi-agent days.** When running a tmux team of 4+ concurrent Claude sessions, `raw/` updates continuously → L2 never clears → today's journal bullets don't become entity facts until the work session ends. Consider gating on *harvested* session idle (sessions marked closed), not raw-file mtime.
