"""Apply extraction results to the brain — create/update entities, index, log, git.

Extraction format is open-vocabulary: each entity carries its own `type`
(free-form, e.g. people/projects/meetings/recipes). Any folder under
entities/ is a valid type; new types create new folders on first use.

Corrections are written to identity/corrections.md (not entities/) under
an exclusive file lock so concurrent extractions can't clobber each other.
"""

import fcntl
from datetime import datetime, timezone

import brain.config as config
from brain.entities import (
    _singular_type,
    append_to_entity,
    create_entity,
    entity_exists,
    entity_path,
)
from brain.git_ops import commit
from brain.index import rebuild_index
from brain.log import append_log
from brain.slugify import slugify

try:
    from brain.db import upsert_entity_from_file  # write-through index
except Exception:  # pragma: no cover - db is optional at first run
    upsert_entity_from_file = None

try:
    from brain.db import record_fact_provenance
except Exception:  # pragma: no cover
    record_fact_provenance = None

try:
    from brain.supersede import recompute_for_entity as _recompute_supersede
except Exception:  # pragma: no cover
    _recompute_supersede = None

try:
    from brain.ontology_guard import validate_entity as _validate_entity
except Exception:  # pragma: no cover
    _validate_entity = None

TRIPLE_CONFIDENCE_THRESHOLD = 0.8


def _apply_triples(triples: list[dict], source_label: str) -> None:
    """Route extracted triples by confidence: high → RDF store, low → pending queue."""
    if not triples:
        return
    try:
        from brain.graph import add_triple, VALID_PREDICATES
        from brain.triple_audit import add_pending
        from brain.triple_rules import adjusted_confidence
    except Exception:
        return  # graph layer is optional; don't break extraction if missing

    high, low = [], []
    for t in triples:
        pred = t.get("predicate", "")
        if pred not in VALID_PREDICATES:
            continue
        raw = float(t.get("confidence", 0.5))
        adj = adjusted_confidence(pred, raw)
        t = dict(t, confidence=adj)
        (high if adj >= TRIPLE_CONFIDENCE_THRESHOLD else low).append(t)

    for t in high:
        add_triple(t["subject"], t["predicate"], t["object"], source=source_label)

    if low:
        add_pending(low, source=source_label)


def _render_frontmatter_value(v) -> str:
    """Render a metadata value for YAML frontmatter."""
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_render_frontmatter_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return str(v)
    return str(v)


def _strip_existing_source_suffix(fact: str) -> str:
    """Drop any trailing `(source: …)` annotations the LLM accidentally
    added so we can attach a single canonical one ourselves."""
    s = fact.strip()
    while True:
        i = s.rfind("(source:")
        if i == -1:
            return s
        j = s.find(")", i)
        if j == -1:
            return s
        s = (s[:i] + s[j + 1:]).rstrip()


def _apply_entity(
    item: dict,
    source_label: str,
    now: str,
    created: list[str],
    updated: list[str],
    touched_paths: set | None = None,
    source_note_paths: list[str] | None = None,
    source_sha: str | None = None,
) -> None:
    """Apply one generic entity from the extraction payload.

    `source_note_paths` (optional) lists vault-relative note paths that
    this extraction was *derived from*. When supplied, each fact gets a
    provenance row in the `fact_provenance` table linking it back to
    those notes — so deleting one of those notes later will retract the
    fact (see `ingest_notes.invalidate_facts_for_note`). Sessions that
    don't pin a source note skip provenance and remain accumulator-only,
    matching pre-existing behaviour.
    """
    if _validate_entity is not None:
        is_valid, reason = _validate_entity(item)
        if not is_valid:
            print(f"[ontology_guard] rejected {item.get('type')!r}:{item.get('name')!r} — {reason}")
            return

    entity_type = (item.get("type") or "").strip().lower()
    name = (item.get("name") or "").strip()
    if not entity_type or not name:
        return

    raw_facts = [str(f).strip() for f in item.get("facts", []) if str(f).strip()]
    facts = [_strip_existing_source_suffix(f) for f in raw_facts]
    metadata = item.get("metadata") or {}

    fm = {}
    for k, v in metadata.items():
        if v in (None, "", [], {}):
            continue
        fm[str(k)] = _render_frontmatter_value(v)

    facts_body = "\n".join(f"- {f} (source: {source_label}, {now})" for f in facts)

    is_new = bool(item.get("is_new"))
    exists = entity_exists(entity_type, name)

    path = None
    if is_new or not exists:
        # Every entity must have ## Key Facts so db.upsert_entity_from_file
        # can index it. If the LLM returned no facts, synthesize a minimal
        # one from the entity name so the entity is findable from day one.
        effective_facts_body = facts_body or f"- {name} (source: {source_label}, {now})"
        body = f"## Key Facts\n{effective_facts_body}\n"
        path = create_entity(entity_type, name, frontmatter=fm, body=body)
        created.append(f"{_singular_type(entity_type)}:{name}")
    elif facts_body:
        try:
            path = append_to_entity(entity_type, name, "Key Facts", facts_body)
            updated.append(f"{_singular_type(entity_type)}:{name}")
        except FileNotFoundError:
            body = f"## Key Facts\n{facts_body}\n"
            path = create_entity(entity_type, name, frontmatter=fm, body=body)
            created.append(f"{_singular_type(entity_type)}:{name}")

    if path is not None and touched_paths is not None:
        touched_paths.add(path)

    if (
        path is not None
        and source_note_paths
        and record_fact_provenance is not None
        and facts
    ):
        for fact_text in facts:
            try:
                record_fact_provenance(path, fact_text, source_note_paths,
                                       source_sha=source_sha)
            except Exception as exc:
                print(f"provenance write failed for {path}: {exc}")


def _apply_corrections(corrections: list[dict], source_label: str, now: str) -> None:
    """Append corrections to identity/corrections.md under an exclusive lock."""
    if not corrections:
        return
    corrections_file = config.IDENTITY_DIR / "corrections.md"
    corrections_file.parent.mkdir(parents=True, exist_ok=True)
    with open(corrections_file, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            text = f.read()
            if not text:
                text = (
                    f"---\ntype: corrections\nlast_updated: {now}\n---\n\n"
                    f"# Corrections\n\n## Active Corrections\n"
                )
            original = text
            for correction in corrections:
                pattern = correction.get("pattern", "")
                correction_text = correction.get("correction", "")
                rule = correction.get("rule", "")
                if pattern and correction_text and correction_text not in text:
                    entry = (
                        f"\n- **{correction_text}** {pattern}\n"
                        f"  Rule: {rule}\n"
                        f"  (Source: {source_label}, {now})\n"
                    )
                    text = text.rstrip() + "\n" + entry
            if text != original:
                f.seek(0)
                f.truncate()
                f.write(text)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def apply_extraction(
    extraction: dict,
    source_label: str,
    *,
    do_commit: bool = True,
    do_rebuild_index: bool = True,
    source_note_paths: list[str] | None = None,
    source_sha: str | None = None,
) -> dict:
    """Apply an extraction payload to the brain.

    Expected payload shape:
        {
          "entities": [ {type, name, is_new, facts, metadata}, ... ],
          "corrections": [ {pattern, correction, rule}, ... ]
        }

    `source_note_paths` (optional) — vault-relative paths of user notes
    this extraction was derived from. Each fact added gets a row in
    `fact_provenance` so deleting any of those notes will later retract
    the fact. Leave empty for session-only extractions (current default).

    Set `do_commit=False` and `do_rebuild_index=False` for batched callers
    that want to apply many extractions and then commit/rebuild once at
    the end. Returns created/updated lists plus the set of touched paths.
    """
    config.ensure_dirs()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    created: list[str] = []
    updated: list[str] = []
    touched: set = set()

    for entity in extraction.get("entities", []):
        _apply_entity(
            entity, source_label, now, created, updated, touched,
            source_note_paths=source_note_paths,
            source_sha=source_sha,
        )

    _apply_corrections(extraction.get("corrections", []), source_label, now)
    _apply_triples(extraction.get("triples", []), source_label)

    if upsert_entity_from_file is not None:
        for p in touched:
            try:
                upsert_entity_from_file(p)
            except Exception as e:  # don't break extraction on db trouble
                print(f"db write-through failed for {p}: {e}")
            if _recompute_supersede is not None:
                try:
                    _recompute_supersede(p)
                except Exception as e:
                    print(f"supersede recompute failed for {p}: {e}")

    if do_rebuild_index:
        rebuild_index()

    summary_parts = []
    if created:
        summary_parts.append(f"created {len(created)}")
    if updated:
        summary_parts.append(f"updated {len(updated)}")
    summary = ", ".join(summary_parts) or "no changes"
    append_log("extract", f"{source_label} → {summary}")

    if do_commit:
        entity_names = [e.split(":", 1)[1] for e in created + updated]
        commit_msg = f"brain: extract from {source_label} — {summary}"
        if entity_names:
            commit_msg += f"\n\nEntities: {', '.join(entity_names[:10])}"
        # Explicit allowlist: only stage what this extraction actually
        # touched + the side-effect files (log, index, corrections).
        # Never `git add -A` — that swept user-deleted root notes into
        # automated commits in the past (see git_ops.commit docstring).
        paths = list(touched) + [
            "log.md",
            "index.md",
            "identity/corrections.md",
        ]
        commit(commit_msg, paths=paths)

    return {
        "created": created,
        "updated": updated,
        "touched_paths": [str(p) for p in touched],
    }
