"""One-time migration: populate `fact_claims` from the legacy `facts` table.

WS6 step 3. Safe to re-run; idempotent via an "already populated"
count check. Pass 2 remaps legacy `superseded_by` FK references
through a `legacy_id → new_id` dict built in pass 1.

Usage:
  python -m brain.backfill_facts            # dry run
  python -m brain.backfill_facts --apply    # actually populate

Design notes
------------
* Predicate parsing uses the same 3-regex classifier `_classify_predicate`
  uses during dual-write; unknown predicates land as `'_unparsed'` so
  WS8's idle consolidation can reparse them with an LLM pass later.
* Every backfilled row carries `scrub_tag='pre-ws4'` so WS8 never
  promotes an unscrubbed episodic row into semantic memory.
* `trust_source` defaults from `source_kind`; `risk_level` is always
  `'trusted'` on backfill (pre-WS4 content has no injection signal on
  record — if a row was malicious we'd need the ledger replay, not
  this import, to classify it).
* `observed_at` falls back to `entities.last_updated`'s epoch (best
  proxy we have; legacy `facts` lacks an observed_at column).
* Backfill is **additive** — it never DELETEs from `fact_claims`,
  never modifies `facts`, and never touches FTS. A failed run can
  be reverted with `DELETE FROM fact_claims WHERE scrub_tag='pre-ws4'`.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import brain.config as config
from brain import db


def _observed_at_from_entity(last_updated: str | None) -> float:
    """Convert entities.last_updated (ISO date) to epoch. Falls back
    to `now()` when the field is missing — better than leaving the
    NOT NULL observed_at unset."""
    if not last_updated:
        return time.time()
    # entities.last_updated in the current schema is an ISO date
    # string — tolerate a few common shapes.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(last_updated, fmt).timestamp()
        except (TypeError, ValueError):
            continue
    return time.time()


def _is_already_populated(conn) -> tuple[int, int]:
    """(fact_claims_count, facts_count). The caller treats fact_claims
    >= facts as 'already backfilled' and short-circuits."""
    fc = conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0]
    f = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    return fc, f


def run(apply: bool = False, verbose: bool = False) -> dict:
    """Backfill fact_claims from facts. Returns a summary dict.

    `apply=False` (default) reports what would happen without writing.
    """
    summary: dict = {
        "facts_total": 0,
        "facts_superseded": 0,
        "skipped_existing": 0,
        "inserted": 0,
        "superseded_remap": 0,
        "already_populated": False,
        "applied": apply,
    }

    with db.connect() as conn:
        fc_count, f_count = _is_already_populated(conn)
        summary["facts_total"] = f_count
        if fc_count >= f_count and f_count > 0:
            summary["already_populated"] = True
            summary["skipped_existing"] = fc_count
            if verbose:
                print(
                    f"fact_claims already has {fc_count} rows for {f_count} facts — "
                    "nothing to do. `DELETE FROM fact_claims WHERE scrub_tag='pre-ws4'` "
                    "to re-run cleanly.",
                    file=sys.stderr,
                )
            return summary

        rows = conn.execute(
            """
            SELECT f.id, f.entity_id, f.text, f.source, f.fact_date, f.status,
                   f.superseded_by,
                   e.slug, e.last_updated
            FROM facts f
            JOIN entities e ON e.id = f.entity_id
            ORDER BY f.id
            """
        ).fetchall()

        id_map: dict[int, int] = {}
        pass1_superseded: list[tuple[int, int]] = []  # (legacy_fact_id, legacy_superseded_by)

        for (legacy_id, entity_id, text, source, fact_date, status,
             sby, subject_slug, last_updated) in rows:
            # Classification mirrors live dual-write.
            predicate, predicate_group = db._classify_predicate(text)
            predicate_key = db._norm_predicate_key(predicate)

            object_phrase = db._extract_object_phrase(text, predicate_group)
            object_slug: str | None = None
            object_entity: int | None = None
            object_type = "string"
            if object_phrase:
                match = conn.execute(
                    "SELECT id, slug FROM entities "
                    "WHERE lower(name)=lower(?) OR slug=? LIMIT 1",
                    (object_phrase, object_phrase.lower().replace(" ", "-")),
                ).fetchone()
                if match:
                    object_entity, object_slug = match[0], match[1]
                    object_type = "entity"
            object_text = (object_phrase or None) if object_entity is None else None

            source_kind, source_path, episode_id = db._parse_source(source)
            trust_source = (
                "user"       if source_kind == "user"       else
                "note"       if source_kind == "note"       else
                "correction" if source_kind == "correction" else
                "extracted"
            )
            lifecycle_status = "superseded" if status == "superseded" else "current"
            observed_at = _observed_at_from_entity(last_updated)
            claim_key = db._claim_key(
                subject_slug, predicate_key, object_slug,
                object_text or text,
            )

            if status == "superseded":
                summary["facts_superseded"] += 1

            if not apply:
                summary["inserted"] += 1
                if sby:
                    pass1_superseded.append((legacy_id, sby))
                continue

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
                    entity_id, subject_slug,
                    predicate, predicate_key, predicate_group,
                    object_entity, object_text, object_slug, object_type,
                    text,
                    fact_date, observed_at,
                    source_kind, source_path, None, "pre-ws4", episode_id,
                    0.5, "trusted", trust_source, 0.3,
                    # Backfilled rows are treated as semantic per the
                    # ontologist's D5: they already survived today's
                    # pipeline, so skip the episodic→semantic dance.
                    "semantic", lifecycle_status, claim_key,
                ),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            id_map[legacy_id] = new_id
            summary["inserted"] += 1
            if sby:
                pass1_superseded.append((legacy_id, sby))

        # Pass 2 — remap superseded_by.
        if apply:
            for legacy_id, legacy_sby in pass1_superseded:
                new_id = id_map.get(legacy_id)
                new_sby = id_map.get(legacy_sby)
                if new_id is None or new_sby is None:
                    continue
                conn.execute(
                    "UPDATE fact_claims SET superseded_by=? WHERE id=?",
                    (new_sby, new_id),
                )
                summary["superseded_remap"] += 1
        else:
            summary["superseded_remap"] = len(pass1_superseded)

    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Backfill fact_claims from the legacy facts table (WS6). "
            "Idempotent: re-running is a no-op once fact_claims is populated."
        ),
    )
    p.add_argument("--apply", action="store_true",
                   help="Actually write rows (default: dry run).")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print per-step summary.")
    args = p.parse_args(argv)

    summary = run(apply=args.apply, verbose=args.verbose)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] fact_claims backfill: "
          f"inserted={summary['inserted']}  "
          f"superseded={summary['facts_superseded']}  "
          f"superseded_remap={summary['superseded_remap']}  "
          f"facts_total={summary['facts_total']}  "
          f"already_populated={summary['already_populated']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
