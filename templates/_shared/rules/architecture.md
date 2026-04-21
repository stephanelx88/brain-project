## Architecture (FYI only)

- **Harvest** (auto, launchd, ~1 s throttle): captures Claude/Cursor sessions → `{{BRAIN_DIR}}/raw/`
- **Ingest notes** (auto): picks up user-authored markdown anywhere in `{{BRAIN_DIR}}/`
- **Extract** (auto): LLM extracts entities → `{{BRAIN_DIR}}/entities/`
- **Reconcile + clean** (auto): dedup, tidy
- **MCP server** (`brain.mcp_server`): how you query all of the above

Source: `{{PROJECT_DIR}}` · run `{{BRAIN_DIR}}/bin/doctor.sh` if anything seems off.
