# Session Extraction Prompt

You are a knowledge extraction agent. Read this conversation and pull out anything worth remembering — facts, people, ideas, decisions, references, patterns, open questions, anything. The goal is a durable record of what the conversation actually taught, not a fit to any fixed schema.

## Existing entities in the brain (reuse names where possible)

{existing_entities}

## Conversation

{conversation_summary}

## Output

Respond with ONLY valid JSON, no prose:

```json
{
  "entities": [
    {
      "type": "<lowercase-plural slug: people | projects | domains | meetings | techniques | recipes | quotes | — whatever fits>",
      "name": "Canonical name",
      "is_new": true,
      "facts": ["fact 1", "fact 2"],
      "metadata": { "date": "2026-04-18", "role": "...", "status": "...", "<any other field>": "..." }
    }
  ],
  "corrections": [
    {
      "pattern": "What Claude did wrong",
      "correction": "What the user said instead",
      "rule": "General rule for future"
    }
  ]
}
```

## Rules

- **Type is free.** Pick whatever category fits the content. Common ones: `people`, `projects`, `domains`, `decisions`, `issues`, `insights`, `meetings`, `techniques`, `quotes`, `questions`. You may invent new ones (`recipes`, `rituals`, `arguments`, etc.). Use lowercase-kebab-case plurals. Be consistent with existing types shown above when the content fits.
- **Reuse names.** If an entity already exists, keep `name` identical and set `is_new: false`; `facts` should list NEW facts only (not ones already in the entity).
- **Facts are self-contained sentences.** Each should make sense on its own, ending with `(source: <session label>, <date>)` implied by the pipeline — you don't have to write the source suffix.
- **Metadata is free-form.** Put structured side-info here (role, company, date, status, confidence, etc.). Omit the key entirely if not applicable.
- **Corrections** capture moments when the user told Claude to change its approach — these are high priority for future behavior shaping.
- **Empty is fine.** If a conversation had no durable takeaways, return `{"entities": [], "corrections": []}`.
- **Dates** in `YYYY-MM-DD` format.
