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


def _warmup() -> None:
    """Pre-load the embedding model + run one dummy encode so the first
    real `brain_recall` call doesn't pay the ~7 s cold-start (torch import
    + model weights + first-encode JIT). Runs synchronously before
    mcp.run() so the model is in RAM by the time Claude's first tool call
    lands. Adds ~7 s to server boot, but boot happens before the user
    types — so it's invisible.

    Set BRAIN_WARMUP=0 to disable (useful for tests / fast-start dev)."""
    import os
    if os.environ.get("BRAIN_WARMUP", "1") == "0":
        return
    try:
        semantic.ensure_built()
        semantic._embed(["warmup"])
    except Exception:
        pass


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
def brain_notes(query: str, k: int = 8) -> str:
    """Search user-written notes anywhere in the vault.

    Returns notes that the user typed directly into Obsidian (anywhere
    outside `entities/`). Results are hybrid (BM25 + semantic). The
    filename and the first heading both count as the title — so a file
    named `son dang o long xuyen.md` is findable even when its body is
    empty.

    Returns JSON list of {title, path, snippet, mtime}.
    """
    k = max(1, min(int(k), 25))
    semantic.ensure_built()
    # Prefer the lexical hit when present (exact filename matches), then
    # backfill with semantic. Caller gets the union, deduped by path.
    seen = set()
    out = []
    for hit in db.search_notes(query, k=k):
        if hit["path"] in seen:
            continue
        seen.add(hit["path"])
        out.append({
            "title": hit["title"],
            "path": hit["path"],
            "snippet": hit["snippet"],
            "mtime": hit["mtime"],
        })
    for hit in semantic.search_notes(query, k=k):
        if hit["path"] in seen:
            continue
        seen.add(hit["path"])
        out.append({
            "title": hit["title"],
            "path": hit["path"],
            "snippet": hit["snippet"],
            "score": hit["score"],
        })
        if len(out) >= k:
            break
    return json.dumps(out[:k], ensure_ascii=False, indent=2)


@mcp.tool()
def brain_note_get(path: str) -> str:
    """Return the full body of a vault note. `path` is relative to ~/.brain/."""
    p = config.BRAIN_DIR / path
    try:
        # Resolve & confine to BRAIN_DIR — no escapes via ../..
        p = p.resolve()
        p.relative_to(config.BRAIN_DIR.resolve())
    except (ValueError, OSError):
        return json.dumps({"error": f"path outside vault: {path}"})
    if not p.exists() or not p.is_file():
        return json.dumps({"error": f"not found: {path}"})
    return p.read_text(errors="replace")


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
    """Hybrid (BM25 + semantic) recall — RECOMMENDED DEFAULT.

    Searches across BOTH:
      - extracted facts/entities (`entities/<type>/*.md`)
      - free-form notes anywhere else in the vault (e.g. a root file
        named `son dang o long xuyen.md`)

    Catches paraphrases ("how do I avoid the freeze" → "dual-instance
    Mac freeze prevention") AND exact-keyword/filename hits in one
    ranked list.

    Each result has a `kind` field of "fact" or "note" so you can tell
    where it came from. Sorted by Reciprocal-Rank Fusion score.
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
def brain_history(path: str, limit: int = 10) -> str:
    """Return git commit history for one entity/note path.

    Inspired by @shikhr_'s observation that git is already an episodic
    memory layer for agents (https://x.com/shikhr_/status/...). Use this
    to see how an entity evolved over time — author, date, subject, and
    file diff stats — without leaving the brain MCP.

    Args:
      path: relative path under ~/.brain/ (e.g. `entities/people/madhav.md`)
      limit: max commits (default 10, capped at 50)

    Returns JSON list of {sha, date, author, subject, insertions, deletions}.
    """
    import subprocess
    limit = max(1, min(int(limit), 50))
    p = config.BRAIN_DIR / path
    try:
        p.resolve().relative_to(config.BRAIN_DIR.resolve())
    except (ValueError, OSError):
        return json.dumps({"error": f"path outside vault: {path}"})
    try:
        out = subprocess.check_output(
            ["git", "log",
             f"-{limit}",
             "--pretty=format:%H\t%aI\t%an\t%s",
             "--shortstat",
             "--", path],
            cwd=str(config.BRAIN_DIR),
            stderr=subprocess.STDOUT,
            timeout=10,
        ).decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        return json.dumps({"error": f"git failed: {e.output.decode(errors='replace')}"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "git timed out"})
    except FileNotFoundError:
        return json.dumps({"error": "git not on PATH"})

    commits: list[dict] = []
    cur: dict | None = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line and len(line.split("\t", 3)) == 4:
            if cur:
                commits.append(cur)
            sha, date, author, subject = line.split("\t", 3)
            cur = {"sha": sha[:12], "date": date, "author": author,
                   "subject": subject, "insertions": 0, "deletions": 0}
        elif cur and ("insertion" in line or "deletion" in line):
            for tok in line.replace(",", "").split():
                if tok.isdigit():
                    n = int(tok)
                elif tok.startswith("insertion"):
                    cur["insertions"] = n
                elif tok.startswith("deletion"):
                    cur["deletions"] = n
    if cur:
        commits.append(cur)
    return json.dumps(commits, ensure_ascii=False, indent=2)


@mcp.tool()
def brain_audit(limit: int = 3) -> str:
    """Top-N audit items the user should review (contested facts, high-confidence
    merge candidates, decayed single-source claims). Call this at session start
    and surface anything returned to the user as a brief 'brain has N items
    needing a quick decision' nudge — don't list them all, just flag the count
    and offer to walk through them on request.

    Returns the same compact block the SessionStart hook prints. Empty string
    when the brain is clean; in that case, surface nothing.
    """
    from brain import audit as audit_mod
    limit = max(0, min(int(limit), 10))
    items = audit_mod.top_n(limit=limit)
    return audit_mod.format_for_session(items)


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
    _warmup()
    mcp.run()


if __name__ == "__main__":
    main()
