"""Brain MCP server — exposes the brain to Claude Code as native tools.

Replaces the "preload index.md into the system prompt" model with
on-demand retrieval. Tools:

  brain_search(query, k, type)    → BM25 fact search across the vault
  brain_entities(query, k)        → entity-name search (with summary)
  brain_get(type, name)           → full entity card
  brain_recent(hours, type)       → entities updated since cutoff
  brain_identity()                → identity + active corrections

Resources:
  brain://identity                → the three identity markdown files
  brain://entity/<type>/<slug>    → one entity file

Run as a stdio MCP server. Wire into ~/.claude/settings.json under
`mcpServers`. Designed to be cheap to invoke (no model calls; pure
SQLite + filesystem reads, sub-50ms).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import brain.config as config
from brain import db, semantic

mcp = FastMCP("brain")


@mcp.tool()
def brain_search(query: str, k: int = 8, type: str | None = None) -> str:
    """BM25 fact search across the brain.

    Args:
      query: free-text. Punctuation is ignored; tokens are OR-combined.
      k: max results (default 8, capped 25)
      type: optional filter — one of people, projects, clients, domains,
            decisions, issues, insights, evolutions, meetings.

    Returns a JSON array of {type,name,path,text,source,date,score}.
    """
    k = max(1, min(int(k), 25))
    rows = db.search(query, k=k, type=type)
    return json.dumps(rows, ensure_ascii=False, indent=2)


@mcp.tool()
def brain_entities(query: str, k: int = 8) -> str:
    """Entity-name search. Use when you want the entity itself, not facts.

    Returns a JSON array of {type,name,path,summary,score}.
    """
    k = max(1, min(int(k), 25))
    rows = db.search_entities(query, k=k)
    return json.dumps(rows, ensure_ascii=False, indent=2)


@mcp.tool()
def brain_get(type: str, name: str) -> str:
    """Return the full markdown of one entity, addressed by type+name.

    `name` may be the canonical name OR an alias. If not found returns
    JSON with `error`.
    """
    type = (type or "").strip().lower()
    name = (name or "").strip()
    if not type or not name:
        return json.dumps({"error": "type and name are required"})

    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.path FROM entities e
            LEFT JOIN aliases a ON a.entity_id = e.id
            WHERE e.type = ? AND (LOWER(e.name) = LOWER(?) OR a.alias = LOWER(?))
            LIMIT 1
            """,
            (type, name, name),
        )
        row = cur.fetchone()
    if not row:
        return json.dumps({"error": f"not found: {type}/{name}"})
    path = config.BRAIN_DIR / row[0]
    if not path.exists():
        return json.dumps({"error": f"file missing: {row[0]}"})
    return path.read_text(errors="replace")


@mcp.tool()
def brain_recent(hours: int = 48, type: str | None = None, k: int = 20) -> str:
    """List entities last_updated within the last N hours.

    Useful at session start: "what changed since I last worked?"
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(hours))).strftime("%Y-%m-%d")
    sql = """
      SELECT type, name, path, summary, last_updated
      FROM entities
      WHERE last_updated >= ?
    """
    args: list = [cutoff]
    if type:
        sql += " AND type = ?"
        args.append(type)
    sql += " ORDER BY last_updated DESC LIMIT ?"
    args.append(int(k))
    with db.connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    cols = ["type", "name", "path", "summary", "last_updated"]
    return json.dumps([dict(zip(cols, r)) for r in rows], ensure_ascii=False, indent=2)


@mcp.tool()
def brain_identity() -> str:
    """Return identity + corrections — what to load at session start.

    Concatenates identity/who-i-am.md, identity/preferences.md, and
    identity/corrections.md so you don't have to fetch them separately.
    """
    out = []
    for name in ("who-i-am.md", "preferences.md", "corrections.md"):
        p = config.IDENTITY_DIR / name
        if p.exists():
            out.append(f"# {name}\n\n{p.read_text(errors='replace')}")
    return "\n\n---\n\n".join(out) if out else "(no identity files)"


@mcp.tool()
def brain_recall(query: str, k: int = 8, type: str | None = None) -> str:
    """Hybrid (BM25 + semantic) recall — recommended default.

    Use this whenever you would have used `brain_search`. Hybrid catches
    paraphrases ("how do I avoid the freeze" → "dual-instance Mac freeze
    prevention") AND exact-keyword hits in one ranked list.

    Returns JSON list of {type,name,text,source,rrf} sorted by RRF score.
    """
    k = max(1, min(int(k), 25))
    semantic.ensure_built()
    return json.dumps(
        semantic.hybrid_search(query, k=k, type=type),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def brain_semantic(query: str, k: int = 8, type: str | None = None) -> str:
    """Pure semantic (dense-vector) fact search. Use when you want
    paraphrase recall and don't care about exact keyword matches."""
    k = max(1, min(int(k), 25))
    semantic.ensure_built()
    return json.dumps(
        semantic.search_facts(query, k=k, type=type),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def brain_stats() -> str:
    """High-level counts. Useful sanity check."""
    with db.connect() as conn:
        ents = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        by_type = dict(
            conn.execute("SELECT type, COUNT(*) FROM entities GROUP BY type").fetchall()
        )
    return json.dumps(
        {"entities": ents, "facts": facts, "by_type": by_type},
        indent=2,
    )


@mcp.resource("brain://identity")
def identity_resource() -> str:
    return brain_identity()


def main():
    mcp.run()


if __name__ == "__main__":
    main()
