"""Brain read-only MCP server — 19 tools, no mutations.

Entry point for Claude Code / Cursor / any untrusted stdio caller.
Exposes retrieval, listing, status, and dashboard tools; has no
ability to write to the vault or the audit ledger.

Sibling of `brain.mcp_server_write`. See `docs/10x-plan.md` WS5 for
the rationale — blast-radius protection for the ingest-autonomy
guarantee: any local process can READ the brain (read is what makes
it useful) but cannot silently mutate or erase it.

Registered as `brain-read` in ~/.claude/settings.json + Cursor
mcp.json. Runs in every host that consumes the brain. The aggregate
server `brain.mcp_server` remains for backward compat through one
release cycle; new hosts should wire the split.

Tool partition is enforced by the factory in `_read_tools()`. The
partition lives in ONE place (this module); the tests in
`test_mcp_server_split.py` assert read + write are disjoint and
cover the union.
"""

from __future__ import annotations

import os
import threading

from mcp.server.fastmcp import FastMCP

import brain.config as config
from brain import mcp_server as _agg


mcp = FastMCP("brain-read")


# Read-only tool names — the authoritative partition.
# Keep in sync with mcp_server_write.WRITE_TOOLS and with the
# partition assertion in tests/test_mcp_server_split.py.
READ_TOOLS: tuple[str, ...] = (
    "brain_recall",
    "brain_search",
    "brain_semantic",
    "brain_entities",
    "brain_notes",
    "brain_get",
    "brain_note_get",
    "brain_identity",
    "brain_recent",
    "brain_stats",
    "brain_status",
    "brain_history",
    "brain_live_sessions",
    "brain_live_tail",
    "brain_live_coverage",
    "brain_audit",
    "brain_learning_gaps",
    "brain_failure_list",
    "brain_tombstones",
    "brain_inbox",
    "brain_progress",
)


def _register_read_tools() -> None:
    """Register every read tool from the aggregate module on our
    FastMCP instance.

    We delegate to the existing implementations in `brain.mcp_server`
    rather than copying them. The aggregate module stays the canonical
    home for tool logic; this module is the partition + entry point.
    """
    for name in READ_TOOLS:
        fn = getattr(_agg, name, None)
        if not callable(fn):
            # Missing tool = partition drift. Loud failure is correct —
            # better than silently shipping a half-registered server.
            raise RuntimeError(
                f"mcp_server_read: tool {name!r} missing from mcp_server aggregate"
            )
        mcp.tool()(fn)


_register_read_tools()


# Identity also exposed as a resource for clients that prefer
# URI-addressed context over a tool call.
@mcp.resource("brain://identity")
def identity_resource() -> str:
    return _agg.brain_identity()


def main() -> None:
    """Run the read-only MCP server over stdio."""
    threading.Thread(target=_agg._warmup, daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()
