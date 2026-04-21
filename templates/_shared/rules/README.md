# Shared rule partials

Canonical source of truth for rule prose shared between host-specific
rule templates (`templates/claude/CLAUDE.md.tmpl`,
`templates/cursor/USER_RULES.md.tmpl`). Each partial is plain Markdown
with `{{PLACEHOLDER}}` tokens — no host-specific paths or names.

Host templates pull these in via an `{{include: _shared/rules/<file>.md}}`
directive; the renderer in `bin/install.sh` (see the `render()` shell
function) expands includes in a pre-pass, then runs its existing `sed`
token substitution as before. Option A — chosen because `render()` is a
6-line sed pipeline already; adding one extra pass is ~10 lines of shell.

## Content-normalisation conventions

Where the two hosts previously had near-identical prose with small
variants, the shared partial uses the neutral form:

- User name: always `{{USERNAME}}` (never the literal "Son").
- Host name: always "the agent" when referring to whichever client
  (Claude Code / Cursor) executed a prior incident. This matches the
  wording Cursor's template already used in Failure mode #2 prior to
  this refactor.
- When one host's paragraph contained a strict superset of the other's
  information (e.g. the `NEVER` list's `claude --print` warning, which
  Cursor's template omitted), the partial carries the superset so both
  hosts pick up the complete guidance.

Host-specific blocks (Cursor's Rule 0 framing with `sessionStart` hook
path; Claude's "Session-start audit" H2; each host's `NEVER` tail
line about `brain.auto_extract`; Cursor's live-sessions
self-exclusion caveat) stay inline in the host template.

## Drift detection

Out of scope for this refactor (task W3b). Until that lands, the only
guarantee that hosts stay in sync is "both `include:` the same partial".
