"""Autoresearch loop for the brain.

Implements Karpathy's autoresearch pattern over the brain's own data:

  - The human edits ONE spec file:  ~/.brain/program.md
  - The agent edits ONE sandbox dir: ~/.brain/playground/
  - Fixed wall-clock budget per cycle (default: 10 min)
  - Crisp metric per cycle (Question Coverage Score, see program.md)
  - Cycles run at idle time (gated like auto-extract.sh)

Single entry point: `python -m brain.autoresearch [--cycles N] [--dry-run]`
or as a Python API: `brain.autoresearch.run(cycles=1)`.

This module deliberately depends on ZERO new third-party packages. Reuses:

  - brain.config       (paths)
  - brain.db           (BM25 + entity reads)
  - brain.semantic     (hybrid recall)
  - brain.auto_extract (call_claude — SDK-then-CLI fallback)
  - brain.log          (append_log)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import brain.config as config
from brain import db, semantic
from brain.auto_extract import call_claude
from brain.log import append_log

PROGRAM_MD = config.BRAIN_DIR / "program.md"
QUEUE_MD = config.BRAIN_DIR / "research-queue.md"
RESEARCH_LOG = config.BRAIN_DIR / "research-log.md"
PLAYGROUND = config.BRAIN_DIR / "playground"

CYCLE_BUDGET_SECONDS = int(os.environ.get("BRAIN_AR_BUDGET_S", "600"))
MAX_LLM_CALLS = int(os.environ.get("BRAIN_AR_MAX_LLM", "8"))
MAX_OUTPUT_FILES = int(os.environ.get("BRAIN_AR_MAX_OUT", "5"))
MAX_ENTITIES_READ = int(os.environ.get("BRAIN_AR_MAX_ENTS", "50"))
IDLE_THRESHOLD_S = int(os.environ.get("BRAIN_AR_IDLE_S", "180"))

VALID_PG_KINDS = {"insight", "article", "contradiction", "hypothesis", "question"}


# ---------------------------------------------------------------------------
# program.md parsing
# ---------------------------------------------------------------------------

@dataclass
class Program:
    version: str
    body: str
    raw_frontmatter: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Program":
        if not PROGRAM_MD.exists():
            raise SystemExit(
                f"missing {PROGRAM_MD}. Create it before running autoresearch — "
                "see ~/.brain/Karpathy AutoResearch Pattern.md for the template."
            )
        text = PROGRAM_MD.read_text(errors="replace")
        fm: dict = {}
        body = text
        m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip().strip('"').strip("'")
            body = text[m.end():]
        version = fm.get("version", "0")
        return cls(version=version, body=body, raw_frontmatter=fm)


# ---------------------------------------------------------------------------
# question queue + auto-generation
# ---------------------------------------------------------------------------

ROUND_ROBIN_QUESTIONS = [
    "stale-entity-sweep: find entities with last_updated > 60 days that are still referenced by ≥2 newer entities. Re-read both — are the older entity's facts still consistent?",
    "cross-project-synthesis: pick a person mentioned in ≥3 projects. Write a narrative article describing their role across all of them.",
    "decision-audit: pick a decision with status=open from 30+ days ago. Search recent sessions for evidence it was actually made, reversed, or remains open. File a contradiction or update.",
    "correction-synthesis: read all corrections from the last 30 days. Cluster by theme. File a meta-correction summarizing the pattern.",
    "issue-follow-through: pick an issue with status=open 14+ days. Search for evidence of resolution or escalation. Update or escalate.",
    "domain-coverage-gap: pick a domain with <3 linked entities. Find recent sessions touching the domain that didn't extract. Propose new entities.",
]


def _load_queue() -> list[str]:
    if not QUEUE_MD.exists():
        return []
    out = []
    for raw in QUEUE_MD.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[done]") or line.startswith("[skip]"):
            continue
        out.append(line.lstrip("- ").strip())
    return out


def _mark_queue_done(question: str) -> None:
    if not QUEUE_MD.exists():
        return
    lines = QUEUE_MD.read_text(errors="replace").splitlines()
    out = []
    marked = False
    for raw in lines:
        if not marked and raw.strip().lstrip("- ").strip() == question:
            out.append(f"- [done] {question}  ({_now()})")
            marked = True
        else:
            out.append(raw)
    if marked:
        QUEUE_MD.write_text("\n".join(out) + "\n")


def _next_round_robin(cycle_n: int) -> str:
    return ROUND_ROBIN_QUESTIONS[cycle_n % len(ROUND_ROBIN_QUESTIONS)]


# ---------------------------------------------------------------------------
# context gathering
# ---------------------------------------------------------------------------

def _gather_context(question: str, k: int = MAX_ENTITIES_READ) -> dict:
    """Grab entities + facts relevant to the question. Returns a structured
    blob the LLM can read."""
    semantic.ensure_built()
    hits = semantic.hybrid_search(question, k=k) or []
    entity_paths: list[str] = []
    facts: list[dict] = []
    for h in hits:
        if h.get("kind") == "fact":
            facts.append({
                "type": h.get("type"),
                "entity": h.get("name"),
                "text": h.get("text"),
                "source": h.get("source"),
                "date": h.get("date"),
            })
        path = h.get("path")
        if path and path not in entity_paths:
            entity_paths.append(path)
    # Read first 12 entities in full so the LLM has context to write articles
    full_entities = []
    for p in entity_paths[:12]:
        fp = config.BRAIN_DIR / p
        if not fp.exists():
            continue
        text = fp.read_text(errors="replace")
        full_entities.append({"path": p, "text": text[:3500]})
    return {
        "facts_top_k": facts[:30],
        "full_entities": full_entities,
        "hit_count": len(hits),
    }


# ---------------------------------------------------------------------------
# prompt assembly
# ---------------------------------------------------------------------------

CYCLE_PROMPT_TEMPLATE = """You are the brain's autoresearch agent. The human edits `program.md` (the spec); you write to `playground/` only. This is one cycle.

# Spec excerpt (program.md, version {version})

{program_excerpt}

# Question for this cycle

{question}

# Brain context retrieved for this question

## Top facts ({n_facts})
{facts_block}

## Full entity cards ({n_entities})
{entities_block}

# Your job

Synthesize new playground items that improve the brain's coverage of this question. Be concrete and concise — every item must add something the brain doesn't already have.

# Output

Return ONLY valid JSON, no prose, with this shape:

```json
{{
  "summary": "one sentence describing what you did this cycle",
  "metric_estimate": {{
    "kind_of_impact": "missed_recall_reduction | contradiction_resolved | stale_refreshed | none",
    "rationale": "one sentence"
  }},
  "items": [
    {{
      "kind": "insight | article | contradiction | hypothesis | question",
      "title": "short title (becomes the filename slug)",
      "body": "markdown body, 50-500 words",
      "refs": ["entities/people/madhav.md", "entities/projects/bms-honeywell.md"],
      "confidence": "low | medium | high"
    }}
  ]
}}
```

Rules:
- 1 to {max_items} items maximum, prefer fewer high-quality ones.
- `kind` must be one of: insight, article, contradiction, hypothesis, question.
- `refs` are paths relative to ~/.brain/, listing entities you used as evidence.
- Use Markdown headings (`##`) and lists in `body`.
- For `contradiction`, body MUST start with `- a: <entity-path>` then `- b: <entity-path>` then `- conflict: <text>`.
- For `hypothesis`, body MUST end with `- testable_via: <method>` and `- status: unverified`.
- If nothing worth writing, return `"items": []` and explain in `summary`.
"""


def _build_cycle_prompt(question: str, program: Program, ctx: dict) -> str:
    facts_lines = []
    for f in ctx["facts_top_k"]:
        date = f.get("date") or "?"
        ent = f.get("entity") or "?"
        text = (f.get("text") or "").replace("\n", " ")[:240]
        facts_lines.append(f"- [{date}] {ent}: {text}")
    facts_block = "\n".join(facts_lines) or "(none)"

    ent_blocks = []
    for e in ctx["full_entities"]:
        ent_blocks.append(f"### {e['path']}\n{e['text']}")
    entities_block = "\n\n".join(ent_blocks) or "(none)"

    program_excerpt = program.body[:2500]

    return CYCLE_PROMPT_TEMPLATE.format(
        version=program.version,
        program_excerpt=program_excerpt,
        question=question,
        n_facts=len(ctx["facts_top_k"]),
        facts_block=facts_block,
        n_entities=len(ctx["full_entities"]),
        entities_block=entities_block,
        max_items=MAX_OUTPUT_FILES,
    )


# ---------------------------------------------------------------------------
# parsing + writing
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.startswith("```"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}") + 1
        if 0 <= s < e:
            try:
                return json.loads(text[s:e])
            except json.JSONDecodeError:
                return None
    return None


SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    s = s.lower().strip()
    s = SLUG_RE.sub("-", s).strip("-")
    return s[:60] or "untitled"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_playground_item(cycle_n: int, item: dict) -> Path | None:
    kind = (item.get("kind") or "").strip().lower()
    if kind not in VALID_PG_KINDS:
        return None
    title = item.get("title") or "untitled"
    body = item.get("body") or ""
    refs = item.get("refs") or []
    confidence = item.get("confidence") or "low"

    sub = PLAYGROUND / (kind + "s")  # insights, articles, etc
    sub.mkdir(parents=True, exist_ok=True)
    slug = _slugify(title)
    path = sub / f"{cycle_n:04d}-{slug}.md"
    front = (
        "---\n"
        f"type: playground-{kind}\n"
        f"created_at: {_now()}\n"
        f"cycle: {cycle_n}\n"
        f"confidence: {confidence}\n"
        f"refs: {json.dumps(refs)}\n"
        "---\n\n"
    )
    path.write_text(front + f"# {title}\n\n{body}\n")
    return path


# ---------------------------------------------------------------------------
# cycle counter
# ---------------------------------------------------------------------------

def _next_cycle_number() -> int:
    PLAYGROUND.mkdir(parents=True, exist_ok=True)
    existing = list(PLAYGROUND.glob("cycle-*.md"))
    nums: list[int] = []
    for p in existing:
        m = re.match(r"cycle-(\d+)\.md", p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


# ---------------------------------------------------------------------------
# idle-time guard (mirrors auto-extract.sh)
# ---------------------------------------------------------------------------

def _is_idle() -> bool:
    proj_dir = Path.home() / ".claude" / "projects"
    if not proj_dir.exists():
        return True
    newest = 0.0
    for p in proj_dir.rglob("*.jsonl"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    if newest == 0:
        return True
    return (time.time() - newest) >= IDLE_THRESHOLD_S


# ---------------------------------------------------------------------------
# one cycle
# ---------------------------------------------------------------------------

def run_cycle(cycle_n: int, *, dry_run: bool = False, force_question: str | None = None) -> dict:
    started = time.time()
    program = Program.load()

    queue = _load_queue()
    if force_question:
        question = force_question
        from_queue = False
    elif queue:
        question = queue[0]
        from_queue = True
    else:
        question = _next_round_robin(cycle_n)
        from_queue = False

    cycle_log = PLAYGROUND / f"cycle-{cycle_n:04d}.md"
    PLAYGROUND.mkdir(parents=True, exist_ok=True)

    log_lines = [
        "---",
        f"cycle: {cycle_n}",
        f"started_at: {_now()}",
        f"program_version: {program.version}",
        f"from_queue: {from_queue}",
        "---",
        "",
        f"# Cycle {cycle_n}",
        "",
        f"**Question:** {question}",
        "",
    ]

    ctx = _gather_context(question)
    log_lines.append(f"Retrieved {ctx['hit_count']} hits, {len(ctx['full_entities'])} full entity cards.")
    log_lines.append("")

    prompt = _build_cycle_prompt(question, program, ctx)

    if dry_run:
        log_lines.append("**DRY RUN — prompt would be:**")
        log_lines.append("```")
        log_lines.append(prompt[:2000] + ("..." if len(prompt) > 2000 else ""))
        log_lines.append("```")
        cycle_log.write_text("\n".join(log_lines))
        return {"status": "dry_run", "cycle": cycle_n, "question": question}

    raw = call_claude(prompt, timeout=CYCLE_BUDGET_SECONDS)
    if not raw:
        log_lines.append("**LLM returned no output. Cycle aborted.**")
        cycle_log.write_text("\n".join(log_lines))
        return {"status": "llm_fail", "cycle": cycle_n, "question": question}

    parsed = _parse_response(raw)
    if not parsed or "items" not in parsed:
        log_lines.append("**Parse failed. Raw output:**")
        log_lines.append("```")
        log_lines.append(raw[:1500])
        log_lines.append("```")
        cycle_log.write_text("\n".join(log_lines))
        return {"status": "parse_fail", "cycle": cycle_n, "question": question}

    written: list[str] = []
    for item in parsed.get("items", [])[:MAX_OUTPUT_FILES]:
        path = _write_playground_item(cycle_n, item)
        if path:
            written.append(str(path.relative_to(config.BRAIN_DIR)))

    log_lines.append(f"**Summary:** {parsed.get('summary', '(none)')}")
    log_lines.append("")
    metric = parsed.get("metric_estimate", {})
    log_lines.append(
        f"**Metric estimate:** {metric.get('kind_of_impact', '?')} — "
        f"{metric.get('rationale', '?')}"
    )
    log_lines.append("")
    if written:
        log_lines.append(f"**Written ({len(written)}):**")
        for w in written:
            log_lines.append(f"- `{w}`")
    else:
        log_lines.append("**Written:** (none — agent decided no items worth filing)")
    log_lines.append("")
    log_lines.append(f"_Wall clock: {round(time.time() - started, 1)} s_")
    cycle_log.write_text("\n".join(log_lines))

    if from_queue:
        _mark_queue_done(question)

    # research-log.md one-line summary
    line = (
        f"## [{_now()}] cycle-{cycle_n:04d} | "
        f"q: {question[:80]} | items: {len(written)} | "
        f"impact: {metric.get('kind_of_impact', '?')}\n"
    )
    if not RESEARCH_LOG.exists():
        RESEARCH_LOG.write_text(
            "# Brain Autoresearch Log\n\n"
            "Append-only record of nightly autoresearch cycles. "
            "See `program.md` for the spec.\n\n"
        )
    with RESEARCH_LOG.open("a") as f:
        f.write(line)

    append_log("autoresearch", f"cycle-{cycle_n:04d} q={question[:60]!r} items={len(written)}")

    return {
        "status": "ok",
        "cycle": cycle_n,
        "question": question,
        "items_written": len(written),
        "duration_s": round(time.time() - started, 1),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run(cycles: int = 1, *, dry_run: bool = False, force_question: str | None = None,
        respect_idle: bool = True) -> list[dict]:
    if respect_idle and not _is_idle() and not dry_run:
        print(f"skip: claude session active in last {IDLE_THRESHOLD_S}s",
              file=sys.stderr)
        return []
    PLAYGROUND.mkdir(parents=True, exist_ok=True)
    results = []
    for _ in range(cycles):
        n = _next_cycle_number()
        r = run_cycle(n, dry_run=dry_run, force_question=force_question)
        print(json.dumps(r), file=sys.stderr)
        results.append(r)
        if r["status"] != "ok" and not dry_run:
            break
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--question", default=None,
                    help="override the queue/round-robin with a single question")
    ap.add_argument("--no-idle-check", action="store_true",
                    help="ignore the active-session guard")
    args = ap.parse_args()
    res = run(
        cycles=args.cycles,
        dry_run=args.dry_run,
        force_question=args.question,
        respect_idle=not args.no_idle_check,
    )
    if not res:
        return 1
    return 0 if all(r["status"] in ("ok", "dry_run") for r in res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
