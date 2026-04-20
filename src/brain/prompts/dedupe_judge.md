# Brain Dedupe Judge

You decide whether two brain entities of the same type describe the **same underlying thing** and should be merged, or whether they are genuinely distinct.

The two entities were flagged because their semantic embeddings are close (cosine = {cosine}). Your job is to confirm or reject that signal using their content — names alone are unreliable.

## Type
{entity_type}

## Entity A — slug: `{slug_a}`
```
{body_a}
```

## Entity B — slug: `{slug_b}`
```
{body_b}
```

## Decide

Return ONE of these verdicts as strict JSON (no prose, no code fence):

- `merge` — A and B are the same thing said two ways. Pick a `winner_slug` (the one whose framing is clearer / more general / better-titled). The other will be marked superseded.
- `split` — A and B are clearly different things that happen to share vocabulary. Do not merge.
- `unrelated` — A and B are about different topics; the embedding similarity is a false alarm.
- `unsure` — Genuinely ambiguous. Skip rather than merge.

Default to **not merging** when in doubt. A bad merge is much more expensive than a missed merge — the dedupe pass will revisit pairs as the brain grows.

For `decisions`, `issues`, `projects`, `people`, `clients`: be especially conservative. Two decisions made on different days, two issues observed in different contexts, or two people with similar names are almost always distinct entities.

## Output schema

```json
{"verdict": "merge", "winner_slug": "<slug_a-or-slug_b>", "reason": "<one short sentence>"}
```

or

```json
{"verdict": "split", "reason": "<one short sentence>"}
```

Output the JSON object only.
