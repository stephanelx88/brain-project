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
