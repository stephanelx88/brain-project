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


def build() -> dict:
    """Full reindex from the SQLite mirror."""
    _ensure_dir()
    t0 = time.time()

    with db.connect() as conn:
        fact_rows = conn.execute(
            """
            SELECT f.id, f.text, f.source, f.entity_id,
                   e.type, e.name, e.slug
            FROM facts f
            JOIN entities e ON e.id = f.entity_id
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
            "source": r[2],
            "entity_id": r[3],
            "type": r[4],
            "name": r[5],
            "slug": r[6],
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

    np.save(FACTS_NPY, fact_vecs)
    np.save(ENT_NPY, ent_vecs)
    FACTS_JSON.write_text(json.dumps(fact_meta))
    ENT_JSON.write_text(json.dumps(ent_meta))

    # Notes — second corpus, indexed alongside facts/entities.
    note_count = _build_notes_full()

    META_JSON.write_text(
        json.dumps(
            {
                "model": DEFAULT_MODEL,
                "dim": DIM,
                "built_at": time.time(),
                "fact_count": len(fact_meta),
                "entity_count": len(ent_meta),
                "note_count": note_count,
                "build_seconds": round(time.time() - t0, 2),
            },
            indent=2,
        )
    )

    return {
        "facts": len(fact_meta),
        "entities": len(ent_meta),
        "notes": note_count,
        "elapsed": round(time.time() - t0, 2),
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
        np.save(NOTES_NPY, np.zeros((0, DIM), dtype=np.float32))
        NOTES_JSON.write_text(json.dumps([]))
        return 0
    # Embed title + truncated body (first 1500 chars). Long-tail content is
    # still keyword-searchable via fts_notes; semantic captures the gist.
    texts = [f"{m['title']}\n{m['body'][:1500]}" for m in meta]
    vecs = _embed(texts)
    np.save(NOTES_NPY, vecs)
    NOTES_JSON.write_text(json.dumps(meta))
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

    np.save(NOTES_NPY, final_vecs.astype(np.float32))
    NOTES_JSON.write_text(json.dumps(final_meta))

    # Bump build_at so `status()` reflects freshness.
    if META_JSON.exists():
        try:
            m = json.loads(META_JSON.read_text())
        except Exception:
            m = {}
        m["built_at"] = time.time()
        m["note_count"] = len(final_meta)
        META_JSON.write_text(json.dumps(m, indent=2))

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
    """Build the index if missing; optionally rebuild when older than 6 h."""
    if not META_JSON.exists() or (rebuild_if_stale and _is_stale()):
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


def search_facts(query: str, k: int = 8, type: str | None = None) -> list[dict]:
    vecs, meta = _load_facts()
    qv = _embed([query])[0]
    over_k = k * 4 if type else k
    hits = _topk(qv, vecs, over_k)
    out: list[dict] = []
    for i, score in hits:
        m = meta[i]
        if type and m["type"] != type:
            continue
        out.append(
            {
                "type": m["type"],
                "name": m["name"],
                "slug": m["slug"],
                "text": m["text"],
                "source": m["source"],
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
        if existing is None:
            pool[key] = {**hit, "kind": "fact", "semantic_rank": rank}
        else:
            existing["semantic_rank"] = rank
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
            if existing is None:
                pool[key] = {**hit, "semantic_rank": rank}
            else:
                existing["semantic_rank"] = rank
            scores[key] += 1.0 / (K + rank)

    # Re-rank step: punish low-signal-density meta files and reward short,
    # specific notes. Without this the auto-generated `index.md`, every
    # `_MOC.md`, and the always-touched `identity/*.md` files dominate any
    # query that mentions Son or a known entity, burying the real answer.
    META_PENALTY_PATHS = {"index.md", "log.md"}
    def _path_penalty(path: str) -> float:
        if not path:
            return 1.0
        if path in META_PENALTY_PATHS:
            return 0.4               # the catch-all index/log files
        name = path.rsplit("/", 1)[-1]
        if name.startswith("_") and name.endswith("_MOC.md"):
            return 0.5               # auto-generated maps-of-content
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
        adj = scores[k_] * _path_penalty(path) * _density_boost(hit) * _recency_factor(hit)
        fused.append({**hit, "rrf": adj})
    fused.sort(key=lambda x: -x["rrf"])
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
