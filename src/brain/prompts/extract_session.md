# Session Extraction Prompt

You are a knowledge extraction agent. Read this conversation and pull out anything worth remembering — facts, people, ideas, decisions, references, patterns, open questions, anything. The goal is a durable record of what the conversation actually taught, not a fit to any fixed schema.

## Existing entities in the brain (reuse names where possible)

{existing_entities}

## Conversation

{conversation_summary}

## Learned triple extraction rules

{triple_rules}

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
  "triples": [
    {
      "subject": "Entity name",
      "predicate": "worksAt",
      "object": "Other entity name",
      "confidence": 0.9,
      "basis": "The exact fact text this triple was derived from"
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
- **Every entity needs at least one fact.** Do not emit an entity with an empty `facts` array — distill at least one self-contained declarative sentence even for brief mentions.
- **Facts are self-contained sentences.** Each should make sense on its own, ending with `(source: <session label>, <date>)` implied by the pipeline — you don't have to write the source suffix.
- **Metadata is free-form.** Put structured side-info here (role, company, date, status, confidence, etc.). Omit the key entirely if not applicable.
- **Corrections** capture moments when the user told Claude to change its approach — these are high priority for future behavior shaping.
- **Empty is fine.** If a conversation had no durable takeaways, return `{"entities": [], "corrections": [], "triples": []}`.
- **Dates** in `YYYY-MM-DD` format.

### Triple rules

- **Valid predicates only**: `worksAt`, `workedAt`, `knows`, `manages`, `reportsTo`, `partOf`, `locatedIn`, `builds`, `uses`, `involves`, `relatedTo`, `about`, `decidedOn`, `learnedFrom`, `contradicts`
- **Only explicit relationships** — never infer. If the text says "Son works at Aitomatic", emit `(Son, worksAt, Aitomatic)`. Do NOT infer `(Aitomatic, locatedIn, Vietnam)` just because Son is in Vietnam.
- **confidence** reflects how certain you are the triple is literally stated (not implied). 0.9+ = clearly stated. 0.5-0.8 = likely but somewhat implicit. Below 0.5 = skip it.
- **basis** must be a direct quote or close paraphrase of the source text. No invented basis.
- Emit `"triples": []` if no clear typed relationships were found.
