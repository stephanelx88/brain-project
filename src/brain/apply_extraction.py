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
)
from brain.git_ops import commit
from brain.index import rebuild_index
from brain.log import append_log
from brain.slugify import slugify


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


def _apply_entity(
    item: dict,
    source_label: str,
    now: str,
    created: list[str],
    updated: list[str],
) -> None:
    """Apply one generic entity from the extraction payload."""
    entity_type = (item.get("type") or "").strip().lower()
    name = (item.get("name") or "").strip()
    if not entity_type or not name:
        return

    facts = [str(f).strip() for f in item.get("facts", []) if str(f).strip()]
    metadata = item.get("metadata") or {}

    fm = {}
    for k, v in metadata.items():
        if v in (None, "", [], {}):
            continue
        fm[str(k)] = _render_frontmatter_value(v)

    facts_body = "\n".join(f"- {f} (source: {source_label}, {now})" for f in facts)

    is_new = bool(item.get("is_new"))
    exists = entity_exists(entity_type, name)

    if is_new or not exists:
        body = f"## Key Facts\n{facts_body}\n" if facts_body else ""
        create_entity(entity_type, name, frontmatter=fm, body=body)
        created.append(f"{_singular_type(entity_type)}:{name}")
    elif facts_body:
        try:
            append_to_entity(entity_type, name, "Key Facts", facts_body)
            updated.append(f"{_singular_type(entity_type)}:{name}")
        except FileNotFoundError:
            body = f"## Key Facts\n{facts_body}\n"
            create_entity(entity_type, name, frontmatter=fm, body=body)
            created.append(f"{_singular_type(entity_type)}:{name}")


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


def apply_extraction(extraction: dict, source_label: str) -> dict:
    """Apply an extraction payload to the brain.

    Expected payload shape:
        {
          "entities": [ {type, name, is_new, facts, metadata}, ... ],
          "corrections": [ {pattern, correction, rule}, ... ]
        }
    """
    config.ensure_dirs()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    created: list[str] = []
    updated: list[str] = []

    for entity in extraction.get("entities", []):
        _apply_entity(entity, source_label, now, created, updated)

    _apply_corrections(extraction.get("corrections", []), source_label, now)

    rebuild_index()

    summary_parts = []
    if created:
        summary_parts.append(f"created {len(created)}")
    if updated:
        summary_parts.append(f"updated {len(updated)}")
    summary = ", ".join(summary_parts) or "no changes"
    append_log("extract", f"{source_label} → {summary}")

    entity_names = [e.split(":", 1)[1] for e in created + updated]
    commit_msg = f"brain: extract from {source_label} — {summary}"
    if entity_names:
        commit_msg += f"\n\nEntities: {', '.join(entity_names[:10])}"
    commit(commit_msg)

    return {"created": created, "updated": updated}
