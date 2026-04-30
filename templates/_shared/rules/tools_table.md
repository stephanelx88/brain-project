## Tools available

| Tool | Use for |
|---|---|
| `brain_recall` | **Default** — hybrid BM25+semantic across everything |
| `brain_semantic` | Paraphrase / concept queries |
| `brain_notes` | User-authored markdown notes only |
| `brain_note_get` | Fetch full note body by path |
| `brain_entities` | List entities by type |
| `brain_get` | Fetch one entity file by slug |
| `brain_recent` | Last N entities/facts |
| `brain_identity` | Who-I-am snapshot |
| `brain_stats` | Counts |
| `brain_audit` | Top-N items needing a human decision (contested / dedupe / low-conf) |
| `brain_learning_gaps` | Queries the user repeatedly asks that keep missing — call at session-start to surface (never to auto-answer) |
| `brain_live_sessions` | List Claude/Cursor sessions alive *right now* (bypasses extraction lag) |
| `brain_live_tail` | Last N turns of a peer session (no LLM, no harvest) |
| `brain_resolve_name` | Resolve a session alias (e.g. `planner`, `quynh`) → uuid + alive flag. Use this — NOT `brain_recall` — when looking up a session by its human name |
