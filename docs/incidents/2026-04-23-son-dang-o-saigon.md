# Incident 2026-04-23: "son dang o dau" — brain denies a note it should know

## Summary
User wrote `/Users/son/code/brain/son.md` containing "son dang o saigon".
A few minutes later asked claude "son dang o dau". Claude called brain
**four times** (across `brain_notes`, `brain_note_get`, `brain_search`,
`brain_recent`, `brain_get`) and every call returned empty or irrelevant.
Following CLAUDE.md's *Brain grounding (MANDATORY)* rule claude truthfully
answered "brain has no record of son's location" and refused to guess.

The user only discovered the bug by pasting the file path
(`/Users/son/code/brain/son.md`). Claude then read the file directly,
found the content, and confirmed: "son đang ở Saigon."

Trust-breaking failure. The correct answer existed in the vault, but
brain consistently said no.

## Root cause

`src/brain/mcp_server.py` defines `_ensure_fresh()` — a three-sweep
freshness pass (`sync_mutated_entities` → `ingest_notes.ingest_all` →
`gc_orphaned_entities` + `semantic.ensure_built`) that runs **before**
a read tool answers, picking up filesystem mutations that happened
since the last pipeline tick.

Before this fix, `_ensure_fresh()` was wired into `brain_recall` only.
Every other read tool — `brain_search`, `brain_notes`, `brain_entities`,
`brain_semantic`, `brain_recent` — skipped the sweep entirely.

So whether a just-written note surfaced to the user depended entirely
on which MCP tool claude happened to pick for the query. A recall query
would see the note; a note-search query would not. That asymmetry is the
bug.

## Fix

`fix/mcp-fresh-on-all-read-tools` (commit on that branch):

1. `_ensure_fresh()` now called at the top of `brain_search`,
   `brain_entities`, `brain_notes`, `brain_recent`, and `brain_semantic`
   in addition to the existing `brain_recall`.
2. Throttled by module-level `_LAST_FRESH_TICK` + `BRAIN_RECALL_FRESH_THROTTLE_SEC`
   (default 1.0 s) so three back-to-back MCP calls only pay the sweep
   cost once.
3. Env-disable (`BRAIN_RECALL_ENSURE_FRESH=0`) short-circuits before
   the tick update — so disabling the sweep doesn't accidentally
   throttle a subsequent enabled call.

Tests added:
- `test_ensure_fresh_throttle_skips_back_to_back_calls`
- `test_ensure_fresh_env_disable_short_circuits`
- `test_read_tools_all_call_ensure_fresh` (structural regression)

Full suite: 562 → 565 tests passing.

## Why CLAUDE.md's "Brain grounding" rule isn't enough

CLAUDE.md instructs claude to trust `brain_recall` and never fabricate
answers. That rule worked perfectly here — claude did not invent a
location. The failure was on brain's side: the *grounded answer* in the
vault wasn't surfaced because the query tool didn't refresh the index.

Claude can't tell the difference between "note truly absent" and "note
exists but not yet indexed" from an empty MCP response. The only
defensible fix is framework-side: make all read tools uniformly
up-to-date, so an empty response actually means "not in the vault".

## Not in this fix

Separate concerns, tracked but not included in this PR:

- **`brain_note_get` filesystem fallback.** Already reads the filesystem
  directly, so it works *if* `BRAIN_DIR` is correctly configured. On
  this user's Mac, `BRAIN_DIR` pointed at a stale path (see
  2026-04-23 doctor incident) — that was a separate config-drift
  issue, now surfaced by the doctor fix in `fix/doctor-brain-dir-validation`.
- **Stale-warning on empty recall.** An empty response could surface
  `{"stale_warning": "N unindexed files modified in the last M minutes"}`
  when the filesystem has fresh content not yet in the index. That
  gives claude the signal to retry or read the file directly rather
  than confidently answer "no". Future WS — useful but not gating.
- **WS3 (watcher daemon).** The real fix for ingest latency is
  sub-second fs-event-driven ingest (inotify / fswatch). Already in
  the 10x plan as WS3. This PR closes the pre-WS3 gap: instead of
  "wait up to 60 s for scheduler tick before queries see new notes",
  every read tool now pays the stat-sweep cost (~10-40 ms warm) and
  sees notes immediately. WS3 will drop this to sub-second and remove
  the per-call cost.

## References

- Commit: `fix/mcp-fresh-on-all-read-tools` branch on origin.
- Related: `fix/doctor-brain-dir-validation` (misleading error when
  BRAIN_DIR points at a non-existent directory).
- 10x plan: WS3 (watcher daemon) for the eventual long-term solution.
