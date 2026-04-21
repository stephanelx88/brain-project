"""SQLite + FTS5 mirror of the brain.

Why: scanning ~900 markdown files or asking Claude to read a 216 KB
`index.md` doesn't scale. This module is a fast, write-through index:

  entities    — one row per entity file (canonical metadata)
  facts       — one row per `- fact …` bullet (provenance preserved)
  aliases     — name → entity_id (for synonym lookup)
  fts_facts   — FTS5 virtual table over fact text (BM25 search)
  fts_entity  — FTS5 over name+aliases+summary (entity name lookup)

The DB lives at `~/.brain/.brain.db`. It is a *cache*: rebuildable from
markdown at any time via `brain.db rebuild`. Markdown stays the source
of truth so Obsidian, git, and humans keep working.

Public API:
  upsert_entity_from_file(path)   — write-through hook
  delete_entity_by_path(path)
  search(query, k=10, type=None)  — BM25 fact search
  search_entities(query, k=10)    — entity-name search
  rebuild()                       — full reindex from disk
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

import brain.config as config

DB_PATH = config.BRAIN_DIR / ".brain.db"

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;

CREATE TABLE IF NOT EXISTS entities (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    type         TEXT NOT NULL,
    slug         TEXT NOT NULL,
    name         TEXT NOT NULL,
    status       TEXT,
    summary      TEXT,
    first_seen   TEXT,
    last_updated TEXT,
    source_count INTEGER DEFAULT 1,
    tags         TEXT
);
CREATE INDEX IF NOT EXISTS entities_type_idx ON entities(type);
CREATE INDEX IF NOT EXISTS entities_updated_idx ON entities(last_updated);

CREATE TABLE IF NOT EXISTS aliases (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias)
);
CREATE INDEX IF NOT EXISTS aliases_alias_idx ON aliases(alias);

CREATE TABLE IF NOT EXISTS facts (
    id        INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    text      TEXT NOT NULL,
    source    TEXT,
    fact_date TEXT
);
CREATE INDEX IF NOT EXISTS facts_entity_idx ON facts(entity_id);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_facts USING fts5(
    text, source, content='facts', content_rowid='id', tokenize='porter'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_entity USING fts5(
    name, aliases, summary, content='', tokenize='porter'
);

-- Free-form notes: anything in the vault that isn't `entities/`. Lives
-- alongside facts so a single search query can hit both human-written
-- markdown (e.g. `son dang o long xuyen.md`) and LLM-extracted facts.
-- `path` is relative to BRAIN_DIR; `mtime` and `sha` form the change-detection
-- ledger so the ingest walker only re-embeds what actually changed.
CREATE TABLE IF NOT EXISTS notes (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    mtime        REAL NOT NULL,
    sha          TEXT NOT NULL,
    last_indexed REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_mtime_idx ON notes(mtime);

-- Standalone (no `content=`) so plain DELETE/INSERT keep it in sync.
-- External-content tables need `INSERT INTO fts_notes(fts_notes,rowid)
-- VALUES('delete',rowid)` for removal which we don't use.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5(
    title, body, path, tokenize='porter'
);

-- Reverse index: which vault notes are responsible for which extracted
-- entity facts. Populated when `apply_extraction` is invoked with an
-- explicit `source_note_paths=[...]`. When a note disappears from the
-- vault, `ingest_notes.invalidate_facts_for_note` looks up rows here
-- and strikethroughs the matching fact lines in the entity files —
-- so deleting `where-is-son.md` retracts the "Son in Long Xuyen" fact
-- from `entities/people/son.md` instead of leaving it frozen forever.
--
-- `fact_hash` is sha256 of the canonical (lower, whitespace-stripped,
-- source-suffix-removed) fact text — stable across re-extractions of
-- the same fact phrasing.
CREATE TABLE IF NOT EXISTS fact_provenance (
    entity_path  TEXT NOT NULL,
    fact_hash    TEXT NOT NULL,
    note_path    TEXT NOT NULL,
    recorded_at  REAL NOT NULL,
    PRIMARY KEY (entity_path, fact_hash, note_path)
);
CREATE INDEX IF NOT EXISTS fact_provenance_note_idx
    ON fact_provenance(note_path);
"""

_SOURCE_RE = re.compile(r"\(source:\s*([^,)]+?)(?:,\s*([\d-]+))?\s*\)")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).split("\n"):
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def _body(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    return text[m.end():] if m else text


def _summary(body: str, limit: int = 200) -> str:
    for line in body.split("\n"):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        if s.startswith("-"):
            s = s.lstrip("- ").strip()
        s = _SOURCE_RE.sub("", s).strip()
        if s:
            return s[:limit]
    return ""


def _facts_from_body(body: str) -> Iterable[tuple[str, str | None, str | None]]:
    """Yield (text, source, date) for each LIVE fact bullet.

    Strikethrough bullets (`- ~~…~~`) are treated as invalidated and
    skipped — they remain in the markdown for human auditability and
    git history, but the search index pretends they don't exist so
    deleted-source facts stop showing up in recall.
    """
    for raw in body.split("\n"):
        line = raw.strip()
        if not line.startswith("- "):
            continue
        body_text = line[2:].strip()
        if not body_text:
            continue
        if body_text.startswith("~~"):
            continue  # invalidated — see ingest_notes.invalidate_facts_for_note
        m = _SOURCE_RE.search(body_text)
        source = m.group(1).strip() if m else None
        date = m.group(2).strip() if m and m.group(2) else None
        cleaned = _SOURCE_RE.sub("", body_text).strip()
        if cleaned:
            yield cleaned, source, date


def _entity_type_from_path(path: Path) -> str | None:
    parts = path.parts
    try:
        i = parts.index("entities")
    except ValueError:
        return None
    if i + 1 >= len(parts):
        return None
    return parts[i + 1]


def upsert_entity_from_file(path: Path | str) -> int | None:
    """Insert/replace one entity row + its facts. Returns entity_id."""
    path = Path(path)
    if not path.exists():
        delete_entity_by_path(path)
        return None
    etype = _entity_type_from_path(path)
    if not etype:
        return None

    text = path.read_text(errors="replace")
    fm = _parse_frontmatter(text)
    body = _body(text)

    name = fm.get("name") or path.stem.replace("-", " ").title()
    aliases_raw = fm.get("aliases", "")
    aliases = []
    if aliases_raw:
        aliases_raw = aliases_raw.strip("[]")
        aliases = [a.strip().strip("'\"") for a in aliases_raw.split(",") if a.strip()]
    summary = _summary(body)

    try:
        source_count = int(fm.get("source_count") or 1)
    except ValueError:
        source_count = 1

    rel_path = str(path.relative_to(config.BRAIN_DIR))

    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM entities WHERE path = ?", (rel_path,))
        row = cur.fetchone()
        is_update = row is not None
        if is_update:
            entity_id = row[0]
            # Capture previous fts_entity column values BEFORE the row's
            # name/summary get overwritten — contentless FTS5 needs the
            # exact previous values to delete the prior shadow row.
            prev_row = cur.execute(
                "SELECT name, COALESCE(summary,'') FROM entities WHERE id=?",
                (entity_id,),
            ).fetchone()
            prev_aliases_row = cur.execute(
                "SELECT GROUP_CONCAT(alias, ' ') FROM aliases WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
            prev_name = prev_row[0] if prev_row else name
            prev_summary = prev_row[1] if prev_row else ""
            prev_aliases = (
                prev_aliases_row[0] if prev_aliases_row and prev_aliases_row[0] else ""
            )
            cur.execute(
                """UPDATE entities
                   SET type=?, slug=?, name=?, status=?, summary=?,
                       first_seen=?, last_updated=?, source_count=?, tags=?
                   WHERE id=?""",
                (
                    etype, path.stem, name, fm.get("status"), summary,
                    fm.get("first_seen"), fm.get("last_updated"),
                    source_count, fm.get("tags"), entity_id,
                ),
            )
        else:
            cur.execute(
                """INSERT INTO entities
                   (path, type, slug, name, status, summary,
                    first_seen, last_updated, source_count, tags)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    rel_path, etype, path.stem, name, fm.get("status"), summary,
                    fm.get("first_seen"), fm.get("last_updated"),
                    source_count, fm.get("tags"),
                ),
            )
            entity_id = cur.lastrowid

        # rewrite aliases + facts atomically
        cur.execute("DELETE FROM aliases WHERE entity_id=?", (entity_id,))
        for a in aliases:
            cur.execute(
                "INSERT OR IGNORE INTO aliases(entity_id, alias) VALUES (?,?)",
                (entity_id, a.lower()),
            )

        cur.execute(
            "DELETE FROM fts_facts WHERE rowid IN (SELECT id FROM facts WHERE entity_id=?)",
            (entity_id,),
        )
        cur.execute("DELETE FROM facts WHERE entity_id=?", (entity_id,))
        for fact_text, source, fact_date in _facts_from_body(body):
            cur.execute(
                "INSERT INTO facts(entity_id, text, source, fact_date) VALUES (?,?,?,?)",
                (entity_id, fact_text, source, fact_date),
            )
            fid = cur.lastrowid
            cur.execute(
                "INSERT INTO fts_facts(rowid, text, source) VALUES (?,?,?)",
                (fid, fact_text, source or ""),
            )

        # fts_entity is contentless (content=''), so a bare
        # `DELETE FROM ... WHERE rowid=?` raises "cannot DELETE from
        # contentless fts5 table". The FTS5-sanctioned drop is the
        # 'delete' command, which requires the exact previous column
        # values — and it MUST NOT be issued for a rowid that was never
        # inserted, or it corrupts the index ("database disk image is
        # malformed"). So delete only when this is an update of an
        # existing row.
        if is_update:
            cur.execute(
                "INSERT INTO fts_entity(fts_entity, rowid, name, aliases, summary) "
                "VALUES('delete', ?, ?, ?, ?)",
                (entity_id, prev_name, prev_aliases, prev_summary),
            )
        cur.execute(
            "INSERT INTO fts_entity(rowid, name, aliases, summary) VALUES (?,?,?,?)",
            (entity_id, name, " ".join(aliases), summary or ""),
        )
    return entity_id


def delete_entity_by_path(path: Path | str) -> None:
    rel_path = str(Path(path).relative_to(config.BRAIN_DIR)) if Path(path).is_absolute() else str(path)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM entities WHERE path=?", (rel_path,))
        row = cur.fetchone()
        if not row:
            return
        eid = row[0]
        cur.execute(
            "DELETE FROM fts_facts WHERE rowid IN (SELECT id FROM facts WHERE entity_id=?)",
            (eid,),
        )
        # Contentless FTS5 — must drop via the 'delete' command, not a
        # bare DELETE. We need the previous column values, so re-read
        # them from the entities row before we drop it below.
        prev = cur.execute(
            "SELECT name, COALESCE(summary,'') FROM entities WHERE id=?", (eid,)
        ).fetchone()
        if prev is not None:
            prev_name, prev_summary = prev
            aliases_row = cur.execute(
                "SELECT GROUP_CONCAT(alias, ' ') FROM aliases WHERE entity_id=?", (eid,)
            ).fetchone()
            prev_aliases = (aliases_row[0] if aliases_row and aliases_row[0] else "")
            cur.execute(
                "INSERT INTO fts_entity(fts_entity, rowid, name, aliases, summary) "
                "VALUES('delete', ?, ?, ?, ?)",
                (eid, prev_name, prev_aliases, prev_summary),
            )
        cur.execute("DELETE FROM facts WHERE entity_id=?", (eid,))
        cur.execute("DELETE FROM entities WHERE id=?", (eid,))


def rebuild() -> dict:
    """Wipe DB and rebuild from every entity markdown file on disk."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    counts = {"entities": 0, "facts": 0}
    config.ENTITY_TYPES.update(config._discover_entity_types())
    for type_dir in config.ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            if f.name.startswith("_"):
                continue
            upsert_entity_from_file(f)
            counts["entities"] += 1
    with connect() as conn:
        counts["facts"] = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    return counts


# ---------------------------------------------------------------------------
# Notes — second ingestion path, for any markdown file outside entities/
# ---------------------------------------------------------------------------

def upsert_note(rel_path: str, title: str, body: str, mtime: float, sha: str) -> int:
    """Insert or replace a single note row + its FTS shadow. Returns note id."""
    import time as _time

    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM notes WHERE path = ?", (rel_path,))
        row = cur.fetchone()
        if row:
            note_id = row[0]
            cur.execute(
                """UPDATE notes SET title=?, body=?, mtime=?, sha=?, last_indexed=?
                   WHERE id=?""",
                (title, body, mtime, sha, _time.time(), note_id),
            )
            cur.execute("DELETE FROM fts_notes WHERE rowid=?", (note_id,))
        else:
            cur.execute(
                """INSERT INTO notes(path, title, body, mtime, sha, last_indexed)
                   VALUES (?,?,?,?,?,?)""",
                (rel_path, title, body, mtime, sha, _time.time()),
            )
            note_id = cur.lastrowid
        cur.execute(
            "INSERT INTO fts_notes(rowid, title, body, path) VALUES (?,?,?,?)",
            (note_id, title, body, rel_path),
        )
    return note_id


def delete_note_by_path(rel_path: str) -> None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM notes WHERE path=?", (rel_path,))
        row = cur.fetchone()
        if not row:
            return
        nid = row[0]
        cur.execute("DELETE FROM fts_notes WHERE rowid=?", (nid,))
        cur.execute("DELETE FROM notes WHERE id=?", (nid,))


def list_note_ledger() -> dict[str, tuple[float, str]]:
    """Return {path: (mtime, sha)} for the diff walker."""
    with connect() as conn:
        rows = conn.execute("SELECT path, mtime, sha FROM notes").fetchall()
    return {p: (m, s) for p, m, s in rows}


def search_notes(query: str, k: int = 10) -> list[dict]:
    safe_q = _sanitize_fts(query)
    if not safe_q:
        return []
    sql = """
      SELECT n.title, n.path, n.body, n.mtime, bm25(fts_notes) AS score
      FROM fts_notes
      JOIN notes n ON n.id = fts_notes.rowid
      WHERE fts_notes MATCH ?
      ORDER BY score
      LIMIT ?
    """
    with connect() as conn:
        rows = conn.execute(sql, (safe_q, k)).fetchall()
    cols = ["title", "path", "body", "mtime", "score"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        # snippet: first ~200 chars of body so callers don't drown in long notes
        d["snippet"] = (d["body"] or "")[:200]
        out.append(d)
    return out


def canonical_fact_hash(fact_text: str) -> str:
    """Sha256 of normalised fact text — stable across re-extractions.

    Normalisation strips: leading dash, the `(source: …)` suffix, and
    surrounding whitespace, then lowercases. Two extractions of "Son in
    Long Xuyen" with different source-session ids hash to the same key,
    so provenance rows survive identical re-extractions.
    """
    import hashlib
    s = fact_text.strip()
    if s.startswith("- "):
        s = s[2:].strip()
    s = _SOURCE_RE.sub("", s).strip().lower()
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def record_fact_provenance(
    entity_path: Path | str,
    fact_text: str,
    note_paths: Iterable[str | Path],
) -> int:
    """Link `fact_text` to one or more source notes. Idempotent."""
    import time as _time

    paths_norm: list[str] = []
    for p in note_paths:
        pp = Path(p)
        if pp.is_absolute():
            try:
                pp = pp.relative_to(config.BRAIN_DIR)
            except ValueError:
                continue
        paths_norm.append(str(pp))
    if not paths_norm:
        return 0

    epath = entity_path
    if isinstance(epath, Path) and epath.is_absolute():
        try:
            epath = epath.relative_to(config.BRAIN_DIR)
        except ValueError:
            return 0
    epath = str(epath)

    fh = canonical_fact_hash(fact_text)
    now = _time.time()
    written = 0
    with connect() as conn:
        cur = conn.cursor()
        for np_ in paths_norm:
            cur.execute(
                """INSERT OR IGNORE INTO fact_provenance
                   (entity_path, fact_hash, note_path, recorded_at)
                   VALUES (?,?,?,?)""",
                (epath, fh, np_, now),
            )
            written += cur.rowcount
    return written


def facts_invalidated_by_note(
    note_path: str | Path,
) -> list[tuple[str, str]]:
    """Return [(entity_path, fact_hash), ...] sourced from `note_path`."""
    np_ = note_path
    if isinstance(np_, Path) and np_.is_absolute():
        try:
            np_ = np_.relative_to(config.BRAIN_DIR)
        except ValueError:
            return []
    np_ = str(np_)
    with connect() as conn:
        rows = conn.execute(
            "SELECT entity_path, fact_hash FROM fact_provenance WHERE note_path=?",
            (np_,),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def forget_note_provenance(note_path: str | Path) -> int:
    """Drop all provenance rows for `note_path`. Returns rows deleted."""
    np_ = note_path
    if isinstance(np_, Path) and np_.is_absolute():
        try:
            np_ = np_.relative_to(config.BRAIN_DIR)
        except ValueError:
            return 0
    np_ = str(np_)
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM fact_provenance WHERE note_path=?", (np_,)
        )
        return cur.rowcount


def search(query: str, k: int = 10, type: str | None = None) -> list[dict]:
    """BM25 fact search. Returns list of dicts joined to their entity."""
    safe_q = _sanitize_fts(query)
    if not safe_q:
        return []
    sql = """
      SELECT e.type, e.name, e.path, f.text, f.source, f.fact_date,
             bm25(fts_facts) AS score
      FROM fts_facts
      JOIN facts f ON f.id = fts_facts.rowid
      JOIN entities e ON e.id = f.entity_id
      WHERE fts_facts MATCH ?
    """
    args: list = [safe_q]
    if type:
        sql += " AND e.type = ?"
        args.append(type)
    sql += " ORDER BY score LIMIT ?"
    args.append(k)
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    cols = ["type", "name", "path", "text", "source", "date", "score"]
    return [dict(zip(cols, r)) for r in rows]


def search_entities(query: str, k: int = 10) -> list[dict]:
    safe_q = _sanitize_fts(query)
    if not safe_q:
        return []
    sql = """
      SELECT e.type, e.name, e.path, e.summary, bm25(fts_entity) AS score
      FROM fts_entity
      JOIN entities e ON e.id = fts_entity.rowid
      WHERE fts_entity MATCH ?
      ORDER BY score
      LIMIT ?
    """
    with connect() as conn:
        rows = conn.execute(sql, (safe_q, k)).fetchall()
    cols = ["type", "name", "path", "summary", "score"]
    return [dict(zip(cols, r)) for r in rows]


_FTS_SAFE = re.compile(r"\w+", re.UNICODE)


def _sanitize_fts(q: str) -> str:
    """FTS5 MATCH is picky about punctuation; reduce to OR'd word tokens.

    Uses Unicode `\\w` so non-ASCII queries (Vietnamese `Sơn`, Chinese
    `长安`, Spanish `años`) survive intact. The previous ASCII-only
    `[A-Za-z0-9_]+` chopped `Sơn` into `['S','n']`, returning garbage
    matches for any accented or non-Latin term.

    Each token is wrapped in double-quotes so FTS5 treats accented
    chars literally; otherwise its default tokenizer would still
    discard them at MATCH time.
    """
    tokens = _FTS_SAFE.findall(q)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def main():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild", help="rebuild DB from markdown")
    sp = sub.add_parser("search", help="BM25 fact search")
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=10)
    sp.add_argument("--type", default=None)
    se = sub.add_parser("entities", help="entity-name search")
    se.add_argument("query")
    se.add_argument("-k", type=int, default=10)
    sn = sub.add_parser("notes", help="note search (vault root files)")
    sn.add_argument("query")
    sn.add_argument("-k", type=int, default=10)
    args = p.parse_args()

    if args.cmd == "rebuild":
        counts = rebuild()
        print(f"rebuilt: {counts['entities']} entities, {counts['facts']} facts")
    elif args.cmd == "search":
        for r in search(args.query, k=args.k, type=args.type):
            print(f"[{r['type']}] {r['name']}  ({r['source'] or '-'}, {r['date'] or '-'})")
            print(f"  {r['text']}")
    elif args.cmd == "entities":
        for r in search_entities(args.query, k=args.k):
            print(f"[{r['type']}] {r['name']}  — {r['summary']}")
    elif args.cmd == "notes":
        for r in search_notes(args.query, k=args.k):
            print(f"[note] {r['title']}  ({r['path']})")
            print(f"  {r['snippet']}")


if __name__ == "__main__":
    main()
