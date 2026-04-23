"""Hash-chained audit ledger for MCP write tools.

Every call to a write tool (brain_remember, brain_note_add,
brain_retract_fact, brain_correct_fact, brain_forget, brain_mark_*,
brain_resolve_contested, brain_failure_record) appends one JSONL row
to `<BRAIN_DIR>/.audit/ledger.jsonl`. Each row carries
`{ts, actor, op, target, prev_hash, hash}` where `hash = sha256(
prev_hash || ts || actor || op || target_json)` — a missing or
mutated row breaks the chain and `validate()` returns False.

Why a hash chain, not signatures: we want tamper-evidence without
keeping a key. Anyone can append (it's an append-only local file),
but no-one can silently rewrite history — because every subsequent
row's hash depends on the modified row. The first row's `prev_hash`
is a constant sentinel `GENESIS` so the chain is self-seeded.

Consumers:
  - `brain doctor` walks the chain at the end of its run.
  - `brain_status` surfaces `{ledger_rows, chain_ok, head_hash}`.
  - Future forensics: `python -m brain._audit_ledger validate`.

Design notes:
  - Counters + structured target only; NEVER raw user content. If a
    tool writes a user note, the ledger records `{"op": "note_add",
    "target": {"path": "journal/2026-04-23.md", "bullet_sha8": "..."}}`,
    not the bullet text. Same rule as the sanitize ledger from WS4.
  - O_APPEND writes — no lock, small rows (<1 KB) are pipe-atomic on
    Linux + macOS. A reader may see partial tail; validator tolerates
    a trailing partial line by dropping it.
  - `BRAIN_AUDIT_ACTOR` env override for CI / test runs. Default is
    `{user}:{pid}` which is enough to tell human writes from an agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Iterator

import brain.config as config


GENESIS = "0" * 64  # sentinel prev_hash for row 0


def ledger_path() -> Path:
    """Resolved at call time — `config.BRAIN_DIR` may be monkeypatched
    in tests, and the default install directory isn't created until
    first write."""
    return config.BRAIN_DIR / ".audit" / "ledger.jsonl"


def _actor() -> str:
    """Stable identifier for the process making the write.

    Override via `BRAIN_AUDIT_ACTOR`. Default combines `$USER` and pid
    so parallel CI runs and a human's own session are distinguishable
    in the ledger without leaking any session transcript content.
    """
    override = os.environ.get("BRAIN_AUDIT_ACTOR")
    if override:
        return override
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    return f"{user}:{os.getpid()}"


def _canonical_target(target) -> str:
    """Deterministic JSON for the target payload.

    `sort_keys=True` + `separators=(",", ":")` so two equal-valued
    dicts always hash identically (Python dict-order is insertion-
    order since 3.7, which is the wrong semantics for a hash chain).
    """
    if target is None:
        target = {}
    return json.dumps(target, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _compute_hash(prev_hash: str, ts: str, actor: str,
                  op: str, target_json: str) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode("utf-8"))
    h.update(b"\x1f")  # ASCII unit separator between fields — prevents
    h.update(ts.encode("utf-8"))           # "12|a|b" colliding with
    h.update(b"\x1f")                       # "1|2a|b" etc.
    h.update(actor.encode("utf-8"))
    h.update(b"\x1f")
    h.update(op.encode("utf-8"))
    h.update(b"\x1f")
    h.update(target_json.encode("utf-8"))
    return h.hexdigest()


def head_hash() -> str:
    """Return the hash of the last valid row, or `GENESIS` if empty."""
    path = ledger_path()
    if not path.exists():
        return GENESIS
    last = GENESIS
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # partial tail — ignore
            h = row.get("hash")
            if isinstance(h, str) and len(h) == 64:
                last = h
    return last


def append(op: str, target=None, *, actor: str | None = None) -> dict:
    """Append one audit row. Returns the written row.

    Writes atomically (O_APPEND, single-line-per-row) so a concurrent
    reader either sees the full row or none at all. No lock: if two
    tools race, both rows land; each hashes over whichever one
    happened to be `head` at the moment of read. That's fine — the
    chain still validates because later `validate()` sees the actual
    file order.

    Best-effort: if writing fails (disk full, readonly, etc.) we
    silently swallow so a write tool never crashes just because the
    audit trail couldn't be appended. The tool's own operation stays
    primary; audit is secondary. Surfaces via doctor/status.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    actor = actor or _actor()
    target_json = _canonical_target(target)
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        prev = head_hash()
    except Exception:
        prev = GENESIS
    h = _compute_hash(prev, ts, actor, op, target_json)
    row = {
        "ts": ts,
        "actor": actor,
        "op": op,
        "target": json.loads(target_json) if target_json else {},
        "prev_hash": prev,
        "hash": h,
    }
    try:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        # O_APPEND semantics: a single write(2) of <4KB is atomic on
        # ext4/xfs/apfs. We're well under that.
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    return row


def _iter_rows(path: Path) -> Iterator[dict]:
    """Yield JSON-parsed rows, skipping unparseable tails."""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                return  # partial tail terminates iteration


def validate(*, return_detail: bool = False):
    """Walk the chain; return `(ok, n_rows, first_bad_idx)` or `ok` bool.

    `first_bad_idx` is 0-indexed row number where the chain first
    diverges — `None` when `ok=True`. Useful for the doctor output
    (points the user at the line to inspect).

    An absent ledger is `ok=True, n_rows=0`. An empty file same. Only
    a malformed chain is `ok=False`.
    """
    path = ledger_path()
    if not path.exists():
        return (True, 0, None) if return_detail else True
    prev = GENESIS
    n = 0
    first_bad: int | None = None
    for i, row in enumerate(_iter_rows(path)):
        expected_prev = row.get("prev_hash")
        ts = row.get("ts")
        actor = row.get("actor")
        op = row.get("op")
        target = row.get("target", {})
        claimed = row.get("hash")
        if not isinstance(ts, str) or not isinstance(op, str) \
           or not isinstance(claimed, str) or expected_prev != prev:
            first_bad = i
            break
        target_json = _canonical_target(target)
        expected = _compute_hash(prev, ts, str(actor or ""), op, target_json)
        if expected != claimed:
            first_bad = i
            break
        prev = claimed
        n += 1
    ok = first_bad is None
    if return_detail:
        return ok, n, first_bad
    return ok


def stats() -> dict:
    """Summary for `brain_status` / `brain doctor` consumption."""
    ok, n, first_bad = validate(return_detail=True)
    return {
        "rows": n,
        "chain_ok": ok,
        "head_hash": head_hash(),
        "first_bad_row": first_bad,
        "path": str(ledger_path()),
    }


# --- CLI: python -m brain._audit_ledger [validate|head|stats]
def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "stats"
    if cmd == "validate":
        ok, n, first_bad = validate(return_detail=True)
        print(f"ok={ok} rows={n} first_bad={first_bad}")
        return 0 if ok else 1
    if cmd == "head":
        print(head_hash())
        return 0
    if cmd == "stats":
        print(json.dumps(stats(), indent=2))
        return 0
    print(f"unknown command: {cmd}", flush=True)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
