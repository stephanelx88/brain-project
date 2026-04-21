"""Promote high-confidence playground items into canonical entities.

Playground items are free-form research artefacts (historically written
by an autoresearch loop, now manual only). Promotion moves selected
items from `playground/` → `entities/` so they become searchable by
future recalls.

## Selection criteria (MVP — intentionally conservative)

A playground item is promoted iff *all* of:

  1. `confidence: high` in its frontmatter (author tags items they're
     sure about; medium/low stay in playground for review).
  2. `len(refs) >= MIN_REFS` (default 2) — the item cites at least two
     existing entities, so we have provenance rather than free-floating
     speculation.
  3. `created_at` within `MAX_AGE_DAYS` (default 14). Older items that
     never promoted likely failed one of the other filters and shouldn't
     get a free pass just because they're stale.
  4. Not already superseded (`status: promoted` or `status: archived`).

Items that pass land in `entities/insights/` with full provenance
(`promoted_from:`) and a `first_seen:` set to the original `created_at`
so downstream recency weighting stays honest.

The source playground file is left intact but annotated with
`status: promoted` + `promoted_to:` — we never delete, only decorate.

## What this intentionally *doesn't* do

- Promote `articles/` — those are narrative; their atomic facts should
  be extracted by auto_extract.py from sessions, not lifted wholesale.
- Dedupe against existing entities — that's the dedupe module's job,
  and it'll catch near-duplicates on the next auto-extract tick. Trying
  to dedupe here would duplicate that logic and tempt feature creep.
- Call LLMs. Promotion is deterministic rule-matching. Every decision
  should be explainable without "because Claude said so".

## CLI

    python -m brain.promote                 # dry-run (default)
    python -m brain.promote --apply         # actually write
    python -m brain.promote --apply --quiet # for launchd / cron

Side effects (only with `--apply`):
  - new `entities/insights/*.md` files
  - source playground files annotated with promotion status
  - one `timeline/YYYY-MM-DD-promote-HHMM.md` audit entry
  - `ingest_notes.ingest_all()` re-run so the new entities are
    immediately semantically searchable (closing the loop in one pass)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import brain.config as config
from brain.slugify import slugify

MIN_REFS = 2
MAX_AGE_DAYS = 14

# Max bullets synthesized in `## Key Facts` during render. Kept tight —
# fact-parsing weights equally across bullets, so flooding dilutes signal.
MAX_KEY_FACTS = 4


@dataclass(frozen=True)
class PromoteRule:
    """How to promote one playground subdir.

    Encapsulates everything that varies between insight / hypothesis /
    contradiction promotion: where the entity lands, what frontmatter
    it gets stamped with, and how strict the gate is.

    Driven by `program.md`'s promotion rules section — keep them in
    sync if either changes.
    """
    target_folder: str          # entities/<this>/
    entity_type: str            # frontmatter `type: ...`
    status: str                 # frontmatter `status: ...`
    min_refs: int = 2
    allowed_confidence: tuple[str, ...] = ("high",)


# Per-kind rules. To enable a new playground subdir, add an entry.
PROMOTE_RULES: dict[str, PromoteRule] = {
    "insights": PromoteRule(
        target_folder="insights", entity_type="insight",
        status="current", min_refs=MIN_REFS,
        allowed_confidence=("high",),
    ),
    "hypotheses": PromoteRule(
        # program.md: "hypotheses auto-promoted to entities/hypotheses/
        # with status: unverified so they're queryable". Lower bar
        # intentional — the *whole point* is to surface medium-conf
        # claims for future evidence to verify or refute. A queryable
        # unverified hypothesis is more useful than one rotting in
        # playground/.
        target_folder="hypotheses", entity_type="hypothesis",
        status="unverified", min_refs=MIN_REFS,
        allowed_confidence=("high", "medium"),
    ),
    "contradictions": PromoteRule(
        # Contradictions surface as entities/contradictions/ so they're
        # queryable in MCP. Keep `high` only — a wrongly-flagged
        # contradiction wastes more attention than a missed one.
        target_folder="contradictions", entity_type="contradiction",
        status="open", min_refs=MIN_REFS,
        allowed_confidence=("high",),
    ),
}

PROMOTED_STATUS = "promoted"


@dataclass
class Candidate:
    path: Path
    kind: str  # "insights" / "hypotheses" / ...
    title: str
    body: str
    confidence: str
    refs: list[str]
    created_at: datetime | None
    raw_frontmatter: dict
    reason_skipped: str | None = None  # None when it passes

    @property
    def passes(self) -> bool:
        return self.reason_skipped is None


@dataclass
class PromoteReport:
    candidates: list[Candidate]
    promoted: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    timeline_path: Path | None = None

    def summary(self) -> str:
        ok = len(self.promoted)
        total = len(self.candidates)
        return f"{ok}/{total} promoted ({len(self.skipped)} skipped, {len(self.errors)} errors)"


# ---------------------------------------------------------------------------
# frontmatter parsing — kept narrow (no yaml dep) so this module runs
# even on install.sh's minimal Python path.
# ---------------------------------------------------------------------------

_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    block, rest = m.group(1), text[m.end():]
    fm: dict = {}
    for line in block.split("\n"):
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if val.startswith("["):
            try:
                fm[key] = json.loads(val)
            except json.JSONDecodeError:
                fm[key] = val
        elif val.startswith('"') and val.endswith('"') and len(val) >= 2:
            fm[key] = val[1:-1]
        else:
            fm[key] = val
    return fm, rest


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ---------------------------------------------------------------------------
# scan + filter
# ---------------------------------------------------------------------------


def _playground_root() -> Path:
    """Resolved lazily so tests that monkey-patch `config.BRAIN_DIR`
    actually redirect the lookup."""
    return config.BRAIN_DIR / "playground"


def _entities_root() -> Path:
    return config.BRAIN_DIR / "entities"


def scan_candidates() -> list[Candidate]:
    """Walk promotable subdirs of `playground/` and produce one Candidate
    per `.md` file. Each Candidate has `reason_skipped` set to a short
    English string when it doesn't qualify; passing ones have it `None`."""
    out: list[Candidate] = []
    root = _playground_root()
    now = datetime.now(timezone.utc)
    for sub in PROMOTE_RULES:
        src = root / sub
        if not src.is_dir():
            continue
        for path in sorted(src.glob("*.md")):
            try:
                text = path.read_text()
            except OSError as exc:
                out.append(Candidate(
                    path=path, kind=sub, title=path.stem, body="",
                    confidence="", refs=[], created_at=None,
                    raw_frontmatter={},
                    reason_skipped=f"unreadable: {exc!r}",
                ))
                continue
            fm, body = _parse_frontmatter(text)
            title = _first_heading(body) or path.stem
            refs_raw = fm.get("refs") or []
            if isinstance(refs_raw, str):
                try:
                    refs = json.loads(refs_raw)
                except json.JSONDecodeError:
                    refs = []
            else:
                refs = list(refs_raw)
            cand = Candidate(
                path=path, kind=sub, title=title,
                body=body, confidence=str(fm.get("confidence", "")).lower(),
                refs=[str(r) for r in refs],
                created_at=_parse_iso(fm.get("created_at")),
                raw_frontmatter=fm,
            )
            cand.reason_skipped = _why_skip(cand, now)
            out.append(cand)
    return out


def _why_skip(c: Candidate, now: datetime) -> str | None:
    rule = PROMOTE_RULES.get(c.kind)
    if rule is None:
        return f"unknown kind: {c.kind}"
    if c.raw_frontmatter.get("status") in (PROMOTED_STATUS, "archived",
                                           "superseded"):
        return f"already {c.raw_frontmatter.get('status')}"
    if c.confidence not in rule.allowed_confidence:
        allowed = "/".join(rule.allowed_confidence)
        return f"confidence={c.confidence or 'missing'} (need: {allowed})"
    if len(c.refs) < rule.min_refs:
        return f"only {len(c.refs)} ref(s) (need ≥ {rule.min_refs})"
    if c.created_at is None:
        return "missing/unparseable created_at"
    age_days = (now - c.created_at).total_seconds() / 86400
    if age_days > MAX_AGE_DAYS:
        return f"age {age_days:.0f}d > {MAX_AGE_DAYS}d"
    return None


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _target_path(c: Candidate) -> Path:
    """Destination entity file. If a file already exists with the same
    slug, append `-promoted` so we never silently overwrite manually
    written entities."""
    rule = PROMOTE_RULES[c.kind]
    # Register the type folder so config.ENTITY_TYPES picks it up —
    # contradictions/ and hypotheses/ are typically new on first use.
    dst_dir = config.get_or_create_type_dir(rule.target_folder)
    slug = slugify(c.title) or c.path.stem.lstrip("0123456789-")
    candidate = dst_dir / f"{slug}.md"
    if candidate.exists():
        candidate = dst_dir / f"{slug}-promoted.md"
    return candidate


_SENT_RE = re.compile(r"(.+?[.!?])(?:\s|$)", re.DOTALL)
_BULLET_RE = re.compile(r"^[\-\*\+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\d+\.\s+(.*)$")


def _extract_fact_paragraphs(body: str, max_n: int = MAX_KEY_FACTS) -> list[str]:
    """Pull up to `max_n` candidate fact statements out of a prose body.

    Strategy (deterministic, no LLM):
      - Split on blank lines into paragraphs
      - Skip headings, code fences, blockquotes, tables, hr, trailing TOC
      - From bullet/numbered lists: take each item's inner text
      - From prose paragraphs: take the first sentence

    Returns short, whitespace-collapsed statements suitable for bulleting
    under `## Key Facts`. Each is truncated to 500 chars — enough for
    semantic embedding to capture meaning, short enough that the entity
    stays readable.
    """
    out: list[str] = []
    # Paragraphs are blocks separated by one or more blank lines.
    for para in re.split(r"\n\s*\n", body.strip()):
        if len(out) >= max_n:
            break
        para = para.strip()
        if not para:
            continue
        first_line = para.splitlines()[0].strip()
        # Skip obvious non-prose blocks.
        if first_line.startswith(("#", "```", ">", "|")) or first_line == "---":
            continue
        is_list = bool(
            _BULLET_RE.match(first_line) or _NUMBERED_RE.match(first_line)
        )
        if is_list:
            for raw in para.splitlines():
                line = raw.strip()
                m = _BULLET_RE.match(line) or _NUMBERED_RE.match(line)
                if not m:
                    continue
                text = re.sub(r"\s+", " ", m.group(1)).strip()
                # Strip markdown bold/italic wrappers that clutter facts.
                text = re.sub(r"^\*+\s*|\s*\*+$", "", text)
                #  testable_via / status metadata bullets aren't facts —
                #  they're machinery. Skip.
                if re.match(r"^(testable_via|status|confidence|refs)\s*:",
                            text, re.I):
                    continue
                if len(text) > 8:
                    out.append(text[:500])
                if len(out) >= max_n:
                    break
            continue
        # Plain prose paragraph: collapse whitespace, take first sentence.
        collapsed = re.sub(r"\s+", " ", para).strip()
        m = _SENT_RE.search(collapsed)
        sent = (m.group(1) if m else collapsed).strip().rstrip(":")
        if len(sent) > 8:
            out.append(sent[:500])
    return out


def _synthesize_key_facts(c: Candidate) -> str:
    """Generate the `## Key Facts` section that makes a promoted entity
    findable via fact-search.

    `db._facts_from_body()` indexes every `- ...` bullet, and
    `_SOURCE_RE` strips the `(source: X, date)` suffix — so we emit the
    playground provenance inline per bullet. Without this section, a
    promoted entity exists on disk but has zero rows in `facts`, and the
    whole promotion is semantically invisible.

    Per the in-vault decision
    `entities/decisions/synthesize-key-facts-section-during-entity-promotion-render.md`.
    """
    created_date = (c.created_at or datetime.now(timezone.utc)).date().isoformat()
    src_tag = f"promoted:{c.path.stem}"
    bullets = _extract_fact_paragraphs(c.body) or [c.title]
    lines = ["## Key Facts"]
    for b in bullets:
        lines.append(f"- {b} (source: {src_tag}, {created_date})")
    return "\n".join(lines) + "\n"


def _render_entity(
    c: Candidate,
    src_rel: str,
    *,
    rule_override: PromoteRule | None = None,
) -> str:
    """Wrap the playground body in canonical entity frontmatter.

    `first_seen` inherits the playground `created_at` rather than "now"
    so the brain's recency weighting reflects when the *thought* was
    formed, not when it happened to be promoted. Same for `last_updated`.

    A synthesized `## Key Facts` section is prepended to the body so
    downstream `db.upsert_entity_from_file()` has bullets to extract.
    Without this, promoted entities stay invisible to fact-search even
    after re-indexing.

    `rule_override` exists for `rerender()` — when refreshing an entity
    that lives in `entities/insights/` because of a legacy
    hypothesis-as-insight promotion, the caller passes the insight rule
    so we don't accidentally restamp it as `type: hypothesis` and
    leave it dangling in the wrong folder.
    """
    rule = rule_override or PROMOTE_RULES[c.kind]
    created_date = (c.created_at or datetime.now(timezone.utc)).date().isoformat()
    front = [
        "---",
        f"type: {rule.entity_type}",
        f"name: {c.title}",
        f"status: {rule.status}",
        f"first_seen: {created_date}",
        f"last_updated: {created_date}",
        f"source_count: {max(1, len(c.refs))}",
        f"promoted_from: {src_rel}",
        "tags: [promoted]",
        "---",
        "",
    ]
    # Strip the H1 heading from body (we'll render our own) + trim.
    body_lines = []
    skipped_first_h1 = False
    for line in c.body.splitlines():
        if not skipped_first_h1 and line.strip().startswith("# "):
            skipped_first_h1 = True
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    #  Drop any pre-existing `## Key Facts` block so we don't double-
    #  render. Stops at the next `##` heading or EOF.
    body = re.sub(
        r"^##\s+Key Facts\s*$.*?(?=^##\s+|\Z)",
        "",
        body,
        flags=re.MULTILINE | re.DOTALL,
    ).strip()
    key_facts = _synthesize_key_facts(c)
    return (
        "\n".join(front)
        + f"# {c.title}\n\n"
        + key_facts
        + ("\n" + body + "\n" if body else "")
    )


def _annotate_source(c: Candidate, target_rel: str) -> str:
    """Return the playground file content with `status: promoted` +
    `promoted_to:` stamped into its frontmatter. Idempotent — if the
    keys already exist, they get replaced, not duplicated."""
    try:
        text = c.path.read_text()
    except OSError:
        return ""
    fm_match = _FRONT_RE.match(text)
    if not fm_match:
        return text
    block = fm_match.group(1)
    # Drop any existing status/promoted_to lines
    kept = [ln for ln in block.split("\n")
            if not ln.startswith("status:") and not ln.startswith("promoted_to:")]
    kept.append(f"status: {PROMOTED_STATUS}")
    kept.append(f"promoted_to: {target_rel}")
    kept.append(f"promoted_at: {_now()}")
    new_block = "\n".join(kept)
    return f"---\n{new_block}\n---\n" + text[fm_match.end():]


# ---------------------------------------------------------------------------
# main entry points
# ---------------------------------------------------------------------------


def run(apply: bool = False, limit: int | None = None) -> PromoteReport:
    """Scan playground, decide, (optionally) write.

    `limit` is a safety knob that caps promotions per invocation so a
    single run can't flood entities/ if something goes wrong upstream.
    Default None = no cap.
    """
    cands = scan_candidates()
    report = PromoteReport(candidates=cands)
    brain_dir = config.BRAIN_DIR
    promotable = [c for c in cands if c.passes]
    if limit is not None:
        promotable = promotable[:limit]

    for c in cands:
        if not c.passes:
            report.skipped.append({
                "src": str(c.path.relative_to(brain_dir)),
                "reason": c.reason_skipped,
            })

    for c in promotable:
        target = _target_path(c)
        src_rel = str(c.path.relative_to(brain_dir))
        target_rel = str(target.relative_to(brain_dir))
        record = {
            "src": src_rel,
            "target": target_rel,
            "title": c.title,
            "refs": c.refs,
        }
        if not apply:
            report.promoted.append(record)
            continue
        try:
            target.write_text(_render_entity(c, src_rel))
            annotated = _annotate_source(c, target_rel)
            if annotated:
                c.path.write_text(annotated)
            _db_upsert_safely(target)
            report.promoted.append(record)
        except OSError as exc:
            report.errors.append({**record, "error": repr(exc)})

    if apply and report.promoted:
        report.timeline_path = _write_timeline(report)
        _reingest_safely()

    return report


def rerender(apply: bool = False) -> PromoteReport:
    """Re-render existing promoted entities against the current
    `_render_entity` logic.

    Needed when the renderer changes (e.g., adding the `Key Facts`
    section in Phase 1) — otherwise legacy promotions stay invisible to
    fact-search until they happen to get re-promoted, which never
    happens because `status: promoted` skips them.

    Walks `entities/*/` for files with `promoted_from:` in frontmatter,
    reads the pointed-at playground file (if still present), and
    rewrites the entity. The playground file's `status: promoted`
    annotation is *not* disturbed.
    """
    report = PromoteReport(candidates=[])
    brain_dir = config.BRAIN_DIR
    ent_root = _entities_root()
    if not ent_root.is_dir():
        return report
    now = datetime.now(timezone.utc)
    for ent_path in sorted(ent_root.rglob("*.md")):
        try:
            ent_text = ent_path.read_text()
        except OSError:
            continue
        fm, _ = _parse_frontmatter(ent_text)
        src_rel = fm.get("promoted_from")
        if not src_rel:
            continue
        pg_path = brain_dir / src_rel
        if not pg_path.exists():
            report.skipped.append({
                "src": src_rel,
                "reason": "playground source missing",
            })
            continue
        try:
            pg_text = pg_path.read_text()
        except OSError as exc:
            report.errors.append({
                "src": src_rel, "target": str(ent_path.relative_to(brain_dir)),
                "error": repr(exc),
            })
            continue
        pg_fm, pg_body = _parse_frontmatter(pg_text)
        #  Reconstruct a Candidate purely for rendering. Filters don't
        #  apply here — this entity already earned promotion once.
        refs_raw = pg_fm.get("refs") or []
        if isinstance(refs_raw, str):
            try:
                refs = json.loads(refs_raw)
            except json.JSONDecodeError:
                refs = []
        else:
            refs = list(refs_raw)
        pg_kind = pg_path.parent.name
        # `kind` may not be a known rule key (legacy/manual playground
        # subdirs, or someone moved a file). Default to `insights` so
        # we never crash on render.
        cand_kind = pg_kind if pg_kind in PROMOTE_RULES else "insights"
        cand = Candidate(
            path=pg_path,
            kind=cand_kind,
            title=_first_heading(pg_body) or pg_path.stem,
            body=pg_body,
            confidence=str(pg_fm.get("confidence", "")).lower(),
            refs=[str(r) for r in refs],
            created_at=_parse_iso(pg_fm.get("created_at")),
            raw_frontmatter=pg_fm,
        )
        # Stay loyal to where the entity already lives — if a legacy
        # hypothesis was promoted into entities/insights/ before the
        # per-kind split, rerender it AS an insight (not a hypothesis)
        # so the file's frontmatter matches its folder.
        ent_folder = ent_path.parent.name
        rule_override: PromoteRule | None = None
        for rule in PROMOTE_RULES.values():
            if rule.target_folder == ent_folder:
                rule_override = rule
                break
        rel_ent = str(ent_path.relative_to(brain_dir))
        record = {
            "src": src_rel,
            "target": rel_ent,
            "title": cand.title,
            "refs": cand.refs,
        }
        if not apply:
            report.promoted.append(record)
            continue
        try:
            ent_path.write_text(
                _render_entity(cand, src_rel, rule_override=rule_override)
            )
            _db_upsert_safely(ent_path)
            report.promoted.append(record)
        except OSError as exc:
            report.errors.append({**record, "error": repr(exc)})
    # Record ran timeline unused — rerender is a maintenance action,
    # not a decision event. Still trigger the reindex so new Key Facts
    # land in the semantic index right away.
    if apply and report.promoted:
        _reingest_safely()
    return report


def _db_upsert_safely(path: Path) -> None:
    """Register the new entity in the SQLite mirror so it's visible to
    semantic search on the next `semantic.build()`. Without this the
    file exists on disk but the facts table doesn't know about it —
    recall stays blind to the freshly promoted item, defeating the
    whole point of promotion.

    Import lazily so dry-run tests don't pay the SQLite connection cost.
    Any failure is downgraded to stderr — promotion itself already
    succeeded (file written); the index refresh is best-effort."""
    try:
        from brain import db
        fn = getattr(db, "upsert_entity_from_file", None)
        if fn is not None:
            fn(path)
    except Exception as exc:
        print(f"db upsert warn for {path.name}: {exc!r}", file=sys.stderr)


def _write_timeline(report: PromoteReport) -> Path:
    ts = datetime.now(timezone.utc)
    name = f"{ts.strftime('%Y-%m-%d')}-promote-{ts.strftime('%H%M')}.md"
    tl_dir = config.BRAIN_DIR / "timeline"
    tl_dir.mkdir(parents=True, exist_ok=True)
    path = tl_dir / name
    lines = [f"# Promotion — {ts.strftime('%Y-%m-%d %H:%M UTC')}", ""]
    lines.append(f"**Summary:** {report.summary()}")
    lines.append("")
    if report.promoted:
        lines.append("## Promoted")
        for p in report.promoted:
            lines.append(f"- **{p['title']}**")
            lines.append(f"  - src: `{p['src']}`")
            lines.append(f"  - target: `{p['target']}`")
            lines.append(f"  - refs: {', '.join(p['refs']) or '(none)'}")
    if report.skipped:
        lines.append("")
        lines.append("## Skipped")
        # Group by reason so the report is readable when there are
        # hundreds of skipped items.
        by_reason: dict[str, list[str]] = {}
        for s in report.skipped:
            by_reason.setdefault(s["reason"], []).append(s["src"])
        for reason, srcs in sorted(by_reason.items()):
            lines.append(f"- **{reason}** ({len(srcs)}):")
            for src in srcs[:10]:
                lines.append(f"  - `{src}`")
            if len(srcs) > 10:
                lines.append(f"  - … {len(srcs) - 10} more")
    if report.errors:
        lines.append("")
        lines.append("## Errors")
        for e in report.errors:
            lines.append(f"- `{e['src']}` → `{e['target']}`: {e['error']}")
    path.write_text("\n".join(lines) + "\n")
    return path


def _reingest_safely() -> None:
    """Refresh the semantic index so newly-written entities are
    searchable on the next recall. Two layers:

      - `ingest_notes.ingest_all()` picks up the promoted files as notes
        (cheap diff walk; skipped if unchanged)
      - `semantic.build()` regenerates fact + note embeddings so the
        upserted entity rows from `_db_upsert_safely()` actually become
        vectors in the search index

    Imported lazily because the semantic stack pulls in sentence-
    transformers + torch and we don't want that in the hot path of a
    dry-run."""
    try:
        from brain import ingest_notes
        ingest_notes.ingest_all(verbose=False)
    except Exception as exc:  # never fail the promotion over a reindex
        print(f"reingest warn: {exc!r}", file=sys.stderr)
    try:
        from brain import semantic
        semantic.build()
    except Exception as exc:
        print(f"semantic rebuild warn: {exc!r}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def format_text(report: PromoteReport) -> str:
    lines = [f"promote: {report.summary()}"]
    if report.promoted:
        lines.append("")
        lines.append("  promoted:")
        for p in report.promoted:
            refs = f" [{len(p['refs'])} refs]"
            lines.append(f"    + {p['src']} → {p['target']}{refs}")
    if report.skipped:
        by_reason: dict[str, int] = {}
        for s in report.skipped:
            by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
        lines.append("")
        lines.append("  skipped:")
        for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            lines.append(f"    · {n:>3}  {reason}")
    if report.errors:
        lines.append("")
        lines.append("  errors:")
        for e in report.errors:
            lines.append(f"    ! {e['src']}: {e['error']}")
    if report.timeline_path:
        lines.append("")
        lines.append(f"  log: {report.timeline_path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Promote high-confidence playground items into entities/"
    )
    p.add_argument("--apply", action="store_true",
                   help="Actually write (default: dry-run)")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress stdout when nothing promoted (for cron)")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap promotions per run (safety knob)")
    p.add_argument("--json", action="store_true",
                   help="Emit a JSON summary instead of the text report")
    p.add_argument("--rerender", action="store_true",
                   help="Regenerate already-promoted entities against the "
                        "current render (use after changing _render_entity; "
                        "does NOT touch playground annotations)")
    args = p.parse_args(argv)

    config.ensure_dirs()
    if args.rerender:
        report = rerender(apply=args.apply)
    else:
        report = run(apply=args.apply, limit=args.limit)

    if args.json:
        print(json.dumps({
            "promoted": report.promoted,
            "skipped": report.skipped,
            "errors": report.errors,
            "timeline": str(report.timeline_path) if report.timeline_path else None,
            "summary": report.summary(),
            "dry_run": not args.apply,
        }, indent=2))
        return 0

    if args.quiet and not report.promoted and not report.errors:
        return 0
    print(format_text(report))
    if not args.apply and report.promoted:
        print("\n(dry-run — rerun with --apply to write)")
    return 0 if not report.errors else 1


if __name__ == "__main__":
    sys.exit(main())
