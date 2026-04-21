#!/usr/bin/env python3
"""One-off cleanup for facts wrongly extracted from system-managed vault
files (cursor-user-rules.md, program.md, etc.) before EXCLUDED_PATHS
caught them. Strikethroughs the contaminated facts in entity files
(reason="auto-managed file"), drops the provenance rows, marks the
source notes as fully extracted so they won't be re-processed.

Safe to re-run — idempotent. Operates only on rows whose `note_path`
is in `note_extract.EXCLUDED_PATHS`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import brain.config as config
from brain import db
from brain.ingest_notes import _strikethrough_fact_in_entity
from brain.note_extract import EXCLUDED_PATHS

REASON = "auto-managed file (excluded from note_extract)"


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_struck = 0
    total_entities = 0
    total_provenance_dropped = 0

    for note_path in EXCLUDED_PATHS:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT entity_path, fact_hash FROM fact_provenance "
                "WHERE note_path = ?",
                (note_path,),
            ).fetchall()

        if not rows:
            continue

        by_entity: dict[str, set[str]] = {}
        for ent, fhash in rows:
            by_entity.setdefault(ent, set()).add(fhash)

        for ent_rel, hashes in by_entity.items():
            ent_path = config.BRAIN_DIR / ent_rel
            if not ent_path.exists():
                continue
            n = _strikethrough_fact_in_entity(
                ent_path, hashes, note_path, today, reason=REASON
            )
            if n > 0:
                total_struck += n
                total_entities += 1
                try:
                    db.upsert_entity_from_file(ent_path)
                except Exception as exc:
                    print(f"  warn: re-upsert {ent_rel} failed: {exc}")

        with db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM fact_provenance WHERE note_path = ?", (note_path,)
            )
            total_provenance_dropped += cur.rowcount
            # Mark the note as fully extracted so the loop won't re-LLM it.
            conn.execute(
                "UPDATE notes SET extracted_sha = sha WHERE path = ?",
                (note_path,),
            )

        print(f"  {note_path}: struck {len(hashes)} fact(s)")

    print(
        f"\nDone: {total_struck} facts struck across {total_entities} entities, "
        f"{total_provenance_dropped} provenance rows dropped."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
