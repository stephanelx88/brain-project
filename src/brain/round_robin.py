"""Deterministic subject pickers for autoresearch round-robin questions.

Background — why this module exists
-----------------------------------

`autoresearch.py` cycles a 6-pattern question wheel when the queue is
empty. Originally each slot was a generic English sentence ("pick a
person mentioned in ≥3 projects…"). The LLM had to do its own retrieval
to decide *which* person, and once the obvious candidates had been
written about, every subsequent same-slot cycle would say "saturated"
and burn ~100 s of LLM time producing nothing.

This module replaces those generic prompts with concrete, picker-driven
ones. Each `pick_*()` runs a tiny SQL query over the SQLite mirror to
find ONE eligible subject (a stale entity, an open decision, etc.) and
embeds its name into the question. The LLM then gets a question it can
*act on* without re-retrieving — which both saves a turn and reduces
saturation false-positives.

If a picker can't find an eligible subject (small vault, fresh data,
nothing matches the criteria), it returns `None` and the caller falls
back to the legacy generic question for that slot. The wheel never
starves; it just degrades gracefully.

The picker SQL is intentionally cheap (single-pass scans, ≤50-row
limits) — autoresearch runs every 30 minutes and the picker is on the
hot path before the LLM call. A picker that took 1+ seconds would be a
regression.

Picker order matches `program.md`'s round-robin pattern list (sections
1–6) so cycle N's slot stays predictable across the wheel.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import brain.db as db


def _conn() -> sqlite3.Connection:
    """Read-only handle. Pickers must never mutate the mirror — autoresearch
    relies on this DB being a stable snapshot of the vault for the cycle."""
    return sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# pickers — one per program.md round-robin slot
# ---------------------------------------------------------------------------


def pick_stale_entity() -> str | None:
    """Slot 1 — an entity not updated in 60+ days that newer entities still
    mention by name. The autoresearch cycle then re-reads it and decides
    whether the older facts still hold given what the newer entities say.

    The schema has no formal `references` edge, so we approximate
    "referenced by" via fact-text containment (`facts.text LIKE
    '%<name>%'`). This is noisy on short common names but we cap the outer
    scan to 50 candidates and trust autoresearch's downstream judgment.
    """
    cutoff = _days_ago(60)
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT name, type, last_updated FROM entities "
                "WHERE last_updated < ? AND COALESCE(status,'') != 'archived' "
                "ORDER BY last_updated ASC LIMIT 50",
                (cutoff,),
            ).fetchall()
            for name, type_, last_upd in rows:
                if not name or len(name) < 4:
                    # Common short names ("Son", "MK") match too many fact
                    # rows to be useful — skip.
                    continue
                n_refs = c.execute(
                    "SELECT COUNT(DISTINCT f.entity_id) FROM facts f "
                    "JOIN entities e ON f.entity_id = e.id "
                    "WHERE f.text LIKE ? AND e.last_updated > ? "
                    "AND e.name != ?",
                    (f"%{name}%", cutoff, name),
                ).fetchone()[0]
                if n_refs >= 2:
                    return (
                        f"stale-entity-sweep: '{name}' ({type_}) was last "
                        f"updated {last_upd} but is still referenced by "
                        f"{n_refs} newer entities. Re-read it — are its "
                        "facts still consistent with what the newer "
                        "entities say? File a contradiction or refresh."
                    )
    except sqlite3.Error:
        return None
    return None


def pick_cross_project_person() -> str | None:
    """Slot 2 — a person whose name shows up in facts of ≥3 distinct
    project entities. Autoresearch then writes a narrative article about
    their role across those projects.

    Skips people the wheel already covered recently (last 14 days of
    `entities/articles/`) to avoid the saturation loop that prompted this
    rewrite — without this guard the picker would happily resurface the
    same well-known person every wheel rotation.
    """
    cutoff = _days_ago(14)
    try:
        with _conn() as c:
            recent_titles = {
                row[0].lower() for row in c.execute(
                    "SELECT name FROM entities "
                    "WHERE type IN ('articles','insights') "
                    "AND last_updated >= ?",
                    (cutoff,),
                ).fetchall() if row[0]
            }
            people = c.execute(
                "SELECT name FROM entities WHERE type = 'people' "
                "AND COALESCE(status,'') != 'archived' "
                "ORDER BY last_updated DESC LIMIT 30",
            ).fetchall()
            for (name,) in people:
                if not name or len(name) < 3:
                    continue
                if any(name.lower() in t for t in recent_titles):
                    continue
                n_projects = c.execute(
                    "SELECT COUNT(DISTINCT e.id) FROM entities e "
                    "JOIN facts f ON f.entity_id = e.id "
                    "WHERE e.type = 'projects' AND f.text LIKE ?",
                    (f"%{name}%",),
                ).fetchone()[0]
                if n_projects >= 3:
                    return (
                        f"cross-project-synthesis: '{name}' appears in "
                        f"facts across {n_projects} project entities. "
                        "Write a narrative article describing their role "
                        "across all of them — what they own, recurring "
                        "patterns, where their decisions repeat."
                    )
    except sqlite3.Error:
        return None
    return None


#  Statuses that mean "this decision is settled". Anything outside this
#  set (including the very common `current`, the default for autoresearch-
#  written entities) is fair game for an audit. Building a NOT-IN list
#  rather than an IN list of "open" synonyms is deliberate — the vault
#  has 12+ free-text statuses on decisions alone (`approved for commit`,
#  `code_complete_pending_commits`, etc.) and an allow-list would miss
#  newly-coined ones.
_CLOSED_DECISION_STATUSES = {
    "accepted", "approved", "approved for commit", "committed",
    "code_complete_pending_commits", "smoke_tested_operational",
    "verified_executed", "shipped", "superseded", "archived",
    "rejected", "abandoned",
}

_CLOSED_ISSUE_STATUSES = {
    "fixed", "resolved", "closed", "archived", "wontfix", "duplicate",
}


def pick_open_decision() -> str | None:
    """Slot 3 — oldest unsettled decision worth auditing.

    "Unsettled" = status is anything other than an accepted/committed/
    shipped synonym. The default `current` status counts as unsettled —
    that's what most autoresearch-promoted decisions sit at, and they're
    exactly the ones worth auditing for "was this actually done?".

    On a *mature* vault we want decisions 30+ days old (program.md's
    threshold). On a *young* vault that filter strips everything; we'd
    rather audit the oldest available decision than fall back to the
    generic prompt. So we try the strict 30-day cutoff first, then
    relax to "older than 3 days" before giving up.
    """
    return _pick_oldest_unsettled(
        type_="decisions",
        closed_statuses=_CLOSED_DECISION_STATUSES,
        cutoff_days=30,
        format_question=lambda name, last_upd, status: (
            f"decision-audit: decision '{name}' has been "
            f"status='{status or 'current'}' since {last_upd}. Search "
            "recent sessions for evidence it was actually made, reversed, "
            "or remains open. File a contradiction or update."
        ),
    )


def pick_correction_cluster() -> str | None:
    """Slot 4 — only worth firing if ≥3 corrections landed in the last
    30 days. Below that the cluster is too thin to find a meta-pattern,
    so we let the slot fall back to the generic prompt (which the LLM
    will likely respond to with `items: []`)."""
    cutoff = _days_ago(30)
    try:
        with _conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM entities WHERE type = 'corrections' "
                "AND last_updated >= ?",
                (cutoff,),
            ).fetchone()[0]
    except sqlite3.Error:
        return None
    if n < 3:
        return None
    return (
        f"correction-synthesis: {n} corrections logged in the last 30 days. "
        "Cluster them by theme. File a meta-correction summarizing the "
        "pattern — what kind of mistake keeps recurring?"
    )


def pick_open_issue() -> str | None:
    """Slot 5 — oldest unresolved issue worth following up on.

    Same NOT-IN rationale + young-vault relaxation as `pick_open_decision`.
    Mature vault: prefer issues 14+ days old. Young vault: anything
    older than 3 days beats falling back to the generic prompt."""
    return _pick_oldest_unsettled(
        type_="issues",
        closed_statuses=_CLOSED_ISSUE_STATUSES,
        cutoff_days=14,
        format_question=lambda name, last_upd, status: (
            f"issue-follow-through: issue '{name}' has been "
            f"status='{status or 'current'}' since {last_upd}. Search for "
            "evidence of resolution or escalation in recent sessions. "
            "Update or escalate."
        ),
    )


def _pick_oldest_unsettled(
    *,
    type_: str,
    closed_statuses: set[str],
    cutoff_days: int,
    format_question,  # callable(name, last_updated, status) -> str
) -> str | None:
    """Shared logic for slot 3 + slot 5: pick the oldest entity of `type_`
    whose status is NOT in `closed_statuses`. Two-pass relaxation:

      1. strict — prefer items older than `cutoff_days` (program.md's
         spec — what a mature vault would surface)
      2. relaxed — fall back to anything older than 3 days, so a young
         vault still gets a concrete subject instead of a generic prompt

    The 3-day floor exists so we don't audit decisions made yesterday
    ("what's the status of this thing I wrote 2h ago?" wastes a cycle)."""
    placeholders = ",".join("?" * len(closed_statuses))
    base_sql = (
        f"SELECT name, last_updated, status FROM entities "
        f"WHERE type = ? "
        f"AND COALESCE(LOWER(status),'') NOT IN ({placeholders}) "
        f"AND last_updated < ? "
        f"ORDER BY last_updated ASC LIMIT 1"
    )
    try:
        with _conn() as c:
            for cutoff in (_days_ago(cutoff_days), _days_ago(3)):
                row = c.execute(
                    base_sql,
                    (type_, *sorted(closed_statuses), cutoff),
                ).fetchone()
                if row:
                    name, last_upd, status = row
                    return format_question(name, last_upd, status)
    except sqlite3.Error:
        return None
    return None


def pick_under_covered_domain() -> str | None:
    """Slot 6 — a domain entity with <3 facts in the index. Thin coverage
    is the signal: there's something the brain knows it should care about
    but hasn't extracted detail on. Skips brand-new entities (created in
    the last 7 days) — they haven't had a chance to accrue facts yet."""
    fresh_cutoff = _days_ago(7)
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT e.name, "
                "  (SELECT COUNT(*) FROM facts f WHERE f.entity_id = e.id) "
                "  AS nf "
                "FROM entities e "
                "WHERE e.type = 'domains' "
                "AND COALESCE(e.status,'') != 'archived' "
                "AND COALESCE(e.first_seen, '') < ? "
                "ORDER BY nf ASC LIMIT 20",
                (fresh_cutoff,),
            ).fetchall()
    except sqlite3.Error:
        return None
    for name, nf in rows:
        if not name:
            continue
        if nf < 3:
            return (
                f"domain-coverage-gap: domain '{name}' has only {nf} fact(s) "
                "indexed. Find recent sessions touching this domain that "
                "didn't extract. Propose new entities or fact bullets to "
                "fill in the coverage."
            )
    return None


# ---------------------------------------------------------------------------
# wheel
# ---------------------------------------------------------------------------

# Slot order MUST match FALLBACK_QUESTIONS — they're paired by index.
PICKERS = [
    pick_stale_entity,
    pick_cross_project_person,
    pick_open_decision,
    pick_correction_cluster,
    pick_open_issue,
    pick_under_covered_domain,
]

# Verbatim from the original `autoresearch.ROUND_ROBIN_QUESTIONS` — used
# only when the slot's picker returns None so a saturated pattern
# doesn't starve the cycle of any question at all.
FALLBACK_QUESTIONS = [
    "stale-entity-sweep: find entities with last_updated > 60 days that are still referenced by ≥2 newer entities. Re-read both — are the older entity's facts still consistent?",
    "cross-project-synthesis: pick a person mentioned in ≥3 projects. Write a narrative article describing their role across all of them.",
    "decision-audit: pick a decision with status=open from 30+ days ago. Search recent sessions for evidence it was actually made, reversed, or remains open. File a contradiction or update.",
    "correction-synthesis: read all corrections from the last 30 days. Cluster by theme. File a meta-correction summarizing the pattern.",
    "issue-follow-through: pick an issue with status=open 14+ days. Search for evidence of resolution or escalation. Update or escalate.",
    "domain-coverage-gap: pick a domain with <3 linked entities. Find recent sessions touching the domain that didn't extract. Propose new entities.",
]

assert len(PICKERS) == len(FALLBACK_QUESTIONS)


def next_question(cycle_n: int) -> str:
    """Return the question autoresearch should run for `cycle_n`.

    Tries the slot's picker first; falls back to the matching generic
    question if the picker yields None. This keeps the wheel turning
    even on a tiny vault while letting a populated vault enjoy concrete,
    pre-targeted prompts.
    """
    slot = cycle_n % len(PICKERS)
    return PICKERS[slot]() or FALLBACK_QUESTIONS[slot]
