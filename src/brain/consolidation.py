"""WS8 idle consolidation worker — episodic → semantic + alias canonicalisation.

Part A (shipped 2026-04-23) = pure-SQL promotion pipeline:

    fact_claims (kind='episodic')
        ↓ scrub_tag gate              (C.1)
        ↓ trust gate                   (C.2)
        ↓ group by (subject_slug, predicate_key, object_key)
        ↓ N≥2 independent episodes     (A.2.1)
        ↓ age floor 48 h               (A.2.5)
        ↓ contested-sibling gate       (C.3)
        ↓ aggregate salience ≥ 0.6     (A.2.4)
        ↓
    INSERT semantic row + mark contributors superseded

Part B (this PR, 2026-04-24) = LLM-judged alias canonicalisation:

    fact_claims (object_slug IS NULL, object_text repeated)
        ↓ Levenshtein≤2 against entities.name
        ↓ cheap-path filter (skip owner, skip correction-sourced,
                             skip existing `disambiguations.jsonl`)
        ↓ per-pair 1500 tok cap via remaining_budget()
        ↓ LLM pair-judge (strict JSON: decision∈{merge,keep_distinct,needs_user})
        ↓ merge → INSERT alias row + rewrite fact_claims.object_slug
        ↓ keep_distinct / needs_user → append disambiguations.jsonl

Every LLM call charges the daily budget (``BRAIN_CONSOLIDATE_DAILY_BUDGET_TOK``,
default 25000). The worker short-circuits when the remaining budget
can't cover one more pair at the cap.

Public API

    promote_episodic_ready(apply=False, max_promotions=None) -> dict
    consolidate_aliases(apply=False, max_pairs=None, budget_tokens=None,
                        judge_fn=None) -> dict
    remaining_budget() -> int
    charge_budget(tokens: int, reason: str) -> None

CLI entry: ``brain consolidate [--apply] [--aliases]``.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import brain.config as config
from brain import db


# ---------------------------------------------------------------------------
# Constants + env knobs
# ---------------------------------------------------------------------------


DAILY_BUDGET_DEFAULT = 25000             # PM 13:30 decision
SALIENCE_MIN_DEFAULT = 0.6               # spec A.2.4
AGE_FLOOR_HOURS_DEFAULT = 48.0           # spec A.2.5
TAU_DAYS_DEFAULT = 14.0                  # spec A.3

# Forbidden scrub_tag values — backfilled pre-WS4 rows must never
# promote. See spec C.1. `NULL` is handled via the SQL clause.
_SCRUB_TAG_BLOCKLIST: frozenset[str] = frozenset({
    "pre-ws4", "pre-ws4-backfill",
})


def _daily_budget() -> int:
    try:
        return max(0, int(os.environ.get(
            "BRAIN_CONSOLIDATE_DAILY_BUDGET_TOK", str(DAILY_BUDGET_DEFAULT),
        )))
    except (TypeError, ValueError):
        return DAILY_BUDGET_DEFAULT


def _salience_min() -> float:
    try:
        return float(os.environ.get(
            "BRAIN_CONSOLIDATE_SALIENCE_MIN", str(SALIENCE_MIN_DEFAULT),
        ))
    except (TypeError, ValueError):
        return SALIENCE_MIN_DEFAULT


def _age_floor_hours() -> float:
    try:
        return float(os.environ.get(
            "BRAIN_CONSOLIDATE_AGE_FLOOR_H", str(AGE_FLOOR_HOURS_DEFAULT),
        ))
    except (TypeError, ValueError):
        return AGE_FLOOR_HOURS_DEFAULT


def _tau_days() -> float:
    try:
        return float(os.environ.get(
            "BRAIN_SALIENCE_TAU_DAYS", str(TAU_DAYS_DEFAULT),
        ))
    except (TypeError, ValueError):
        return TAU_DAYS_DEFAULT


# ---------------------------------------------------------------------------
# Daily budget counter
# ---------------------------------------------------------------------------


def _budget_log_path() -> Path:
    return config.BRAIN_DIR / ".audit" / "consolidation-budget.jsonl"


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _spent_today() -> int:
    """Sum tokens charged to today's UTC window. Cheap: the file is
    append-only and rotated naturally (old days stay as prior
    entries, just don't count). A dedicated rotation job is an ops
    follow-up — not in the minimal PR."""
    p = _budget_log_path()
    if not p.exists():
        return 0
    today = _today_utc()
    total = 0
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("day") != today:
                continue
            try:
                total += int(row.get("tokens") or 0)
            except (TypeError, ValueError):
                continue
    except OSError:
        return 0
    return total


def remaining_budget() -> int:
    """Tokens left in today's consolidation budget. 0 means any
    further LLM call must be skipped."""
    return max(0, _daily_budget() - _spent_today())


def charge_budget(tokens: int, reason: str) -> None:
    """Append one counter-only row to the budget log. Silent-fail on
    OSError — a failed append does NOT unblock the caller, but it
    also does not crash the worker; the next tick re-checks the
    spend and falls back to a conservative estimate (today's spend
    appears 0, next call may accidentally overshoot by one tick —
    acceptable vs crashing the worker)."""
    if tokens <= 0:
        return
    p = _budget_log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "day": _today_utc(),
                "tokens": int(tokens),
                "reason": reason,
            }) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Trust weights + salience helpers
# ---------------------------------------------------------------------------


def _trust_weight(trust_source: str | None, risk_level: str | None) -> float:
    """Spec A.4 trust weight table. Schema column is ``risk_level``
    (orthogonal to trust_source); anything other than ``trusted``
    collapses to the low/quarantined rows regardless of source."""
    rl = (risk_level or "trusted").lower()
    if rl == "quarantined":
        return 0.0
    if rl == "low":
        return 0.3
    ts = (trust_source or "extracted").lower()
    if ts in ("user", "correction"):
        return 1.0
    if ts == "note":
        return 0.85
    return 0.70  # extracted / anything else trusted


def _decayed_salience(s0: float, observed_at: float, now: float) -> float:
    """Exponential decay per spec A.3. Floor at 0 to guard against
    numerical underflow on very old rows."""
    if observed_at is None or s0 is None:
        return 0.0
    age_days = max(0.0, (now - observed_at) / 86400.0)
    tau = _tau_days()
    if tau <= 0:
        return float(s0)
    return max(0.0, float(s0) * math.exp(-age_days / tau))


def _aggregate_salience(decayed_weighted: list[float]) -> float:
    """Spec A.4: 1 - ∏(1 - sᵢ·wᵢ).  Rows where sᵢ·wᵢ ≥ 1.0 are
    clamped so we don't blow up on (temporary) floating-point
    overshoot."""
    if not decayed_weighted:
        return 0.0
    product = 1.0
    for x in decayed_weighted:
        if x is None:
            continue
        x_clamped = min(max(float(x), 0.0), 1.0)
        product *= (1.0 - x_clamped)
    return 1.0 - product


# ---------------------------------------------------------------------------
# Candidate key — groups episodic rows that would be "the same fact"
# ---------------------------------------------------------------------------


def _object_key(object_slug: str | None, object_text: str | None) -> str:
    """Stable group key for object equivalence.

    ``object_slug`` wins when resolved (two rows sharing the same
    entity-referent group regardless of surface phrasing). Otherwise
    we fall back to canonical_fact_hash on the literal object_text,
    the same normalisation tombstones and claim_key already use — so
    "Paris " and " paris" collapse.
    """
    if object_slug:
        return f"slug:{object_slug}"
    if object_text:
        return f"text:{db.canonical_fact_hash(object_text)}"
    return "none"


# ---------------------------------------------------------------------------
# Gate predicates (factored for testability)
# ---------------------------------------------------------------------------


def _passes_trust_gate(row: dict) -> bool:
    """Spec C.2. Any 'low' or 'quarantined' risk_level blocks; any
    unexpected trust_source blocks."""
    rl = (row.get("risk_level") or "").lower()
    if rl != "trusted":
        return False
    ts = (row.get("trust_source") or "").lower()
    return ts in ("user", "note", "extracted", "correction")


def _passes_scrub_gate(row: dict) -> bool:
    """Spec C.1. Backfilled pre-WS4 rows (scrub_tag=NULL or
    pre-ws4*) never promote. Any other tag is accepted — the
    stricter `LIKE 'post-ws4-%'` refinement from the spec can land
    in a later PR once all live-dual-write rows carry the versioned
    tag."""
    tag = row.get("scrub_tag")
    if tag is None:
        return False
    return tag not in _SCRUB_TAG_BLOCKLIST


# ---------------------------------------------------------------------------
# Main worker
# ---------------------------------------------------------------------------


def promote_episodic_ready(*,
                            apply: bool = False,
                            max_promotions: int | None = None
                            ) -> dict:
    """Scan fact_claims for episodic triples that clear every gate and
    promote them to semantic.

    Returns a summary dict:
        {
            "checked_groups": int,
            "eligible": int,
            "promoted": int,
            "blocked_contested": int,
            "blocked_salience": int,
            "blocked_scrub": int,
            "blocked_trust": int,
            "blocked_age": int,
            "blocked_disagreement": int,
            "budget_remaining": int,
            "applied": bool,
            "promoted_ids": [int, ...],      # new semantic row ids
        }
    """
    summary: dict = {
        "checked_groups": 0,
        "eligible": 0,
        "promoted": 0,
        "blocked_contested": 0,
        "blocked_salience": 0,
        "blocked_scrub": 0,
        "blocked_trust": 0,
        "blocked_age": 0,
        "blocked_disagreement": 0,
        "budget_remaining": remaining_budget(),
        "applied": apply,
        "promoted_ids": [],
    }

    if summary["budget_remaining"] <= 0:
        summary["status"] = "budget_exhausted"
        return summary

    salience_floor = _salience_min()
    age_floor_s = _age_floor_hours() * 3600.0
    now = time.time()

    with db.connect() as conn:
        # Fetch every episodic-current row up-front. Small corpus
        # (hundreds to low thousands in the foreseeable future); the
        # Python side groups + checks are cheaper + more testable
        # than a multi-CTE SQL plan.
        rows = conn.execute(
            """
            SELECT id, entity_id, subject_slug, predicate, predicate_key,
                   predicate_group, object_entity, object_text, object_slug,
                   object_type, text, fact_time, observed_at,
                   source_kind, source_path, source_sha, scrub_tag,
                   episode_id, confidence, risk_level, trust_source,
                   salience, kind, status, claim_key
            FROM fact_claims
            WHERE kind = 'episodic'
              AND status = 'current'
            """
        ).fetchall()
        col_names = [d[0] for d in conn.execute(
            "SELECT id, entity_id, subject_slug, predicate, predicate_key, "
            "predicate_group, object_entity, object_text, object_slug, "
            "object_type, text, fact_time, observed_at, source_kind, "
            "source_path, source_sha, scrub_tag, episode_id, confidence, "
            "risk_level, trust_source, salience, kind, status, claim_key "
            "FROM fact_claims WHERE 0"
        ).description]
        dict_rows = [dict(zip(col_names, r)) for r in rows]

        # --- scrub + trust gates (apply at row level) ---------------
        survivors: list[dict] = []
        for r in dict_rows:
            if not _passes_scrub_gate(r):
                summary["blocked_scrub"] += 1
                continue
            if not _passes_trust_gate(r):
                summary["blocked_trust"] += 1
                continue
            survivors.append(r)

        # --- group by (subject_slug, predicate_key, object_key) -----
        groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for r in survivors:
            key = (
                r["subject_slug"] or "",
                r["predicate_key"] or "",
                _object_key(r["object_slug"], r["object_text"]),
            )
            groups[key].append(r)

        promoted_here = 0
        for (subject_slug, pred_key, object_key), members in groups.items():
            summary["checked_groups"] += 1

            # A.2.1 — distinct episode_id AND distinct source.path
            distinct_eps = {m.get("episode_id") for m in members
                            if m.get("episode_id") is not None}
            distinct_paths = {m.get("source_path") for m in members
                              if m.get("source_path") is not None}
            # Two episodes mean two distinct episode_id AND two
            # distinct source_path. Rows without either identifier
            # can't count — they collapse to a single implicit
            # source and fail the independence check.
            if len(distinct_eps) < 2 or len(distinct_paths) < 2:
                summary["blocked_disagreement"] += 1
                continue

            # A.2.5 — newest contributor ≥ 48h old
            newest = max(float(m.get("observed_at") or 0) for m in members)
            if (now - newest) < age_floor_s:
                summary["blocked_age"] += 1
                continue

            # C.3 — contested sibling check
            if _has_contested_sibling(conn, subject_slug, pred_key, object_key):
                summary["blocked_contested"] += 1
                continue

            # A.2.4 — aggregate salience
            decayed_weighted = [
                _decayed_salience(m.get("salience"), m.get("observed_at"), now)
                * _trust_weight(m.get("trust_source"), m.get("risk_level"))
                for m in members
            ]
            agg = _aggregate_salience(decayed_weighted)
            if agg < salience_floor:
                summary["blocked_salience"] += 1
                continue

            summary["eligible"] += 1

            if apply:
                new_id = _promote_group(conn, members, agg, now)
                if new_id is not None:
                    summary["promoted"] += 1
                    summary["promoted_ids"].append(new_id)
                    _audit_promotion(
                        subject_slug=subject_slug,
                        predicate=members[0].get("predicate"),
                        predicate_key=pred_key,
                        n_contributors=len(members),
                        aggregate_salience=agg,
                        promoted_id=new_id,
                    )
                    promoted_here += 1
                    if max_promotions is not None and promoted_here >= max_promotions:
                        break

    summary["budget_remaining"] = remaining_budget()
    return summary


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------


def _has_contested_sibling(conn,
                           subject_slug: str,
                           predicate_key: str,
                           object_key: str) -> bool:
    """Spec C.3. A live (non-retracted/superseded) claim with the
    same (subject_slug, predicate_key) but a DIFFERENT object_key
    is a contested sibling — block.
    """
    rows = conn.execute(
        """
        SELECT object_slug, object_text, status
        FROM fact_claims
        WHERE subject_slug = ?
          AND predicate_key = ?
          AND status NOT IN ('retracted', 'superseded')
        """,
        (subject_slug, predicate_key),
    ).fetchall()
    for obj_slug, obj_text, _status in rows:
        other_key = _object_key(obj_slug, obj_text)
        if other_key != object_key:
            return True
    return False


def _min_trust_source(members: list[dict]) -> str:
    """Semantic row inherits the weakest contributing trust_source
    so downstream consumers can filter. Spec A.5 step 1."""
    # Ordering: extracted < note < correction < user (user most
    # trusted). Semantic takes the MIN so the aggregate reflects
    # the weakest link.
    order = {"extracted": 0, "note": 1, "correction": 2, "user": 3}
    weakest = min(members,
                  key=lambda m: order.get(m.get("trust_source", "extracted"), 0))
    return weakest.get("trust_source") or "extracted"


def _promote_group(conn, members: list[dict], aggregate: float, now: float) -> int | None:
    """INSERT a semantic row + mark contributors superseded.

    All writes run inside the caller's `db.connect()` transaction —
    on exception the context manager rolls back, leaving the
    episodic rows untouched.
    """
    representative = max(
        members,
        key=lambda m: float(m.get("observed_at") or 0),
    )
    run_sha = f"ws8-{int(now)}"
    new_text = representative.get("text") or ""
    new_claim_key = db.canonical_fact_hash(
        f"{representative.get('subject_slug', '')}"
        f"|{representative.get('predicate_key', '')}"
        f"|{_object_key(representative.get('object_slug'), representative.get('object_text'))}"
    )

    conn.execute(
        """
        INSERT INTO fact_claims (
            entity_id, subject_slug,
            predicate, predicate_key, predicate_group,
            object_entity, object_text, object_slug, object_type,
            text,
            fact_time, observed_at,
            source_kind, source_path, source_sha, scrub_tag, episode_id,
            confidence, risk_level, trust_source, salience,
            kind, status, claim_key
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            representative.get("entity_id"),
            representative.get("subject_slug"),
            representative.get("predicate"),
            representative.get("predicate_key"),
            representative.get("predicate_group"),
            representative.get("object_entity"),
            representative.get("object_text"),
            representative.get("object_slug"),
            representative.get("object_type") or "string",
            new_text,
            representative.get("fact_time"),
            now,
            "consolidation",
            "worker/WS8",
            run_sha,
            "post-ws4-consolidation",
            None,                                   # episode_id NULL for semantic row
            max(float(m.get("confidence") or 0) for m in members),
            "trusted",                              # risk_level of the aggregate
            _min_trust_source(members),
            aggregate,
            "semantic",
            "current",
            new_claim_key,
        ),
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Mark contributors superseded (audit trail preserved in place).
    superseded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for m in members:
        conn.execute(
            "UPDATE fact_claims "
            "SET status='superseded', superseded_by=?, superseded_at=? "
            "WHERE id=?",
            (new_id, superseded_at, m["id"]),
        )
    return int(new_id)


def _audit_path() -> Path:
    return config.BRAIN_DIR / ".audit" / "consolidation.jsonl"


def _audit_promotion(*,
                     subject_slug: str,
                     predicate: str,
                     predicate_key: str,
                     n_contributors: int,
                     aggregate_salience: float,
                     promoted_id: int) -> None:
    """Append one counter-only JSONL line per promotion. Content:
    subject slug + predicate + n + aggregate. No fact text — the
    ledger entry (when WS5 wiring lands) carries the canonical
    pointer."""
    p = _audit_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "action": "promote",
                "subject_slug": subject_slug,
                "predicate": predicate,
                "predicate_key": predicate_key,
                "n_contributors": n_contributors,
                "aggregate_salience": round(float(aggregate_salience), 4),
                "promoted_id": promoted_id,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ===========================================================================
# Part B — alias canonicalisation (WS8 round-3, 2026-04-24)
# ===========================================================================
#
# Goal: when fact_claims.object_text points at an entity-like noun
# that we failed to resolve at ingest time (object_slug IS NULL), an
# idle worker scans the unresolved phrases, finds the closest
# existing entity via cheap Levenshtein, and asks the LLM whether
# they're the same referent. On a confident "merge" the worker
# inserts an alias row into the existing `aliases` table and
# rewrites every matching `fact_claims.object_slug` so future recall
# hits follow the graph edge.

TOKENS_PER_PAIR_DEFAULT = 1500           # spec B.3
ALIAS_LEVENSHTEIN_MAX = 2                # spec B.1 cheap-path
ALIAS_MIN_MENTIONS = 2                   # orphan must appear ≥2x
_ALIAS_AUDIT_ACTION = "alias_merge"
_ALIAS_SHORT_PHRASE_MIN = 2              # skip single-char noise


def _tokens_per_pair() -> int:
    try:
        return max(100, int(os.environ.get(
            "BRAIN_ALIAS_TOKENS_PER_PAIR", str(TOKENS_PER_PAIR_DEFAULT),
        )))
    except (TypeError, ValueError):
        return TOKENS_PER_PAIR_DEFAULT


def _disambig_path() -> Path:
    """User-editable override + audit trail for the LLM alias judge.

    Lives at the vault root (not under `.audit/`) because stephane
    edits it — "these two are NOT the same" user assertions belong
    alongside the vault's other user-facing files.
    """
    return config.BRAIN_DIR / "disambiguations.jsonl"


def _load_disambiguations() -> dict[tuple[str, str], str]:
    """Return {(text_norm, slug): decision} from the override file.

    ``decision`` ∈ {``not_same``, ``needs_user``, ``merge_done``}.
    The worker skips any pair whose key is already present — user
    assertions are final, LLM re-judging would either waste tokens
    or (worse) silently reverse a human decision.
    """
    out: dict[tuple[str, str], str] = {}
    p = _disambig_path()
    if not p.exists():
        return out
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = (row.get("text") or "").strip().lower()
            slug = (row.get("slug") or "").strip()
            decision = row.get("decision") or ""
            if text and slug and decision:
                out[(text, slug)] = decision
    except OSError:
        pass
    return out


def _append_disambiguation(*, text: str, slug: str, decision: str,
                            reasoning: str | None = None) -> None:
    """Atomic-append a line to disambiguations.jsonl. Silent-fail on
    OSError — the LLM verdict is also in consolidation.jsonl, so a
    failed write here doesn't lose the decision."""
    p = _disambig_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "text": text,
                "slug": slug,
                "decision": decision,
                "reasoning": reasoning,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _levenshtein(a: str, b: str, *, cutoff: int = ALIAS_LEVENSHTEIN_MAX) -> int:
    """Edit distance with early exit. Returns ``cutoff + 1`` when the
    true distance exceeds ``cutoff`` — we never need the precise
    large values."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > cutoff:
        return cutoff + 1
    if la == 0 or lb == 0:
        return max(la, lb)
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    prev = list(range(la + 1))
    for j, cb in enumerate(b, start=1):
        curr = [j] + [0] * la
        min_in_row = j
        for i, ca in enumerate(a, start=1):
            cost = 0 if ca == cb else 1
            curr[i] = min(
                curr[i - 1] + 1,       # insertion
                prev[i] + 1,           # deletion
                prev[i - 1] + cost,    # substitution
            )
            if curr[i] < min_in_row:
                min_in_row = curr[i]
        if min_in_row > cutoff:
            return cutoff + 1
        prev = curr
    return prev[la]


def _norm_phrase(s: str) -> str:
    """Unicode NFKC + casefold + whitespace collapse. Used for
    comparing object_text vs entity.name."""
    import unicodedata
    n = unicodedata.normalize("NFKC", s or "").casefold()
    return " ".join(n.split())


def _find_alias_candidates(conn) -> list[dict]:
    """Return the list of candidate pairs to ship to the judge.

    Each candidate:
      { object_text, norm_text, mentions, candidate_entity_id,
        candidate_slug, candidate_type, candidate_name, distance,
        correction_sourced: bool }
    """
    # Unresolved object_text groups — phrases that appear as the
    # object of ≥ALIAS_MIN_MENTIONS claims with no object_slug.
    rows = conn.execute(
        """
        SELECT object_text, COUNT(*) AS n,
               SUM(CASE WHEN trust_source='correction' THEN 1 ELSE 0 END) AS correction_n
        FROM fact_claims
        WHERE object_slug IS NULL
          AND object_text IS NOT NULL
          AND TRIM(object_text) != ''
          AND status = 'current'
        GROUP BY object_text
        HAVING COUNT(*) >= ?
        """,
        (ALIAS_MIN_MENTIONS,),
    ).fetchall()

    # Entity name table, excluding any slug already flagged as owner
    # or any entity whose name is shorter than the cutoff.
    entity_rows = conn.execute(
        "SELECT id, slug, type, name FROM entities "
        "WHERE name IS NOT NULL AND length(name) >= ?",
        (_ALIAS_SHORT_PHRASE_MIN,),
    ).fetchall()
    entities_by_norm: list[tuple[str, int, str, str, str]] = [
        (_norm_phrase(name), eid, slug, etype, name)
        for (eid, slug, etype, name) in entity_rows
    ]

    # Owner entity must never silently gain aliases — look up the
    # canonical slug via subject_reject (same source-of-truth the
    # WS7a filter uses, so behaviour stays consistent).
    try:
        from brain import subject_reject
        owner = subject_reject._owner_slug()
    except Exception:
        owner = None
    out: list[dict] = []

    for (object_text, mentions, correction_n) in rows:
        norm = _norm_phrase(object_text)
        if len(norm) < _ALIAS_SHORT_PHRASE_MIN:
            continue
        # Find the closest entity by edit distance.
        best: tuple[int, int, str, str, str] | None = None  # (dist, eid, slug, type, name)
        for (ent_norm, eid, slug, etype, name) in entities_by_norm:
            if ent_norm == norm:
                dist = 0
            else:
                dist = _levenshtein(norm, ent_norm)
            if dist > ALIAS_LEVENSHTEIN_MAX:
                continue
            if best is None or dist < best[0]:
                best = (dist, eid, slug, etype, name)
                if dist == 0:
                    break
        if best is None:
            continue
        if owner and best[2] == owner:
            # Never silently grant aliases to the owner entity.
            _append_disambiguation(
                text=object_text, slug=best[2],
                decision="needs_user",
                reasoning="owner-entity never auto-merged",
            )
            continue
        out.append({
            "object_text": object_text,
            "norm_text": norm,
            "mentions": int(mentions),
            "candidate_entity_id": best[1],
            "candidate_slug": best[2],
            "candidate_type": best[3],
            "candidate_name": best[4],
            "distance": best[0],
            "correction_sourced": bool(correction_n),
        })
    # Deterministic order: more mentions first, then shorter distance.
    out.sort(key=lambda r: (-r["mentions"], r["distance"], r["object_text"]))
    return out


# ---------------------------------------------------------------------------
# LLM pair-judge
# ---------------------------------------------------------------------------


_ALIAS_PROMPT = """You are reviewing two entity candidates for possible merger.
Do NOT merge if there is ANY reasonable doubt.

Unresolved object phrase: {text!r}
  appears in {mentions} fact_claims rows with no entity slug resolved.

Candidate existing entity: {slug}
  type: {etype}
  name: {name}
  aliases: {aliases}
  sample facts:
{facts_block}

Output JSON exactly, no prose:
{{
  "decision": "merge" | "keep_distinct" | "needs_user",
  "winner_slug": {slug_literal},
  "merged_aliases": ["<alias>", ...],
  "confidence": 0.0..1.0,
  "reasoning": "<one sentence>"
}}

Rules:
- decision="merge" requires confidence >= 0.90.
- If the phrase looks like a common English noun, a generic role
  title, or a mismatched concept, prefer "keep_distinct".
- If the phrase plausibly refers to the candidate but you cannot
  be sure, choose "needs_user" — never guess.
"""


def _build_alias_prompt(cand: dict, *, aliases: list[str], facts: list[str]) -> str:
    slug = cand["candidate_slug"]
    facts_block = "\n".join(f"    - {f[:120]}" for f in facts[:5]) or "    (none)"
    aliases_str = ", ".join(aliases) if aliases else "(none)"
    return _ALIAS_PROMPT.format(
        text=cand["object_text"],
        mentions=cand["mentions"],
        slug=slug,
        etype=cand["candidate_type"],
        name=cand["candidate_name"],
        aliases=aliases_str,
        facts_block=facts_block,
        slug_literal=json.dumps(slug),
    )


def _parse_alias_verdict(raw: str | None) -> dict | None:
    """Strict JSON expected. Tolerates code fences + surrounding prose."""
    if raw is None:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.split("\n") if not line.startswith("```")
        )
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
    if obj.get("decision") not in {"merge", "keep_distinct", "needs_user"}:
        return None
    return obj


def _default_judge(prompt: str, timeout: int = 60) -> str | None:
    """Default LLM caller. Lazy-imported so tests that monkeypatch
    the judge don't pay the sdk import."""
    from brain.auto_extract import call_claude
    return call_claude(prompt, timeout=timeout)


def _apply_alias_merge(conn,
                       *,
                       object_text: str,
                       entity_id: int,
                       entity_slug: str) -> int:
    """Insert alias row + rewrite object_slug/object_entity on every
    fact_claims row whose object_text matches. Returns the number of
    fact_claims rows rewritten.
    """
    conn.execute(
        "INSERT OR IGNORE INTO aliases(entity_id, alias) VALUES (?, ?)",
        (entity_id, object_text.lower()),
    )
    # Rewrite object_slug on every matching fact_claims row — future
    # recall hits resolve through the aliases map.
    res = conn.execute(
        "UPDATE fact_claims "
        "SET object_slug=?, object_entity=?, object_type='entity' "
        "WHERE object_text=? AND object_slug IS NULL",
        (entity_slug, entity_id, object_text),
    )
    return res.rowcount or 0


def _audit_alias_decision(*,
                          object_text: str,
                          candidate_slug: str,
                          decision: str,
                          mentions: int,
                          confidence: float | None,
                          rewritten: int,
                          tokens_spent: int) -> None:
    """Counter-only JSONL row on the consolidation audit file."""
    p = _audit_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "action": _ALIAS_AUDIT_ACTION,
                "object_text": object_text,
                "candidate_slug": candidate_slug,
                "decision": decision,
                "mentions": mentions,
                "confidence": (round(float(confidence), 3)
                               if confidence is not None else None),
                "rewritten_rows": int(rewritten),
                "tokens_spent": int(tokens_spent),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def consolidate_aliases(*,
                         apply: bool = False,
                         max_pairs: int | None = None,
                         budget_tokens: int | None = None,
                         judge_fn=None,
                         tokens_per_pair: int | None = None,
                         ) -> dict:
    """Scan unresolved object_text groups, judge each against the
    closest existing entity, merge on confident verdict.

    Args:
      apply: actually write aliases + rewrite object_slug.
      max_pairs: upper bound on pairs judged this run.
      budget_tokens: override the remaining_budget() read — tests
        pass a fixed budget without touching the log file.
      judge_fn: injectable LLM caller for tests. Signature
        ``(prompt: str) -> str | None``. Defaults to
        ``brain.auto_extract.call_claude``.
      tokens_per_pair: per-pair estimated cost. Defaults to
        ``BRAIN_ALIAS_TOKENS_PER_PAIR`` (1500).

    Returns a summary:
      { checked, skipped_disambig, skipped_correction, skipped_owner,
        judged, merged, kept_distinct, needs_user, rewritten_rows,
        tokens_spent, budget_remaining, applied, status }
    """
    summary: dict = {
        "checked": 0,
        "skipped_disambig": 0,
        "skipped_correction": 0,
        "skipped_owner": 0,
        "skipped_budget": 0,
        "judged": 0,
        "merged": 0,
        "kept_distinct": 0,
        "needs_user": 0,
        "judge_failed": 0,
        "rewritten_rows": 0,
        "tokens_spent": 0,
        "applied": apply,
        "status": "ok",
    }

    remaining = budget_tokens if budget_tokens is not None else remaining_budget()
    per_pair = tokens_per_pair if tokens_per_pair is not None else _tokens_per_pair()
    summary["budget_remaining"] = int(remaining)

    if remaining < per_pair:
        summary["status"] = "budget_exhausted"
        return summary

    if judge_fn is None:
        judge_fn = _default_judge

    disambig = _load_disambiguations()

    with db.connect() as conn:
        candidates = _find_alias_candidates(conn)
        summary["checked"] = len(candidates)

        for cand in candidates:
            key = (cand["norm_text"], cand["candidate_slug"])
            if key in disambig:
                summary["skipped_disambig"] += 1
                continue
            # C.4 alias-merge safety: trust_source='correction'
            # contributors need user sign-off.
            if cand["correction_sourced"]:
                summary["skipped_correction"] += 1
                _append_disambiguation(
                    text=cand["object_text"], slug=cand["candidate_slug"],
                    decision="needs_user",
                    reasoning="correction-sourced fact; user confirmation required",
                )
                continue

            if remaining < per_pair:
                summary["skipped_budget"] += 1
                summary["status"] = "budget_exhausted"
                break

            aliases = [r[0] for r in conn.execute(
                "SELECT alias FROM aliases WHERE entity_id=?",
                (cand["candidate_entity_id"],),
            ).fetchall()]
            facts = [r[0] for r in conn.execute(
                "SELECT text FROM fact_claims "
                "WHERE entity_id=? AND status='current' "
                "ORDER BY observed_at DESC LIMIT 5",
                (cand["candidate_entity_id"],),
            ).fetchall()]
            prompt = _build_alias_prompt(cand, aliases=aliases, facts=facts)

            try:
                raw = judge_fn(prompt)
            except Exception:
                raw = None

            # Charge the budget even on parse/judge failure so a
            # broken LLM endpoint can't burn through retries.
            if apply:
                charge_budget(per_pair, reason="alias_judge")
            summary["tokens_spent"] += per_pair
            remaining -= per_pair

            verdict = _parse_alias_verdict(raw)
            if verdict is None:
                summary["judge_failed"] += 1
                continue

            summary["judged"] += 1
            decision = verdict["decision"]
            confidence = verdict.get("confidence")
            rewritten = 0

            if decision == "merge":
                # Final guard — require winner_slug match and
                # confidence ≥ 0.90 before writing.
                try:
                    conf_f = float(confidence) if confidence is not None else 0.0
                except (TypeError, ValueError):
                    conf_f = 0.0
                winner = verdict.get("winner_slug")
                if winner != cand["candidate_slug"] or conf_f < 0.90:
                    # Downgrade to needs_user — the LLM contradicted
                    # itself (merge decision without high confidence
                    # on the candidate slug).
                    summary["needs_user"] += 1
                    _append_disambiguation(
                        text=cand["object_text"], slug=cand["candidate_slug"],
                        decision="needs_user",
                        reasoning=str(verdict.get("reasoning") or "")[:200],
                    )
                    if apply:
                        _audit_alias_decision(
                            object_text=cand["object_text"],
                            candidate_slug=cand["candidate_slug"],
                            decision="needs_user",
                            mentions=cand["mentions"],
                            confidence=confidence,
                            rewritten=0,
                            tokens_spent=per_pair,
                        )
                    continue

                if apply:
                    rewritten = _apply_alias_merge(
                        conn,
                        object_text=cand["object_text"],
                        entity_id=cand["candidate_entity_id"],
                        entity_slug=cand["candidate_slug"],
                    )
                    _append_disambiguation(
                        text=cand["object_text"], slug=cand["candidate_slug"],
                        decision="merge_done",
                        reasoning=str(verdict.get("reasoning") or "")[:200],
                    )
                    _audit_alias_decision(
                        object_text=cand["object_text"],
                        candidate_slug=cand["candidate_slug"],
                        decision="merge",
                        mentions=cand["mentions"],
                        confidence=confidence,
                        rewritten=rewritten,
                        tokens_spent=per_pair,
                    )
                summary["merged"] += 1
                summary["rewritten_rows"] += rewritten
            elif decision == "keep_distinct":
                summary["kept_distinct"] += 1
                _append_disambiguation(
                    text=cand["object_text"], slug=cand["candidate_slug"],
                    decision="not_same",
                    reasoning=str(verdict.get("reasoning") or "")[:200],
                )
                if apply:
                    _audit_alias_decision(
                        object_text=cand["object_text"],
                        candidate_slug=cand["candidate_slug"],
                        decision="keep_distinct",
                        mentions=cand["mentions"],
                        confidence=confidence,
                        rewritten=0,
                        tokens_spent=per_pair,
                    )
            else:  # needs_user
                summary["needs_user"] += 1
                _append_disambiguation(
                    text=cand["object_text"], slug=cand["candidate_slug"],
                    decision="needs_user",
                    reasoning=str(verdict.get("reasoning") or "")[:200],
                )
                if apply:
                    _audit_alias_decision(
                        object_text=cand["object_text"],
                        candidate_slug=cand["candidate_slug"],
                        decision="needs_user",
                        mentions=cand["mentions"],
                        confidence=confidence,
                        rewritten=0,
                        tokens_spent=per_pair,
                    )

            if max_pairs is not None and summary["judged"] >= max_pairs:
                break

    summary["budget_remaining"] = remaining_budget()
    return summary
def _audit_rollback(*,
                    promoted_id: int,
                    restored: int,
                    subject_slug: str | None,
                    predicate_key: str | None,
                    reason: str) -> None:
    """Append one counter-only JSONL line per rollback action. Paired
    with a `_audit_promotion` row for the same `promoted_id` so an
    auditor can reconstruct the before/after without reading the DB.

    WS5 hash-chained ledger entry (`consolidation_rollback`) also
    fires so the rollback is tamper-evident."""
    p = _audit_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "action": "rollback",
                "promoted_id": promoted_id,
                "restored": int(restored),
                "subject_slug": subject_slug or "",
                "predicate_key": predicate_key or "",
                "reason": reason,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    # WS5 hash-chain mirror (best-effort).
    try:
        from brain import _audit_ledger
        _audit_ledger.append(
            "consolidation_rollback",
            {
                "promoted_id": int(promoted_id),
                "restored": int(restored),
                "subject_slug": subject_slug or "",
                "predicate_key": predicate_key or "",
                "reason": reason,
            },
            actor="brain.consolidation.rollback",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# List + rollback (public API)
# ---------------------------------------------------------------------------


def _iter_audit_rows():
    """Yield parsed rows from consolidation.jsonl, skipping junk."""
    p = _audit_path()
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _parse_ts(ts: str | None) -> float:
    """Parse an ISO-8601 audit timestamp to epoch seconds. Returns 0
    on anything unparseable (ancient rows sort oldest-first by
    default, which is what callers want)."""
    if not ts:
        return 0.0
    try:
        # The audit writer uses `isoformat(timespec='seconds')`, which
        # emits `2026-04-24T13:05:00+00:00`. `datetime.fromisoformat`
        # handles both naive and aware forms.
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


def list_actions(*,
                 since: str | None = None,
                 action_id: int | None = None,
                 action: str | None = None,
                 limit: int | None = None) -> list[dict]:
    """Return audit rows, newest-first, optionally filtered.

    Args:
        since: ISO-8601 timestamp OR ``YYYY-MM-DD`` — only rows with
               ``ts >= since`` are returned. A bare date is treated
               as midnight UTC.
        action_id: promoted_id (int). Matches promotion rows where
               ``promoted_id == action_id`` AND rollback rows with
               the same id — so the caller sees both sides of the
               lifecycle.
        action: filter by ``action`` column (``'promote'`` /
               ``'rollback'``). None = no filter.
        limit: cap after filtering. None = unbounded.

    Returns a list of dicts (parsed JSONL rows), sorted by ts DESC.
    """
    rows = list(_iter_audit_rows())

    if since is not None:
        since_epoch = _parse_ts(
            since if "T" in since else f"{since}T00:00:00+00:00"
        )
        rows = [r for r in rows if _parse_ts(r.get("ts")) >= since_epoch]

    if action_id is not None:
        target = int(action_id)
        rows = [r for r in rows if int(r.get("promoted_id") or 0) == target]

    if action:
        rows = [r for r in rows if r.get("action") == action]

    rows.sort(key=lambda r: _parse_ts(r.get("ts")), reverse=True)

    if limit is not None:
        rows = rows[: max(0, int(limit))]

    return rows


def rollback(promoted_id: int, *, reason: str = "manual") -> dict:
    """Undo one promotion. Idempotent — calling twice on the same id
    (or on an id that was never promoted) is a no-op.

    Restores every `fact_claims` row that was `superseded_by =
    promoted_id` back to `status='current'`, clears its
    `superseded_by / superseded_at` pointers, and re-flags it as
    episodic (the only kind a consolidation worker would have
    superseded). The semantic row itself is deleted — not
    soft-deleted — because it was a pure derivative with no
    independent provenance. An `_audit_rollback` row is appended to
    `consolidation.jsonl` AND a `consolidation_rollback` op hits the
    WS5 hash-chain so the reversal is tamper-evident.

    Returns::

        {
          "promoted_id": int,
          "restored": int,      # number of contributors flipped back
          "semantic_deleted": bool,
          "already_rolled_back": bool,   # True → call was a no-op
        }
    """
    result = {
        "promoted_id": int(promoted_id),
        "restored": 0,
        "semantic_deleted": False,
        "already_rolled_back": False,
    }

    with db.connect() as conn:
        # Fetch the semantic row so we know what to audit even if the
        # lookup beats us to the contributors.
        sem_row = conn.execute(
            "SELECT subject_slug, predicate_key, kind, source_kind "
            "FROM fact_claims WHERE id = ?",
            (int(promoted_id),),
        ).fetchone()

        subject_slug = sem_row[0] if sem_row else None
        predicate_key = sem_row[1] if sem_row else None

        # Find contributors that were superseded BY this promotion.
        contribs = conn.execute(
            "SELECT id FROM fact_claims WHERE superseded_by = ?",
            (int(promoted_id),),
        ).fetchall()

        if not contribs and not sem_row:
            # Nothing points at this id and no semantic row exists:
            # the id was never promoted, or was already rolled back.
            result["already_rolled_back"] = True
            _audit_rollback(
                promoted_id=int(promoted_id),
                restored=0,
                subject_slug=subject_slug,
                predicate_key=predicate_key,
                reason=f"{reason}:noop",
            )
            return result

        for (cid,) in contribs:
            conn.execute(
                "UPDATE fact_claims "
                "SET status='current', kind='episodic', "
                "    superseded_by=NULL, superseded_at=NULL "
                "WHERE id=?",
                (int(cid),),
            )
        result["restored"] = len(contribs)

        # Delete the semantic derivative. Safety check: only delete a
        # row produced by the consolidation worker — never an
        # arbitrary fact_claims id. The source_kind discriminator is
        # `_promote_group`'s own write ("consolidation"), so anything
        # else is a caller bug and we refuse.
        if sem_row and sem_row[3] == "consolidation":
            conn.execute("DELETE FROM fact_claims WHERE id=?", (int(promoted_id),))
            result["semantic_deleted"] = True

    _audit_rollback(
        promoted_id=int(promoted_id),
        restored=result["restored"],
        subject_slug=subject_slug,
        predicate_key=predicate_key,
        reason=reason,
    )
    return result


# ---------------------------------------------------------------------------
# Scheduler installer (systemd user units + launchd plist)
# ---------------------------------------------------------------------------


def _repo_templates_dir() -> Path | None:
    """Locate the templates dir next to the source tree (dev + editable
    installs) or the system-install location."""
    here = Path(__file__).resolve()
    for candidate in (
        here.parent.parent.parent / "templates",
        Path("/usr/local/share/brain/templates"),
    ):
        if candidate.is_dir():
            return candidate
    return None


def _brain_cmd() -> str:
    """Resolve a usable `brain` CLI invocation string.

    Prefer a `brain` on PATH so users get the shortest command in the
    generated unit. Fall back to the current interpreter + module
    path, which works in a venv / editable install without further
    wiring.
    """
    found = shutil.which("brain")
    if found:
        return found
    return f"{sys.executable} -m brain.cli"


def _render(tmpl: str) -> str:
    brain_cmd = _brain_cmd()
    return (
        tmpl
        .replace("{{BRAIN_DIR}}", str(config.BRAIN_DIR))
        .replace("{{HOME}}", str(Path.home()))
        .replace("{{BRAIN_CMD}}", brain_cmd)
        .replace("{{BRAIN_CMD_BIN}}", shutil.which("brain") or sys.executable)
        .replace("{{USERNAME}}", os.environ.get("USER") or os.environ.get("USERNAME") or "user")
    )


def _systemd_user_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user"


def _launchd_user_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def install_scheduler(*, enable: bool = True) -> dict:
    """Install the consolidation scheduler for the current platform.

    * Linux  → `brain-consolidate.service` + `.timer` under the user's
      systemd unit directory, `systemctl --user daemon-reload` +
      `enable --now brain-consolidate.timer` when `enable=True`.
    * macOS  → `~/Library/LaunchAgents/com.<USER>.brain-consolidate.plist`,
      `launchctl load` when `enable=True`.
    * Other  → returns `{"platform": "unsupported"}` without error.

    Returns a dict describing the paths written and the enablement
    outcome. Callers can print/serialise directly.
    """
    templates = _repo_templates_dir()
    if templates is None:
        return {
            "error": "templates_not_found",
            "searched": [
                "<repo>/templates",
                "/usr/local/share/brain/templates",
            ],
        }

    system = platform.system()
    if system == "Linux":
        svc_tmpl = templates / "systemd" / "brain-consolidate.service.tmpl"
        tim_tmpl = templates / "systemd" / "brain-consolidate.timer.tmpl"
        if not svc_tmpl.exists() or not tim_tmpl.exists():
            return {"error": "template_missing",
                    "expected": [str(svc_tmpl), str(tim_tmpl)]}
        unit_dir = _systemd_user_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        svc_path = unit_dir / "brain-consolidate.service"
        tim_path = unit_dir / "brain-consolidate.timer"
        svc_path.write_text(_render(svc_tmpl.read_text()))
        tim_path.write_text(_render(tim_tmpl.read_text()))

        enabled = False
        if enable:
            for args in (
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now",
                 "brain-consolidate.timer"],
            ):
                try:
                    subprocess.run(args, check=False)
                except FileNotFoundError:
                    return {
                        "platform": "linux",
                        "service": str(svc_path),
                        "timer": str(tim_path),
                        "enabled": False,
                        "note": "systemctl not found; units written, "
                                "start manually when systemd is available",
                    }
            enabled = True
        return {
            "platform": "linux",
            "service": str(svc_path),
            "timer": str(tim_path),
            "enabled": enabled,
        }

    if system == "Darwin":
        plist_tmpl = templates / "launchd" / "brain-consolidate.plist.tmpl"
        if not plist_tmpl.exists():
            return {"error": "template_missing", "expected": [str(plist_tmpl)]}
        agents_dir = _launchd_user_dir()
        agents_dir.mkdir(parents=True, exist_ok=True)
        username = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
        plist_path = agents_dir / f"com.{username}.brain-consolidate.plist"
        plist_path.write_text(_render(plist_tmpl.read_text()))

        enabled = False
        if enable:
            try:
                subprocess.run(
                    ["launchctl", "load", "-w", str(plist_path)], check=False,
                )
                enabled = True
            except FileNotFoundError:
                return {
                    "platform": "darwin",
                    "plist": str(plist_path),
                    "enabled": False,
                    "note": "launchctl not found; plist written, "
                            "load manually when launchd is available",
                }
        return {
            "platform": "darwin",
            "plist": str(plist_path),
            "enabled": enabled,
        }

    return {"platform": "unsupported", "system": system}
