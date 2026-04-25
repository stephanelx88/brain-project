---
title: Realtime named-session messaging
date: 2026-04-25
status: design — ready for implementation plan
supersedes: docs/realtime-session-comm-spec.md
---

# Realtime Named-Session Messaging — Design Spec

## 0. Problem

Today, two Claude Code (or Cursor) sessions cannot directly hand off
work to each other. Coordination requires the user to copy/paste
between two terminal windows. The 2026-04-25 ECM port episode is the
canonical failure: session A finished a spec, wrote a "GO" journal
note, and could not push the GO signal to session B — the user had to
manually relay.

## 1. Goal

A push channel between live sessions, addressable by **human-readable
names**, with knowledge persistence handled by brain's **existing**
harvest+extract pipeline rather than a parallel one.

Non-goal: replacing journal notes for durable knowledge. Inbox is
transport, not storage.

## 2. Decisions made during brainstorm (2026-04-25)

| # | Decision | Choice |
|---|---|---|
| Q1 | Session topology | Hybrid: MVP = 2-session peer-to-peer; design leaves room for 1:N coordinator/N:N peers in v2 |
| Q2 | Naming model | Hybrid auto-derive default + explicit override |
| Q3 | Name scope | Per-project namespace; cross-project requires `<project>/<name>` qualification |
| Q4 | Default name format | `<project>-<short-pid>` (Claude); `<project>-<short-uuid>` (Cursor, no PID) |
| Q5 | MVP API | `brain_send`, `brain_inbox`, `brain_set_name` only — no broadcast/receipt/recall in MVP |
| Q6 | Dead-recipient policy | Hybrid: name → live-only (fail loud); UUID → fire-and-forget |
| Storage | Primitive | File tree under `~/.brain-runtime/` (separate from `BRAIN_DIR` vault) |

## 3. Architecture

### 3.1 Filesystem layout

```
~/.brain-runtime/                       ← runtime, NOT vault. Never indexed.
  inbox/
    <recipient-uuid>/
      pending/
        <ulid>.json                     ← unread message
      delivered/
        <ulid>.json                     ← read; pruned at TTL
  names/
    <session-uuid>.json                 ← {name, project, cwd, pid, set_at}
  log/
    inbox-hook.log                      ← hook stderr (best-effort)
```

Why outside `BRAIN_DIR` (e.g., `/Users/son/code/brain/` on this
machine, or default `~/.brain/` for fresh installs): inbox is
**transport**, not curated knowledge. Vault content goes through the
ontology pipeline (prefilter → ontology_guard → triple_audit). Inbox
content stays out of that pipeline by physical separation. This rules
out one whole class of bugs ("did this transient ack get extracted as
a fact?").

Knowledge from inter-session messages is preserved automatically:
the `brain_send` tool call is recorded in the sender's transcript
jsonl (`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`); the
`SystemReminder` injection is recorded in the receiver's transcript
jsonl. Both are already in `WatchPaths` for harvest. No parallel
extraction path needed.

### 3.2 Identifiers

- **`session_uuid`**: assigned by Claude/Cursor at process start.
  Source of truth, immutable. For Claude: the basename of
  `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`. For Cursor:
  prefixed `cursor:<uuid>` (matches existing `brain_live_sessions`
  contract).
- **`session_name`**: per-session alias, mutable. Resolves only for
  live sessions. Storage: `~/.brain-runtime/names/<session_uuid>.json`.
  Default = `<project_label>-<short_id>`, normalized to lowercase
  (`Honeywell-Forge-Cognition` → `honeywell-forge-cognition-68293`)
  to keep the same character class as user-set names; override via
  `brain_set_name`.

### 3.3 Project label

Reuse `brain_live_sessions`' existing `project` field — derived from
cwd path (e.g., `Honeywell-Forge-Cognition`). Per-project name scope
keys off this string. Sessions with the same `project` form one name
namespace.

### 3.4 Message envelope

```json
{
  "id": "01JBXY7K9RZNG7M2XKSZP4Q3VC",
  "from_uuid": "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
  "from_name_at_send": "honeywell-forge-cognition-68293",
  "to_uuid": "083c8e38-5a63-4158-b398-bb2f7114447d",
  "to_name_at_send": "planner",
  "body": "GO — read /Users/son/Desktop/.../spec.md and execute per §4-§6.",
  "sent_at": "2026-04-25T17:05:11.342Z"
}
```

ULID id → naturally time-ordered filename. No `urgent`, no `tags`, no
`read_at` — state is encoded by `pending/` vs `delivered/` directory.
YAGNI everything else for MVP.

### 3.5 Name registry entry

```json
{
  "uuid": "ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2",
  "name": "planner",
  "project": "Honeywell-Forge-Cognition",
  "cwd": "/Users/son/Documents/bms-loc-apr-24/Honeywell-Forge-Cognition",
  "pid": 68293,
  "set_at": "2026-04-25T17:04:02.811Z"
}
```

Default-named sessions still get a registry entry (written on first
`brain_send` if not already present, or eagerly by SessionStart hook
if cheap). This ensures `brain_live_sessions` and inbox tools can
share a single name lookup path.

## 4. Components

### 4.1 New module — `brain.runtime`

Self-contained subsystem. **Does not import** from `brain.entities`,
`brain.semantic`, `brain.graph`, etc. The transport layer touches
neither vault nor pipeline.

```
src/brain/runtime/
  __init__.py
  paths.py            ← runtime root resolution (BRAIN_RUNTIME_DIR env or ~/.brain-runtime)
  names.py            ← name registry: register, set, lookup, resolve
  inbox.py            ← send (write pending), pull (list pending), mark_delivered (rename)
  surface.py          ← format SystemReminder block from pending messages
  hook.py             ← entry point invoked by UserPromptSubmit hook
```

LOC budget per file: ≤ 200. If any grows past that, split before
merging.

### 4.2 New MCP tools

Three tools added to `brain.mcp_server` (or its split write/read
modules — match existing pattern):

#### `brain_send(to: str, body: str) -> dict`

Resolution rules:
1. If `to` matches UUID pattern — bare UUIDv4 regex
   `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`,
   or `cursor:<uuidv4>` — use as `to_uuid`.
   - Fire-and-forget — write to inbox even if recipient not in
     `brain_live_sessions`.
2. Else if `to` contains `/`: split into `<project>/<name>`. Both
   parts case-folded to lowercase before lookup; project label
   normalized the same way as the default-name format (§3.2).
   Resolve `name` within that project's name namespace.
3. Else: lowercase the value, resolve `name` within sender's own
   project.
4. Resolution failures (any of: ambiguous, not found, dead session for
   name-based send): return error envelope; do not write.

Returns:
```json
{
  "ok": true,
  "message_id": "01JBXY...",
  "to_uuid": "083c8e38-...",
  "to_name_at_send": "planner"
}
```

Or, on failure:
```json
{
  "ok": false,
  "error": "recipient_dead",
  "detail": "name 'planner' resolves to 083c8e38-... but no live session has that UUID"
}
```

Error codes:
- `name_not_found` — no name registry entry matches
- `ambiguous_name` — >1 match in scope (should be rare; only happens during a brief race window before `brain_set_name` collision-checks land)
- `recipient_dead` — name-based send to a UUID with no live session
- `cursor_recipient_unsupported` — name resolves to a `cursor:<uuid>` session in MVP (deferred to v2)
- `invalid_recipient` — malformed `to=` value (fails both UUID regex and name pattern)
- `body_too_large` — body > `BRAIN_INBOX_MAX_BODY` (default 32 KiB)

#### `brain_inbox(unread_only: bool = True, limit: int = 50, mark_read: bool = False) -> dict`

Read own session's inbox. Default = peek only; `mark_read=True` moves
listed messages from `pending/` to `delivered/`. The hook (§4.3) is
the normal mark-read path; manual calls default to peek so user can
inspect without consuming.

```json
{
  "messages": [{...envelope...}, ...],
  "pending_count": 3,
  "delivered_count": 12
}
```

#### `brain_set_name(name: str) -> dict`

Validates:
- 1-64 chars, `[a-z0-9][a-z0-9-]*` (lowercase only, no slashes)
- Not equal to any other session's name in same project (else `name_taken`)
- Not a reserved word: `peer`, `self`, `all`, `me` (reserved for future v2 shortcuts)

Writes/updates `~/.brain-runtime/names/<own_uuid>.json` atomically.
Returns:
```json
{"ok": true, "uuid": "ab2b1fa6-...", "name": "planner", "project": "Honeywell-Forge-Cognition"}
```

Detection of "own UUID" — see §6.

### 4.3 Hook integration — `UserPromptSubmit`

Brain's `brain install` command currently wires `SessionStart`. This
spec extends it to also wire `UserPromptSubmit`.

Hook script (`brain/bin/inbox-surface-hook.sh`, generated by
`brain install` with the absolute path to brain's python):

```bash
#!/bin/bash
# Wired into Claude Code as UserPromptSubmit hook.
# Runs once per user prompt, BEFORE the assistant turn begins.
# Empty-inbox fast path (mitigation 1 in §4.4) — exit before
# starting Python if there's nothing to surface.
PENDING_DIR="$HOME/.brain-runtime/inbox/$CLAUDE_SESSION_ID/pending"
[ -d "$PENDING_DIR" ] || exit 0
# `compgen -G` is a builtin glob check, no fork. Fall through if we
# cannot detect own UUID (env var not set) — Python module handles
# the slower fallback chain.
if [ -n "$CLAUDE_SESSION_ID" ]; then
  compgen -G "$PENDING_DIR/*.json" >/dev/null || exit 0
fi
exec "{{BRAIN_PYTHON}}" -m brain.runtime.hook --since-last-turn 2>>"$HOME/.brain-runtime/log/inbox-hook.log"
```

`{{BRAIN_PYTHON}}` is templated to the absolute interpreter path at
install time (e.g., `/Users/son/code/brain/bin/python`) so the hook
doesn't depend on `PATH` and resolves brain's installed package
without site-packages drift.

The Python module:

1. Detect own session UUID from environment (`CLAUDE_SESSION_ID` if
   exposed; else from parent PID lookup against
   `~/.claude/sessions/`).
2. List `~/.brain-runtime/inbox/<own_uuid>/pending/*.json` sorted by
   ULID.
3. If empty: emit nothing (sub-ms exit).
4. If non-empty: format SystemReminder block, then atomically move
   each surfaced file from `pending/` to `delivered/`.
5. Print SystemReminder block to stdout. Claude Code injects it as
   system content prepended to the upcoming assistant turn.

Surface format (≤ 1 KB target per surface, body truncated at 800
chars; user can call `brain_inbox` for full body if needed):

```
<system-reminder>
📬 1 new message (since last turn):
  - from `honeywell-forge-cognition-68293` at 17:05:
    "GO — read /Users/son/Desktop/.../spec.md and execute per §4-§6."
Run `brain_inbox` for full bodies. Already marked delivered.
</system-reminder>
```

For 2+ messages, list newest-first up to 5; older summarized as `… N
older message(s) in inbox/delivered`.

### 4.4 Performance budget

Hook fires on every user prompt. Hard budget: median ≤ 50 ms,
p99 ≤ 200 ms.

Risk: Python cold-start. `python3 -m brain.runtime.hook` from cold
disk on macOS is typically 80-150 ms — over budget on p99.

Mitigations (in order of preference, take first that meets budget):

1. **Empty-inbox fast path in shell**: shell wrapper checks
   `[ -d ~/.brain-runtime/inbox/<uuid>/pending ] && [ "$(ls -A ...)" ]`
   before invoking Python. The vast majority of prompts hit empty
   inbox → Python never starts. Zero deps, ~5 ms total.
2. **Persistent daemon**: `brain runtime-daemon` listens on Unix
   socket, hook is a tiny socket client. Adds operational complexity;
   only adopt if mitigation 1 fails the budget.

Initial implementation: mitigation 1. Benchmark in test suite; if
median > 50 ms or p99 > 200 ms, switch to daemon.

## 5. Data flow — same-project replay

(The 2026-04-25 ECM port case actually crossed projects — planner in
`Honeywell-Forge-Cognition`, executor in `RICHARDSON/master-0425`.
With per-project name scope, that case requires the cross-project
form `to="richardson-master-0425/executor"`. For clarity, this
walkthrough uses the simpler same-project setup that future
coordination flows are likely to use.)

```
Window A (planner, project=acme)             Window B (executor, project=acme)
────────────────────────────────             ─────────────────────────────────
brain_set_name("planner")
  ↓ writes ~/.brain-runtime/names/<A_uuid>.json
                                            brain_set_name("executor")
                                              ↓ writes names/<B_uuid>.json
brain_send(to="executor", body="GO — ...")
  ↓ resolves name in same project namespace
  ↓ writes ~/.brain-runtime/inbox/<B_uuid>/pending/<ulid>.json
  ↓ tool call appears in A's jsonl (harvested later)
  ↓ returns {ok: true, message_id: ...}

                                            (B is idle, waiting for user input)

                                            User types literally anything in B
                                              ↓ UserPromptSubmit hook fires
                                              ↓ shell wrapper: pending dir non-empty
                                              ↓ python brain.runtime.hook runs
                                              ↓ formats SystemReminder, moves to delivered/
                                              ↓ stdout → Claude Code injects
                                            B's assistant sees:
                                              <system-reminder>
                                              📬 1 new message from `planner`...
                                              </system-reminder>
                                              ↓ SystemReminder also lands in B's jsonl
                                              ↓ B's brain-first reflex acks message,
                                                opens spec, executes
```

Knowledge captured at harvest:
- A's jsonl contains `brain_send` tool call with body
- B's jsonl contains the surfaced message (in user-turn position via SystemReminder)
- Both run through `prefilter` → `ontology_guard` → `triple_audit`
- Worthwhile facts (decisions, designs in body) land in `entities/`
- Acks/status pings hit `triple_audit` → low confidence → reject (or
  user audits y/n; learned `triple_rules` scales future similar
  predicates down automatically)

## 6. Detecting "own session UUID"

This is the hardest implementation question. Three options, in order
of robustness:

1. **Environment variable** — Claude Code exposes `CLAUDE_SESSION_ID`
   in the hook's process env. (Confirm during implementation; if not
   exposed in the agent's hook envelope, drop straight to fallback
   2.) When present this is O(1) and matches the empty-inbox fast
   path in §4.3 hook script.
2. **PID lookup** — hook's parent PID = Claude Code process. Read
   `~/.claude/sessions/<ppid>.json` (already used by
   `brain_live_sessions` to detect liveness) → get session UUID.
3. **Cursor analog** — Cursor exposes session id via separate
   mechanism; mirror what `brain_live_sessions` already does.

Implementation: try (1), fall back to (2), fall back to (3). If all
fail: hook logs `cannot_detect_session` and exits cleanly (no
surface, no error to user).

In MCP tool context (`brain_send`, `brain_set_name`,
`brain_inbox`), the tool runs **inside** the Claude Code process —
the same detection chain applies. The MCP server can cache the
detected UUID per connection.

## 7. Edge cases + error handling

| Scenario | Behavior |
|---|---|
| Two senders write to same recipient simultaneously | ULID filenames unique → both land. Surface order = ULID order ≈ wall-clock send order. |
| Recipient session dies between resolve and write (UUID-based send) | File lands in `pending/<dead_uuid>/`. TTL prune (§8) clears it. Sender doesn't know — fire-and-forget contract. |
| `brain_set_name` with name already taken in project | Error `name_taken`, no write. User picks different name or asks the other session to release. |
| Disk full | `brain_send` raises `BrainStorageError` from the atomic write layer. Sender retries or aborts. |
| Hook script crashes mid-execution | `brain.runtime.hook` wraps everything in try/except; logs to `~/.brain-runtime/log/inbox-hook.log`; exits 0 so Claude Code doesn't show error. Worst case: messages stay in `pending/` and surface next prompt. |
| `~/.brain-runtime/` doesn't exist on first call | Auto-created (mkdir -p) by first writer. No install step required. |
| Body > `BRAIN_INBOX_MAX_BODY` (32 KiB) | `brain_send` errors `body_too_large`. Sender splits or writes a journal note and sends a pointer. |
| User runs `brain_inbox` with `mark_read=True` while hook is moving same files | Atomic per-file rename; double-move is a no-op (second rename on missing source ignored). |
| Cursor session as recipient | MVP: out of scope. `brain_send` to a Cursor UUID errors `cursor_recipient_unsupported`. v2 wires Cursor's hook equivalent. |
| Hook adds latency to every prompt | Empty-inbox fast path in shell (§4.4 mitigation 1). Benchmark in tests. |

## 8. TTL + cleanup

- `inbox/<uuid>/delivered/<ulid>.json` — pruned after
  `BRAIN_INBOX_DELIVERED_TTL_DAYS` (default 7).
- `inbox/<uuid>/pending/<ulid>.json` — pruned after
  `BRAIN_INBOX_PENDING_TTL_DAYS` (default 30) **iff** the recipient
  UUID hasn't been seen in `brain_live_sessions` for that period.
  Prevents accumulation under abandoned sessions while preserving
  pending messages for sessions that come and go.
- `names/<uuid>.json` — pruned when the UUID hasn't been seen alive
  for `BRAIN_NAME_TTL_DAYS` (default 30).

Cleanup runs as a launchd-scheduled job (`brain runtime-gc`) — daily.
Lazy fallback: each `brain_send` does an O(1) age check on the
recipient's `pending/` directory and may trigger a sync prune.

## 9. Compatibility, rollout, opt-out

- **Backward compat**: existing `brain_note_add`, `brain_recall`,
  `brain_live_sessions`, `brain_live_tail` unchanged. Inbox is
  additive. Sessions that never call `brain_send` see zero behavior
  change.
- **Hook addition**: `brain install` gains `UserPromptSubmit` wiring.
  Existing installs run `brain install --upgrade` to add it. Removable
  via `brain install --no-inbox-hook` for users who don't want
  per-prompt overhead.
- **Doctor check**: `brain doctor` adds an `inbox` section verifying
  the hook is wired, `~/.brain-runtime/` exists and is writable, and
  the empty-inbox fast path is functional.
- **Cursor parity**: deferred to v2. Doctor reports Cursor inbox as
  "not wired".

## 10. Acceptance criteria

Implementation is complete when **all** of these hold:

- [ ] `brain.runtime.{paths,names,inbox,surface,hook}` modules exist
      with unit-test coverage for: name resolve, send happy path,
      send to dead recipient (name + UUID variants), ambiguous name,
      mark_read race, surface formatting, hook empty-inbox fast path.
- [ ] `brain_send`, `brain_inbox`, `brain_set_name` MCP tools land in
      `brain.mcp_server` (and split read/write modules) with the
      exact error codes in §4.2.
- [ ] `brain install --upgrade` wires `UserPromptSubmit` and
      preserves existing `SessionStart` wiring; `--no-inbox-hook`
      flag honored.
- [ ] `brain doctor` reports inbox status (hook wired, dir writable,
      pending count, last hook fire age).
- [ ] End-to-end **integration** test (not unit; gated by harvest
      idle threshold): spawn 2 Claude Code sessions in same project;
      A `brain_set_name("a")`, B `brain_set_name("b")`; A
      `brain_send(to="b", body=...)`; B's next `/prompt` shows the
      SystemReminder; B's `brain_inbox` returns delivered with
      `mark_read=False` showing 0 pending. Marked as
      `@pytest.mark.integration` and skipped from default unit run.
- [ ] Performance: hook median ≤ 50 ms, p99 ≤ 200 ms on the empty
      path. Microbenchmark check in test suite (no Claude Code
      needed — invoke the hook script directly).
- [ ] Knowledge persistence — manual smoke test in the spec PR's
      test plan: after running the integration test above and
      waiting one harvest cycle (60-180 s), confirm A's `brain_send`
      body text appears in `~/code/brain/raw/`. Does NOT assert
      specific entity creation (depends on LLM extraction). This
      check stays out of CI because it depends on the harvest
      daemon being running on the developer's machine.
- [ ] No code in `brain.runtime.*` imports from `brain.entities`,
      `brain.graph`, `brain.semantic`. Enforced by an import-graph
      test (`tests/test_runtime_isolation.py`).

## 11. Out-of-scope (explicitly NOT this spec)

- Mid-turn delivery (LLM agent loop is pull-based at API level —
  hard limit, accept it; receiver gets messages on next turn
  boundary).
- Broadcast (`to="@all"`) — v2.
- Read receipts / `brain_send_status` — v2; for MVP, sender
  inferring read state from receiver's reply is sufficient.
- Recall unsent (`brain_send_recall`) — YAGNI.
- Threading / `reply_to` chains — v2 if needed; for now, body can
  reference prior `message_id` as plain text.
- Cross-user messaging — brain is single-user.
- Per-session opt-out (`brain_inbox_pause`) — v2; for MVP,
  `--no-inbox-hook` at install time is the opt-out.
- Cursor as recipient — v2 (Cursor as sender works in MVP if its
  hook system supports the equivalent of `UserPromptSubmit`;
  otherwise SessionStart-only delivery on next session restart).
- Replacing journal notes for durable knowledge — inbox is transport,
  journal is curated knowledge. Two layers, two purposes.

## 12. References

- 2026-04-25 ECM port coordination failure (the motivating case):
  session `ab2b1fa6-22a4-4a7c-b719-7fb62a972aa2` (planner,
  Honeywell-Forge-Cognition) → session
  `083c8e38-5a63-4158-b398-bb2f7114447d` (executor,
  RICHARDSON/master-0425). Planner wrote a "GO" journal note;
  executor had no signal until user manually relayed.
- Existing live tools: `brain_live_sessions`, `brain_live_tail` —
  this spec extends with delivery semantics, not identification
  semantics (UUIDs are reused).
- Existing pipeline that captures inter-session content
  automatically: `prefilter.py` → `ontology_guard.py` →
  `triple_audit.py` → `entities/`. Inbox does NOT bypass or
  duplicate this; it relies on it via the transcript-jsonl path.
- Superseded prior draft: `docs/realtime-session-comm-spec.md`
  (kept for history; this spec narrows scope, fixes the in-vault
  vs out-of-vault mistake, and removes YAGNI features).
