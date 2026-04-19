"""LLM-powered duplicate judgment for borderline pairs.

Stage 1 of the full-brain clean: for each pair that the word-overlap detector
flagged but the auto-merge skipped, ask Haiku "are these the same entity?".
Applies the merge when the LLM says yes.

Reuses reconcile_merge.merge_pair and auto_extract.call_claude.
"""

import json
import re
import sys

from brain.auto_extract import call_claude
from brain.reconcile import find_possible_duplicates
from brain.reconcile_merge import (
    entity_path,
    is_high_confidence,
    merge_pair,
    parse_duplicate_line,
)
from brain.index import rebuild_index


PROMPT_TEMPLATE = """Are these two brain entities duplicates (same real-world thing) or legitimately distinct (parent/child, different variants, different scope)?

Entity A ({k1}):
```
{content1}
```

Entity B ({k2}):
```
{content2}
```

Respond with ONLY a JSON object, no prose:
{{"duplicate": true|false, "canonical": "A"|"B", "reasoning": "<1 sentence>"}}

- duplicate=true means merge them
- canonical is the slug to KEEP; the other is deleted after content merge
- Prefer the more descriptive/specific slug as canonical
"""


def read_entity(key: str) -> str:
    return entity_path(key).read_text()


def parse_json_response(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def judge_pair(k1: str, k2: str) -> dict | None:
    content1 = read_entity(k1)
    content2 = read_entity(k2)
    prompt = PROMPT_TEMPLATE.format(
        k1=k1, k2=k2, content1=content1, content2=content2
    )
    output = call_claude(prompt)
    if not output:
        return None
    return parse_json_response(output)


def main():
    execute = "--execute" in sys.argv

    import builtins
    def say(*args, **kwargs):
        kwargs.setdefault("flush", True)
        builtins.print(*args, **kwargs)

    raw = find_possible_duplicates()
    if raw == "None found.":
        say("No duplicates to judge.")
        return

    all_pairs = []
    for line in raw.split("\n"):
        p = parse_duplicate_line(line)
        if p:
            all_pairs.append(p)

    borderline = [p for p in all_pairs if not is_high_confidence(p)]
    say(f"Borderline pairs needing LLM judgment: {len(borderline)}")
    say()

    merged = 0
    kept = 0
    failed = 0
    deleted_keys = set()

    for i, (k1, k2) in enumerate(borderline, 1):
        if k1 in deleted_keys or k2 in deleted_keys:
            say(f"[{i}/{len(borderline)}] skip (already processed): {k1} vs {k2}")
            continue

        judgment = judge_pair(k1, k2)
        if not judgment:
            failed += 1
            say(f"[{i}/{len(borderline)}] LLM FAIL: {k1} vs {k2}")
            continue

        dup = judgment.get("duplicate", False)
        canonical_label = judgment.get("canonical", "A")
        reasoning = judgment.get("reasoning", "")

        if not dup:
            kept += 1
            say(f"[{i}/{len(borderline)}] KEEP: {k1} vs {k2} — {reasoning}")
            continue

        canonical = k1 if canonical_label == "A" else k2
        duplicate = k2 if canonical == k1 else k1

        if execute:
            status = merge_pair(canonical, duplicate, execute=True)
            if status == "merged":
                deleted_keys.add(duplicate)
                merged += 1
                say(f"[{i}/{len(borderline)}] MERGE: keep={canonical} delete={duplicate} — {reasoning}")
            else:
                failed += 1
                say(f"[{i}/{len(borderline)}] MERGE FAIL ({status}): {canonical} + {duplicate}")
        else:
            merged += 1
            say(f"[{i}/{len(borderline)}] would MERGE: keep={canonical} delete={duplicate} — {reasoning}")

    say()
    say(f"Merged: {merged}, kept distinct: {kept}, failed: {failed}")

    if execute and merged:
        say("Rebuilding index...")
        rebuild_index()
        say("Done.")
    elif not execute:
        say("Dry run. Re-run with --execute to apply.")


if __name__ == "__main__":
    main()
