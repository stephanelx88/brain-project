"""Brain write MCP server — 9 mutation tools, hash-chained audit.

Entry point for the user's primary host only. Registration is
gated by `BRAIN_WRITE=1` (default on primary, absent elsewhere);
`brain install` wires this by reading `$HOME/.brain/.brain.conf`.

Every write-tool call appends a hash-chained row to
`~/.brain/.audit/ledger.jsonl` inside the tool's implementation (see
`brain._audit_ledger` + wrappers in `brain.mcp_server`). That means
calling a write tool via ANY entry point — this split server, the
aggregate `brain.mcp_server`, or a direct Python import in a test —
produces the same audit trail. Audit is not bolted on at the
registration layer; it's a property of the write operation.

Sibling of `brain.mcp_server_read`. See `docs/10x-plan.md` WS5.
"""

from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP

from brain import mcp_server as _agg


mcp = FastMCP("brain-write")


# Write tool names — the authoritative partition.
# Keep in sync with mcp_server_read.READ_TOOLS and with the
# partition assertion in tests/test_mcp_server_split.py.
WRITE_TOOLS: tuple[str, ...] = (
    "brain_remember",
    "brain_note_add",
    "brain_retract_fact",
    "brain_correct_fact",
    "brain_forget",
    "brain_mark_reviewed",
    "brain_mark_contested",
    "brain_resolve_contested",
    "brain_failure_record",
    "brain_send",
    "brain_set_name",
)


def _write_enabled() -> bool:
    """Registration gate. Default ON (so `pip install` + `brain install`
    on a primary host "just works" without needing to know the flag).
    Non-primary hosts set `BRAIN_WRITE=0` in their MCP-server env
    block, which keeps this process from exposing any write tool.
    """
    raw = os.environ.get("BRAIN_WRITE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _register_write_tools() -> None:
    """Register each write tool on our FastMCP, delegating to the
    aggregate module's existing implementation. Audit-ledger append
    lives inside each implementation, so this layer adds nothing.
    """
    if not _write_enabled():
        return
    for name in WRITE_TOOLS:
        fn = getattr(_agg, name, None)
        if not callable(fn):
            raise RuntimeError(
                f"mcp_server_write: tool {name!r} missing from mcp_server aggregate"
            )
        mcp.tool()(fn)


_register_write_tools()


def main() -> None:
    """Run the write MCP server over stdio.

    When `BRAIN_WRITE=0`, we still start the server (so a bad host
    wiring doesn't silently vanish) but register zero tools. The
    caller sees "brain-write connected, 0 tools" — which is what
    `brain doctor` looks for to produce the "half-wired" warning.
    """
    if not _write_enabled():
        print(
            "brain-write: BRAIN_WRITE is disabled on this host — no tools registered.",
            file=sys.stderr,
        )
    mcp.run()


if __name__ == "__main__":
    main()
