"""Semantic recall layer for the brain.

Why numpy-and-not-sqlite-vec: pyenv's Python ships without
`enable_load_extension`, so the loadable-extension path is closed unless we
recompile Python. With ~3K facts and ~900 entities the corpus is tiny —
brute-force cosine on a (3000, 384) matrix is <5 ms, well under the 50 ms
retrieval budget. When the corpus crosses ~50K rows we should swap in
hnswlib or rebuild Python with extensions; the public interface here will
stay the same.

Storage layout (under ~/.brain/.vec/):
  facts.npy      float32 [N, D] L2-normalised
  facts.json     [{rowid, text, source, entity_id, type, name, slug}]
  entities.npy   float32 [M, D] L2-normalised  (name + summary embedding)
  entities.json  [{id, type, name, slug, path, summary}]
  meta.json      {model, dim, built_at, fact_count, entity_count}

Public API:
  build()                       — full reindex from brain.db (idempotent)
  search_facts(query, k=8)      — semantic fact search
  search_entities(query, k=8)   — semantic entity search
  hybrid_search(query, k=8)     — RRF fusion of BM25 (db.search) + dense
  ensure_built()                — build only if missing/stale
  status()                      — counts + freshness, for the CLI
"""
from __future__ import annotations

import io as _io
import json
import os
import socket
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import numpy as np

import brain.config as config
from brain import db
from brain.io import atomic_write_bytes, atomic_write_text

VEC_DIR = config.BRAIN_DIR / ".vec"
FACTS_NPY = VEC_DIR / "facts.npy"
FACTS_JSON = VEC_DIR / "facts.json"
ENT_NPY = VEC_DIR / "entities.npy"
ENT_JSON = VEC_DIR / "entities.json"
NOTES_NPY = VEC_DIR / "notes.npy"
NOTES_JSON = VEC_DIR / "notes.json"
META_JSON = VEC_DIR / "meta.json"

# Multilingual by default — Vietnamese / Spanish / Chinese queries return real
# semantic matches, not just lucky-keyword hits. Same 384-d output as the
# English-only MiniLM, so the .npy layout is unchanged. ~120 MB, ~6 ms/query.
# Override with BRAIN_EMBED_MODEL=all-MiniLM-L6-v2 for English-only / smaller.
DEFAULT_MODEL = os.environ.get(
    "BRAIN_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
)
DIM = 384

_model = None  # lazy-loaded sentence-transformers model
_model_lock = __import__("threading").Lock()


def _get_model():
    """Load the embedding model. Thread-safe: a background warmup thread
    and the first real query may race; the lock makes sure we pay the
    ~7 s torch-import + weights-load exactly once."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(DEFAULT_MODEL)
    return _model


def _embed(texts: list[str], batch_size: int = 64) -> np.ndarray:
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    model = _get_model()
    arr = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype(np.float32)
    return arr


def _ensure_dir():
    VEC_DIR.mkdir(parents=True, exist_ok=True)


def _current_scrub_tag() -> str:
    """Return the active sanitize ruleset version string.

    Lazy import so a stripped environment (tests without sanitize
    wired, or a future scrubber-less subset of the codebase) falls
    back to a sentinel rather than crashing the builder. Sentinel
    cannot match `sanitize.VERSION` so it would force a rebuild on
    the next tick if the scrubber is re-enabled — safer than assuming
    "no scrubber == scrubbed".
    """
    try:
        from brain.sanitize import VERSION as _v
        return _v
    except Exception:
        return "no-scrubber"


def _meta_scrub_tag() -> str | None:
    """Return the scrub_tag recorded in `.vec/meta.json`, or None."""
    if not META_JSON.exists():
        return None
    try:
        meta = json.loads(META_JSON.read_text())
    except Exception:
        return None
    tag = meta.get("scrub_tag")
    return tag if isinstance(tag, str) else None


def _stamp_scrub_tag_in_place(tag: str) -> None:
    """Write `scrub_tag = tag` into an existing `.vec/meta.json`
    without rebuilding the index. Used for the one-shot migration
    when the bundle predates this field — we don't know whether the
    embedded content is actually older than the current scrubber, so
    we adopt the current tag rather than force rebuild.
    """
    if not META_JSON.exists():
        return
    try:
        meta = json.loads(META_JSON.read_text())
    except Exception:
        return
    meta["scrub_tag"] = tag
    atomic_write_text(META_JSON, json.dumps(meta, indent=2))


def _audit_scrub_version_init(*, new_tag: str) -> None:
    """Record the one-shot migration that stamps an existing `.vec`
    bundle with its first scrub_tag. Distinct op from
    `scrub_version_bump_reingest` because no re-embedding happened —
    an incident responder looking at the ledger needs to tell
    "migrated unknown" apart from "redid because ruleset bumped".
    """
    try:
        from brain import _audit_ledger
        _audit_ledger.append(
            "scrub_version_init",
            {"new_scrub_tag": new_tag},
            actor="semantic.ensure_built",
        )
    except Exception:
        pass


def _audit_scrub_version_bump(
    *, old_tag: str | None, new_tag: str, reingested: int
) -> None:
    """Record a scrub-version forced re-ingest in the WS5 ledger.

    Fired exactly once per detected bump (never on every tick). Payload:
    * `old_scrub_tag` — what the `.vec` bundle was stamped with (may
      be null if the bundle predates the field).
    * `new_scrub_tag` — `sanitize.VERSION` at the moment of rebuild.
    * `reingested` — count of fact rows re-embedded.

    Counter-only, no raw content. Best-effort: import/disk failures
    are swallowed so the rebuild path is never blocked by audit.
    """
    try:
        from brain import _audit_ledger
        _audit_ledger.append(
            "scrub_version_bump_reingest",
            {
                "old_scrub_tag": old_tag or "",
                "new_scrub_tag": new_tag,
                "reingested": int(reingested),
            },
            actor="semantic.ensure_built",
        )
    except Exception:
        pass


def _atomic_save_npy(path: Path, arr: np.ndarray) -> None:
    """Atomic `np.save` equivalent.

    `np.save(tmp, arr)` would silently rewrite the name to `<tmp>.npy`
    because numpy auto-appends the `.npy` extension when it's missing
    from the target. Serialise through a BytesIO buffer first, then let
    `atomic_write_bytes` own the temp-file + rename dance — that way the
    final name is exactly whatever we asked for.
    """
    buf = _io.BytesIO()
    np.save(buf, arr, allow_pickle=False)
    atomic_write_bytes(path, buf.getvalue())


def build() -> dict:
    """Full reindex from the SQLite mirror."""
    _ensure_dir()
    t0 = time.time()

    with db.connect() as conn:
        fact_rows = conn.execute(
            """
            SELECT f.id, f.text, f.source, f.entity_id,
                   e.type, e.name, e.slug, f.fact_date, e.path
            FROM facts f
            JOIN entities e ON e.id = f.entity_id
            WHERE f.status IS NULL OR f.status != 'superseded'
            """
        ).fetchall()

        ent_rows = conn.execute(
            """
            SELECT id, type, name, slug, path, COALESCE(summary, '')
            FROM entities
            """
        ).fetchall()

    fact_meta = [
        {
            "rowid": r[0],
            "text": r[1],
            # `fact_hash` pins the embedded text to the exact bytes that
            # produced its vector. `search_facts` compares this against
            # the current DB text at query time and drops any hit whose
            # hash drifted — closes the "stale fact confidently served"
            # class from the 2026-04-23 Thuha-in-Cần-Thơ incident.
            "fact_hash": db.canonical_fact_hash(r[1] or ""),
            "source": r[2],
            "entity_id": r[3],
            "type": r[4],
            "name": r[5],
            "slug": r[6],
            # date + path carried through so hybrid_search's recency factor
            # and path-penalty apply to semantic-only hits too. Without these,
            # a fact found only via cosine skipped both adjustments.
            "date": r[7],
            "path": r[8],
        }
        for r in fact_rows
    ]
    ent_meta = [
        {
            "id": r[0],
            "type": r[1],
            "name": r[2],
            "slug": r[3],
            "path": r[4],
            "summary": r[5],
        }
        for r in ent_rows
    ]

    fact_texts = [f"[{m['type']}/{m['name']}] {m['text']}" for m in fact_meta]
    # Entity embedding = name + summary (so a name-only query still hits).
    ent_texts = [
        f"{m['type']}: {m['name']}. {m['summary']}".strip() for m in ent_meta
    ]

    fact_vecs = _embed(fact_texts)
    ent_vecs = _embed(ent_texts)

    _atomic_save_npy(FACTS_NPY, fact_vecs)
    _atomic_save_npy(ENT_NPY, ent_vecs)
    atomic_write_text(FACTS_JSON, json.dumps(fact_meta))
    atomic_write_text(ENT_JSON, json.dumps(ent_meta))

    # Notes — second corpus, indexed alongside facts/entities.
    note_count = _build_notes_full()

    atomic_write_text(
        META_JSON,
        json.dumps(
            {
                "model": DEFAULT_MODEL,
                "dim": DIM,
                "built_at": time.time(),
                "fact_count": len(fact_meta),
                "entity_count": len(ent_meta),
                "note_count": note_count,
                # High-water marks so `_incremental_update_facts_entities`
                # can cheaply detect rows added since this build and
                # append-embed them instead of forcing a full rebuild.
                # Indexing lag was the 0–6 hour blindness window before.
                "fact_max_id": max((m["rowid"] for m in fact_meta), default=0),
                "entity_max_id": max((m["id"] for m in ent_meta), default=0),
                "build_seconds": round(time.time() - t0, 2),
                # WS4 scrub-version cross-reference. The embedded text
                # was produced by this ruleset; `ensure_built()`
                # compares against `sanitize.VERSION` at read time and
                # forces a full re-embed on mismatch so a scrubber
                # upgrade can't leave pre-upgrade content in the index.
                "scrub_tag": _current_scrub_tag(),
            },
            indent=2,
        ),
    )

    return {
        "facts": len(fact_meta),
        "entities": len(ent_meta),
        "notes": note_count,
        "elapsed": round(time.time() - t0, 2),
    }


def _db_maxes() -> tuple[int, int]:
    """(max(facts.id), max(entities.id)) — cheap freshness probe."""
    with db.connect() as conn:
        fm = conn.execute("SELECT COALESCE(MAX(id), 0) FROM facts").fetchone()[0]
        em = conn.execute("SELECT COALESCE(MAX(id), 0) FROM entities").fetchone()[0]
    return int(fm or 0), int(em or 0)


def _has_new_rows() -> bool:
    """True iff the DB has facts/entities with id above the last build's
    recorded max. Returns True if metadata is missing/unparseable so
    callers trigger a safe full rebuild."""
    if not META_JSON.exists():
        return True
    try:
        meta = json.loads(META_JSON.read_text())
    except Exception:
        return True
    fact_max, ent_max = _db_maxes()
    return (
        fact_max > int(meta.get("fact_max_id") or 0)
        or ent_max > int(meta.get("entity_max_id") or 0)
    )


def incremental_update_facts_entities() -> dict:
    """Embed ONLY facts/entities added since the last build; append to the
    on-disk arrays and patch META. Falls back to a full `build()` when
    the index files are missing or the metadata is unreadable.

    This is the fix for the 0–6 hour "just-extracted fact is invisible"
    blindness: every `brain_recall` can afford a ~1 ms DB probe, and
    when it hits, embedding a handful of new rows is ~100 ms — vs
    ~17 s for a full rebuild that also re-does work that hasn't
    changed.
    """
    if (
        not META_JSON.exists()
        or not FACTS_NPY.exists()
        or not ENT_NPY.exists()
        or not FACTS_JSON.exists()
        or not ENT_JSON.exists()
    ):
        return build()
    try:
        meta = json.loads(META_JSON.read_text())
    except Exception:
        return build()

    last_fact_id = int(meta.get("fact_max_id") or 0)
    last_ent_id = int(meta.get("entity_max_id") or 0)

    with db.connect() as conn:
        fact_rows = conn.execute(
            """
            SELECT f.id, f.text, f.source, f.entity_id,
                   e.type, e.name, e.slug, f.fact_date, e.path
            FROM facts f
            JOIN entities e ON e.id = f.entity_id
            WHERE f.id > ?
              AND (f.status IS NULL OR f.status != 'superseded')
            """,
            (last_fact_id,),
        ).fetchall()
        ent_rows = conn.execute(
            """
            SELECT id, type, name, slug, path, COALESCE(summary, '')
            FROM entities
            WHERE id > ?
            """,
            (last_ent_id,),
        ).fetchall()

    new_fact_meta = [
        {
            "rowid": r[0], "text": r[1],
            "fact_hash": db.canonical_fact_hash(r[1] or ""),
            "source": r[2],
            "entity_id": r[3], "type": r[4], "name": r[5],
            "slug": r[6], "date": r[7], "path": r[8],
        }
        for r in fact_rows
    ]
    new_ent_meta = [
        {
            "id": r[0], "type": r[1], "name": r[2], "slug": r[3],
            "path": r[4], "summary": r[5],
        }
        for r in ent_rows
    ]

    if not new_fact_meta and not new_ent_meta:
        # Nothing new — refresh built_at so downstream staleness checks
        # don't re-run this probe on every recall within the same batch.
        meta["built_at"] = time.time()
        atomic_write_text(META_JSON, json.dumps(meta, indent=2))
        return {"facts_added": 0, "entities_added": 0, "incremental": True}

    if new_fact_meta:
        fact_texts = [
            f"[{m['type']}/{m['name']}] {m['text']}" for m in new_fact_meta
        ]
        new_vecs = _embed(fact_texts)
        old_vecs = np.load(FACTS_NPY)
        old_meta = json.loads(FACTS_JSON.read_text())
        final_vecs = (
            np.concatenate([old_vecs, new_vecs], axis=0)
            if old_vecs.size
            else new_vecs
        )
        final_meta = old_meta + new_fact_meta
        _atomic_save_npy(FACTS_NPY, final_vecs.astype(np.float32))
        atomic_write_text(FACTS_JSON, json.dumps(final_meta))

    if new_ent_meta:
        ent_texts = [
            f"{m['type']}: {m['name']}. {m['summary']}".strip()
            for m in new_ent_meta
        ]
        new_vecs = _embed(ent_texts)
        old_vecs = np.load(ENT_NPY)
        old_meta = json.loads(ENT_JSON.read_text())
        final_vecs = (
            np.concatenate([old_vecs, new_vecs], axis=0)
            if old_vecs.size
            else new_vecs
        )
        final_meta = old_meta + new_ent_meta
        _atomic_save_npy(ENT_NPY, final_vecs.astype(np.float32))
        atomic_write_text(ENT_JSON, json.dumps(final_meta))

    meta["built_at"] = time.time()
    meta["fact_count"] = int(meta.get("fact_count") or 0) + len(new_fact_meta)
    meta["entity_count"] = int(meta.get("entity_count") or 0) + len(new_ent_meta)
    meta["fact_max_id"] = max(
        last_fact_id, max((m["rowid"] for m in new_fact_meta), default=0)
    )
    meta["entity_max_id"] = max(
        last_ent_id, max((m["id"] for m in new_ent_meta), default=0)
    )
    atomic_write_text(META_JSON, json.dumps(meta, indent=2))

    return {
        "facts_added": len(new_fact_meta),
        "entities_added": len(new_ent_meta),
        "incremental": True,
    }


def _build_notes_full() -> int:
    """Full-rebuild the notes embedding store from the SQLite mirror."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, path, title, body FROM notes"
        ).fetchall()

    meta = [
        {"id": r[0], "path": r[1], "title": r[2], "body": r[3]} for r in rows
    ]
    if not meta:
        _atomic_save_npy(NOTES_NPY, np.zeros((0, DIM), dtype=np.float32))
        atomic_write_text(NOTES_JSON, json.dumps([]))
        return 0
    # Embed title + truncated body (first 1500 chars). Long-tail content is
    # still keyword-searchable via fts_notes; semantic captures the gist.
    texts = [f"{m['title']}\n{m['body'][:1500]}" for m in meta]
    vecs = _embed(texts)
    _atomic_save_npy(NOTES_NPY, vecs)
    atomic_write_text(NOTES_JSON, json.dumps(meta))
    return len(meta)


def update_notes(changed: list[tuple[str, str, str]],
                 deleted_paths: list[str]) -> dict:
    """Incremental update: re-embed only changed notes, drop deleted ones.

    `changed` = [(rel_path, title, body), ...]
    `deleted_paths` = [rel_path, ...]

    Falls back to a full notes rebuild if the existing index is missing
    (first run) or out of sync. Cheap when only a handful of files moved.
    """
    if not changed and not deleted_paths:
        return {"changed": 0, "deleted": 0}

    if not NOTES_NPY.exists() or not NOTES_JSON.exists():
        n = _build_notes_full()
        return {"changed": n, "deleted": 0, "full_rebuild": True}

    vecs = np.load(NOTES_NPY)
    meta = json.loads(NOTES_JSON.read_text())
    by_path = {m["path"]: i for i, m in enumerate(meta)}

    # Delete: drop rows from both arrays (mask).
    drop_idx = {by_path[p] for p in deleted_paths if p in by_path}

    # Update: re-embed changed (or new) rows.
    new_texts = [f"{title}\n{body[:1500]}" for _, title, body in changed]
    new_vecs = _embed(new_texts) if new_texts else np.zeros((0, DIM), dtype=np.float32)

    keep_mask = np.array([i not in drop_idx for i in range(len(meta))], dtype=bool)

    # Among the kept rows, replace any whose path appears in `changed`.
    kept_meta = [meta[i] for i in range(len(meta)) if keep_mask[i]]
    kept_vecs = vecs[keep_mask] if vecs.size else vecs

    changed_paths = {rel for rel, _, _ in changed}
    keep_after_replace_idx = [
        i for i, m in enumerate(kept_meta) if m["path"] not in changed_paths
    ]
    final_meta = [kept_meta[i] for i in keep_after_replace_idx]
    final_vecs = (
        kept_vecs[keep_after_replace_idx]
        if kept_vecs.size
        else np.zeros((0, DIM), dtype=np.float32)
    )

    # Append updated rows at the end.
    next_id = (max((m.get("id", 0) for m in final_meta), default=0) or 0) + 1
    appended = [
        {"id": next_id + i, "path": rel, "title": title, "body": body}
        for i, (rel, title, body) in enumerate(changed)
    ]
    final_meta.extend(appended)
    final_vecs = (
        np.concatenate([final_vecs, new_vecs], axis=0)
        if new_vecs.size
        else final_vecs
    )

    _atomic_save_npy(NOTES_NPY, final_vecs.astype(np.float32))
    atomic_write_text(NOTES_JSON, json.dumps(final_meta))

    # Bump build_at so `status()` reflects freshness.
    if META_JSON.exists():
        try:
            m = json.loads(META_JSON.read_text())
        except Exception:
            m = {}
        m["built_at"] = time.time()
        m["note_count"] = len(final_meta)
        atomic_write_text(META_JSON, json.dumps(m, indent=2))

    return {"changed": len(changed), "deleted": len(drop_idx)}


def _worker_socket_path() -> Path:
    """Resolve the persistent worker's UNIX socket. Indirected so tests can
    point it at a temp socket without monkey-patching multiple call sites."""
    return config.BRAIN_DIR / ".semantic.sock"


def _readline_json(sock: socket.socket, max_bytes: int = 1 << 20) -> dict:
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise IOError("worker closed connection mid-reply")
        buf += chunk
        if b"\n" in buf:
            line, _, _ = buf.partition(b"\n")
            return json.loads(line)
        if len(buf) > max_bytes:
            raise IOError("worker reply exceeded max_bytes")


def update_notes_via_worker(
    changed: list[tuple[str, str, str]],
    deleted_paths: list[str],
    *,
    connect_timeout: float = 0.5,
    request_timeout: float = 30.0,
) -> dict:
    """Hand the diff to the persistent semantic worker over a UNIX socket.

    Falls back to the in-process update_notes() on any failure (socket
    missing, worker dead, timeout, malformed reply). The fallback path
    pays the ~10 s cold-start exactly once, then keeps using the in-process
    model — no worse than the pre-worker baseline.

    Returns a dict shaped like update_notes() with an extra `via_worker`
    flag so callers / tests can tell which path ran.
    """
    if not changed and not deleted_paths:
        return {"changed": 0, "deleted": 0}

    sock_path = _worker_socket_path()
    if not sock_path.exists():
        return update_notes(changed, deleted_paths)

    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(connect_timeout)
        s.connect(str(sock_path))
        s.settimeout(request_timeout)

        result: dict = {"changed": 0, "deleted": 0, "via_worker": True}

        if deleted_paths:
            req = {"op": "delete_notes", "paths": deleted_paths}
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            r = _readline_json(s)
            if not r.get("ok"):
                raise RuntimeError(r.get("error", "worker error on delete"))
            result["deleted"] = r.get("deleted", 0)

        if changed:
            items = [{"path": rel, "title": t, "body": b} for rel, t, b in changed]
            req = {"op": "upsert_notes", "items": items}
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            r = _readline_json(s)
            if not r.get("ok"):
                raise RuntimeError(r.get("error", "worker error on upsert"))
            result["changed"] = r.get("changed", 0)

        return result
    except Exception:
        return update_notes(changed, deleted_paths)
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _is_stale(threshold_seconds: float = 6 * 3600) -> bool:
    if not META_JSON.exists():
        return True
    try:
        meta = json.loads(META_JSON.read_text())
    except Exception:
        return True
    return (time.time() - float(meta.get("built_at", 0))) > threshold_seconds


def ensure_built(rebuild_if_stale: bool = False) -> dict | None:
    """Build the index if missing; run a cheap incremental embed when the
    DB has facts/entities newer than the last indexed high-water mark;
    optionally full-rebuild when older than 6 h.

    The incremental path closes the 0–6 hour "freshly-extracted fact is
    invisible to recall" window — the old default `ensure_built()` only
    built when META was absent, so every new fact stayed dark until the
    next scheduled full rebuild.

    **Scrub-version cross-reference (WS4 follow-up)**: when the `.vec`
    bundle's `scrub_tag` disagrees with the active `sanitize.VERSION`,
    force a full rebuild. Rationale: a newer scrubber may redact or
    reject content the older one kept. Without this check, the
    embeddings + on-disk meta keep surfacing pre-upgrade text
    (including flagged-but-not-rejected injection strings) until the
    next 6 h stale-rebuild — same "v1 → v2 prompt-injection window"
    Security flagged in the 18:24 stale-fact post. Fires a WS5
    `scrub_version_bump_reingest` audit entry exactly once per bump.

    Failures in the incremental path are swallowed and fall through to
    the old behaviour (no refresh) — the recall hot path must never be
    broken by an indexing error.
    """
    if not META_JSON.exists():
        return build()

    # Scrub-version cross-reference — runs BEFORE the incremental
    # path so a bump triggers a full rebuild, not an append of new
    # rows on top of a stale old-version bundle.
    #
    # Three states:
    #   stored is None           → bundle predates this field entirely
    #                              (pre-WS4-cross-ref vault). Stamp the
    #                              current tag in-place, DON'T rebuild.
    #                              This is a one-shot migration — we
    #                              don't know the bundle is actually
    #                              stale w.r.t. the scrubber, and
    #                              forcing a full rebuild on every
    #                              legacy vault penalises correct
    #                              behaviour. Emit `scrub_version_init`
    #                              for audit visibility but don't touch
    #                              the index.
    #   stored == current        → no-op (hot-path common case).
    #   stored != current (both non-null)
    #                            → real version bump. Force full
    #                              rebuild + `scrub_version_bump_reingest`
    #                              audit. The existing bundle may hold
    #                              content the new ruleset would have
    #                              redacted/rejected.
    stored = _meta_scrub_tag()
    current = _current_scrub_tag()
    if stored is None:
        try:
            _stamp_scrub_tag_in_place(current)
            _audit_scrub_version_init(new_tag=current)
        except Exception:
            pass  # never block the read path
    elif stored != current:
        try:
            # Peek fact count *before* rebuild so the audit ledger
            # records how many rows were forcibly re-embedded. This
            # is what a downstream incident-response tool wants to
            # know ("the bump touched N facts").
            try:
                pre_count = len(json.loads(FACTS_JSON.read_text()))
            except Exception:
                pre_count = 0
            result = build()
            _audit_scrub_version_bump(
                old_tag=stored,
                new_tag=current,
                reingested=pre_count,
            )
            return result
        except Exception:
            # Rebuild failed — fall through rather than leave the
            # read path broken. The next tick retries.
            pass

    try:
        if _has_new_rows():
            incremental_update_facts_entities()
    except Exception:
        pass
    if rebuild_if_stale and _is_stale():
        return build()
    return None


def _load_facts() -> tuple[np.ndarray, list[dict]]:
    if not FACTS_NPY.exists():
        ensure_built()
    vecs = np.load(FACTS_NPY)
    meta = json.loads(FACTS_JSON.read_text())
    return vecs, meta


def _load_entities() -> tuple[np.ndarray, list[dict]]:
    if not ENT_NPY.exists():
        ensure_built()
    vecs = np.load(ENT_NPY)
    meta = json.loads(ENT_JSON.read_text())
    return vecs, meta


def _load_notes() -> tuple[np.ndarray, list[dict]]:
    if not NOTES_NPY.exists():
        # Build only the notes side, not the whole vault — cheap when
        # entities/facts indexes are already up to date.
        _build_notes_full()
    vecs = np.load(NOTES_NPY)
    meta = json.loads(NOTES_JSON.read_text())
    return vecs, meta


def _topk(query_vec: np.ndarray, mat: np.ndarray, k: int) -> list[tuple[int, float]]:
    if mat.shape[0] == 0:
        return []
    sims = mat @ query_vec
    if k >= sims.shape[0]:
        idx = np.argsort(-sims)
    else:
        # argpartition is O(N); arg-sort just the top-k slice.
        part = np.argpartition(-sims, k)[:k]
        idx = part[np.argsort(-sims[part])]
    return [(int(i), float(sims[i])) for i in idx[:k]]


def _audit_stale_snippet(
    fact_id: int,
    *,
    meta_hash: str,
    db_hash: str,
    source_path: str | None,
) -> None:
    """Record a stale-embedding drop in the WS5 hash-chained ledger.

    Counter-only — sha8 correlation keys, never raw fact text. Security
    WS5 requirement: ledger rows must contain no content, only what a
    post-hoc auditor needs to tell *which* fact was bad. `brain doctor`
    walks `_audit_ledger.validate()` separately; this function just
    emits the row.

    Best-effort: an import failure or disk-full swallow silently so the
    recall path is never blocked by the audit side-effect.
    """
    try:
        from brain import _audit_ledger
        _audit_ledger.append(
            "stale_snippet_served",
            {
                "fact_id": int(fact_id),
                "meta_sha8": (meta_hash or "")[:8],
                "db_sha8": (db_hash or "")[:8],
                "source_path": source_path or "",
            },
            actor="semantic.search_facts",
        )
    except Exception:
        pass


def count_stale_fact_meta() -> dict:
    """Return `{total, stale, orphan, ratio}` comparing .vec/facts.json
    against the current DB.

    Consumer: `brain doctor`. Stale = meta hash != db hash on a live
    row. Orphan = meta row whose rowid is absent from the facts table
    (already handled by the existing rowid-missing drop, but doctor
    still counts it so the admin sees the staleness budget).
    """
    if not FACTS_JSON.exists():
        return {"total": 0, "stale": 0, "orphan": 0, "ratio": 0.0}
    try:
        meta = json.loads(FACTS_JSON.read_text())
    except Exception:
        return {"total": 0, "stale": 0, "orphan": 0, "ratio": 0.0}
    if not isinstance(meta, list) or not meta:
        return {"total": 0, "stale": 0, "orphan": 0, "ratio": 0.0}
    rowids = [int(m.get("rowid") or 0) for m in meta]
    try:
        with db.connect() as conn:
            placeholders = ",".join("?" * len(rowids))
            rows = conn.execute(
                f"SELECT id, text FROM facts WHERE id IN ({placeholders})",
                rowids,
            ).fetchall()
    except Exception:
        return {"total": len(meta), "stale": 0, "orphan": 0, "ratio": 0.0}
    text_by_id = {int(r[0]): r[1] or "" for r in rows}
    stale = orphan = 0
    for m in meta:
        rid = int(m.get("rowid") or 0)
        if rid not in text_by_id:
            orphan += 1
            continue
        meta_hash = m.get("fact_hash")
        if not meta_hash:
            continue  # pre-guard build; neither stale nor orphan
        if db.canonical_fact_hash(text_by_id[rid]) != meta_hash:
            stale += 1
    bad = stale + orphan
    ratio = (bad / len(meta)) if meta else 0.0
    return {
        "total": len(meta),
        "stale": stale,
        "orphan": orphan,
        "ratio": round(ratio, 4),
    }


def invalidate_for(entity_type: str, slug: str | None = None) -> dict:
    """Pop `.vec` rows belonging to one entity (or an entire type).

    Proactive invalidation hook for the WS3 watcher: when a note or
    entity file changes, the DB rewrite may delete + re-insert facts,
    but `.vec/{facts,entities}.{npy,json}` is append-only. The
    stale-text guard in `search_facts` still catches the drift at
    serve time, but each ghost hit burns one rerank round-trip + one
    WS5 audit-ledger write. This function closes the gap by removing
    the ghost rows on disk so the next `brain_recall` doesn't even
    see them; incremental `ensure_built()` re-embeds the current DB
    rows on the next tick.

    `slug=None` → invalidate every row of `entity_type` (rare).
    `slug=<x>` → invalidate rows for `(entity_type, x)` (common
    watcher path). Missing vec files or any io error → {0, 0}; this
    is cache hygiene, not a correctness boundary — the serve-time
    guard remains the final authority.
    """
    facts_dropped = 0
    entities_dropped = 0

    try:
        if FACTS_NPY.exists() and FACTS_JSON.exists():
            vecs = np.load(FACTS_NPY)
            meta = json.loads(FACTS_JSON.read_text())
            if isinstance(meta, list) and meta:
                keep_idx = []
                for i, m in enumerate(meta):
                    if m.get("type") != entity_type:
                        keep_idx.append(i); continue
                    if slug is not None and m.get("slug") != slug:
                        keep_idx.append(i); continue
                    facts_dropped += 1
                if facts_dropped:
                    new_meta = [meta[i] for i in keep_idx]
                    new_vecs = (vecs[keep_idx] if vecs.size and vecs.shape[0] == len(meta) else vecs)
                    _atomic_save_npy(FACTS_NPY, new_vecs.astype(np.float32))
                    atomic_write_text(FACTS_JSON, json.dumps(new_meta))
    except Exception:
        pass

    try:
        if ENT_NPY.exists() and ENT_JSON.exists():
            vecs = np.load(ENT_NPY)
            meta = json.loads(ENT_JSON.read_text())
            if isinstance(meta, list) and meta:
                keep_idx = []
                for i, m in enumerate(meta):
                    if m.get("type") != entity_type:
                        keep_idx.append(i); continue
                    if slug is not None and m.get("slug") != slug:
                        keep_idx.append(i); continue
                    entities_dropped += 1
                if entities_dropped:
                    new_meta = [meta[i] for i in keep_idx]
                    new_vecs = (vecs[keep_idx] if vecs.size and vecs.shape[0] == len(meta) else vecs)
                    _atomic_save_npy(ENT_NPY, new_vecs.astype(np.float32))
                    atomic_write_text(ENT_JSON, json.dumps(new_meta))
    except Exception:
        pass

    return {"facts_dropped": facts_dropped, "entities_dropped": entities_dropped}


def search_facts(query: str, k: int = 8, type: str | None = None) -> list[dict]:
    """Pure-semantic fact search, with a query-time supersession filter.

    Over-fetches from the index, then drops any hit whose fact row has
    been marked superseded since last build. Without this, a fact
    active at index-time but contradicted between rebuilds keeps
    surfacing via cosine similarity — the index is append-only and
    status flips aren't reflected until the next full rebuild (6 h
    cadence). The bulk status lookup is one SQL query keyed on the
    top-k rowids; cheap even at k=32.
    """
    vecs, meta = _load_facts()
    qv = _embed([query])[0]
    # Over-fetch so we still return `k` rows after dropping supersedes
    # and applying the type filter; 3x covers the worst observed
    # superseded-rate without eating noticeable latency.
    over_k = max(k * 4, k + 16) if type else max(k * 3, k + 8)
    hits = _topk(qv, vecs, min(over_k, vecs.shape[0]))

    # One status lookup for every candidate, so a fact whose status flipped
    # to 'superseded' after indexing gets dropped. The rowid in FACTS_JSON
    # mirrors facts.id in the DB, so we can lookup directly.
    # `db_queried_ok` distinguishes "query succeeded but this rowid is
    # absent" (→ drop; the fact was retracted and its row DELETE+INSERTed
    # under a new id by `upsert_entity_from_file`) from "query failed
    # entirely" (→ pass through so a transient DB hiccup never hides
    # results). Without this split a retracted fact's *old* embedding
    # stays in facts.npy indefinitely and leaks through cosine hits —
    # which is exactly the "brain keeps answering 'Thuha ở Cần Thơ'
    # after the note was deleted" failure we just closed.
    rowids = [int(meta[i].get("rowid") or 0) for i, _ in hits]
    status_by_id: dict[int, str | None] = {}
    text_by_id: dict[int, str] = {}
    db_queried_ok = False
    if rowids:
        try:
            with db.connect() as conn:
                placeholders = ",".join("?" * len(rowids))
                # `text` pulled alongside status so we can compute the
                # current DB hash and compare vs the hash baked into the
                # meta entry at embed time. Stale-embedding guard (incident
                # 2026-04-23): a fact whose text was edited in place
                # after indexing still had its old vector cached; without
                # this guard cosine search happily served the ghost text.
                rows = conn.execute(
                    f"SELECT id, status, text FROM facts WHERE id IN ({placeholders})",
                    rowids,
                ).fetchall()
            status_by_id = {int(r[0]): r[1] for r in rows}
            text_by_id = {int(r[0]): r[2] or "" for r in rows}
            db_queried_ok = True
        except Exception:
            status_by_id = {}
            text_by_id = {}
            db_queried_ok = False

    out: list[dict] = []
    for i, score in hits:
        m = meta[i]
        if type and m["type"] != type:
            continue
        rowid = int(m.get("rowid") or 0)
        if db_queried_ok:
            if rowid not in status_by_id:
                # Orphan: embedding still cached in .vec/facts.npy but the
                # facts row is gone. Upsert DELETE+INSERT cycles retracted
                # rows through new ids, so an absent rowid means the old
                # claim was retracted. Drop it.
                continue
            if status_by_id[rowid] == "superseded":
                continue
            # Stale-text guard. `fact_hash` is attached to each meta
            # entry at embed time (`build` / `incremental_update_...`);
            # absent only on caches built before this guard landed —
            # treat missing hash as "not-yet-backfilled, pass through"
            # so first-deploy doesn't drop every hit. Full rebuild on
            # next scheduled tick repopulates.
            meta_hash = m.get("fact_hash")
            if meta_hash:
                db_hash = db.canonical_fact_hash(text_by_id.get(rowid, ""))
                if db_hash != meta_hash:
                    _audit_stale_snippet(
                        rowid,
                        meta_hash=meta_hash,
                        db_hash=db_hash,
                        source_path=m.get("path"),
                    )
                    continue
        out.append(
            {
                "type": m["type"],
                "name": m["name"],
                "slug": m["slug"],
                "text": m["text"],
                "source": m["source"],
                "date": m.get("date"),
                "path": m.get("path"),
                "score": score,
            }
        )
        if len(out) >= k:
            break
    return out


def search_notes(query: str, k: int = 8) -> list[dict]:
    """Pure-semantic search across user-authored vault notes."""
    vecs, meta = _load_notes()
    if vecs.shape[0] == 0:
        return []
    qv = _embed([query])[0]
    hits = _topk(qv, vecs, k)
    out = []
    for i, score in hits:
        m = meta[i]
        out.append(
            {
                "kind": "note",
                "title": m["title"],
                "path": m["path"],
                "snippet": (m.get("body") or "")[:200],
                "score": score,
            }
        )
    return out


def search_entities(query: str, k: int = 8) -> list[dict]:
    vecs, meta = _load_entities()
    qv = _embed([query])[0]
    hits = _topk(qv, vecs, k)
    return [
        {
            "type": meta[i]["type"],
            "name": meta[i]["name"],
            "slug": meta[i]["slug"],
            "path": meta[i]["path"],
            "summary": meta[i]["summary"],
            "score": score,
        }
        for i, score in hits
    ]


def hybrid_search(query: str, k: int = 8, type: str | None = None) -> list[dict]:
    """Reciprocal-Rank Fusion across four branches:

      1. BM25 facts        (db.search)
      2. Semantic facts    (search_facts)
      3. BM25 notes        (db.search_notes)        ← user-written .md
      4. Semantic notes    (search_notes)           ← user-written .md

    Notes are skipped when `type` is set, since `type` filters entity
    families (people/projects/…) that don't apply to free-form notes.
    """
    K = 60
    pool: dict[tuple, dict] = {}
    scores: dict[tuple, float] = defaultdict(float)

    def _key_fact(hit):
        return ("fact", hit["type"], hit["name"], hit.get("text", "")[:120])

    def _key_note(hit):
        return ("note", hit.get("path") or hit.get("title", ""))

    for rank, hit in enumerate(db.search(query, k=k * 2, type=type)):
        key = _key_fact(hit)
        pool.setdefault(key, {**hit, "kind": "fact", "lexical_rank": rank})
        scores[key] += 1.0 / (K + rank)

    for rank, hit in enumerate(search_facts(query, k=k * 2, type=type)):
        key = _key_fact(hit)
        existing = pool.get(key)
        # Preserve the raw cosine under `sem_score` — `score` may
        # already be a BM25 value (negative) from the lexical branch,
        # and `brain_recall`'s semantic-fallback check needs the real
        # cosine similarity, not whichever branch set `score` first.
        sem_score = float(hit.get("score", 0.0))
        if existing is None:
            pool[key] = {**hit, "kind": "fact", "semantic_rank": rank, "sem_score": sem_score}
        else:
            existing["semantic_rank"] = rank
            existing["sem_score"] = sem_score
        scores[key] += 1.0 / (K + rank)

    if not type:
        for rank, hit in enumerate(db.search_notes(query, k=k * 2)):
            key = _key_note(hit)
            pool.setdefault(
                key,
                {
                    "kind": "note",
                    "title": hit["title"],
                    "path": hit["path"],
                    "snippet": hit.get("snippet", ""),
                    "lexical_rank": rank,
                },
            )
            scores[key] += 1.0 / (K + rank)

        for rank, hit in enumerate(search_notes(query, k=k * 2)):
            key = _key_note(hit)
            existing = pool.get(key)
            sem_score = float(hit.get("score", 0.0))
            if existing is None:
                pool[key] = {**hit, "semantic_rank": rank, "sem_score": sem_score}
            else:
                existing["semantic_rank"] = rank
                existing["sem_score"] = sem_score
            scores[key] += 1.0 / (K + rank)

    # Re-rank step: punish low-signal-density meta files and reward short,
    # specific notes. Without this the auto-generated `index.md`, every
    # `_MOC.md`, and the always-touched `identity/*.md` files dominate any
    # query that mentions Son or a known entity, burying the real answer.
    #
    # `cursor-user-rules.md` specifically is a 12 KB rendered rules template
    # that semantically matches almost every brain-meta query (because it
    # *describes* brain). Empirically it took top-hit on 7 of 13 near-miss
    # queries in the last 14 days — strong enough to fight through the
    # default 1.0 weight and bury entity facts. Penalise it like index.md.
    META_PENALTY_PATHS = {"index.md", "log.md", "cursor-user-rules.md"}
    def _path_penalty(path: str) -> float:
        if not path:
            return 1.0
        if path in META_PENALTY_PATHS:
            return 0.4               # the catch-all index/log/rules files
        name = path.rsplit("/", 1)[-1]
        if name.startswith("_") and name.endswith("_MOC.md"):
            return 0.5               # auto-generated maps-of-content
        # Rendered rule/template files live in the root (`*-rules.md`,
        # `*-tmpl.md`) — same failure mode as cursor-user-rules.md but
        # for future templates a user might drop in.
        if name.endswith("-rules.md") or name.endswith(".tmpl.md"):
            return 0.4
        if path.startswith("identity/"):
            return 0.7               # useful but answers a different question
        return 1.0

    def _density_boost(hit: dict) -> float:
        # Short notes carry concentrated signal — reward up to +50%.
        snippet = hit.get("snippet") or hit.get("text") or ""
        n = len(snippet)
        if n == 0:
            return 1.3               # filename-only notes (e.g. `Son location.md`)
        if n < 200:
            return 1.3
        if n < 800:
            return 1.1
        return 1.0

    # Extracted entities ARE the canonical record of a topic (multi-source
    # dedup'd summary); user notes are raw input. On a concept query both
    # branches hit, but the entity should outrank the tangential note.
    def _primary_entity_boost(hit: dict) -> float:
        return 1.5 if hit.get("kind") == "fact" else 1.0

    # Recency weighting — addresses Karpathy's "memory distraction" complaint
    # (https://x.com/karpathy/status/2036836816654147718): irrelevant ancient
    # context keeps surfacing as if it's a current interest. We give recent
    # facts a small boost and old facts a small dampening, but never zero them
    # out — the user can still recall things from years ago, they just won't
    # crowd out fresh material on identical-similarity hits.
    #
    # Disable with BRAIN_TIME_DECAY=0; tune halflife via BRAIN_TIME_HALFLIFE_D.
    _TIME_DECAY_ON = os.environ.get("BRAIN_TIME_DECAY", "1") != "0"
    _HALFLIFE_DAYS = float(os.environ.get("BRAIN_TIME_HALFLIFE_D", "180"))

    def _recency_factor(hit: dict) -> float:
        if not _TIME_DECAY_ON:
            return 1.0
        # Try fact date first (sources are dated YYYY-MM-DD), then mtime, then path.
        date_str = hit.get("date") or hit.get("last_updated")
        ts: float | None = None
        if date_str and isinstance(date_str, str) and len(date_str) >= 10:
            try:
                ts = time.mktime(time.strptime(date_str[:10], "%Y-%m-%d"))
            except (ValueError, OverflowError):
                ts = None
        if ts is None:
            mt = hit.get("mtime")
            if isinstance(mt, (int, float)) and mt > 0:
                ts = float(mt)
        if ts is None:
            path = hit.get("path")
            if path:
                fp = config.BRAIN_DIR / path
                try:
                    ts = fp.stat().st_mtime
                except OSError:
                    ts = None
        if ts is None:
            return 1.0
        age_days = max(0.0, (time.time() - ts) / 86400.0)
        # Smooth bell: 0d→1.20, halflife→1.0, 2*halflife→0.83, 4*halflife→0.69
        # Implemented as a soft 0.5^(age/(2*halflife)) curve so we never go below ~0.4.
        decay = 0.5 ** (age_days / (2.0 * _HALFLIFE_DAYS))
        return 0.8 + 0.4 * decay  # ∈ [0.8, 1.2]

    fused = []
    for k_ in pool:
        hit = pool[k_]
        path = hit.get("path", "")
        adj = scores[k_] * _path_penalty(path) * _density_boost(hit) * _recency_factor(hit) * _primary_entity_boost(hit)
        fused.append({**hit, "rrf": adj})
    fused.sort(key=lambda x: -x["rrf"])

    # WS7a subject-reject: hard filter on owner-self-reference +
    # proper-noun queries, gated by BRAIN_SUBJECT_REJECT.
    # Default flipped to "1" on 2026-04-23 after WS1 golden-set expansion
    # showed strict improvement (weak_hit_rate 0.000→0.400 on held-out
    # n=20, positive metrics unchanged). Set BRAIN_SUBJECT_REJECT=0 to
    # disable on a per-session basis. When on, dropped hits leave an
    # audit trail at ~/.brain/.audit/subject_reject.jsonl.
    if os.environ.get("BRAIN_SUBJECT_REJECT", "1") == "1":
        try:
            from brain import subject_reject
            hint = subject_reject.parse_query_subject(query)
            if hint.subject_slug is not None:
                fused = subject_reject.filter_hits(fused, hint, query=query)
        except Exception:
            # Filter must never break recall — silent fallback to the
            # un-filtered pool if anything in the parser blows up.
            pass
    return fused[:k]


def status() -> dict:
    if not META_JSON.exists():
        return {"built": False}
    meta = json.loads(META_JSON.read_text())
    age = time.time() - float(meta.get("built_at", 0))
    meta["age_hours"] = round(age / 3600, 2)
    meta["stale"] = age > 6 * 3600
    return {"built": True, **meta}


def main():
    import argparse, sys

    p = argparse.ArgumentParser(description="Semantic recall layer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="Full reindex (slow first time, fast after)")
    sub.add_parser("ensure", help="Build only if missing")
    sub.add_parser("status", help="Print metadata")
    sp = sub.add_parser("search", help="Semantic fact search")
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=8)
    sp.add_argument("--type", default=None)
    sp.add_argument("--hybrid", action="store_true", help="RRF fusion across all branches")
    sp.add_argument("--entities", action="store_true", help="Search entity names")
    sp.add_argument("--notes", action="store_true", help="Search user-written notes only")

    args = p.parse_args()

    if args.cmd == "build":
        out = build()
        print(json.dumps(out, indent=2))
    elif args.cmd == "ensure":
        out = ensure_built()
        print(json.dumps(out or {"already_built": True}, indent=2))
    elif args.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif args.cmd == "search":
        if args.entities:
            results = search_entities(args.query, k=args.k)
        elif args.notes:
            results = search_notes(args.query, k=args.k)
        elif args.hybrid:
            results = hybrid_search(args.query, k=args.k, type=args.type)
        else:
            results = search_facts(args.query, k=args.k, type=args.type)
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
