"""LLM judge layer for brain.dedupe — extracted from dedupe.py.

Isolates the Claude call + JSON parsing from the merge machinery so each
part can be tested (and replaced) independently.
"""

from __future__ import annotations

import json
from pathlib import Path

BODY_TRUNCATE_CHARS = 1500


def _load_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "dedupe_judge.md").read_text()


def read_body(path: Path) -> str:
    """Return entity body trimmed to BODY_TRUNCATE_CHARS, frontmatter included."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return ""
    return text[:BODY_TRUNCATE_CHARS]


def build_prompt(cand: dict) -> str:
    template = _load_prompt()
    return (template
            .replace("{entity_type}", cand["type"])
            .replace("{cosine}", f"{cand['cosine']:.3f}")
            .replace("{slug_a}", cand["slug_a"])
            .replace("{slug_b}", cand["slug_b"])
            .replace("{body_a}", read_body(cand["path_a"]))
            .replace("{body_b}", read_body(cand["path_b"])))


def parse_verdict(raw: str) -> dict | None:
    """Strict JSON expected. Tolerates code fences and surrounding prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.startswith("```"))
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}") + 1
        if s < 0 or e <= s:
            return None
        try:
            obj = json.loads(text[s:e])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    if obj.get("verdict") not in {"merge", "split", "unrelated", "unsure"}:
        return None
    return obj


def judge_pair(cand: dict) -> dict | None:
    """One LLM call. Returns parsed verdict dict or None on failure."""
    # Imported lazily so a fresh checkout without extraction deps can still
    # call find_candidates without pulling in call_claude.
    from brain.auto_extract import call_claude
    prompt = build_prompt(cand)
    out = call_claude(prompt, timeout=120)
    if not out:
        return None
    return parse_verdict(out)
