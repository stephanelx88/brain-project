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

    # Notes that vanished from disk → delete from db
    deleted = 0
    for rel in list(ledger.keys()):
        if rel in seen:
            continue
        if not (root / rel).exists():
            db.delete_note_by_path(rel)
            deleted += 1
            if verbose:
                print(f"  - {rel}", flush=True)

    # Push the diff into the semantic index (incremental — see semantic.py)
    if changed or deleted:
        try:
            from brain import semantic
            semantic.update_notes(changed=[(r, t, b) for r, t, b in changed],
                                  deleted_paths=[r for r in ledger if r not in seen])
        except Exception as exc:
            if verbose:
                print(f"  semantic update skipped: {exc}", flush=True)

    return {
        "scanned": scanned,
        "changed": len(changed),
        "deleted": deleted,
        "skipped_large": skipped_large,
    }


def main():
    import argparse
    p = argparse.ArgumentParser(description="Ingest vault notes into the brain")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    t0 = time.time()
    out = ingest_all(verbose=args.verbose)
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    print(
        f"notes: scanned={out['scanned']} changed={out['changed']} "
        f"deleted={out['deleted']} skipped_large={out['skipped_large']} "
        f"in {out['elapsed_ms']} ms"
    )


if __name__ == "__main__":
    main()
