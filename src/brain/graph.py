"""Oxigraph RDF triple store wrapper for the brain.

Triples represent typed relationships between entities extracted from
sessions and notes. The store lives at ~/.brain/.brain.rdf/ (Oxigraph
persistent directory format).

Namespace:
  http://brain.local/e/<slug>   — entity node
  http://brain.local/p/<pred>   — predicate

SPARQL example:
  PREFIX be: <http://brain.local/e/>
  PREFIX bp: <http://brain.local/p/>
  SELECT ?org WHERE { be:son bp:worksAt ?org }

Predicate vocabulary lives in ``brain.predicate_registry`` — an
audit-gated learning ledger. ``add_triple`` only writes for predicates
whose status is ``active``; ``unknown`` and ``proposed`` predicates
route to the discovery queue; ``retired`` ones drop + log a failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import brain.config as config

BRAIN_NS = "http://brain.local/"
ENTITY_NS = f"{BRAIN_NS}e/"
PREDICATE_NS = f"{BRAIN_NS}p/"


def is_valid_predicate(pred: str) -> bool:
    """True iff the predicate is ``active`` in the registry.

    Kept as a thin wrapper so callers outside this module don't need to
    know about registry internals.
    """
    from brain import predicate_registry
    return predicate_registry.status(pred) == "active"


def _store():
    """Lazy-load the Oxigraph Store (persistent, thread-safe)."""
    from pyoxigraph import Store
    config.GRAPH_STORE_DIR.mkdir(parents=True, exist_ok=True)
    return Store(path=str(config.GRAPH_STORE_DIR))


def _en(slug: str):
    """Entity NamedNode."""
    from pyoxigraph import NamedNode
    return NamedNode(ENTITY_NS + slug.lower().replace(" ", "-"))


def _pn(pred: str):
    """Predicate NamedNode."""
    from pyoxigraph import NamedNode
    return NamedNode(PREDICATE_NS + pred)


def _val(obj: str):
    """Object — NamedNode if slug-like, Literal otherwise."""
    from pyoxigraph import NamedNode, Literal
    # Treat multi-word values as literals; single-word / slug-like as entities
    clean = obj.strip()
    if " " not in clean and len(clean) < 60:
        return NamedNode(ENTITY_NS + clean.lower().replace(" ", "-"))
    return Literal(clean)


def _slug_from_node(node) -> str:
    """Reverse-convert a NamedNode IRI to a readable slug."""
    # pyoxigraph str() wraps IRIs in angle brackets: <http://...>
    iri = node.value if hasattr(node, "value") else str(node)
    if iri.startswith("<") and iri.endswith(">"):
        iri = iri[1:-1]
    if iri.startswith(ENTITY_NS):
        return iri[len(ENTITY_NS):]
    if iri.startswith(PREDICATE_NS):
        return iri[len(PREDICATE_NS):]
    return iri


def add_triple(subject: str, predicate: str, obj: str, source: str = "") -> bool:
    """Add one triple to the store.

    Returns True iff the triple was written. Unknown and proposed
    predicates are routed to the registry's discovery queue (returns
    False — the triple doesn't land, but the predicate accumulates
    audit evidence). Retired predicates drop silently and append to
    ``failures.jsonl`` with ``source=retired_predicate`` so silent
    drops stay discoverable.
    """
    from brain import predicate_registry
    stat = predicate_registry.status(predicate)

    if stat == "active":
        from pyoxigraph import Quad, DefaultGraph
        store = _store()
        store.add(Quad(_en(subject), _pn(predicate), _val(obj), DefaultGraph()))
        return True

    basis = f"{subject} {predicate} {obj}"
    if stat in ("unknown", "proposed"):
        predicate_registry.observe(predicate, basis=basis)
        return False

    # retired — drop + log so we can grep for false-negative surprises
    try:
        from brain.failures import record_failure
        record_failure(
            source="retired_predicate",
            tool="graph.add_triple",
            query=predicate,
            result_digest=basis,
        )
    except Exception:
        pass
    return False


def remove_triple(subject: str, predicate: str, obj: str) -> None:
    from pyoxigraph import Quad, DefaultGraph
    store = _store()
    store.remove(Quad(_en(subject), _pn(predicate), _val(obj), DefaultGraph()))


def neighbors(
    entity: str,
    predicate: str | None = None,
    depth: int = 1,
) -> list[dict]:
    """Return all triples reachable from `entity` within `depth` hops.

    depth=1 returns direct edges only. depth=2 follows those targets one
    more step. Capped at depth=3 to prevent runaway traversal.
    """
    depth = max(1, min(int(depth), 3))
    store = _store()
    visited: set[str] = set()
    frontier = {entity.lower().replace(" ", "-")}
    results: list[dict] = []

    for _ in range(depth):
        next_frontier: set[str] = set()
        for slug in frontier:
            if slug in visited:
                continue
            visited.add(slug)
            subject_node = _en(slug)
            pred_filter = _pn(predicate) if predicate else None
            for triple in store.quads_for_pattern(subject_node, pred_filter, None, None):
                t = triple.triple if hasattr(triple, "triple") else triple
                obj_slug = _slug_from_node(t.object)
                pred_name = _slug_from_node(t.predicate)
                results.append({
                    "subject": slug,
                    "predicate": pred_name,
                    "object": obj_slug,
                })
                next_frontier.add(obj_slug)
        frontier = next_frontier - visited

    return results


def query(sparql: str) -> list[dict] | dict:
    """Execute a SPARQL SELECT query. Returns list of binding dicts."""
    store = _store()
    try:
        results = store.query(sparql)
        variables = results.variables if hasattr(results, "variables") else []
        out = []
        for solution in results:
            row = {}
            for var in variables:
                val = solution[var]
                row[str(var)] = _slug_from_node(val) if val else None
            out.append(row)
        return out
    except Exception as exc:
        return {"error": str(exc)}


def triple_count() -> int:
    return len(_store())


def export_ttl() -> str:
    """Export the entire store as Turtle text (for backup/inspection)."""
    from pyoxigraph import RdfFormat
    import io
    buf = io.BytesIO()
    _store().dump(buf, RdfFormat.TURTLE)
    return buf.getvalue().decode("utf-8", errors="replace")
