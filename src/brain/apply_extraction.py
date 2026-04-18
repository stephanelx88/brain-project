"""Apply extraction results to the brain — create/update entities, index, log, git."""

import fcntl
from datetime import datetime, timezone
from pathlib import Path

import brain.config as config
from brain.entities import (
    append_to_entity,
    create_entity,
    entity_exists,
)
from brain.git_ops import commit
from brain.index import rebuild_index
from brain.log import append_log


def _apply_fact_entity(
    entity_type: str,
    items: list[dict],
    source_label: str,
    now: str,
    created: list[str],
    updated: list[str],
) -> None:
    """Generic handler for fact-based entities (people, clients, projects, domains).

    Creates new entity if is_new=true, otherwise appends facts to existing.
    """
    frontmatter_fields = {
        "people": ("role", "company"),
        "clients": (),
        "projects": ("client",),
        "domains": ("source_context",),
    }
    extra_fields = frontmatter_fields.get(entity_type, ())

    for item in items:
        name = item.get("name", "")
        if not name:
            continue
        facts = item.get("facts", [])
        if not facts:
            continue

        facts_text = "\n".join(
            f"- {f} (source: {source_label}, {now})" for f in facts
        )

        # Build frontmatter from extra fields
        fm = {}
        for field in extra_fields:
            if item.get(field):
                fm[field] = f'"[[{item[field]}]]"'

        is_new = item.get("is_new", False)
        exists = entity_exists(entity_type, name)

        if is_new and not exists:
            create_entity(entity_type, name, frontmatter=fm, body=f"## Key Facts\n{facts_text}")
            created.append(f"{entity_type.rstrip('s')}:{name}")
        else:
            try:
                append_to_entity(entity_type, name, "Key Facts", facts_text)
                updated.append(f"{entity_type.rstrip('s')}:{name}")
            except FileNotFoundError:
                create_entity(entity_type, name, frontmatter=fm, body=f"## Key Facts\n{facts_text}")
                created.append(f"{entity_type.rstrip('s')}:{name}")


def apply_extraction(extraction: dict, source_label: str) -> dict:
    """Apply a structured extraction result to the brain.

    Args:
        extraction: parsed JSON from the extraction prompt
        source_label: description for the log entry (e.g. "Session in project-x")

    Returns:
        dict with counts of created/updated entities
    """
    config.ensure_dirs()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    created = []
    updated = []

    # Fact-based entities (people, clients, projects, domains)
    _apply_fact_entity("people", extraction.get("people", []), source_label, now, created, updated)
    _apply_fact_entity("clients", extraction.get("clients", []), source_label, now, created, updated)
    _apply_fact_entity("projects", extraction.get("projects", []), source_label, now, created, updated)
    _apply_fact_entity("domains", extraction.get("domains", []), source_label, now, created, updated)

    # Decisions
    for decision in extraction.get("decisions", []):
        title = decision.get("title", "")
        if not title:
            continue
        date = decision.get("date", now)
        name = f"{date} {title}"
        alternatives = "\n".join(f"- {a}" for a in decision.get("alternatives", []))
        body = f"## Context\n{decision.get('context', '')}\n"
        if alternatives:
            body += f"\n## Alternatives Considered\n{alternatives}\n"
        if not entity_exists("decisions", name):
            create_entity("decisions", name, frontmatter={"date": date}, body=body)
            created.append(f"decision:{title}")
        else:
            try:
                append_to_entity("decisions", name, "Updates", f"- Re-referenced (source: {source_label}, {now})")
                updated.append(f"decision:{title}")
            except FileNotFoundError:
                create_entity("decisions", name, frontmatter={"date": date}, body=body)
                created.append(f"decision:{title}")

    # Issues
    for issue in extraction.get("issues", []):
        title = issue.get("title", "")
        if not title:
            continue
        raised_by = issue.get("raised_by", "unknown")
        about = issue.get("about", "unknown")
        status = issue.get("status", "open")
        fm = {
            "raised_by": f"\"[[{raised_by}]]\"",
            "about": f"\"[[{about}]]\"",
            "issue_status": status,
        }
        if not entity_exists("issues", title):
            create_entity("issues", title, frontmatter=fm)
            created.append(f"issue:{title}")
        else:
            try:
                update_text = f"- Status: {status} (updated {now}, source: {source_label})"
                append_to_entity("issues", title, "Updates", update_text)
                updated.append(f"issue:{title}")
            except FileNotFoundError:
                create_entity("issues", title, frontmatter=fm)
                created.append(f"issue:{title}")

    # Action items (from file ingestion) — store as issues
    for item in extraction.get("action_items", []):
        task_desc = item.get("task", "")
        if not task_desc:
            continue
        owner = item.get("owner", "unknown")
        deadline = item.get("deadline")
        related = item.get("related_to", "")
        title = f"Action: {task_desc[:80]}"
        fm = {
            "raised_by": f'"[[{owner}]]"',
            "about": f'"[[{related}]]"' if related else '""',
            "issue_status": "open",
        }
        if deadline:
            fm["deadline"] = deadline
        if not entity_exists("issues", title):
            create_entity("issues", title, frontmatter=fm, body=f"## Details\n{task_desc}\n\nOwner: {owner}\nDeadline: {deadline or 'none'}\n")
            created.append(f"issue:{title[:40]}")
        else:
            try:
                update_text = f"- Re-confirmed (owner: {owner}, deadline: {deadline or 'none'}, updated {now}, source: {source_label})"
                append_to_entity("issues", title, "Updates", update_text)
                updated.append(f"issue:{title[:40]}")
            except FileNotFoundError:
                create_entity("issues", title, frontmatter=fm, body=f"## Details\n{task_desc}\n\nOwner: {owner}\nDeadline: {deadline or 'none'}\n")
                created.append(f"issue:{title[:40]}")

    # Insights
    for insight in extraction.get("insights", []):
        content = insight.get("content", "")
        if not content:
            continue
        source = insight.get("source", source_label)
        confidence = insight.get("confidence", "medium")
        name = f"{now} {content[:60]}"
        fm = {"confidence": confidence, "insight_source": source}
        if not entity_exists("insights", name):
            create_entity("insights", name, frontmatter=fm, body=content)
            created.append(f"insight:{content[:40]}")
        else:
            try:
                append_to_entity("insights", name, "Updates", f"- Re-referenced (source: {source_label}, {now})")
                updated.append(f"insight:{content[:40]}")
            except FileNotFoundError:
                create_entity("insights", name, frontmatter=fm, body=content)
                created.append(f"insight:{content[:40]}")

    # Corrections — append to identity/corrections.md under an exclusive file lock
    # so concurrent extractions can't clobber each other.
    corrections_file = config.IDENTITY_DIR / "corrections.md"
    corrections_file.parent.mkdir(parents=True, exist_ok=True)
    with open(corrections_file, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            text = f.read()
            if not text:
                text = f"---\ntype: corrections\nlast_updated: {now}\n---\n\n# Corrections\n\n## Active Corrections\n"
            original = text
            for correction in extraction.get("corrections", []):
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

    # Evolutions
    for evolution in extraction.get("evolutions", []):
        topic = evolution.get("topic", "")
        if not topic:
            continue
        name = f"{now} {topic}"
        body = (
            f"## Evolution\n"
            f"**Before**: {evolution.get('old_position', 'unknown')}\n\n"
            f"**After**: {evolution.get('new_position', 'unknown')}\n\n"
            f"**Cause**: {evolution.get('cause', 'unknown')}\n"
        )
        if not entity_exists("evolutions", name):
            create_entity("evolutions", name, body=body)
            created.append(f"evolution:{topic[:40]}")
        else:
            try:
                append_to_entity("evolutions", name, "Updates", f"- Re-referenced (source: {source_label}, {now})")
                updated.append(f"evolution:{topic[:40]}")
            except FileNotFoundError:
                create_entity("evolutions", name, body=body)
                created.append(f"evolution:{topic[:40]}")

    # High-value outputs → insights
    for output in extraction.get("high_value_outputs", []):
        title = output.get("title", "")
        if not title:
            continue
        content = output.get("content", "")
        related = ", ".join(f"[[{e}]]" for e in output.get("related_entities", []))
        name = f"{now} {title}"
        body = f"{content}\n\n## Related\n{related}\n" if related else content
        fm = {"confidence": "high", "insight_source": source_label}
        create_entity("insights", name, frontmatter=fm, body=body)
        created.append(f"insight:{title[:40]}")

    # Rebuild index
    rebuild_index()

    # Log
    summary_parts = []
    if created:
        summary_parts.append(f"created {len(created)}")
    if updated:
        summary_parts.append(f"updated {len(updated)}")
    summary = ", ".join(summary_parts) or "no changes"
    append_log("extract", f"{source_label} → {summary}")

    # Git commit
    entity_names = [e.split(":", 1)[1] for e in created + updated]
    commit_msg = f"brain: extract from {source_label} — {summary}"
    if entity_names:
        commit_msg += f"\n\nEntities: {', '.join(entity_names[:10])}"
    commit(commit_msg)

    return {"created": created, "updated": updated}
