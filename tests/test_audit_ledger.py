"""Tests for the WS5 hash-chained audit ledger."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def tmp_brain_audit(tmp_path, monkeypatch):
    """Isolate BRAIN_DIR so each test writes a fresh audit ledger."""
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", tmp_path / "brain")
    (tmp_path / "brain").mkdir()
    # Stable actor for reproducible hashes in assertions.
    monkeypatch.setenv("BRAIN_AUDIT_ACTOR", "test:1")
    return tmp_path / "brain"


def test_head_hash_on_empty_returns_genesis(tmp_brain_audit):
    from brain import _audit_ledger
    assert _audit_ledger.head_hash() == _audit_ledger.GENESIS


def test_append_writes_row_and_advances_head(tmp_brain_audit):
    from brain import _audit_ledger
    row1 = _audit_ledger.append("note_add", {"path": "journal/x.md"})
    assert row1["prev_hash"] == _audit_ledger.GENESIS
    assert len(row1["hash"]) == 64
    assert _audit_ledger.head_hash() == row1["hash"]

    row2 = _audit_ledger.append("forget", {"scope": "global"})
    assert row2["prev_hash"] == row1["hash"]
    assert row2["hash"] != row1["hash"]
    assert _audit_ledger.head_hash() == row2["hash"]


def test_validate_clean_chain(tmp_brain_audit):
    from brain import _audit_ledger
    for i in range(5):
        _audit_ledger.append(f"op_{i}", {"i": i})
    ok, n, first_bad = _audit_ledger.validate(return_detail=True)
    assert ok is True
    assert n == 5
    assert first_bad is None


def test_validate_detects_mutation(tmp_brain_audit):
    """Flip one field in a row; chain breaks at that index."""
    from brain import _audit_ledger
    for i in range(3):
        _audit_ledger.append(f"op_{i}", {"i": i})

    # Rewrite the middle row with a tampered target (but keep its hash).
    path = _audit_ledger.ledger_path()
    lines = path.read_text().splitlines()
    rewritten = json.loads(lines[1])
    rewritten["target"] = {"i": 99}
    lines[1] = json.dumps(rewritten, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    ok, n, first_bad = _audit_ledger.validate(return_detail=True)
    assert ok is False
    # Row 0 is clean; row 1 fails because its hash no longer matches its
    # target. The validator stops at the first break.
    assert first_bad == 1


def test_validate_detects_missing_row(tmp_brain_audit):
    """Deleting a middle row breaks prev_hash continuity for the next one."""
    from brain import _audit_ledger
    for i in range(4):
        _audit_ledger.append(f"op_{i}", {"i": i})

    path = _audit_ledger.ledger_path()
    lines = path.read_text().splitlines()
    # Drop the second row.
    path.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n")

    ok, n, first_bad = _audit_ledger.validate(return_detail=True)
    assert ok is False
    # Row 0 still valid; row 1 (originally row 2) now has a dangling
    # prev_hash that points at the removed row's hash.
    assert first_bad == 1


def test_validate_tolerates_partial_tail(tmp_brain_audit):
    """A crash mid-write may leave an unterminated JSON tail. The
    validator must drop it silently, not flag it as corruption —
    append() is pipe-atomic only for lines, so a partial tail is a
    recoverable state (the next append will restart after it)."""
    from brain import _audit_ledger
    _audit_ledger.append("op0", {"x": 1})

    path = _audit_ledger.ledger_path()
    with path.open("a", encoding="utf-8") as f:
        f.write('{"ts":"2026-04-23T00:00:00Z","actor":"test:1","op":"half')  # no newline, no close

    ok, n, _ = _audit_ledger.validate(return_detail=True)
    assert ok is True
    assert n == 1  # partial tail ignored; first valid row still counts


def test_target_canonical_json_is_stable(tmp_brain_audit):
    """Two append() calls with equivalent dicts must produce the same
    hash shape — dict-insertion-order cannot leak into the chain."""
    from brain import _audit_ledger
    a = _audit_ledger.append("op", {"z": 1, "a": 2})
    # Rewind: delete ledger and re-append with keys in different order.
    _audit_ledger.ledger_path().unlink()
    b = _audit_ledger.append("op", {"a": 2, "z": 1})
    # Both rows 0 → prev_hash=GENESIS; hashes must match.
    assert a["hash"] == b["hash"]


def test_append_does_not_raise_on_unwritable_ledger(tmp_brain_audit, monkeypatch):
    """If disk is full / readonly, append swallows the error. Audit is
    secondary; the write operation itself is primary and must not
    crash because the ledger couldn't be written."""
    from brain import _audit_ledger

    def boom(*a, **kw):
        raise OSError("simulated disk full")

    # Make open() fail specifically for the ledger path.
    real_open = open

    def patched_open(path, *args, **kwargs):
        if str(path).endswith("ledger.jsonl") and "a" in (args[0] if args else kwargs.get("mode", "")):
            raise OSError("simulated disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", patched_open)
    # Should not raise.
    row = _audit_ledger.append("op", {"x": 1})
    assert row["op"] == "op"


def test_stats_shape(tmp_brain_audit):
    from brain import _audit_ledger
    _audit_ledger.append("op0", {})
    _audit_ledger.append("op1", {})
    s = _audit_ledger.stats()
    assert s["rows"] == 2
    assert s["chain_ok"] is True
    assert s["first_bad_row"] is None
    assert s["head_hash"] == _audit_ledger.head_hash()


def test_actor_override_env(tmp_brain_audit, monkeypatch):
    from brain import _audit_ledger
    monkeypatch.setenv("BRAIN_AUDIT_ACTOR", "harness-42")
    row = _audit_ledger.append("op", {})
    assert row["actor"] == "harness-42"
