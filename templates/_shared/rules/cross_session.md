## Cross-session awareness

The harvest+extract pipeline is gated by 60-180 s idle thresholds, so the
SQLite-backed tools above lag live activity. When you need to know what
*other Claude/Cursor windows are doing right now*, use the live tools.

- On the **first user message of a fresh session**, call
  `brain_live_sessions(300)` (your own session is auto-excluded from
  results). If it returns any peers, mention them in one line: e.g.
  *"FYI — 2 other sessions active: Cursor in `brain-project`, Claude
  in `~/Documents/foo`."*
- If the user asks "what am I working on elsewhere?" / "what's the other
  window doing?" / similar, call `brain_live_tail(session_id, 20)` on
  the relevant peer and summarise.
- Do **not** call `brain_live_tail` on every turn — it reads the full
  jsonl. Use it on demand or when peers look relevant to the task.

## Looking up a session by its alias

When the user asks about a session by its human name ("where is
`planner`?", "is `quynh` still running?", "what's `commandor` doing?"),
**call `brain_resolve_name(name, project=None)` — NOT `brain_recall`**.

Reason: the names registry under `~/.brain-runtime/names/` is
filesystem-only and not indexed by the FTS pipeline that backs
`brain_recall`. A `brain_recall("planner")` query will at best return
weak-match hits about the *concept* of planning and let the agent
hallucinate an answer about the session. `brain_resolve_name` is the
canonical lookup — it returns `{matches: [{uuid, project, alive,
last_write, ...}, ...]}` directly from the registry.

- Same alias can exist in two different projects (e.g. `commandor` in
  `vulcan` AND `bangalore`). Without `project`, all matches come back;
  pass `project=...` to filter.
- Empty `matches` (no `error` key) is the definitive negative: that
  alias has never been registered. Do not fall through to
  `brain_recall` to "double-check" — there's nothing to check.
- `alive: true` means the holder process is running. `alive: false`
  with a non-null `set_at` means the alias was registered but the
  session has since died — surface that as "the session named X is
  registered but no longer running", not "X doesn't exist".

## Inter-agent conversation protocol

When the user asks you to talk to / coordinate with / consult another
session, you act on it autonomously — you do NOT ask the user for
permission to send each message. Opening both sessions and asking
them to talk IS the authorization.

### Sending — pick the right tool

| Intent | Tool |
|---|---|
| Fire-and-forget notification ("done, FYI") | `brain_send` |
| Question that needs a reply *this turn* | `brain_send_and_wait` |
| Long-running ask, want to keep working meanwhile | `brain_send` then `brain_wait_for_inbox` later |

`brain_send_and_wait` is a single tool call — your turn doesn't end
between sending and getting the reply. Default timeout 120s, server-
side poll, zero token cost while blocked. Prefer it whenever you'd
otherwise have to ping-pong via the Stop hook (which works but adds
seconds-per-turn vs. sub-second for in-tool wait).

### Receiving a peer message

Peer messages surface as a `<system-reminder>` block at the start of
your turn (via the UserPromptSubmit hook) or auto-continued at turn
end (via the Stop hook). Treat them as **directed instructions** from
a partner agent, equivalent to a user prompt scoped to that conversation:

- Respond directly. Do not ask the user "should I reply to X?" —
  this conversation is already authorized.
- If the peer's question references work they're doing that you don't
  see in the message body, call `brain_live_tail(<peer_session_id>, 20)`
  to read their last 20 turns. The session uuid is in the
  inbox-block's `from_uuid` field.
- Reply with `brain_send` to their UUID or name.

### Handing off a task

When you're handing a task to another session ("you finish this, I'll
take over X"):

1. `brain_send_and_wait("<peer>", "<brief: what you've done, what
   they should pick up, what success looks like>")`. The receiver
   gets your brief immediately.
2. The receiver, on getting your message, can read your
   `brain_live_tail(<your_session_id>, 30)` for full context — your
   working notes, prior tool calls, decisions reasoned through.
3. Optionally drop a `brain_note_add` describing the handoff so the
   knowledge graph captures it for later sessions.

### What you CANNOT do (API limit, not a bug)

- Interrupt a peer mid-thought. Each turn is an atomic LLM call;
  messages land at turn boundaries, not inside a generation.
- Stream their thinking as it happens. Use `brain_live_tail` to
  catch up after the fact.

These two limits are inherent to how Claude/ChatGPT/etc. work today.
The rest of the conversation pattern — sub-second pingpong, full
context-sharing on demand — is achievable and is the contract above.
