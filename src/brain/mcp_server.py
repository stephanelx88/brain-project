"""Brain MCP server — exposes the brain to Claude Code as native tools.

Replaces the "preload index.md into the system prompt" model with
on-demand retrieval. Tools:

  brain_search(query, k, type)         → BM25 fact search across the vault
  brain_entities(query, k)             → entity-name search (with summary)
  brain_get(type, name)                → full entity card
  brain_notes(query, k)                → user-note search (BM25 + semantic)
  brain_note_get(path)                 → full body of one vault note
  brain_recent(hours, type, k)         → entities updated since cutoff
  brain_identity()                     → identity + active corrections
  brain_recall(query, k, type)         → hybrid fact + note search (RRF)
  brain_semantic(query, k, type)       → pure semantic fact search
  brain_history(path, limit)           → git commit history for one entity/note
  brain_live_sessions(active_within_sec, include_self)
                                       → live Claude/Cursor sessions
  brain_live_tail(session_id, n)       → last N turns of one live session
  brain_audit(limit)                   → top-N items needing a human decision
  brain_failure_record(...)            → append a row to the failure ledger
  brain_failure_list(...)              → list recorded failures (newest first)
  brain_status()                       → operational dashboard
  brain_stats()                        → high-level counts

Resources:
  brain://identity                     → the three identity markdown files

Run as a stdio MCP server. Wire into ~/.claude/settings.json under
`mcpServers`. Designed to be cheap to invoke (no model calls; pure
SQLite + filesystem reads, sub-50ms — except brain_recall/semantic
which pay the embedding warmup, see _warmup() below).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import brain.config as config
from brain import db, harvest_session

# NOTE: `brain.semantic` is intentionally NOT imported here. It pulls
# torch + sentence-transformers (~2.8 s cold import on M-series Macs),
# which delays the MCP stdio handshake. Cursor surfaces that delay as
# "Brain MCP is still connecting" and the agent falls back to raw-file
# reads — exactly the failure mode we hit on 2026-04-21 with the
# "where is Trinh and Thuha" query. Tools that genuinely need semantic
# import it lazily inside their function bodies (see `_semantic()` below).

mcp = FastMCP("brain")


def _semantic():
    """Lazy import of brain.semantic — pays ~3 s on first call instead
    of every server boot. Subsequent calls hit the module cache."""
    from brain import semantic
    return semantic


def _warmup() -> None:
    """Pre-load the embedding model + run one dummy encode so the first
    real `brain_recall` call doesn't pay the ~17 s cold-start (torch
    import + model weights + first-encode JIT).

    Run in a background thread by `main()` AFTER `mcp.run()` starts —
    we used to call this synchronously before mcp.run(), which delayed
    the MCP handshake by ~20 s and made Cursor timeout. Now the server
    answers BM25/SQLite tools (the common case) instantly while the
    embedding model loads in the background. Semantic tools called
    before warmup completes pay the cost on first call (one-time).

    Set BRAIN_WARMUP=0 to skip the background load entirely (useful in
    tests / on machines without enough RAM for the model)."""
    if os.environ.get("BRAIN_WARMUP", "1") == "0":
        return
    try:
        semantic = _semantic()
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
    semantic = _semantic()
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


_CORRECTIONS_CAP = 20  # most-recent entries to include; rest are recall-able


@mcp.tool()
def brain_identity() -> str:
    """Return identity + recent corrections — what to load at session start.

    Returns who-i-am.md and preferences.md in full, plus the most recent
    `_CORRECTIONS_CAP` entries from corrections.md. Older corrections are
    still searchable via brain_recall("corrections <topic>").
    """
    out = []
    for name in ("who-i-am.md", "preferences.md"):
        p = config.IDENTITY_DIR / name
        if p.exists():
            out.append(f"# {name}\n\n{p.read_text(errors='replace')}")

    corrections_path = config.IDENTITY_DIR / "corrections.md"
    if corrections_path.exists():
        raw = corrections_path.read_text(errors="replace")
        # Split on lines that start a new bullet entry ("- **")
        parts = re.split(r"\n(?=- \*\*)", raw)
        header = parts[0]  # frontmatter + section heading
        entries = [p for p in parts[1:] if p.strip().startswith("- **")]
        recent = entries[-_CORRECTIONS_CAP:]
        older = len(entries) - len(recent)
        body = header + "\n" + "\n".join(recent)
        if older:
            body += (
                f"\n\n<!-- {older} older corrections omitted — "
                "use brain_recall('corrections <topic>') to find them -->"
            )
        out.append(f"# corrections.md\n\n{body}")

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
    semantic = _semantic()
    semantic.ensure_built()
    results = semantic.hybrid_search(query, k=k, type=type)
    try:
        from brain import recall_metric
        recall_metric.log_live_recall(query)
    except Exception:
        pass

    import os as _os
    try:
        threshold = float(_os.environ.get("BRAIN_RECALL_WEAK_RRF", "0.035"))
    except (ValueError, TypeError):
        threshold = 0.035
    top_score = max((h.get("rrf") or 0.0 for h in results), default=0.0)
    weak_match = top_score < threshold
    if not results:
        guidance = "The brain has no record of this."
    elif weak_match:
        guidance = (
            "Hits are below the confidence threshold — do not fabricate answers "
            "or paraphrase hits as answers. Name the file only; do not bridge "
            "from a hit to an answer."
        )
    else:
        guidance = None

    envelope = {
        "query": query,
        "weak_match": weak_match,
        "top_score": top_score,
        "threshold": threshold,
        "guidance": guidance,
        "hits": results,
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2)


@mcp.tool()
def brain_semantic(query: str, k: int = 8, type: str | None = None) -> str:
    """Pure semantic (dense-vector) fact search. Use when you want
    paraphrase recall and don't care about exact keyword matches."""
    semantic = _semantic()
    k = max(1, min(int(k), 25))
    semantic.ensure_built()
    results = semantic.search_facts(query, k=k, type=type)
    try:
        from brain import recall_metric
        recall_metric.log_live_recall(query)
    except Exception:
        pass
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
def brain_live_coverage(days: int = 7, top_miss: int = 10) -> str:
    """Rolling recall coverage computed from real MCP `brain_recall`
    calls over the last `days`.

    Unlike the synthetic eval-set score (which answers "does the brain
    do well on the questions we told it would matter?"), this reflects
    actual usage — the miss rate on whatever Son has been asking this
    week. A high live miss rate means the brain is failing on real
    queries and either needs new canonical entities or needs the eval
    set expanded to capture those topics.

    Returns JSON:
      {
        "window_days": 7,
        "summary": { total_calls, hits, misses, score, avg_top, ... },
        "top_miss_queries": [ { query, misses, hits, best_score,
                                latest_hit }, ... ],
      }

    Use this at session start alongside `brain_audit()` to ask: "what's
    the user's brain consistently failing to remember right now?".
    """
    days = max(1, min(int(days), 90))
    top_miss = max(0, min(int(top_miss), 50))
    try:
        from brain import recall_metric
        summary = recall_metric.live_coverage(days=days)
        misses = recall_metric.top_miss_queries(days=days, n=top_miss)
    except Exception as exc:
        return json.dumps({"error": repr(exc)})
    return json.dumps({
        "window_days": days,
        "summary": summary,
        "top_miss_queries": misses,
    }, ensure_ascii=False, indent=2)


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


def _find_session_jsonl(session_id: str) -> Path | None:
    """Resolve a session_id (Claude UUID, `cursor:<uuid>`, or bare Cursor
    UUID) to its on-disk transcript jsonl. None if not found."""
    want_cursor_only = session_id.startswith(harvest_session.CURSOR_PREFIX)
    bare = session_id.split(":", 1)[-1]
    candidates: list[Path] = []
    if not want_cursor_only:
        candidates.extend(harvest_session.find_all_session_jsonls())
    try:
        candidates.extend(harvest_session.find_cursor_session_jsonls())
    except Exception:
        pass
    for p in candidates:
        if p.stem == bare:
            return p
    return None


@mcp.tool()
def brain_live_sessions(active_within_sec: int = 300, include_self: bool = False) -> str:
    """List Claude Code + Cursor sessions that are alive *right now*.

    Bypasses the harvest/extract pipeline (which is gated by 60-180 s
    idle thresholds). Use this when you need to know what other LLM
    windows are doing in real time — e.g. at session start to coordinate
    with peers, or when the user asks "what else am I working on?".

    Activity rule:
      - Claude Code: PID recorded in ~/.claude/sessions/<pid>.json is alive.
      - Cursor:      transcript jsonl mtime within `active_within_sec` s
                     (Cursor exposes no PID file, so mtime is the proxy).

    `active_within_sec` filters Cursor only. Alive Claude PIDs are always
    returned regardless of recency.

    By default, the calling session itself is excluded from results
    (detected via os.getppid() against the registered Claude PID). Pass
    `include_self=True` to include it. Cursor has no PID->session mapping,
    so Cursor self-exclusion is not possible and is silently skipped.

    Returns JSON list of {source, session_id, project, cwd, last_write,
    age_sec, path}, newest write first. Cursor `session_id`s come back
    namespaced as `cursor:<uuid>` — pass them as-is to brain_live_tail.

    Caps `active_within_sec` to [1, 86400].
    """
    window = max(1, min(int(active_within_sec), 86400))
    now = datetime.now(timezone.utc)
    out: list[dict] = []

    self_sid: str | None = None
    if not include_self:
        ppid = os.getppid()
        for cs in harvest_session.claude_active_sessions():
            if cs["pid"] == ppid:
                self_sid = cs["session_id"]
                break

    for cs in harvest_session.claude_active_sessions():
        if cs["session_id"] == self_sid:
            continue
        jsonl = _find_session_jsonl(cs["session_id"])
        last_write_iso = None
        age = None
        path_str = None
        project = ""
        if jsonl is not None:
            try:
                mtime = jsonl.stat().st_mtime
                last_write_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                age = int(now.timestamp() - mtime)
                path_str = str(jsonl)
                project = harvest_session.derive_project_name(jsonl)
            except OSError:
                pass
        out.append({
            "source": "claude",
            "session_id": cs["session_id"],
            "project": project,
            "cwd": cs["cwd"],
            "pid": cs["pid"],
            "last_write": last_write_iso,
            "age_sec": age,
            "path": path_str,
        })

    cutoff = now.timestamp() - window
    try:
        cursor_jsonls = harvest_session.find_cursor_session_jsonls()
    except Exception:
        cursor_jsonls = []
    for jsonl in cursor_jsonls:
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        out.append({
            "source": "cursor",
            "session_id": f"{harvest_session.CURSOR_PREFIX}{jsonl.stem}",
            "project": harvest_session.derive_project_name(jsonl),
            "cwd": None,
            "pid": None,
            "last_write": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "age_sec": int(now.timestamp() - mtime),
            "path": str(jsonl),
        })

    out.sort(key=lambda r: r.get("age_sec") if r.get("age_sec") is not None else 10**9)
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool()
def brain_live_tail(session_id: str, n: int = 20) -> str:
    """Return the last N user/assistant turns of one live session.

    Realtime read of the raw transcript — no LLM, no harvest, no idle
    gating. Pair with brain_live_sessions to see what a peer window is
    currently doing.

    Args:
      session_id: a Claude UUID, a `cursor:<uuid>` ID returned by
                  brain_live_sessions, or a bare Cursor UUID. We try
                  Claude first for bare IDs, then fall back to Cursor.
      n: max turns to return (default 20, capped at 200).

    Returns JSON {source, session_id, project, last_write, turns:
    [{role, text, timestamp}]} or {error}.
    """
    n = max(1, min(int(n), 200))
    session_id = (session_id or "").strip()
    if not session_id:
        return json.dumps({"error": "session_id is required"})

    jsonl = _find_session_jsonl(session_id)
    if jsonl is None:
        return json.dumps({"error": f"session not found: {session_id}"})

    try:
        messages, _ = harvest_session.extract_messages(jsonl, start_offset=0)
    except Exception as e:
        return json.dumps({"error": f"failed to read transcript: {e}"})

    try:
        mtime = jsonl.stat().st_mtime
        last_write = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except OSError:
        last_write = None

    source = "cursor" if harvest_session.is_cursor_path(jsonl) else "claude"
    return json.dumps({
        "source": source,
        "session_id": session_id,
        "project": harvest_session.derive_project_name(jsonl),
        "last_write": last_write,
        "turns": messages[-n:],
        "total_turns": len(messages),
    }, ensure_ascii=False, indent=2)


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
def brain_failure_record(
    source: str,
    tool: str | None = None,
    query: str | None = None,
    result_digest: str | None = None,
    user_correction: str | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
    extra: dict | None = None,
) -> str:
    """Append one row to the brain's failure ledger. Returns `{"id": ...}`.

    Substrate for the self-correction loop: record a structured failure
    event (wrong recall, bad extraction, template drift, etc.) so a
    later wave can drive patches or verify fixes. Consumers do not yet
    read the ledger automatically — use this when you want to make an
    incident queryable.

    `source` is required ("recall" | "extraction" | "template_drift" |
    "manual" | ...); every other field is optional. `tags` accepts a
    list of strings; `extra` is an open-ended dict for per-source metadata.
    """
    from brain import failures
    fid = failures.record_failure(
        source=source,
        tool=tool,
        query=query,
        result_digest=result_digest,
        user_correction=user_correction,
        tags=list(tags) if tags else [],
        session_id=session_id,
        extra=dict(extra) if extra else None,
    )
    return json.dumps({"id": fid})


@mcp.tool()
def brain_failure_list(
    source: str | None = None,
    tag: str | None = None,
    unresolved_only: bool = False,
    limit: int = 50,
) -> str:
    """List recorded failures, newest first. Filters compose (AND).

    Use to surface recent unresolved failures at session start or when
    investigating a class of bugs ("what recall misses have I logged
    this week?"). Returns a JSON list of ledger rows — see
    `brain.failures` for the schema.
    """
    from brain import failures
    rows = failures.list_failures(
        source=source,
        tag=tag,
        unresolved_only=bool(unresolved_only),
        limit=int(limit),
    )
    return json.dumps(rows, ensure_ascii=False, indent=2)


@mcp.tool()
def brain_status() -> str:
    """Operational dashboard — is anything running in the background?

    Returns JSON with: launchd job state, in-flight lock, last/next run
    timing, currently-spawned brain/LLM processes, ledger sizes, pending
    audit count, and vault counts.

    Use this to answer 'is the brain doing anything right now?' without
    the user having to know launchctl, the log path, the lock dir, etc.
    Cheap (one launchctl + one ps call); safe to call freely.
    """
    from brain import status as status_mod
    return status_mod.to_json(status_mod.gather())


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
    # Kick warmup off in a daemon thread BEFORE mcp.run() so the
    # embedding model loads in the background while the MCP stdio
    # handshake completes immediately. Previously we ran _warmup()
    # synchronously here, which delayed the handshake by ~20 s and
    # caused Cursor to report "Brain MCP is still connecting" — the
    # agent then fell back to raw-file reads (incident 2026-04-21).
    #
    # Daemon=True: don't block server shutdown if warmup is still
    # mid-load (rare; Mac mps usually finishes in ~15 s).
    import threading
    threading.Thread(target=_warmup, daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()
