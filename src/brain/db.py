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

import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

import brain.config as config

# DB_PATH is the source-of-truth handle for the active SQLite file.
# Two override mechanisms layer on top, both routed through `_db_path()`:
#   1. Tests that `monkeypatch.setattr(db, "DB_PATH", ...)` swap the
#      module attribute — `_db_path()` honours it when it differs from
#      the import-time default.
#   2. `rebuild()` pins to its scratch `.new` file via `DB_PATH = …`
#      for the rebuild window, then restores.
# Tests that only `monkeypatch.setattr(config, "BRAIN_DIR", tmp)` (without
# also patching DB_PATH) used to leak fixture rows into ~/.brain/.brain.db
# because the old `DB_PATH = config.BRAIN_DIR / ".brain.db"` froze the
# prod path at import time. `_db_path()` now resolves from config at call
# time when no explicit override is set (incident 2026-04-23).
DB_PATH = config.BRAIN_DIR / ".brain.db"
_IMPORT_TIME_DB_PATH = DB_PATH


def _db_path() -> Path:
    """Resolve the active SQLite path with the resolution order above."""
    if DB_PATH != _IMPORT_TIME_DB_PATH:
        return DB_PATH
    return config.BRAIN_DIR / ".brain.db"

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

-- Forget primitive (2026-04-23). Symmetric counterpart of extraction:
-- when a claim is retracted, a source note is deleted, or the user
-- explicitly forgets something, we write a tombstone keyed on the
-- canonical fact hash. The extractor consults this table before
-- promoting any claim, so re-mentioning a forgotten fact in a later
-- session does not resurrect it. Without this, retract was lossy —
-- strikethrough was only in-place on the entity file; the next
-- extraction from a fresh session would re-create the fact (incident
-- 2026-04-23, "Thuha ở Cần Thơ" kept returning after the note was
-- deleted).
--
-- `claim_key`: canonical_fact_hash(text) — normalised, case-folded,
--              source-stripped. Same key used by fact_provenance so the
--              extractor's dedupe and the tombstone check share a namespace.
-- `entity_type` / `entity_name`: optional scoping. NULL means "forget
--              this claim everywhere"; set means "forget only when
--              promoted under this entity" (rare; used when the same
--              text is valid for one entity and not another).
CREATE TABLE IF NOT EXISTS tombstones (
    id             INTEGER PRIMARY KEY,
    claim_key      TEXT NOT NULL,
    entity_type    TEXT,
    entity_name    TEXT,
    original_text  TEXT NOT NULL,
    reason         TEXT,
    created_at     REAL NOT NULL,
    created_by     TEXT,
    UNIQUE(claim_key, entity_type, entity_name)
);
CREATE INDEX IF NOT EXISTS tombstones_entity_idx
    ON tombstones(entity_type, entity_name);
CREATE INDEX IF NOT EXISTS tombstones_created_idx
    ON tombstones(created_at);

-- Reified fact substrate (WS6, 2026-04-23). Additive — `facts` remains
-- the legacy source of truth and the read path keeps reading it until
-- a later PR flips the FTS5 source + swaps `facts` → VIEW over
-- `fact_claims WHERE status IN ('current','superseded')`.
--
-- Populated today via:
--   1. dual-write from `upsert_entity_from_file` when BRAIN_USE_CLAIMS=1;
--   2. one-time `python -m brain.backfill_facts` over existing rows.
--
-- Columns are decomposed per ontologist spec (scratch/ontologist-ws6-schema.md):
-- structured subject / predicate / object, orthogonal (confidence,
-- risk_level, trust_source, salience), structured source, and
-- lifecycle state that is independent of the legacy `facts.status`.
--
-- risk_level vs trust_source (WS4 + ontologist merge):
--   risk_level   — pipeline integrity axis ('trusted'|'low'|'quarantined').
--                  Security's WS4 scrubber bumps this to 'low' on any
--                  flagged ingest; 'quarantined' reserved for explicit
--                  admin revocation (never auto-set).
--   trust_source — provenance axis ('user'|'note'|'extracted'|'correction').
--                  Who said it: the human vs an LLM pass vs a user
--                  correction. Orthogonal to risk_level: a user-authored
--                  fact that tripped an injection tripwire is
--                  trust_source='user' + risk_level='low'.
CREATE TABLE IF NOT EXISTS fact_claims (
    id                INTEGER PRIMARY KEY,

    -- subject (resolved entity)
    entity_id         INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    subject_slug      TEXT    NOT NULL,

    -- predicate (WS8 may reparse '_unparsed' rows via LLM)
    predicate         TEXT    NOT NULL,
    predicate_key     TEXT    NOT NULL,
    predicate_group   TEXT,

    -- object (one of object_entity / object_text required via CHECK)
    object_entity     INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    object_text       TEXT,
    object_slug       TEXT,
    object_type       TEXT    NOT NULL DEFAULT 'string',

    -- surface form (preserved verbatim for FTS + markdown round-trip)
    text              TEXT    NOT NULL,

    -- time
    fact_time         TEXT,
    observed_at       REAL    NOT NULL,

    -- provenance (WS4 scrub_tag links to sanitize ledger)
    source_kind       TEXT    NOT NULL,
    source_path       TEXT,
    source_sha        TEXT,
    scrub_tag         TEXT,
    episode_id        TEXT,

    -- epistemic
    confidence        REAL    NOT NULL DEFAULT 0.5,
    risk_level        TEXT    NOT NULL DEFAULT 'trusted',
    trust_source      TEXT    NOT NULL DEFAULT 'extracted',
    salience          REAL    NOT NULL DEFAULT 0.3,
    last_accessed     REAL,

    -- lifecycle
    kind              TEXT    NOT NULL DEFAULT 'episodic',
    status            TEXT    NOT NULL DEFAULT 'current',
    superseded_by     INTEGER REFERENCES fact_claims(id) ON DELETE SET NULL,
    superseded_at     TEXT,

    -- dedup — shares namespace with tombstones.claim_key
    claim_key         TEXT    NOT NULL,

    CHECK (object_entity IS NOT NULL OR object_text IS NOT NULL)
);

-- 9 indices per spec (scratch/ontologist-ws6-schema.md §1).
CREATE INDEX IF NOT EXISTS fact_claims_entity_idx
    ON fact_claims(entity_id);
CREATE INDEX IF NOT EXISTS fact_claims_entity_pred_idx
    ON fact_claims(entity_id, predicate_key);
CREATE INDEX IF NOT EXISTS fact_claims_claim_key_idx
    ON fact_claims(claim_key);
CREATE INDEX IF NOT EXISTS fact_claims_status_kind_idx
    ON fact_claims(status, kind);
CREATE INDEX IF NOT EXISTS fact_claims_episode_idx
    ON fact_claims(episode_id);
CREATE INDEX IF NOT EXISTS fact_claims_observed_idx
    ON fact_claims(observed_at);
CREATE INDEX IF NOT EXISTS fact_claims_salience_idx
    ON fact_claims(salience) WHERE status='current';
CREATE INDEX IF NOT EXISTS fact_claims_source_idx
    ON fact_claims(source_kind, source_path);
CREATE INDEX IF NOT EXISTS fact_claims_object_entity_idx
    ON fact_claims(object_entity) WHERE object_entity IS NOT NULL;
"""

_SOURCE_RE = re.compile(r"\(source:\s*([^,)]+?)(?:,\s*([\d-]+))?\s*\)")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column adds for vaults created before a column existed.

    SQLite's `ADD COLUMN IF NOT EXISTS` only landed in 3.35; we read
    `PRAGMA table_info` and ALTER manually to support older systems.
    Add new migrations here — one block per new column. Cheap enough
    to run on every connect.
    """
    def cols(table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}

    # 2026-04-21 — note→fact provenance: track which note sha we last
    # extracted so re-runs only LLM-call on changed notes.
    if "extracted_sha" not in cols("notes"):
        conn.execute("ALTER TABLE notes ADD COLUMN extracted_sha TEXT")

    # 2026-04-22 — fact supersession: when the LLM re-extracts a
    # contradictory fact (e.g. note says "Cần Thơ", session said
    # "Long Xuyên"), the older fact is not deleted — it is marked
    # `superseded` and rendered with `~~strikethrough~~` in the
    # entity markdown so the user still sees history at a glance
    # while the MCP read path only surfaces current facts.
    fact_cols = cols("facts")
    if "status" not in fact_cols:
        conn.execute("ALTER TABLE facts ADD COLUMN status TEXT")
    if "superseded_by" not in fact_cols:
        conn.execute("ALTER TABLE facts ADD COLUMN superseded_by INTEGER")
    if "superseded_at" not in fact_cols:
        conn.execute("ALTER TABLE facts ADD COLUMN superseded_at TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS facts_status_idx ON facts(status)")

    # 2026-04-22 — source integrity: store the note's sha256 at the
    # time a fact was extracted so verify.py can detect when a source
    # note has been edited (current sha != source_sha → potentially
    # stale) or deleted (no notes row → orphaned).
    if "source_sha" not in cols("fact_provenance"):
        conn.execute("ALTER TABLE fact_provenance ADD COLUMN source_sha TEXT")

    # 2026-04-23 — entity fs mtime tracking for read-time freshness.
    # Without this, a user editing an entity file directly (Obsidian,
    # manual vim, git pull) leaves the DB stale until some other path
    # triggers an upsert. brain_recall's freshness sweep walks the
    # entities dir, compares fs mtime to indexed_mtime, and reindexes
    # only what changed — ~1 stat per file, plus full upsert for the
    # handful that mutated.
    if "indexed_mtime" not in cols("entities"):
        conn.execute("ALTER TABLE entities ADD COLUMN indexed_mtime REAL")


@contextmanager
def connect():
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    _migrate(conn)
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


_STRIKE_RE = re.compile(r"^~~(.+?)~~\s*(.*)$")


def _facts_from_body(body: str) -> Iterable[tuple[str, str | None, str | None, str | None]]:
    """Yield (text, source, date, status) for each fact bullet.

    Strikethrough bullets (`- ~~…~~`) survive with status='superseded'
    so they remain inspectable via the audit surface but are filtered
    out of the FTS index (see upsert_entity_from_file) and the
    semantic vector store (see semantic.build). Live bullets yield
    status=None ("current").
    """
    for raw in body.split("\n"):
        line = raw.strip()
        if not line.startswith("- "):
            continue
        body_text = line[2:].strip()
        if not body_text:
            continue
        status: str | None = None
        if body_text.startswith("~~"):
            m_strike = _STRIKE_RE.match(body_text)
            if not m_strike:
                continue
            inner = m_strike.group(1).strip()
            trailing = m_strike.group(2).strip()
            body_text = f"{inner} {trailing}".strip() if trailing else inner
            status = "superseded"
        m = _SOURCE_RE.search(body_text)
        source = m.group(1).strip() if m else None
        date = m.group(2).strip() if m and m.group(2) else None
        cleaned = _SOURCE_RE.sub("", body_text).strip()
        if cleaned:
            yield cleaned, source, date, status


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
    try:
        file_mtime: float | None = path.stat().st_mtime
    except OSError:
        file_mtime = None

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
                       first_seen=?, last_updated=?, source_count=?, tags=?,
                       indexed_mtime=?
                   WHERE id=?""",
                (
                    etype, path.stem, name, fm.get("status"), summary,
                    fm.get("first_seen"), fm.get("last_updated"),
                    source_count, fm.get("tags"), file_mtime, entity_id,
                ),
            )
        else:
            cur.execute(
                """INSERT INTO entities
                   (path, type, slug, name, status, summary,
                    first_seen, last_updated, source_count, tags, indexed_mtime)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rel_path, etype, path.stem, name, fm.get("status"), summary,
                    fm.get("first_seen"), fm.get("last_updated"),
                    source_count, fm.get("tags"), file_mtime,
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
        # Dual-write (WS6): when BRAIN_USE_CLAIMS=1 we also rebuild the
        # fact_claims rows for this entity. Kept symmetric with `facts`
        # — delete-then-insert so a re-upserted entity file doesn't
        # accumulate stale claim rows. Flag-off default: zero cost.
        claims_on = use_claims_enabled()
        if claims_on:
            cur.execute("DELETE FROM fact_claims WHERE entity_id=?", (entity_id,))
        for fact_text, source, fact_date, status in _facts_from_body(body):
            cur.execute(
                "INSERT INTO facts(entity_id, text, source, fact_date, status) "
                "VALUES (?,?,?,?,?)",
                (entity_id, fact_text, source, fact_date, status),
            )
            fid = cur.lastrowid
            # Superseded facts stay in the `facts` table (audit trail)
            # but do NOT enter the FTS index — keeps `brain_recall` /
            # `db.search` returning only current facts.
            if status != "superseded":
                cur.execute(
                    "INSERT INTO fts_facts(rowid, text, source) VALUES (?,?,?)",
                    (fid, fact_text, source or ""),
                )
            if claims_on:
                try:
                    _insert_fact_claim(
                        conn,
                        entity_id=entity_id,
                        subject_slug=path.stem,
                        text=fact_text,
                        source=source,
                        fact_date=fact_date,
                        status=status,
                        # Live extractions flow through WS4's sanitize
                        # call sites before they reach the markdown,
                        # so the dual-write path emits 'ws4' as the
                        # scrub_tag. Backfilled rows (no ws4 scrubber
                        # in their history) carry 'pre-ws4' instead.
                        scrub_tag="ws4",
                    )
                except Exception:
                    # Dual-write is best-effort — never take down the
                    # primary `facts` insert because of a claims-side
                    # bug. A missing claim row will be reconciled by
                    # the backfill CLI if the user runs it.
                    pass

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

    # Proactive `.vec` invalidation (WS3 follow-up, 2026-04-23). The
    # DB rewrite above deleted + re-inserted every fact for this
    # entity; old facts.id values are gone but their embeddings still
    # live in `.vec/facts.{npy,json}`. Proactive pop removes them now
    # so the next `ensure_built()` re-embeds from the current DB rows.
    # Best-effort: any failure stays a no-op — the serve-time guard
    # in `semantic.search_facts` is the correctness authority.
    try:
        from brain import semantic
        semantic.invalidate_for(etype, path.stem)
    except Exception:
        pass
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
    """Wipe DB and rebuild from every entity markdown file on disk.

    Atomic: the rebuild is performed against a sibling `<name>.new` file.
    Only after every entity has been upserted do we `os.replace` the
    `.new` file over `DB_PATH`. A crash mid-rebuild leaves the stale-but-
    consistent original in place plus an abandoned `.new` sibling — the
    next successful rebuild will overwrite that temp, so no manual
    cleanup is required. Previously the non-atomic `unlink(); upsert`
    sequence could leave a partial index that looked "built" to callers
    (meta file present, rows missing), which is exactly the silent-data-
    loss surface Storage clause 3 prohibits.
    """
    import os as _os

    global DB_PATH
    orig_path = _db_path()
    new_path = orig_path.parent / (orig_path.name + ".new")
    # Remove any stale `.new` left by a previous crashed rebuild so
    # `connect()` starts from a clean slate.
    if new_path.exists():
        new_path.unlink()

    counts = {"entities": 0, "facts": 0}
    # Redirect every `connect()` call below at the scratch path for the
    # duration of the rebuild. `_db_path()` honours an explicit DB_PATH
    # override, so this swap reaches all existing call sites
    # (`upsert_entity_from_file`, the final count query) with zero changes.
    DB_PATH = new_path
    try:
        config.ENTITY_TYPES.update(config._discover_entity_types())
        for type_dir in config.ENTITY_TYPES.values():
            if not type_dir.exists():
                continue
            for f in type_dir.glob("*.md"):
                if f.name.startswith("_"):
                    continue
                upsert_entity_from_file(f)
                counts["entities"] += 1
        # Backfill supersession: after all entity files have been
        # upserted, collapse contradictions (note > session, newer
        # wins) so a fresh rebuild brings stale vaults into the new
        # model in one pass. Import lazily to avoid a circular import
        # during package load.
        try:
            from brain.supersede import recompute_all as _recompute_all
            sup = _recompute_all()
            counts["superseded"] = sup.get("facts_superseded", 0)
        except Exception as _e:
            counts["superseded"] = 0
        with connect() as conn:
            counts["facts"] = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    except BaseException:
        # Best-effort scratch cleanup — don't mask the underlying error.
        try:
            new_path.unlink()
        except FileNotFoundError:
            pass
        DB_PATH = orig_path
        raise

    # Swap the freshly-built DB over the original in one atomic call.
    # SQLite WAL sidecars (-wal, -shm) are ephemeral and must be removed
    # for both the original (stale after replace) and the temp .new file
    # (dangling after replace). Leaving either set causes the next
    # connect() to see a mismatched WAL and read 0 rows from a valid DB.
    _os.replace(new_path, orig_path)
    for sidecar in (
        orig_path.with_name(orig_path.name + "-wal"),
        orig_path.with_name(orig_path.name + "-shm"),
        new_path.with_name(new_path.name + "-wal"),
        new_path.with_name(new_path.name + "-shm"),
    ):
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass
    DB_PATH = orig_path
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


def pending_note_extractions(
    limit: int = 50,
    min_body_chars: int = 0,
    exclude_prefixes: tuple[str, ...] = (),
    exclude_paths: tuple[str, ...] = (),
) -> list[dict]:
    """Notes whose current sha hasn't been processed by note_extract yet.

    Returned dicts carry everything the extractor needs (path, title,
    body, sha) so the caller doesn't re-read the file. Ordered by
    mtime DESC so newly-typed notes get priority over years-old ones
    when the queue is long.

    `exclude_prefixes` / `exclude_paths` filter out machine-managed
    notes that happen to live in the FTS index but shouldn't be sent
    to the extractor (playground/, timeline/, log.md, etc.). Filtered
    in SQL so we never read those bodies into Python.
    """
    where = ["(extracted_sha IS NULL OR extracted_sha != sha)"]
    params: list = []
    for prefix in exclude_prefixes:
        where.append("path NOT LIKE ?")
        params.append(prefix.rstrip("/") + "/%")
    for p in exclude_paths:
        where.append("path != ?")
        params.append(p)
    sql = (
        "SELECT path, title, body, sha, mtime, extracted_sha FROM notes "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY mtime DESC LIMIT ?"
    )
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[dict] = []
    for path, title, body, sha, mtime, prev_sha in rows:
        if min_body_chars and len(body or "") < min_body_chars:
            continue
        out.append({
            "path": path, "title": title, "body": body or "",
            "sha": sha, "mtime": mtime,
            "extracted_sha": prev_sha,  # None = first extraction; non-None = edit
        })
    return out


def mark_note_extracted(rel_path: str, sha: str) -> None:
    """Record that we've processed `rel_path` at this `sha`."""
    with connect() as conn:
        conn.execute(
            "UPDATE notes SET extracted_sha=? WHERE path=?", (sha, rel_path)
        )


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


# -- fact_claims (WS6) dual-write helpers ---------------------------------
#
# Kept small + lazy so the flag-off path pays nothing: environment
# check is a single `os.environ.get`, supersede is imported only when
# we actually write a claim. supersede imports db at module load —
# importing it eagerly at the top of db.py would form a cycle, so
# every helper below imports it in the hot path.

_SOURCE_PREFIX_NOTE = "note:"
_SOURCE_PREFIX_SESSION = "session-"


def _parse_source(source: str | None) -> tuple[str, str | None, str | None]:
    """Split a legacy `source` string into (source_kind, source_path,
    episode_id). Mirrors ontologist spec §2 step 3.
    """
    if not source:
        return ("import", None, None)
    s = source.strip()
    if s.startswith(_SOURCE_PREFIX_NOTE):
        path = s[len(_SOURCE_PREFIX_NOTE):]
        return ("note", path or None, path or None)
    if s.startswith(_SOURCE_PREFIX_SESSION):
        return ("session", s, s)
    if s == "user":
        return ("user", None, None)
    if s.startswith("correction"):
        return ("correction", s, None)
    return ("import", s, None)


def _classify_predicate(text: str) -> tuple[str, str | None]:
    """Return (canonical_predicate, predicate_group).

    Falls through to `'_unparsed'` when the 3-regex classifier in
    `supersede` doesn't hit — WS8 can later reparse those rows with
    an LLM pass. Keeping the v1 classifier lightweight avoids an
    LLM call on every dual-write.
    """
    try:
        from brain import supersede
    except Exception:
        return ("_unparsed", None)
    try:
        group = supersede.classify_predicate(text or "")
    except Exception:
        group = None
    if group == "location":
        return ("locatedIn", "location")
    if group == "employer":
        return ("worksAt", "employer")
    if group == "role":
        return ("hasRole", "role")
    return ("_unparsed", None)


def _norm_predicate_key(predicate: str) -> str:
    """Casefold + strip separators so 'locatedIn', 'located_in',
    'LOCATED-IN' all share an index key. Mirrors
    predicate_registry._norm with one exception: the reserved
    sentinel `'_unparsed'` is preserved verbatim so it stays
    distinguishable from a real predicate literally spelled
    'unparsed'."""
    if predicate == "_unparsed":
        return "_unparsed"
    return "".join(c for c in (predicate or "").lower() if c.isalnum()) or "_unparsed"


def _claim_key(subject_slug: str, predicate_key: str,
               object_slug: str | None, object_text: str | None) -> str:
    """Deterministic dedup key. `object_hash` uses the canonical
    normalisation when we don't have a resolved object entity."""
    obj = object_slug or canonical_fact_hash(object_text or "")
    return canonical_fact_hash(f"{subject_slug}|{predicate_key}|{obj}")


def _extract_object_phrase(text: str, group: str | None) -> str:
    """Cheap object-phrase extraction used during dual-write /
    backfill. WS6 step 1-3 is explicitly additive — a perfect
    object parser is WS8 territory."""
    import re
    cleaned = _SOURCE_RE.sub("", (text or "")).strip()
    if not group:
        return cleaned
    # For the three known groups, strip the predicate phrase so the
    # object_text holds just the tail (object phrase). Patterns mirror
    # supersede._PREDICATE_GROUPS but as prefix-trimmers.
    for pat in (
        r"^.*?\b(currently\s+(?:in|at)|located\s+in|is\s+(?:in|at|located)|"
        r"lives\s+in|based\s+in|đang\s+ở|hiện\s+ở|ở\s+tại)\b\s*",
        r"^.*?\b(works\s+at|employed\s+by|is\s+employed\s+at|"
        r"làm\s+(?:việc\s+)?(?:ở|tại|cho))\b\s*",
        r"^.*?\b(role\s+is|title\s+is|position\s+is|is\s+a\s+|serves\s+as|"
        r"chức\s+vụ|vị\s+trí)\b\s*",
    ):
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            return cleaned[m.end():].strip(" .,-:;")
    return cleaned


def use_claims_enabled() -> bool:
    """Read `BRAIN_USE_CLAIMS` each call so tests can flip it with
    `monkeypatch.setenv` without reloading the module."""
    return os.environ.get("BRAIN_USE_CLAIMS", "0") == "1"


def _insert_fact_claim(
    conn: sqlite3.Connection,
    *,
    entity_id: int,
    subject_slug: str,
    text: str,
    source: str | None,
    fact_date: str | None,
    status: str | None,
    scrub_tag: str | None = None,
) -> int | None:
    """Dual-write one fact_claims row. Returns the new id or None on
    skip. Caller owns the transaction + must have passed the
    `use_claims_enabled()` gate — this helper trusts its inputs and
    inserts unconditionally."""
    if not text:
        return None

    predicate, predicate_group = _classify_predicate(text)
    predicate_key = _norm_predicate_key(predicate)

    object_phrase = _extract_object_phrase(text, predicate_group)
    object_slug: str | None = None
    object_entity: int | None = None
    object_type = "string"
    if object_phrase:
        row = conn.execute(
            "SELECT id, slug FROM entities "
            "WHERE lower(name)=lower(?) OR slug=? LIMIT 1",
            (object_phrase, object_phrase.lower().replace(" ", "-")),
        ).fetchone()
        if row:
            object_entity, object_slug = row[0], row[1]
            object_type = "entity"
    object_text = object_phrase or None if object_entity is None else None

    source_kind, source_path, episode_id = _parse_source(source)
    trust_source = (
        "user"       if source_kind == "user"       else
        "note"       if source_kind == "note"       else
        "correction" if source_kind == "correction" else
        "extracted"
    )

    lifecycle_status = "superseded" if status == "superseded" else "current"
    claim_key = _claim_key(subject_slug, predicate_key, object_slug, object_text or text)

    observed_at = time.time()

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
            source_kind, source_path, None, scrub_tag, episode_id,
            0.5, "trusted", trust_source, 0.3,
            "episodic", lifecycle_status, claim_key,
        ),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


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
    source_sha: str | None = None,
) -> int:
    """Link `fact_text` to one or more source notes. Idempotent.

    `source_sha` is the sha256 of the source note at extraction time.
    Stored alongside the provenance row so verify.py can later detect
    whether the note has changed (stale) or been deleted (orphaned).
    """
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
                   (entity_path, fact_hash, note_path, recorded_at, source_sha)
                   VALUES (?,?,?,?,?)""",
                (epath, fh, np_, now, source_sha),
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


def add_tombstone(
    fact_text: str,
    *,
    entity_type: str | None = None,
    entity_name: str | None = None,
    reason: str | None = None,
    created_by: str = "user",
) -> bool:
    """Record a forget-intent for `fact_text`. Returns True if newly added.

    The canonical fact hash is used as the dedupe key so repeated forget
    calls on equivalent phrasings (mixed case, trailing source suffix,
    whitespace noise) collapse into one row. Scope with
    `entity_type`+`entity_name` to forget a claim only under one entity;
    leave both None to block globally.
    """
    import time as _time

    text = (fact_text or "").strip()
    if not text:
        return False
    key = canonical_fact_hash(text)
    # SQLite's UNIQUE treats NULL != NULL, so two global tombstones
    # with the same claim_key but both scopes NULL would both insert.
    # Use empty string as the "unscoped" sentinel so UNIQUE dedupes.
    etype = (entity_type or "").strip().lower()
    ename = (entity_name or "").strip()
    with connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO tombstones
               (claim_key, entity_type, entity_name, original_text,
                reason, created_at, created_by)
               VALUES (?,?,?,?,?,?,?)""",
            (key, etype, ename, text, reason, _time.time(), created_by),
        )
        return cur.rowcount > 0


def is_forgotten(
    fact_text: str,
    *,
    entity_type: str | None = None,
    entity_name: str | None = None,
) -> bool:
    """Would `fact_text` be blocked by a tombstone under this entity scope?

    Matches when a tombstone with the same canonical hash exists AND
    its scope is (a) global (entity_type/name both NULL) OR
    (b) scoped to the same entity_type+entity_name as the caller.
    Broader scopes win: a global tombstone blocks promotion under any
    entity; an entity-scoped tombstone only blocks its own entity.
    """
    text = (fact_text or "").strip()
    if not text:
        return False
    key = canonical_fact_hash(text)
    etype = (entity_type or "").strip().lower()
    ename = (entity_name or "").strip()
    with connect() as conn:
        rows = conn.execute(
            "SELECT entity_type, entity_name FROM tombstones WHERE claim_key=?",
            (key,),
        ).fetchall()
    for t_etype, t_ename in rows:
        te = t_etype or ""
        tn = t_ename or ""
        if te == "" and tn == "":
            return True  # global tombstone blocks any scope
        if te == etype and tn == ename:
            return True  # exact-scope match
    return False


def list_tombstones(limit: int = 50) -> list[dict]:
    """Return tombstones newest-first for audit / admin."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, claim_key, entity_type, entity_name,
                      original_text, reason, created_at, created_by
               FROM tombstones ORDER BY created_at DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    cols = ["id", "claim_key", "entity_type", "entity_name",
            "original_text", "reason", "created_at", "created_by"]
    return [dict(zip(cols, r)) for r in rows]


def remove_tombstone(
    fact_text: str,
    *,
    entity_type: str | None = None,
    entity_name: str | None = None,
) -> int:
    """Lift a tombstone (user changed their mind). Returns rows deleted."""
    text = (fact_text or "").strip()
    if not text:
        return 0
    key = canonical_fact_hash(text)
    etype = (entity_type or "").strip().lower()
    ename = (entity_name or "").strip()
    with connect() as conn:
        cur = conn.execute(
            """DELETE FROM tombstones
               WHERE claim_key=?
                 AND COALESCE(entity_type, '') = ?
                 AND COALESCE(entity_name, '') = ?""",
            (key, etype, ename),
        )
        return cur.rowcount


def gc_orphaned_entities() -> list[str]:
    """Remove DB + FTS index entries for entity files deleted from disk.

    Called at the start of auto_clean and verify passes so phantom
    entries left by manual deletes or auto_clean don't pollute recall.
    Returns vault-relative paths of the removed entries.
    """
    with connect() as conn:
        rows = conn.execute("SELECT path FROM entities").fetchall()

    removed: list[str] = []
    for (rel_path,) in rows:
        abs_path = config.BRAIN_DIR / rel_path
        if not abs_path.exists():
            delete_entity_by_path(rel_path)
            removed.append(rel_path)
    return removed


def index_untracked_entities() -> list[str]:
    """Upsert entity files that exist on disk but are missing from the DB index.

    The inverse of gc_orphaned_entities. Happens when entity files are
    created outside the normal extraction path or when the DB is rebuilt
    from a stale snapshot that predates recent files.
    Returns vault-relative paths of the newly-indexed entries.
    """
    with connect() as conn:
        indexed = {row[0] for row in conn.execute("SELECT path FROM entities").fetchall()}

    added: list[str] = []
    for type_dir in config.ENTITY_TYPES.values():
        if not type_dir.exists():
            continue
        for f in sorted(type_dir.glob("*.md")):
            if f.name.startswith("_"):
                continue
            try:
                rel = str(f.relative_to(config.BRAIN_DIR))
            except ValueError:
                continue
            if rel not in indexed:
                try:
                    upsert_entity_from_file(f)
                    added.append(rel)
                except Exception:
                    continue
    return added


def sync_mutated_entities() -> list[str]:
    """Re-upsert entity files whose fs mtime is newer than `indexed_mtime`.

    Read-time freshness primitive. Without this, a user editing an
    entity markdown directly (Obsidian, vim, git pull) leaves the DB
    stale until some other path triggers an upsert — so brain_recall
    could return the old pre-edit text for hours. One `stat()` per
    entity plus a full upsert for the handful that changed.

    Returns vault-relative paths of the re-indexed files.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT path, indexed_mtime FROM entities"
        ).fetchall()

    changed: list[str] = []
    for rel_path, indexed_mtime in rows:
        abs_path = config.BRAIN_DIR / rel_path
        try:
            cur_mtime = abs_path.stat().st_mtime
        except OSError:
            # Missing file — leave to gc_orphaned_entities which runs
            # in the same freshness pass and knows how to remove it.
            continue
        if indexed_mtime is None or cur_mtime > float(indexed_mtime) + 1e-3:
            try:
                upsert_entity_from_file(abs_path)
                changed.append(rel_path)
            except Exception:
                continue
    return changed


def find_stale_provenance() -> list[dict]:
    """Return provenance rows whose source note has changed or been deleted.

    Only rows with a recorded `source_sha` are considered — older rows
    (NULL source_sha, pre-migration) are skipped since we have no baseline.

    Returns list of dicts with keys:
      entity_path, fact_hash, note_path, source_sha, current_sha, status
    where `status` is 'orphaned' (note gone) or 'stale' (note edited).
    """
    with connect() as conn:
        rows = conn.execute("""
            SELECT fp.entity_path, fp.fact_hash, fp.note_path,
                   fp.source_sha, n.sha AS current_sha
            FROM fact_provenance fp
            LEFT JOIN notes n ON n.path = fp.note_path
            WHERE fp.source_sha IS NOT NULL
              AND (n.sha IS NULL OR n.sha != fp.source_sha)
        """).fetchall()

    result = []
    for entity_path, fact_hash, note_path, source_sha, current_sha in rows:
        result.append({
            "entity_path": entity_path,
            "fact_hash": fact_hash,
            "note_path": note_path,
            "source_sha": source_sha,
            "current_sha": current_sha,
            "status": "orphaned" if current_sha is None else "stale",
        })
    return result


def get_entity_summaries(keys: list[tuple]) -> dict:
    """Batch-fetch summaries for a list of (type, name) pairs.

    Returns {(type, name): summary} for rows that have a non-empty summary.
    Designed for post-processing search results without N individual queries.
    """
    if not keys:
        return {}
    where = " OR ".join("(type=? AND name=?)" for _ in keys)
    flat = [v for t, n in keys for v in (t, n)]
    with connect() as conn:
        rows = conn.execute(
            f"SELECT type, name, summary FROM entities WHERE {where}",
            flat,
        ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows if r[2]}


def search(
    query: str,
    k: int = 10,
    type: str | None = None,
    *,
    include_superseded: bool = False,
) -> list[dict]:
    """BM25 fact search. Returns list of dicts joined to their entity.

    Superseded facts (those that got contradicted by a newer
    extraction) are filtered out by default — the semantic branch
    already excludes them in `semantic.build()`, and leaving them in
    the BM25 branch creates a recall-accuracy bug where an obsolete
    fact ("Son lives in Long Xuyen") can outrank the current one
    ("Son lives in Can Tho") on a BM25-heavy query.

    Pass `include_superseded=True` only for history/audit lookups
    where the obsolete rows are the point (e.g. `brain_history`).
    """
    safe_q = _sanitize_fts(query)
    if not safe_q:
        return []
    sql = """
      SELECT e.type, e.name, e.slug, e.path, f.text, f.source, f.fact_date, f.status,
             bm25(fts_facts) AS score
      FROM fts_facts
      JOIN facts f ON f.id = fts_facts.rowid
      JOIN entities e ON e.id = f.entity_id
      WHERE fts_facts MATCH ?
    """
    args: list = [safe_q]
    if not include_superseded:
        sql += " AND (f.status IS NULL OR f.status != 'superseded')"
    if type:
        sql += " AND e.type = ?"
        args.append(type)
    sql += " ORDER BY score LIMIT ?"
    args.append(k)
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    cols = ["type", "name", "slug", "path", "text", "source", "date", "status", "score"]
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
