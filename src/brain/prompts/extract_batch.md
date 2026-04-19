# Batched Session Extraction Prompt

You are a knowledge extraction agent. Below are MULTIPLE conversation summaries from independent Claude Code sessions. For each one, pull out anything worth remembering — facts, people, ideas, decisions, references, patterns, open questions. The goal is a durable record of what each conversation actually taught.

## Existing entities in the brain (reuse names where possible)

{existing_entities}

## Sessions

{sessions_block}

## Output

Respond with ONLY valid JSON, no prose. The top-level shape is:

```json
{
  "results": [
    {
      "session_id": "<the id from `### SESSION <id>` header above>",
      "entities": [
        {
          "type": "<lowercase-plural slug — prefer one of: people, projects, clients, domains, decisions, issues, insights, evolutions, meetings>",
          "name": "Canonical name",
          "is_new": true,
          "facts": ["fact 1", "fact 2"],
          "metadata": { "date": "YYYY-MM-DD", "<any field>": "..." }
        }
      ],
      "corrections": [
        { "pattern": "...", "correction": "...", "rule": "..." }
      ]
    }
  ]
}
```

## Rules

- One entry in `results[]` per session, in input order. Use the exact `session_id` shown.
- **Reuse names.** If an entity already exists in the brain index, keep `name` identical and set `is_new: false`; only list NEW facts.
- **Prefer existing types.** Invent a new type only if none of the canonical ones fit.
- **Facts are self-contained sentences.** Do not append "(source: …)" — the pipeline does that.
- **Empty is fine.** A session with no durable takeaways → `{"session_id": "<id>", "entities": [], "corrections": []}`.
- Dates in `YYYY-MM-DD`.
