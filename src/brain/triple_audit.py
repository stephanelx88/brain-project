"""Pending triple queue and audit walker.

Triples below the confidence threshold (< 0.8) are written here for
human review instead of going directly into the RDF store. After audit,
confirmed triples go to graph.add_triple(); the decision is recorded in
triple_rules.py so future extractions adjust confidence automatically.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import brain.config as config
from brain.io import atomic_write_text

CONFIDENCE_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# Queue I/O
# ---------------------------------------------------------------------------

def load_pending() -> list[dict]:
    p = config.PENDING_TRIPLES_PATH
    if not p.exists():
        return []
    items = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return items


def _save_pending(items: list[dict]) -> None:
    config.PENDING_TRIPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        config.PENDING_TRIPLES_PATH,
        "\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n" if items else "",
    )


def add_pending(triples: list[dict], source: str = "") -> None:
    """Append triples to the pending queue (applied confidence adjustment first)."""
    from brain.triple_rules import adjusted_confidence
    existing = load_pending()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for t in triples:
        raw_conf = float(t.get("confidence", 0.5))
        adj_conf = adjusted_confidence(t.get("predicate", ""), raw_conf)
        existing.append({
            "id": str(uuid.uuid4())[:8],
            "subject": t.get("subject", ""),
            "predicate": t.get("predicate", ""),
            "object": t.get("object", ""),
            "confidence": round(adj_conf, 3),
            "basis": t.get("basis", ""),
            "source": source,
            "added_at": now,
        })
    _save_pending(existing)


def remove_pending(ids: list[str]) -> None:
    """Drop items by id from the queue."""
    keep = [i for i in load_pending() if i.get("id") not in ids]
    _save_pending(keep)


def pending_count() -> int:
    return len(load_pending())


# ---------------------------------------------------------------------------
# Interactive walker
# ---------------------------------------------------------------------------

def _ask(prompt: str, valid: str, *, _input: Callable[[str], str] | None = None) -> str:
    fn = _input or input
    while True:
        try:
            raw = fn(prompt).strip().lower()
        except EOFError:
            return "q"
        if raw in valid:
            return raw
        print(f"  Please enter one of: {', '.join(valid)}")


def walk(
    limit: int = 10,
    *,
    _input: Callable[[str], str] | None = None,
    _today: date | None = None,
) -> dict:
    """Interactive y/n/q walker for pending triples.

    y → write triple to RDF store + record confirmed decision
    n → discard + record rejected decision
    q → quit, remaining items stay in queue
    """
    from brain.graph import add_triple
    from brain.triple_rules import record_decision

    items = load_pending()
    if not items:
        print("No pending triples to review.")
        return {"yes": 0, "no": 0, "skipped": 0}

    batch = items[:limit]
    processed_ids: list[str] = []
    tally = {"yes": 0, "no": 0, "skipped": 0, "quit": 0}

    for i, item in enumerate(batch, 1):
        subj = item["subject"]
        pred = item["predicate"]
        obj = item["object"]
        conf = item.get("confidence", 0)
        basis = item.get("basis", "")

        print()
        print(f"[{i}/{len(batch)}]  ({subj}, {pred}, {obj})")
        if basis:
            print(f"        basis: \"{basis}\"")
        print(f"        confidence: {conf:.2f}")

        choice = _ask("  y/n/q  (y=add to graph, n=reject) > ", "ynq", _input=_input)

        if choice == "q":
            tally["quit"] += 1
            break

        processed_ids.append(item["id"])

        if choice == "y":
            add_triple(subj, pred, obj, source=item.get("source", ""))
            record_decision(pred, basis, "y")
            print(f"  ✓ added: ({subj}, {pred}, {obj})")
            tally["yes"] += 1
        else:
            record_decision(pred, basis, "n")
            print(f"  ✗ rejected")
            tally["no"] += 1

    remove_pending(processed_ids)

    total = tally["yes"] + tally["no"]
    if total:
        print(f"\nTriple audit: {tally['yes']} added, {tally['no']} rejected, "
              f"{len(items) - len(processed_ids)} remaining in queue.")
    return tally
