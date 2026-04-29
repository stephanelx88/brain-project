# Playbooks — runnable knowledge

Playbooks are the third entry kind in the vault, alongside notes and
extracted entities. Where a note records *what happened* and an entity
crystallizes *what is true*, a playbook captures **what to do, and how
to do it, with the script attached**.

Brain itself never executes anything. The contract is:

1. A user (or agent) asks for something.
2. Brain returns the playbook via `brain_recall` / `brain_notes`.
3. The agent reads the playbook + any referenced script files.
4. The agent runs the script through whatever shell-execution tool it
   has (Claude Code's `Bash`, Cursor's terminal tool, a function-call
   tool routed to `subprocess.run`, a human typing into their shell).

Every step is text-in / text-out and works the same way regardless of
which LLM is reading. ChatGPT, Claude, Gemini, a local Llama with a
shell tool — they all read the same markdown and decide the same way.

## Where playbooks live

Anywhere under `<vault>/playbooks/`. The directory is the only
discriminator: any file under that subtree is indexed by brain
regardless of extension (`.md` for the doc, `.sh` / `.py` / `.ts` for
the script). Outside `playbooks/`, only `.md` / `.txt` are indexed —
you can keep ad-hoc shell scripts elsewhere in the vault without
flooding `brain_recall`.

```
playbooks/
  redeploy-staging.md         ← the human + agent doc
  redeploy-staging.sh         ← the script (referenced from .md)
  rotate-api-keys/
    README.md                 ← multi-script playbooks live in subdirs
    rotate.py
    verify.sh
```

Files starting with `_` are still skipped (so `_draft.md` is private).

## File format

The doc is **markdown with YAML frontmatter**. Schema is intentionally
small so every LLM can parse it without special prompting:

```markdown
---
name: Redeploy staging
slug: redeploy-staging
summary: Push current branch to staging and verify health.
when_to_use: |
  User says "redeploy staging" / "push to staging" / "ship to stg".
  Use after a green CI run on the branch you want live.
inputs:
  branch: git ref to deploy (default = current HEAD)
outputs:
  - HTTP 200 from the staging healthcheck
  - Slack notification posted to #deploys
language: bash
script: redeploy-staging.sh
safety: destructive          # readonly | destructive
requires_confirm: true       # agent should confirm with user before running
---

# Redeploy staging

## When to use
Only after CI is green. If `pr_status != "merged"`, prefer the PR
preview URL instead.

## Steps
1. `bash playbooks/redeploy-staging.sh`
2. Watch the script's output for the "healthcheck OK" line.
3. If healthcheck fails, run `playbooks/redeploy-staging.sh --rollback`
   and ping `#deploys`.

## Verify
```bash
curl -fsS https://staging.example.com/healthz
```

## Rollback
Run the script with `--rollback` to redeploy the previous green
revision. Recorded in `~/.brain/logs/deploy.log`.
```

### Required frontmatter fields

| Field | Why |
|---|---|
| `name` | Human-readable title. |
| `slug` | Stable identifier. URL/filename-safe. |
| `summary` | One line. Surfaces in recall hit lists. |
| `when_to_use` | The activation criteria. The agent reads this to decide. |
| `safety` | `readonly` (querying, listing) or `destructive` (mutates state, calls APIs, modifies files). |

### Optional but recommended

| Field | Purpose |
|---|---|
| `inputs` / `outputs` | Tell the agent what to gather and what to expect. |
| `language` / `script` | If the playbook has an executable companion. |
| `requires_confirm` | If `true`, agent must confirm with user before running. |
| `tags` | Free-form categorization. |

### Body structure

Sections that any LLM finds useful, in this order:

1. **When to use** — duplicate / expand the `when_to_use` frontmatter
   with examples.
2. **Steps** — the procedure, including command lines.
3. **Verify** — post-conditions the agent should check.
4. **Rollback** — how to undo if step N failed.

Keep each section short. Every line in the doc costs context window
when surfaced.

## Discovery

Brain's existing recall does the work — no new tool:

- `brain_recall("redeploy staging")` → returns the playbook doc + any
  matching lines from the script.
- `brain_notes("staging deploy")` → notes-only, will hit the doc but
  not the script.

Agents that want a list of every available playbook can:

```python
brain_notes(query="when_to_use", k=50)
```

…since every playbook frontmatter contains the literal string
`when_to_use:`. (A purpose-built `brain_playbooks()` tool can land
later if recall-by-keyword turns out too noisy.)

## Self-improvement loop

Static skills go stale. Brain playbooks are designed to learn from
every run: when an LLM finishes a playbook and discovered something
non-obvious — a new precondition, an unhandled failure, an
optimization — it should record that for the next session.

The write path is one MCP call:

```
brain_playbook_record_lesson(
  slug="redeploy-staging",
  lesson="If secret X expired, run rotate-secret-x.sh first. "
         "Detected by error 'auth failed'."
)
```

What that does, in `<vault>/playbooks/redeploy-staging.md`:

1. Locates the file by slug (`playbooks/<slug>.md`,
   `playbooks/<slug>/README.md`, or any nested `<slug>.md` match).
2. Appends a dated bullet under `## Lessons learned`. The section is
   created if missing. Newest lessons first, so a cold reader sees the
   most recent learning at the top.
3. Bumps two audit fields in the frontmatter:
   - `last_updated`: ISO-8601 timestamp of this write.
   - `lessons_count`: incrementing counter.
4. Optional attribution: if the calling MCP session has a UUID,
   the bullet is suffixed with `(session abcd1234)` so future audits
   can trace which session contributed the lesson.

Atomic write. Last-write-wins under concurrent calls — brain isn't a
database, and conflicting lessons are rare in practice.

### When the agent should call it

- After a step fails and the agent figured out the workaround.
- After running and noticing the doc misses a precondition or
  side effect.
- When the script's behavior diverges from what the doc claims.

### When NOT to call it

- For one-time, machine-specific quirks (those belong in a personal
  note, not a shareable playbook).
- For trivial restatements of what the doc already says.
- During dry-runs / hypothetical reasoning.

### Cross-LLM behavior

Just like the playbook itself, the lessons section is plain markdown.
ChatGPT reading the playbook later sees the same `## Lessons learned`
section with the same bullets. The frontmatter `lessons_count` lets a
reader say "this playbook has been touched N times — probably mature"
without parsing the body.

## What brain does NOT do

- **Run scripts.** Brain is a knowledge layer. The agent runs them.
- **Sandbox or audit execution.** That's the agent's runtime concern
  (Claude Code prompts for permission per Bash command; Cursor
  similar; ChatGPT custom GPT actions go through the action's own
  approval).
- **Auto-suggest a playbook.** Recall is pull-based — the agent decides
  to look. Description-match auto-activation (the way Claude Code
  "Skills" work) is out of scope; brain isn't a skills replacement.
- **Edit the executable script.** `record_lesson` only modifies the
  .md doc. If a script needs to change, the agent uses its normal
  Edit tool — and ideally records a lesson explaining why.

## Why this isn't a clone of Claude Code Skills

| | **Brain playbook** | **Claude Code Skill** |
|---|---|---|
| Activation | Pulled by `brain_recall` query | Auto-matched against current task |
| Format | YAML+md, schema-free | YAML+md, schema-prescribed |
| Execution | Agent's normal Bash/Read tools | Skill tool's runner |
| LLM scope | Any LLM with shell access | Claude Code only |
| Distribution | User's brain vault | Plugin marketplace |

The two coexist. Use brain playbooks for personal procedures tied to
*your* knowledge graph (project history, decisions, peer sessions).
Use Claude Code Skills for shareable, marketplace-distributed
agent capabilities. They overlap in form, not in purpose.
