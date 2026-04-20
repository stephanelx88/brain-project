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
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import brain.config as config
from brain import db, ingest_notes, recall_metric, semantic
from brain.auto_extract import call_claude as _call_claude_default
from brain.log import append_log


# ---------------------------------------------------------------------------
# Dedicated LLM caller for autoresearch.
#
# auto_extract.call_claude works for session extraction because the
# extraction prompt is tiny + format-strict. Autoresearch prompts are
# bigger and the brain's own MCP/CLAUDE.md keep tempting Claude to
# "look it up via tools" instead of synthesizing from the inlined
# context. We use --bare to skip CLAUDE.md + hooks, --tools "" to
# disable tool calls outright, and --system-prompt to override the
# default system prompt with one tailored for one-shot JSON synthesis.
# ---------------------------------------------------------------------------

AR_SYSTEM_PROMPT = (
    "You are a one-shot JSON synthesis agent for a knowledge brain. "
    "The user message contains ALL the context you need (entity cards, "
    "facts, the spec excerpt). The brain MCP tools are NOT available "
    "in this run; do not request them, do not refer to them. "
    "Do NOT ask clarifying questions. Do NOT produce any prose "
    "preamble. Read the user message, synthesize new playground items "
    "from the inlined context, and respond with a SINGLE JSON object "
    "matching the schema described in the user message. Begin your "
    "output with `{` and end with `}`. Wrap in a ```json fence is OK "
    "but optional."
)


def call_claude(prompt: str, timeout: int) -> str | None:
    """CLI invocation tuned for one-shot JSON synthesis: full
    --system-prompt override (replaces the default 'you are Claude
    Code' system prompt that argues with our instructions) and
    --tools '' so the model can't try to call brain MCP tools that
    aren't wired into this run. CLAUDE.md is still discovered, but
    with no system-prompt anchor it just becomes flavour text the
    model can ignore."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _call_claude_default(prompt, timeout=timeout)
    try:
        env = {**os.environ, "BRAIN_EXTRACTING": "1"}
        result = subprocess.run(
            [
                "claude", "-p",
                "--model", "haiku",
                "--no-session-persistence",
                "--system-prompt", AR_SYSTEM_PROMPT,
                "--tools", "",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        print(
            f"claude exit={result.returncode} "
            f"stderr={result.stderr[:300]!r}; falling back to default caller",
            file=sys.stderr,
        )
        return _call_claude_default(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"claude call timed out after {timeout}s", file=sys.stderr)
        return None
    except FileNotFoundError:
        return _call_claude_default(prompt, timeout=timeout)

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
    if not text:
        return None
    #  Drop fenced code blocks if present.
    if "```" in text:
        text = "\n".join(l for l in text.split("\n") if not l.startswith("```"))
    #  Walk forward to the first {…} balanced object so prose preambles
    #  ("Sure, here is the JSON:") and SessionStart hook output don't
    #  derail json.loads.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    s = text.find("{")
    while s != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(s, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[s:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # try next `{`
        s = text.find("{", s + 1)
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

    #  English plural rules — "hypothesis" → "hypotheses", others just +s.
    plural = {"hypothesis": "hypotheses"}.get(kind, kind + "s")
    sub = PLAYGROUND / plural
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

def _refresh_index_after_writes() -> None:
    """Re-ingest the vault so newly-written playground items are visible
    to semantic.search_notes / hybrid_search on the next eval pass."""
    try:
        ingest_notes.ingest_all(verbose=False)
    except Exception as exc:
        print(f"refresh-index warn: {exc!r}", file=sys.stderr)


def run_cycle(cycle_n: int, *, dry_run: bool = False, force_question: str | None = None,
              measure_metric: bool = True) -> dict:
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

    # Pre-cycle metric (computed AFTER context gather so the slow embed
    # cold-start happens once for both gather + score). Skip if the caller
    # asked to (e.g. inside a long --cycles N run we score once at the
    # outer loop's start, then incrementally between cycles).
    pre = recall_metric.score_coverage(persist=False) if measure_metric else None

    written: list[str] = []
    for item in parsed.get("items", [])[:MAX_OUTPUT_FILES]:
        path = _write_playground_item(cycle_n, item)
        if path:
            written.append(str(path.relative_to(config.BRAIN_DIR)))

    # Post-cycle metric: re-ingest so playground writes are searchable,
    # then re-score the same eval set.
    post = None
    delta = None
    promoted_targets: list[str] = []
    if measure_metric:
        if written:
            _refresh_index_after_writes()
        #  Auto-promote after the reindex so any item the agent just
        #  wrote that meets the rule (confidence: high, ≥2 refs, ≤14d)
        #  becomes a canonical entity *before* we re-score. Cap to 1
        #  per cycle so a runaway cycle can't flood entities/.
        if written and not dry_run:
            try:
                from brain import promote as _promote
                promo = _promote.run(apply=True, limit=1)
                promoted_targets = [p["target"] for p in promo.promoted]
            except Exception as exc:
                log_lines.append(f"- promote warn: {exc!r}")
        post = recall_metric.score_coverage(persist=True)
        delta = recall_metric.diff_reports(pre, post)

    log_lines.append(f"**Summary:** {parsed.get('summary', '(none)')}")
    log_lines.append("")
    metric_self = parsed.get("metric_estimate", {})
    log_lines.append(
        f"**Agent self-estimate:** {metric_self.get('kind_of_impact', '?')} — "
        f"{metric_self.get('rationale', '?')}"
    )
    log_lines.append("")
    if pre is not None and post is not None:
        log_lines.append("## Coverage metric (val_bpb analog)")
        log_lines.append("")
        log_lines.append(f"- before: {pre.headline()}")
        log_lines.append(f"-  after: {post.headline()}")
        log_lines.append(
            f"-  delta: {delta['score_delta']:+.4f}  "
            f"({'IMPROVED' if delta['improved'] else 'no improvement'})"
        )
        if delta["flipped_to_hit"]:
            log_lines.append(f"- newly answered ({len(delta['flipped_to_hit'])}):")
            for q in delta["flipped_to_hit"]:
                log_lines.append(f"  - {q}")
        if delta["flipped_to_miss"]:
            log_lines.append(f"- regressed ({len(delta['flipped_to_miss'])}):")
            for q in delta["flipped_to_miss"]:
                log_lines.append(f"  - {q}")
        if delta["biggest_score_gains"]:
            log_lines.append("- top score gains:")
            for q, d in delta["biggest_score_gains"]:
                log_lines.append(f"  - {d:+.3f}  {q}")
        log_lines.append("")
    if written:
        log_lines.append(f"**Written ({len(written)}):**")
        for w in written:
            log_lines.append(f"- `{w}`")
    else:
        log_lines.append("**Written:** (none — agent decided no items worth filing)")
    if promoted_targets:
        log_lines.append("")
        log_lines.append(f"**Promoted ({len(promoted_targets)}):**")
        for t in promoted_targets:
            log_lines.append(f"- `{t}`")
    log_lines.append("")
    log_lines.append(f"_Wall clock: {round(time.time() - started, 1)} s_")
    cycle_log.write_text("\n".join(log_lines))

    if from_queue:
        _mark_queue_done(question)

    # research-log.md one-line summary
    metric_str = (
        f"score: {pre.score:.3f}->{post.score:.3f} (Δ{delta['score_delta']:+.3f})"
        if pre is not None and post is not None else "score: n/a"
    )
    line = (
        f"## [{_now()}] cycle-{cycle_n:04d} | "
        f"q: {question[:80]} | items: {len(written)} | "
        f"{metric_str}\n"
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
        "score_before": pre.score if pre is not None else None,
        "score_after": post.score if post is not None else None,
        "score_delta": delta["score_delta"] if delta is not None else None,
        "avg_top_before": pre.avg_top_score if pre is not None else None,
        "avg_top_after": post.avg_top_score if post is not None else None,
        "newly_answered": delta["flipped_to_hit"] if delta is not None else [],
        "regressed": delta["flipped_to_miss"] if delta is not None else [],
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run(cycles: int = 1, *, dry_run: bool = False, force_question: str | None = None,
        respect_idle: bool = True, measure_metric: bool = True) -> list[dict]:
    if respect_idle and not _is_idle() and not dry_run:
        print(f"skip: claude session active in last {IDLE_THRESHOLD_S}s",
              file=sys.stderr)
        return []
    PLAYGROUND.mkdir(parents=True, exist_ok=True)

    if measure_metric and not dry_run:
        baseline = recall_metric.score_coverage(persist=True)
        print(f"[autoresearch] BASELINE  {baseline.headline()}", file=sys.stderr)

    results = []
    for i in range(cycles):
        n = _next_cycle_number()
        r = run_cycle(n, dry_run=dry_run, force_question=force_question,
                      measure_metric=measure_metric and not dry_run)
        if r["status"] == "ok" and r.get("score_after") is not None:
            arrow = "↓" if (r["score_delta"] or 0) < 0 else (
                "↑" if (r["score_delta"] or 0) > 0 else "·")
            avg_d = (r.get("avg_top_after") or 0) - (r.get("avg_top_before") or 0)
            avg_arrow = "↑" if avg_d > 0 else ("↓" if avg_d < 0 else "·")
            print(
                f"[autoresearch] cycle {r['cycle']:>4}  "
                f"items={r['items_written']}  "
                f"miss-rate {r['score_before']:.3f}→{r['score_after']:.3f} "
                f"({arrow}{abs(r['score_delta']):.3f})  "
                f"avg-top {r.get('avg_top_before',0):.3f}→{r.get('avg_top_after',0):.3f} "
                f"({avg_arrow}{abs(avg_d):.3f})  "
                f"flipped+={len(r['newly_answered'])}/-={len(r['regressed'])}",
                file=sys.stderr,
            )
        else:
            print(json.dumps(r), file=sys.stderr)
        results.append(r)
        if r["status"] != "ok" and not dry_run:
            break

    if results and measure_metric and not dry_run:
        scores = [r.get("score_after") for r in results if r.get("score_after") is not None]
        if scores:
            print(
                f"[autoresearch] FINAL trajectory: "
                f"{baseline.score:.3f} → " + " → ".join(f"{s:.3f}" for s in scores),
                file=sys.stderr,
            )
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--question", default=None,
                    help="override the queue/round-robin with a single question")
    ap.add_argument("--no-idle-check", action="store_true",
                    help="ignore the active-session guard")
    ap.add_argument("--no-metric", action="store_true",
                    help="skip the pre/post coverage scoring (faster)")
    args = ap.parse_args()
    res = run(
        cycles=args.cycles,
        dry_run=args.dry_run,
        force_question=args.question,
        respect_idle=not args.no_idle_check,
        measure_metric=not args.no_metric,
    )
    if not res:
        return 1
    return 0 if all(r["status"] in ("ok", "dry_run") for r in res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
