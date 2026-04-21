# Note Extraction Prompt

You are extracting durable knowledge from a user-authored vault note. The user typed this note into their personal brain because they wanted it findable later — treat the filename and body as the *answer* to some future question.

Pull out anything worth remembering as entity facts. Be conservative: prefer 0–3 high-signal facts over a long list. Title-only notes (empty body) often *are* the fact — e.g. a file named `son is at work.md` with empty body says Son is at work.

## Existing entities in the brain (reuse names where possible)

{existing_entities}

## Note

- Path: `{note_path}`
- Title: {title}
- Last modified: {date}

```
{body}
```

## Output

Respond with ONLY valid JSON, no prose:

```json
{
  "entities": [
    {
      "type": "<lowercase-plural slug: people | projects | domains | decisions | issues | insights | meetings | recipes | — whatever fits>",
      "name": "Canonical name",
      "is_new": true,
      "facts": ["fact 1"],
      "metadata": { "date": "2026-04-21", "<any other field>": "..." }
    }
  ],
  "corrections": []
}
```

## Rules

- **Only entities this note discusses.** Do NOT add an entity just because its name appears in the existing-entities list above. The list is for *reuse* (so you spell "Côn Đảo" the same way every time), not for *coverage*. If the note doesn't mention "Trinh", you cannot output a Trinh entity — even if Trinh exists in the brain. The reuse list is a dictionary, not a roll call.
- **Reuse names.** When the note DOES discuss an entity, and that entity is in the listing above, keep `name` identical and set `is_new: false`. Do NOT restate facts already in that entity — list only what this note adds.
- **The note IS the source.** Don't invent supporting context — extract only what the note literally says (or what its filename clearly implies).
- **Prefer positive declarative facts over negations or hedges.** If the note says "X is no longer in A, X is now in B", extract `["X is in B"]` — NOT `["X no longer in A", "X is now somewhere new"]`. The positive statement is the durable fact; the negation is just transition language. Old contradicting facts are auto-retracted by the pipeline when the note is edited, so you don't need to spell out the retraction.
- **Use proper nouns exactly as written.** "Côn Đảo" stays "Côn Đảo" — do NOT generalise to "an island". "Cần Thơ" stays "Cần Thơ", not "the Mekong Delta". Place names, person names, project codes: copy them verbatim including diacritics.
- **One fact per atomic claim.** "Trinh and Thuha are in Côn Đảo" splits into two entities (Trinh, Thuha), each with the fact `Currently in Côn Đảo` — not one entity per relationship-pair.
- **Empty is fine.** If the note is meta/scratch/already-known, return `{"entities": [], "corrections": []}`. We'd rather skip than hallucinate.
- **Title-as-fact.** A short note like `son is at work.md` with empty body → `entities: [{type: "people", name: "Son", is_new: false, facts: ["Son is at work"]}]`.
- **No source suffix.** The pipeline appends `(source: <note path>, <date>)` — you don't write it.
- **Corrections** are rare for notes (mostly for sessions). Usually `[]`.
- **Dates** in `YYYY-MM-DD`.
