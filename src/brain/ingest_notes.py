"""Ingest free-form notes from anywhere in the vault into the brain.

The second extraction path (the first is `auto_extract` for Claude
sessions). Walks every `.md` file under `~/.brain/` that isn't part of
the machine-managed dirs, diffs against the SQLite ledger
(`notes.mtime + notes.sha`), and only re-indexes what actually changed.

Why this exists: a user can write `son dang o long xuyen.md` at the
vault root, or scribble into `inbox/2026-04-19.md`, and the brain must
see it without them having to file it under `entities/<type>/`.

Cost model: walking ~3000 files + stat = ~30 ms; SHA only for those
whose mtime changed; embed only for content-changed files. Idle
re-runs are essentially free.

Public API:
  ingest_all() -> dict     # diff walk + db upsert + semantic update
  main()                   # CLI: `python -m brain.ingest_notes`
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import brain.config as config
from brain import db


# Directories we never treat as "user notes" — they're machine-managed.
EXCLUDE_DIR_NAMES = {
    "entities",       # extracted facts; lives in db via upsert_entity_from_file
    "raw",            # transient session captures
    "_archive",       # archived entities
    "logs",           # log files
    ".obsidian",      # Obsidian config
    ".git",           # git internals
    ".vec",           # semantic index cache
    ".extract.lock.d",
    "node_modules",
    ".trash",
}

# Filename prefixes that signal "machine-managed metadata, skip".
EXCLUDE_FILE_PREFIXES = ("_",)  # _MOC.md, _placeholder.md, etc.

# Hard cap so we don't OOM on a 50 MB markdown dump someone pasted in.
MAX_BYTES = 256 * 1024


def _should_skip_dir(path: Path) -> bool:
    return path.name in EXCLUDE_DIR_NAMES or path.name.startswith(".")


def _should_skip_file(path: Path) -> bool:
    if path.suffix.lower() != ".md":
        return True
    if any(path.name.startswith(p) for p in EXCLUDE_FILE_PREFIXES):
        return True
    return False


def _iter_note_paths(root: Path):
    """Yield every candidate `.md` outside the excluded dirs."""
    for dirpath, dirnames, filenames in os.walk(root):
        dpath = Path(dirpath)
        # in-place prune so os.walk doesn't descend into excluded subtrees
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(dpath / d)]
        for fname in filenames:
            fpath = dpath / fname
            if _should_skip_file(fpath):
                continue
            yield fpath


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _strikethrough_fact_in_entity(
    entity_path: Path,
    target_hashes: set[str],
    note_rel: str,
    today: str,
) -> int:
    """Wrap matching fact lines in `~~…~~` and append an invalidation tag.

    A fact "matches" when `db.canonical_fact_hash` of the line's text
    (after the `- ` and minus the source suffix) is in `target_hashes`.
    Returns the number of lines actually modified.

    Lines already strikethroughed are left alone (idempotent — repeated
    note-delete events on the same provenance won't double-mark).
    """
    try:
        text = entity_path.read_text(errors="replace")
    except OSError:
        return 0

    new_lines: list[str] = []
    changed = 0
    for raw in text.split("\n"):
        line = raw
        stripped = line.lstrip()
        # Preserve original leading whitespace (Obsidian sub-bullets).
        indent = line[: len(line) - len(stripped)]
        if not stripped.startswith("- "):
            new_lines.append(line)
            continue
        body_text = stripped[2:]
        if body_text.lstrip().startswith("~~"):
            new_lines.append(line)  # already invalidated
            continue
        h = db.canonical_fact_hash(body_text)
        if h not in target_hashes:
            new_lines.append(line)
            continue
        # Wrap the fact text in strikethrough but keep the source suffix
        # outside the marker so the trail of provenance stays readable.
        import re as _re
        m = _re.search(r"\(source:[^)]*\)", body_text)
        if m:
            head = body_text[: m.start()].rstrip()
            tail = body_text[m.start():]
            new_body = (
                f"~~{head}~~ {tail} "
                f"[invalidated {today}: source note `{note_rel}` deleted]"
            )
        else:
            new_body = (
                f"~~{body_text.rstrip()}~~ "
                f"[invalidated {today}: source note `{note_rel}` deleted]"
            )
        new_lines.append(f"{indent}- {new_body}")
        changed += 1

    if changed:
        entity_path.write_text("\n".join(new_lines))
    return changed


def invalidate_facts_for_note(note_rel: str, verbose: bool = False) -> dict:
    """Strikethrough every fact whose provenance points at `note_rel`.

    Called after a note is detected as gone from disk. Walks the
    `fact_provenance` table, edits the affected entity files, then
    drops the provenance rows so a subsequent deletion of a different
    note doesn't re-process them. Re-upserts each touched entity to
    refresh the FTS index (struck-through facts are excluded by
    `db._facts_from_body`).
    """
    from datetime import datetime, timezone

    rows = db.facts_invalidated_by_note(note_rel)
    if not rows:
        return {"facts_invalidated": 0, "entities_touched": 0}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_entity: dict[str, set[str]] = {}
    for entity_rel, fact_hash in rows:
        by_entity.setdefault(entity_rel, set()).add(fact_hash)

    facts_changed = 0
    entities_touched = 0
    for entity_rel, hashes in by_entity.items():
        epath = config.BRAIN_DIR / entity_rel
        if not epath.exists():
            continue
        n = _strikethrough_fact_in_entity(epath, hashes, note_rel, today)
        if n > 0:
            facts_changed += n
            entities_touched += 1
            try:
                db.upsert_entity_from_file(epath)  # refresh FTS index
            except Exception as exc:
                if verbose:
                    print(f"  fts refresh failed for {entity_rel}: {exc}")
            if verbose:
                print(f"  ~~ invalidated {n} fact(s) in {entity_rel}")

    db.forget_note_provenance(note_rel)
    return {
        "facts_invalidated": facts_changed,
        "entities_touched": entities_touched,
    }


def _title_from(path: Path, body: str) -> str:
    """Use the first `# heading` if present, else humanise the filename.

    The filename matters: a user named the file `son dang o long xuyen.md`
    precisely so the answer is in the title.
    """
    for line in body.split("\n", 50):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ").strip()


def ingest_all(verbose: bool = False) -> dict:
    """Diff-walk the vault and upsert any changed notes."""
    root = config.BRAIN_DIR
    ledger = db.list_note_ledger()  # path -> (mtime, sha)

    changed: list[tuple[str, str, str]] = []  # (rel_path, title, body)
    seen: set[str] = set()
    scanned = 0
    skipped_large = 0

    for fpath in _iter_note_paths(root):
        scanned += 1
        try:
            stat = fpath.stat()
        except OSError:
            continue
        rel = str(fpath.relative_to(root))
        seen.add(rel)
        prev = ledger.get(rel)
        if prev and abs(prev[0] - stat.st_mtime) < 1e-3:
            continue
        if stat.st_size > MAX_BYTES:
            skipped_large += 1
            continue
        try:
            text = fpath.read_text(errors="replace")
        except OSError:
            continue
        sha = _sha(text)
        if prev and prev[1] == sha:
            # mtime touched but content unchanged — bump mtime only
            db.upsert_note(rel, _title_from(fpath, text), text, stat.st_mtime, sha)
            continue
        title = _title_from(fpath, text)
        db.upsert_note(rel, title, text, stat.st_mtime, sha)
        changed.append((rel, title, text))
        if verbose:
            print(f"  + {rel}", flush=True)

    # Notes that vanished from disk → delete from db AND retract any
    # entity facts whose provenance points back at them. Without this
    # second step, deleting `where-is-son.md` left the extracted fact
    # "Son in Long Xuyen" frozen in `entities/people/son.md` forever —
    # see the 2026-04-21 postmortem in git_ops.commit's docstring.
    deleted_paths: list[str] = []
    invalidation_summary = {"facts": 0, "entities": 0}
    for rel in list(ledger.keys()):
        if rel in seen:
            continue
        if not (root / rel).exists():
            try:
                inv = invalidate_facts_for_note(rel, verbose=verbose)
                invalidation_summary["facts"] += inv["facts_invalidated"]
                invalidation_summary["entities"] += inv["entities_touched"]
            except Exception as exc:
                if verbose:
                    print(f"  invalidation skipped for {rel}: {exc}", flush=True)
            db.delete_note_by_path(rel)
            deleted_paths.append(rel)
            if verbose:
                print(f"  - {rel}", flush=True)

    # Push the diff into the semantic index (incremental — see semantic.py).
    # Pass the *actually* deleted paths, not "everything in the ledger that
    # we didn't see this run" — a transient stat() failure on a still-alive
    # file would otherwise drop its embedding until the next full rebuild.
    if changed or deleted_paths:
        try:
            from brain import semantic
            # Prefer the persistent worker (model stays warm between launchd
            # ticks → ~0.5 s instead of ~10 s cold-start). Falls back to
            # in-process embedding when the worker isn't running.
            semantic.update_notes_via_worker(
                changed=[(r, t, b) for r, t, b in changed],
                deleted_paths=deleted_paths,
            )
        except Exception as exc:
            if verbose:
                print(f"  semantic update skipped: {exc}", flush=True)
    deleted = len(deleted_paths)

    return {
        "scanned": scanned,
        "changed": len(changed),
        "deleted": deleted,
        "skipped_large": skipped_large,
        "facts_invalidated": invalidation_summary["facts"],
        "entities_touched_by_invalidation": invalidation_summary["entities"],
    }


def main():
    import argparse
    p = argparse.ArgumentParser(description="Ingest vault notes into the brain")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    t0 = time.time()
    out = ingest_all(verbose=args.verbose)
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    inv = out.get("facts_invalidated", 0)
    inv_ent = out.get("entities_touched_by_invalidation", 0)
    msg = (
        f"notes: scanned={out['scanned']} changed={out['changed']} "
        f"deleted={out['deleted']} skipped_large={out['skipped_large']}"
    )
    if inv:
        msg += f" invalidated={inv}/{inv_ent}entities"
    msg += f" in {out['elapsed_ms']} ms"
    print(msg)


if __name__ == "__main__":
    main()
