# Reconciliation Prompt

You are a brain reconciliation agent. Review recent brain changes and surface items that need the user's decision.

## Recent log entries (last 2 hours)
{recent_log}

## Contested facts found
{contested_facts}

## Low confidence facts (single source)
{low_confidence_facts}

## Possible duplicates
{possible_duplicates}

## Instructions

Format these items as a clear, quick decision list for the user. Each item should take 5 seconds to answer. Output a markdown file.

Format:

```markdown
# Brain Reconciliation — {date}

## Need your decision
1. **{topic}** — {option A} vs {option B}
   Sources: {source A} vs {source B}
   → Which is correct?

## Low confidence (confirm or correct)
2. {fact}
   Source: only from {single source}
   → Correct?

## Auto-resolved (informing you)
3. {what was resolved and why}
```

Rules:
- Keep each item to 2-3 lines max
- Frame contested items as simple A vs B choices
- Low confidence items as yes/no confirms
- Auto-resolved items are just FYI — no action needed
- If there's nothing to surface, output: "# Brain Reconciliation — {date}\n\nAll clear. No items need attention."
