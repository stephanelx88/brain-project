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


def _semantic_notes_drift() -> bool:
    """Detect whether the semantic notes index is out of sync with the DB.

    The ``ingest_notes`` pipeline has incremental semantic updates
    (worker → in-process fallback). If *both* paths fail, the DB
    reflects the deletion but ``.vec/notes.json`` still embeds the
    ghost entry — we saw julia/hana/ivan surface in rankings for
    deleted files. Returns ``True`` when the two disagree on path set.

    Path is derived from the live ``config.BRAIN_DIR`` at call time
    rather than the import-time constant in ``semantic`` so tests
    that monkeypatch ``BRAIN_DIR`` see their own vault's index
    instead of leaking into the real one.
    """
    from brain import db, config
    import json

    notes_json = config.BRAIN_DIR / ".vec" / "notes.json"
    if not notes_json.exists():
        return False  # nothing built yet; let the first build handle it
    try:
        sem_meta = json.loads(notes_json.read_text())
        sem_paths = {m.get("path", "") for m in sem_meta}
    except (json.JSONDecodeError, OSError):
        return True  # malformed = drifted for our purposes

    try:
        with db.connect() as conn:
            db_paths = {
                r[0] for r in conn.execute("SELECT path FROM notes").fetchall()
            }
    except Exception:
        return False  # DB unreadable — let caller's own error handler deal

    return sem_paths != db_paths


def post_extraction_sync() -> dict:
    """Run after any extraction batch to ensure index + provenance consistency.

    Three passes:

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

    3. Semantic-notes drift safety net — if the semantic notes index
         disagrees with the DB on path set, force a full rebuild.
         Belt-and-suspenders for the silent-failure case where
         ``ingest_notes`` updated the DB but the semantic worker AND
         the in-process fallback both errored (leaving ghost entries
         that surface as wrong hits in ``brain_recall``).

    Returns ``{"gc_removed": N, "gc_added": M, "notes_requeued": K,
    "semantic_rebuilt": bool}``. All passes idempotent — safe to call
    repeatedly.
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

    semantic_rebuilt = False
    if _semantic_notes_drift():
        try:
            from brain.semantic import build as semantic_build
            semantic_build()
            semantic_rebuilt = True
        except Exception:
            pass  # non-fatal; surfaces again next run

    return {
        "gc_removed": len(removed_paths),
        "gc_added": len(added_paths),
        "notes_requeued": len(stale_notes),
        "semantic_rebuilt": semantic_rebuilt,
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
