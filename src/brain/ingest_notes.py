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

# File extensions we accept as user notes. Markdown is the primary, but
# `.txt` is deliberately included so a user who writes a plain-text note
# (`echo 'son dang o saigon' > son.txt`) still gets indexed. That exact
# class of silent-skip is a trust-breaking failure (2026-04-23 incident:
# `~/.brain/son.txt` held the answer but brain claimed no record, because
# the filter hard-required `.md`). Convention stays "prefer markdown";
# the whitelist just prevents a silent trust break when a user forgets.
# `.markdown` is the long-form alias a few editors emit.
INGEST_EXTENSIONS = {".md", ".markdown", ".txt", ".text"}

# Hard cap so we don't OOM on a 50 MB markdown dump someone pasted in.
MAX_BYTES = 256 * 1024


def _should_skip_dir(path: Path) -> bool:
    return path.name in EXCLUDE_DIR_NAMES or path.name.startswith(".")


def _should_skip_file(path: Path) -> bool:
    if path.suffix.lower() not in INGEST_EXTENSIONS:
        return True
    if any(path.name.startswith(p) for p in EXCLUDE_FILE_PREFIXES):
        return True
    return False


def _iter_note_paths(root: Path):
    """Yield every candidate note file outside the excluded dirs."""
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


def _entity_type_name_from_path(entity_rel: str) -> tuple[str | None, str | None]:
    """Parse `entities/<type>/<slug>.md` into (type, name).

    Reads the entity file's frontmatter when available so the tombstone
    records the canonical `name` rather than the slug. Falls back to the
    slugified filename when the file is gone or has no frontmatter.
    """
    parts = entity_rel.split("/")
    if len(parts) < 3 or parts[0] != "entities":
        return None, None
    etype = parts[1]
    slug = parts[-1].rsplit(".", 1)[0]
    name: str | None = slug
    try:
        epath = config.BRAIN_DIR / entity_rel
        if epath.exists():
            text = epath.read_text(errors="replace")
            from brain.db import _parse_frontmatter
            fm = _parse_frontmatter(text)
            if fm.get("name"):
                name = fm["name"]
    except Exception:
        pass
    return etype, name


def _strikethrough_fact_in_entity(
    entity_path: Path,
    target_hashes: set[str],
    note_rel: str,
    today: str,
    reason: str = "deleted",
) -> tuple[int, list[str]]:
    """Wrap matching fact lines in `~~…~~` and append an invalidation tag.

    A fact "matches" when `db.canonical_fact_hash` of the line's text
    (after the `- ` and minus the source suffix) is in `target_hashes`.
    Returns `(count, texts)` — number of lines modified and the
    canonical (source-stripped) text of each, so the caller can emit
    tombstones for them.

    Lines already strikethroughed are left alone (idempotent — repeated
    note-delete events on the same provenance won't double-mark).

    `reason` flavours the audit tag — "deleted" when the note vanished
    from disk, "edited" when the note still exists but the user
    rewrote it (note_extract treats the previous version's facts as
    retracted before applying the new extraction).
    """
    try:
        text = entity_path.read_text(errors="replace")
    except OSError:
        return 0, []

    new_lines: list[str] = []
    changed = 0
    texts: list[str] = []
    import re as _re
    _SRC_RE = _re.compile(r"\(source:[^)]*\)")
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
        m = _SRC_RE.search(body_text)
        if m:
            head = body_text[: m.start()].rstrip()
            tail = body_text[m.start():]
            canonical_text = head
            new_body = (
                f"~~{head}~~ {tail} "
                f"[invalidated {today}: source note `{note_rel}` {reason}]"
            )
        else:
            canonical_text = body_text.rstrip()
            new_body = (
                f"~~{body_text.rstrip()}~~ "
                f"[invalidated {today}: source note `{note_rel}` {reason}]"
            )
        new_lines.append(f"{indent}- {new_body}")
        changed += 1
        texts.append(canonical_text)

    if changed:
        entity_path.write_text("\n".join(new_lines))
    return changed, texts


def invalidate_facts_for_note(
    note_rel: str, verbose: bool = False, reason: str = "deleted"
) -> dict:
    """Strikethrough every fact whose provenance points at `note_rel`.

    Called after a note is detected as gone from disk (`reason="deleted"`)
    or before re-extracting an edited note (`reason="edited"`). Walks
    the `fact_provenance` table, edits the affected entity files, then
    drops the provenance rows so subsequent invalidations won't re-mark
    the same lines. Re-upserts each touched entity to refresh FTS
    (strikethroughed facts are excluded by `db._facts_from_body`).
    """
    from datetime import datetime, timezone

    rows = db.facts_invalidated_by_note(note_rel)
    if not rows:
        return {"facts_invalidated": 0, "entities_touched": 0, "entity_paths": []}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_entity: dict[str, set[str]] = {}
    for entity_rel, fact_hash in rows:
        by_entity.setdefault(entity_rel, set()).add(fact_hash)

    facts_changed = 0
    entities_touched = 0
    touched_entity_paths: list[str] = []
    tombstones_written = 0
    for entity_rel, hashes in by_entity.items():
        epath = config.BRAIN_DIR / entity_rel
        if not epath.exists():
            continue
        n, texts = _strikethrough_fact_in_entity(
            epath, hashes, note_rel, today, reason=reason
        )
        if n > 0:
            facts_changed += n
            entities_touched += 1
            touched_entity_paths.append(entity_rel)
            try:
                db.upsert_entity_from_file(epath)  # refresh FTS index
            except Exception as exc:
                if verbose:
                    print(f"  fts refresh failed for {entity_rel}: {exc}")
            # Tombstone on DELETE only. Edits must not tombstone, or
            # the new extraction from the edited content would be blocked
            # from restating a claim the user still wrote in the new body.
            if reason == "deleted":
                etype, ename = _entity_type_name_from_path(entity_rel)
                for t in texts:
                    try:
                        if db.add_tombstone(
                            t,
                            entity_type=etype,
                            entity_name=ename,
                            reason=f"note-deleted:{note_rel}",
                            created_by="note-delete",
                        ):
                            tombstones_written += 1
                    except Exception as exc:
                        if verbose:
                            print(f"  tombstone failed for {t!r}: {exc}")
            if verbose:
                print(f"  ~~ invalidated {n} fact(s) in {entity_rel}")

    db.forget_note_provenance(note_rel)
    return {
        "facts_invalidated": facts_changed,
        "entities_touched": entities_touched,
        "entity_paths": touched_entity_paths,
        "tombstones_written": tombstones_written,
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


def ingest_one(path: Path | str) -> dict:
    """Ingest a single note file. Complements `ingest_all`.

    Used by the fs-event watcher (`brain watcher`) to react to individual
    file writes without re-walking the vault. Semantics mirror the
    per-file branch of `ingest_all`:
      * skipped if path is outside BRAIN_DIR, under a machine-managed
        dir, non-`.md`, or starts with an underscore;
      * skipped if oversized (`MAX_BYTES`);
      * a short-circuit when mtime is unchanged (sha still recomputed
        only if mtime moved, mirroring ingest_all's ledger semantics).

    Returns `{"status": str, "rel_path": str | None, "changed": bool,
    "deleted": bool}`. `status` is one of `changed | unchanged |
    deleted | skipped`. The caller (watcher) is responsible for
    follow-up semantic reindexing.
    """
    p = Path(path)
    root = config.BRAIN_DIR
    out: dict = {"status": "skipped", "rel_path": None, "changed": False, "deleted": False}

    # Resolve relative path; reject anything outside the vault.
    try:
        rel = str(p.resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return out
    out["rel_path"] = rel

    # Machine-managed dir or non-note filename — the walker skips
    # these; apply the same rule here so `inotify` noise from
    # `entities/`, `raw/`, `.git/`, `.vec/` etc. is a no-op.
    rel_path = Path(rel)
    if any(part in EXCLUDE_DIR_NAMES or part.startswith(".") for part in rel_path.parts[:-1]):
        return out
    if _should_skip_file(p):
        return out

    if not p.exists():
        # Deletion path — reuse ingest_all's cascade.
        try:
            inv = invalidate_facts_for_note(rel, verbose=False)
        except Exception:
            inv = {"facts_invalidated": 0, "entities_touched": 0, "tombstones_written": 0}
        db.delete_note_by_path(rel)
        try:
            from brain import semantic
            semantic.update_notes_via_worker(changed=[], deleted_paths=[rel])
        except Exception:
            pass
        out.update({"status": "deleted", "deleted": True,
                    "facts_invalidated": inv.get("facts_invalidated", 0)})
        return out

    try:
        stat = p.stat()
    except OSError:
        return out
    if stat.st_size > MAX_BYTES:
        return out
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return out
    sha = _sha(text)
    title = _title_from(p, text)

    prev_map = db.list_note_ledger()
    prev = prev_map.get(rel)
    if prev and prev[1] == sha:
        # Content unchanged; only bump mtime so the ledger stays
        # tight with the fs. No semantic work.
        db.upsert_note(rel, title, text, stat.st_mtime, sha)
        out["status"] = "unchanged"
        return out

    db.upsert_note(rel, title, text, stat.st_mtime, sha)
    # Push to semantic worker (or fall back to in-process build).
    try:
        from brain import semantic
        semantic.update_notes_via_worker(
            changed=[(rel, title, text)],
            deleted_paths=[],
        )
    except Exception:
        pass
    out.update({"status": "changed", "changed": True})
    return out


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
        # WS4: sanitize BEFORE sha so that rotating a secret in the
        # source file (or re-scrubbing with a stricter rule set) shows
        # up as a content change and triggers re-indexing. Sha is
        # computed on the cleaned text so db.notes.body, the indexed
        # snippet, and the embedding all see the redacted form.
        try:
            from brain.sanitize import sanitize
            text = sanitize(text, source_kind="note", source_path=rel).text
        except Exception:
            pass
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
    invalidation_summary = {"facts": 0, "entities": 0, "tombstones": 0}
    for rel in list(ledger.keys()):
        if rel in seen:
            continue
        if not (root / rel).exists():
            try:
                inv = invalidate_facts_for_note(rel, verbose=verbose)
                invalidation_summary["facts"] += inv["facts_invalidated"]
                invalidation_summary["entities"] += inv["entities_touched"]
                invalidation_summary["tombstones"] += inv.get("tombstones_written", 0)
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
        "tombstones_written": invalidation_summary["tombstones"],
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
