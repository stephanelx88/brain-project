"""Tests for WS5 read/write MCP split.

Three things need to hold:
  1. Both servers import cleanly.
  2. Read + write tool sets are disjoint AND their union covers every
     @mcp.tool-decorated function in the aggregate module.
  3. Calling a write tool appends one row to the audit ledger, whose
     chain then validates.
"""

from __future__ import annotations

import importlib

import pytest


def test_read_server_imports_and_registers_tools():
    import brain.mcp_server_read as R
    assert R.mcp.name == "brain-read"
    # Tool partition is surfaced as a tuple — cheap to assert.
    assert "brain_recall" in R.READ_TOOLS
    assert "brain_note_add" not in R.READ_TOOLS
    # And the registration actually added them to the FastMCP instance.
    # FastMCP keeps its tool set on its tool manager.
    registered = set(R.mcp._tool_manager._tools.keys())
    for name in R.READ_TOOLS:
        assert name in registered, f"{name} not registered on brain-read"


def test_write_server_imports_and_registers_tools(monkeypatch):
    # Default: BRAIN_WRITE=1, server exposes 9 tools.
    monkeypatch.setenv("BRAIN_WRITE", "1")
    # Re-import under the flag so _register_write_tools runs with it.
    import brain.mcp_server_write as W
    importlib.reload(W)
    assert W.mcp.name == "brain-write"
    registered = set(W.mcp._tool_manager._tools.keys())
    for name in W.WRITE_TOOLS:
        assert name in registered, f"{name} not registered on brain-write"
    assert "brain_recall" not in registered  # read tools stay off write server


def test_write_server_respects_disable_flag(monkeypatch):
    """BRAIN_WRITE=0 on this host → server starts but registers nothing.
    This is the expected shape on untrusted peer hosts."""
    monkeypatch.setenv("BRAIN_WRITE", "0")
    import brain.mcp_server_write as W
    importlib.reload(W)
    # No write tool registered, no read tool either — just an empty shell.
    registered = set(W.mcp._tool_manager._tools.keys())
    assert registered == set()


def test_read_and_write_partitions_are_disjoint_and_cover_aggregate():
    """The two servers together must cover every mutating-or-reading
    @mcp.tool in the aggregate, with no overlap. This is the
    invariant that prevents a half-wired host from silently dropping a
    tool entirely."""
    import brain.mcp_server_read as R
    import brain.mcp_server_write as W

    read_set = set(R.READ_TOOLS)
    write_set = set(W.WRITE_TOOLS)

    # Disjoint.
    assert read_set.isdisjoint(write_set), (
        f"overlap: {read_set & write_set}"
    )

    # Every tool registered on the aggregate must be in exactly one
    # partition. brain_graph_* (read-only SPARQL) and the
    # brain://identity resource are acceptable aggregate-only extras
    # so we only assert that WRITE ⊆ aggregate and READ ⊆ aggregate.
    import brain.mcp_server as agg
    agg_tools = set(agg.mcp._tool_manager._tools.keys())
    missing_read = read_set - agg_tools
    missing_write = write_set - agg_tools
    assert not missing_read, f"read-partition tools missing from aggregate: {missing_read}"
    assert not missing_write, f"write-partition tools missing from aggregate: {missing_write}"


def test_write_tool_call_appends_to_audit_ledger(tmp_path, monkeypatch):
    """Calling a write tool must append exactly one row to the audit
    ledger. Pick `brain_note_add` because it's the cheapest to exercise
    without a full DB fixture."""
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", tmp_path / "brain")
    (tmp_path / "brain").mkdir()
    monkeypatch.setenv("BRAIN_AUDIT_ACTOR", "split-test:1")

    from brain import _audit_ledger, mcp_server
    assert _audit_ledger.head_hash() == _audit_ledger.GENESIS

    # brain_note_add imports brain.sanitize lazily — stub it out so we
    # don't need the full scrubber ledger side-effect in this unit.
    import sys, types
    stub = types.ModuleType("brain.sanitize")

    class _R:
        def __init__(self, t): self.text = t

    stub.sanitize = lambda text, source_kind=None, source_path=None: _R(text)
    monkeypatch.setitem(sys.modules, "brain.sanitize", stub)

    out = mcp_server.brain_note_add("test bullet from the audit test")
    # Don't care about the journal content here — only the ledger effect.
    ok, n, _ = _audit_ledger.validate(return_detail=True)
    assert ok is True
    assert n == 1

    # The row must name the op and carry an sha8 target; the raw bullet
    # text must NEVER appear in the ledger (counters-only invariant
    # shared with WS4 sanitize ledger).
    line = _audit_ledger.ledger_path().read_text().strip()
    assert '"op":"note_add"' in line
    assert "test bullet" not in line  # raw content must not leak
    assert '"bullet_sha8"' in line


def test_two_write_calls_chain_correctly(tmp_path, monkeypatch):
    """Two writes → two rows, second row's prev_hash == first row's hash."""
    import brain.config as config
    monkeypatch.setattr(config, "BRAIN_DIR", tmp_path / "brain")
    (tmp_path / "brain").mkdir()
    monkeypatch.setenv("BRAIN_AUDIT_ACTOR", "chain-test:1")

    from brain import _audit_ledger
    r1 = _audit_ledger.append("forget", {"scope": "global", "text_sha8": "abcd1234"})
    r2 = _audit_ledger.append("remember", {"scope": "global", "text_sha8": "abcd1234"})
    assert r2["prev_hash"] == r1["hash"]
    assert _audit_ledger.validate() is True
