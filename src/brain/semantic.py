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
META_JSON = VEC_DIR / "meta.json"

# Small, fast, CPU-friendly. 384-d, ~80 MB, ~5 ms per query embedding on M-series.
DEFAULT_MODEL = os.environ.get("BRAIN_EMBED_MODEL", "all-MiniLM-L6-v2")
DIM = 384

_model = None  # lazy-loaded sentence-transformers model


def _get_model():
    global _model
    if _model is None:
        # Import lazy because sentence-transformers pulls torch (slow startup).
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
    META_JSON.write_text(
        json.dumps(
            {
                "model": DEFAULT_MODEL,
                "dim": DIM,
                "built_at": time.time(),
                "fact_count": len(fact_meta),
                "entity_count": len(ent_meta),
                "build_seconds": round(time.time() - t0, 2),
            },
            indent=2,
        )
    )

    return {
        "facts": len(fact_meta),
        "entities": len(ent_meta),
        "elapsed": round(time.time() - t0, 2),
    }


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
    """Reciprocal-Rank Fusion of BM25 (lexical) and dense (semantic) results.

    RRF score = Σ 1/(60 + rank_i). Pulls the top 2k from each branch so the
    fused list has both strict-keyword hits AND meaning-based ones.
    """
    K = 60
    pool: dict[tuple, dict] = {}
    scores: dict[tuple, float] = defaultdict(float)

    for rank, hit in enumerate(db.search(query, k=k * 2, type=type)):
        key = (hit["type"], hit["name"], hit["text"][:120])
        pool.setdefault(key, {**hit, "lexical_rank": rank})
        scores[key] += 1.0 / (K + rank)

    for rank, hit in enumerate(search_facts(query, k=k * 2, type=type)):
        key = (hit["type"], hit["name"], hit["text"][:120])
        existing = pool.get(key)
        if existing is None:
            pool[key] = {**hit, "semantic_rank": rank}
        else:
            existing["semantic_rank"] = rank
        scores[key] += 1.0 / (K + rank)

    fused = [
        {**pool[k_], "rrf": scores[k_]}
        for k_ in sorted(pool, key=lambda x: -scores[x])
    ]
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
    sp.add_argument("--hybrid", action="store_true", help="RRF fusion with BM25")
    sp.add_argument("--entities", action="store_true", help="Search entity names")

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
        elif args.hybrid:
            results = hybrid_search(args.query, k=args.k, type=args.type)
        else:
            results = search_facts(args.query, k=args.k, type=args.type)
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
