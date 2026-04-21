"""Learned rules for triple extraction — built from user audit decisions.

Each rule records how often the user confirmed or rejected a particular
(predicate, pattern) combination. This feeds back into two places:
  1. The extraction prompt: triple_rules.md is injected so the LLM
     sees which patterns reliably yield correct triples.
  2. Confidence adjustment: a predicate with a high rejection rate
     gets its LLM-assigned confidence scaled down.

Storage: JSONL at ~/.brain/identity/triple_rules.jsonl
Human summary: ~/.brain/identity/triple_rules.md (auto-generated)
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import brain.config as config
from brain.io import atomic_write_text


def _load() -> list[dict]:
    p = config.TRIPLE_RULES_PATH
    if not p.exists():
        return []
    rules = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rules.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rules


def _save(rules: list[dict]) -> None:
    config.TRIPLE_RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        config.TRIPLE_RULES_PATH,
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rules) + "\n",
    )
    _regenerate_md(rules)


def record_decision(
    predicate: str,
    basis: str,
    decision: str,  # "y" or "n"
) -> None:
    """Update the rule ledger after a user audit decision.

    `basis` is the original fact text the LLM extracted the triple from.
    `decision` is "y" (confirmed → correct) or "n" (rejected → wrong).
    """
    rules = _load()
    # Find existing rule for this predicate
    for rule in rules:
        if rule["predicate"] == predicate:
            if decision == "y":
                rule["confirmed"] = rule.get("confirmed", 0) + 1
            else:
                rule["rejected"] = rule.get("rejected", 0) + 1
            # Keep up to 5 recent examples
            examples = rule.get("examples", [])
            if basis and basis not in examples:
                examples = (examples + [basis])[-5:]
            rule["examples"] = examples
            rule["updated"] = date.today().isoformat()
            _save(rules)
            return
    # New predicate — create rule
    new_rule: dict = {
        "predicate": predicate,
        "confirmed": 1 if decision == "y" else 0,
        "rejected": 0 if decision == "y" else 1,
        "examples": [basis] if basis else [],
        "updated": date.today().isoformat(),
    }
    _save(rules + [new_rule])


def adjusted_confidence(predicate: str, raw_confidence: float) -> float:
    """Scale the LLM's raw confidence by the historical rejection rate.

    A predicate the user rejects 50% of the time cuts confidence in half.
    A predicate with no history is passed through unchanged.
    """
    for rule in _load():
        if rule["predicate"] == predicate:
            confirmed = rule.get("confirmed", 0)
            rejected = rule.get("rejected", 0)
            total = confirmed + rejected
            if total < 3:
                return raw_confidence  # not enough data yet
            accuracy = confirmed / total
            return raw_confidence * accuracy
    return raw_confidence


def _regenerate_md(rules: list[dict]) -> None:
    """Write a human-readable summary that gets included in extraction prompts."""
    if not rules:
        atomic_write_text(config.TRIPLE_RULES_MD_PATH, "")
        return
    lines = ["# Triple Extraction Rules (learned from audit)\n"]
    good = [r for r in rules if r.get("confirmed", 0) > r.get("rejected", 0)]
    bad = [r for r in rules if r.get("rejected", 0) >= r.get("confirmed", 0) and r.get("rejected", 0) > 0]
    if good:
        lines.append("## High-confidence patterns (use freely)\n")
        for r in sorted(good, key=lambda x: -x.get("confirmed", 0)):
            total = r.get("confirmed", 0) + r.get("rejected", 0)
            pct = int(100 * r.get("confirmed", 0) / total) if total else 0
            ex = r["examples"][0] if r.get("examples") else ""
            lines.append(
                f"- `{r['predicate']}` — {pct}% accurate "
                f"({r.get('confirmed',0)}✓ {r.get('rejected',0)}✗)"
                + (f'\n  e.g. "{ex}"' if ex else "")
            )
        lines.append("")
    if bad:
        lines.append("## Low-accuracy patterns (be conservative)\n")
        for r in sorted(bad, key=lambda x: -x.get("rejected", 0)):
            total = r.get("confirmed", 0) + r.get("rejected", 0)
            pct = int(100 * r.get("confirmed", 0) / total) if total else 0
            lines.append(
                f"- `{r['predicate']}` — only {pct}% accurate "
                f"({r.get('confirmed',0)}✓ {r.get('rejected',0)}✗) — set confidence low"
            )
        lines.append("")
    atomic_write_text(config.TRIPLE_RULES_MD_PATH, "\n".join(lines))


def rules_for_prompt() -> str:
    """Return the triple_rules.md content for injection into extraction prompts."""
    p = config.TRIPLE_RULES_MD_PATH
    if p.exists():
        text = p.read_text().strip()
        if text:
            return text
    return ""
