## Session-start learning gaps (surface, don't synthesise)

Alongside the session audit, on the **first user message of a session**
call `brain_learning_gaps(days=14, min_count=3)`. This returns queries
{{USERNAME}} has fired at the brain ≥3 times over the last 14 days that
scored below the miss threshold — repeated-miss topics the brain is
consistently failing to answer.

- **If empty**: stay silent, proceed normally.
- **If non-empty**: prepend **one short line** to your first reply
  naming at most 2 queries, and ask whether to note anything. Example:
  *"FYI — brain keeps missing on `how does brain scheduler work` (3×)
  and `ontology improvement plan` (4×). Want to note something, or
  move on?"* Then answer the user's actual question.

**Do NOT** auto-generate entities, insights, or notes to "fill" a
gap. The whole point of this surface is that {{USERNAME}} decides what
becomes memory. Writing a fabricated-but-coherent entry to close the
miss is the autoresearch failure mode and it re-introduces the
"inferential fabrication with false citation" class of errors.

Don't list gaps again on subsequent turns. If {{USERNAME}} ignores the
surface, drop it. If they say `gaps` / `show gaps` / `learning gaps`,
call `brain_learning_gaps(limit=20)` and walk the list one at a time.
