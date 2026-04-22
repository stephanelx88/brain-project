"""Source integrity verification for the brain pipeline.

Two passes:

  gc()     — remove phantom DB/FTS entries for entity files deleted from
              disk. Fast, runs automatically inside auto_clean. Call this
              any time you suspect index drift (manual file deletes, failed
              mid-run, etc.).

  stale()  — inspect fact_provenance rows that carry a source_sha and
              compare against the current notes.sha. Returns facts whose
              source note has been edited (stale) or deleted (orphaned)
              since the last extraction. Requires source_sha tracking
              (facts recorded before 2026-04-22 have NULL source_sha and
              are skipped).

Public API:
  gc()      -> dict                  # {removed: int, paths: list[str]}
  stale()   -> list[dict]            # [{entity_path, note_path, status, ...}]
  main()    -> int                   # CLI: `brain verify`
"""

from __future__ import annotations

import argparse


def gc() -> dict:
    """Sync the DB index with the entity files on disk — both directions.

    1. Phantom pass: remove DB entries for entity files that no longer exist.
    2. Untracked pass: upsert entity files on disk that are missing from DB.
    3. Trigger semantic rebuild if anything changed so vector recall stays
       consistent with the DB state.

    Returns:
      {"removed": N, "added": M, "removed_paths": [...], "added_paths": [...]}
    """
    from brain.db import gc_orphaned_entities, index_untracked_entities

    removed_paths = gc_orphaned_entities()
    added_paths = index_untracked_entities()

    if removed_paths or added_paths:
        try:
            from brain.semantic import build as semantic_build
            semantic_build()
        except Exception:
            pass

    return {
        "removed": len(removed_paths),
        "added": len(added_paths),
        "removed_paths": removed_paths,
        "added_paths": added_paths,
    }


def post_extraction_sync() -> dict:
    """Run after any extraction batch to ensure index + provenance consistency.

    Two passes:

    1. GC — sync DB index with disk in both directions:
         phantom entries (in DB, file deleted) are removed;
         untracked entities (file on disk, not in DB) are indexed.
         Does NOT trigger a semantic rebuild — the extraction pipeline
         already calls semantic.build() and we don't want a double build.

    2. Stale requeue — for note-sourced facts whose source note has been
         edited since extraction, reset notes.extracted_sha = NULL so
         note_extract.process_pending() picks them up on its next run and
         re-derives fresh facts. This closes the loop: session extraction
         can't mutate notes, but it can flag them for re-verification.

    Returns {"gc_removed": N, "gc_added": M, "notes_requeued": K}.
    Safe to call even when nothing changed — all passes are idempotent.
    """
    from brain.db import (
        gc_orphaned_entities, index_untracked_entities,
        find_stale_provenance, connect,
    )

    removed_paths = gc_orphaned_entities()
    added_paths = index_untracked_entities()

    stale_rows = find_stale_provenance()
    stale_notes = {r["note_path"] for r in stale_rows if r["status"] == "stale"}
    if stale_notes:
        with connect() as conn:
            for note_path in stale_notes:
                conn.execute(
                    "UPDATE notes SET extracted_sha=NULL WHERE path=?",
                    (note_path,),
                )

    return {
        "gc_removed": len(removed_paths),
        "gc_added": len(added_paths),
        "notes_requeued": len(stale_notes),
    }


def stale() -> list[dict]:
    """Return facts whose source note has changed or been deleted.

    Only facts with a recorded source_sha are checked — older provenance
    rows (NULL source_sha) are skipped since we have no extraction baseline.

    Each returned dict has:
      entity_path  — vault-relative path to the entity file
      fact_hash    — canonical sha256 of the fact text
      note_path    — vault-relative path to the source note
      source_sha   — sha recorded at extraction time
      current_sha  — current sha of the note (None if deleted)
      status       — 'stale' (note edited) or 'orphaned' (note gone)
    """
    from brain.db import find_stale_provenance

    return find_stale_provenance()


def main(argv: list[str] | None = None) -> int:
    """`brain verify` — run GC and report stale/orphaned facts."""
    p = argparse.ArgumentParser(
        description="Verify brain source integrity: GC orphans and detect stale facts.")
    p.add_argument("--gc-only", action="store_true",
                   help="Only run GC pass (skip stale fact report).")
    p.add_argument("--stale-only", action="store_true",
                   help="Only report stale/orphaned facts (skip GC).")
    args = p.parse_args(argv)

    run_gc = not args.stale_only
    run_stale = not args.gc_only

    if run_gc:
        result = gc()
        if result["removed"]:
            print(f"gc: removed {result['removed']} phantom index entries")
            for p_ in result["removed_paths"]:
                print(f"  - {p_}")
        if result["added"]:
            print(f"gc: indexed {result['added']} untracked entity files")
            for p_ in result["added_paths"]:
                print(f"  + {p_}")
        if not result["removed"] and not result["added"]:
            print("gc: index is clean")

    if run_stale:
        rows = stale()
        if not rows:
            print("verify: all tracked facts have matching source hashes")
        else:
            orphaned = [r for r in rows if r["status"] == "orphaned"]
            stale_rows = [r for r in rows if r["status"] == "stale"]
            if orphaned:
                print(f"\norphaned facts ({len(orphaned)}) — source note deleted:")
                for r in orphaned:
                    print(f"  {r['entity_path']}  ←  {r['note_path']}")
            if stale_rows:
                print(f"\nstale facts ({len(stale_rows)}) — source note edited since extraction:")
                for r in stale_rows:
                    print(f"  {r['entity_path']}  ←  {r['note_path']}")
            print(
                "\nRun `brain note-extract` to re-extract changed notes and "
                "auto-retract stale facts."
            )

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
