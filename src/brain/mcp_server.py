"""Brain MCP server — aggregate (DEPRECATED, transition only).

.. deprecated:: 2026-04-23 (WS5)
   New installs register the split pair `brain-read` + `brain-write`
   (see `brain.mcp_server_read`, `brain.mcp_server_write`). This
   module stays around for one release cycle so hosts wired against
   the old single `brain` server keep working; all tool logic lives
   here and the split servers delegate to it. Remove after WS5 has
   been in main for 2 weeks and `brain doctor` has had time to
   re-wire every host.

Blast-radius split (WS5): read tools are safe for any local process;
write tools mutate the vault + append a hash-chained audit entry.
Host wiring: untrusted hosts get `brain-read` only; the user's
primary host gets both.

Exposes the brain to Claude Code as native tools. Replaces the
"preload index.md into the system prompt" model with on-demand
retrieval. Tools:

  brain_search(query, k, type, verbose=False, debug=False)
                                       → hybrid fact search (BM25 + semantic fallback)
  brain_entities(query, k, verbose=False, debug=False)
                                       → hybrid entity-name search (BM25 + semantic fallback)
  brain_get(type, name)                → full entity card
  brain_notes(query, k, verbose=False, debug=False)
                                       → user-note search (BM25 + semantic)
  brain_note_get(path)                 → full body of one vault note
  brain_note_add(text, tags)           → append knowledge bullet to today's journal file
  brain_recent(hours, type, k)         → entities updated since cutoff
  brain_identity()                     → identity + active corrections
  brain_recall(query, k, type, verbose=False, debug=False)
                                       → hybrid fact + note search (RRF, compact envelope)
  brain_semantic(query, k, type, verbose=False, debug=False)
                                       → pure semantic fact search
  brain_history(path, limit)           → git commit history for one entity/note
  brain_live_sessions(active_within_sec, include_self)
                                       → live Claude/Cursor sessions
  brain_live_tail(session_id, n)       → last N turns of one live session
  brain_audit(limit)                   → top-N items needing a human decision
  brain_mark_reviewed(path)            → confirm a single-source entity (stamp `reviewed: today`)
  brain_mark_contested(path)           → flag an entity as contested
  brain_resolve_contested(path)        → clear the contested flag
  brain_failure_record(...)            → append a row to the failure ledger
  brain_failure_list(...)              → list recorded failures (newest first)
  brain_learning_gaps(...)             → repeated recall misses to surface
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
import time
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

import brain.config as config
from brain import _audit_ledger, _projection, db


def _sha8(s: str) -> str:
    """8-char sha256 prefix — stable correlation key for the audit
    ledger without leaking the original content."""
    import hashlib
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:8]

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


_LAST_FRESH_TICK: float = 0.0


def _ensure_fresh() -> None:
    """Bring the indexes in line with current filesystem state.

    Runs cheap sweeps before a read tool answers so mutations that
    happened since the last pipeline tick are reflected immediately:

      1. `sync_mutated_entities` — reindex entity files whose on-disk
         mtime is newer than the indexed_mtime column (direct edits
         via Obsidian/vim/git-pull).
      2. `ingest_notes.ingest_all` — detect created/edited/deleted
         vault notes and cascade fact invalidations + tombstones for
         deleted sources.
      3. `gc_orphaned_entities` + incremental semantic update — purge
         entity rows whose markdown is gone and embed any facts that
         landed since the last `.vec` build.

    **Per-source watermarks (WS3, 2026-04-23)**: each fs-walking sweep
    is skipped when its source dir's newest mtime is not past the
    watermark recorded at `<BRAIN_DIR>/.freshness.json`. An idle vault
    therefore pays only the cheap recursive stat (via
    `brain.freshness`) instead of three full walks. When a sweep runs,
    it bumps the watermark. DB-side sweeps (gc, semantic probe) run
    unconditionally — their inputs are rows, not file mtimes.

    **Called from every read-path tool** (brain_recall, brain_search,
    brain_notes, brain_semantic, brain_entities, brain_recent).
    Previously only brain_recall paid this tax, which produced the
    2026-04-23 `son.md` incident: a user wrote a note, asked about it,
    claude's brain call happened to hit `brain_notes` / `brain_search`
    — neither of which refreshed — so the just-written note didn't
    surface and claude truthfully reported "brain has no record".
    Making the sweep uniform closes that asymmetry.

    **Throttled** to avoid hammering when claude makes 3-5 brain calls
    in a row: if this ran within `BRAIN_RECALL_FRESH_THROTTLE_SEC`
    (default 1.0 s), skip. The stat-sweep cost on a warm cache is
    ~10-40 ms but paying it three times inside 300 ms would still show
    up on latency-sensitive loops.

    Each sweep is idempotent. Set `BRAIN_RECALL_ENSURE_FRESH=0` to
    disable entirely if the cost ever shows up in a hot-path profile
    — the pipeline will then fall back to its scheduled ticks for
    consistency.
    """
    global _LAST_FRESH_TICK
    if os.environ.get("BRAIN_RECALL_ENSURE_FRESH", "1") == "0":
        return
    try:
        throttle = float(os.environ.get("BRAIN_RECALL_FRESH_THROTTLE_SEC", "1.0"))
    except (ValueError, TypeError):
        throttle = 1.0
    now = time.monotonic()
    if now - _LAST_FRESH_TICK < throttle:
        return
    _LAST_FRESH_TICK = now

    from brain import freshness

    try:
        entities_mtime = freshness.entities_dir_mtime()
    except Exception:
        entities_mtime = 0.0
    try:
        notes_mtime = freshness.notes_dir_mtime()
    except Exception:
        notes_mtime = 0.0

    if freshness.needs_sweep("entities", probe_mtime=entities_mtime):
        try:
            db.sync_mutated_entities()
            freshness.bump("entities", entities_mtime)
        except Exception:
            pass

    if freshness.needs_sweep("notes", probe_mtime=notes_mtime):
        try:
            from brain import ingest_notes
            ingest_notes.ingest_all()
            freshness.bump("notes", notes_mtime)
        except Exception:
            pass

    try:
        db.gc_orphaned_entities()
    except Exception:
        pass
    try:
        semantic = _semantic()
        semantic.ensure_built()
    except Exception:
        pass


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
def brain_search(
    query: str,
    k: int = 8,
    type: str | None = None,
    verbose: bool = False,
    debug: bool = False,
) -> str:
    """Hybrid fact search across the brain (BM25 + semantic fallback).

    BM25 handles exact-keyword queries; semantic fills the gap for
    Vietnamese / CJK / paraphrase queries where BM25 produces zero or
    sparse results. Results are deduped by canonical-fact-hash.

    Args:
      query: free-text. May be Vietnamese, Chinese, or any language.
      k: max results (default 8, capped 25)
      type: optional filter — one of people, projects, clients, domains,
            decisions, issues, insights, evolutions, meetings.
      verbose/debug: see `brain_recall` for tier shapes.

    Envelope: `{query, weak_match, guidance, hits}`. `weak_match` here
    = "no hits returned" (this tool does not compute RRF semantics);
    for confidence-scored recall, use `brain_recall`.

    Strict-claim mode (BRAIN_USE_CLAIMS=1 + BRAIN_STRICT_CLAIMS=1):
    delegates to `_recall_strict_claims` so this tool returns the
    same claim-only hit shape as `brain_recall`. The `type` filter
    is ignored in strict mode (claims aren't typed by entity-type
    yet).
    """
    if _strict_claims_misconfigured():
        return json.dumps({
            "error": "configuration_error",
            "detail": "BRAIN_STRICT_CLAIMS=1 requires BRAIN_USE_CLAIMS=1",
        }, ensure_ascii=False, indent=2)
    if _strict_claims_enabled():
        return _recall_strict_claims(query, max(1, min(int(k), 25)), verbose)
    if _projection.default_verbose() and not debug:
        verbose = True
    _ensure_fresh()
    k = max(1, min(int(k), 25))
    # Over-fetch so canonical-fact-hash dedup doesn't leave the response short.
    fetch_k = min(k * 2, 25)
    seen: set = set()
    merged: list = []

    for hit in db.search(query, k=fetch_k, type=type):
        key = (hit["type"], hit["name"], (hit["text"] or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        hit.setdefault("kind", "fact")
        merged.append(hit)

    sem = _semantic()
    sem.ensure_built()
    for hit in sem.search_facts(query, k=fetch_k, type=type):
        key = (hit["type"], hit["name"], (hit["text"] or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        merged.append({
            "kind": "fact",
            "type": hit["type"],
            "name": hit["name"],
            "slug": hit.get("slug"),
            "path": hit.get("path") or f"entities/{hit['type']}/{hit['slug']}.md",
            "text": hit["text"],
            "source": hit.get("source") or "",
            "date": hit.get("date"),
            "score": hit["score"],
        })
        if len(merged) >= fetch_k:
            break

    projected = _projection.project_hits(
        merged, k=k, verbose=verbose, debug=debug,
    )
    weak_match = not projected
    guidance = "The brain has no record of this." if weak_match else None
    env = _projection.envelope(
        query, projected,
        weak_match=weak_match, guidance=guidance, debug=debug,
        fetch_k=fetch_k,
    )
    return json.dumps(env, ensure_ascii=False)


@mcp.tool()
def brain_entities(
    query: str,
    k: int = 8,
    verbose: bool = False,
    debug: bool = False,
) -> str:
    """Hybrid entity-name search (BM25 + semantic fallback).

    Use when you want the entity itself, not individual facts.
    BM25 handles exact-name queries; semantic fills gaps for
    Vietnamese / CJK / paraphrase names.

    Envelope: `{query, weak_match, guidance, hits}`. Default hit shape:
    `{kind, path, text, name, entity_summary?}` where `text` is the
    entity summary (no per-fact text for this tool).
    """
    if _projection.default_verbose() and not debug:
        verbose = True
    _ensure_fresh()
    k = max(1, min(int(k), 25))
    seen: set = set()
    merged: list = []

    for hit in db.search_entities(query, k=k):
        key = (hit["type"], hit["name"])
        if key in seen:
            continue
        seen.add(key)
        # Entity row → fact-shaped for projection so the default-tier
        # output is uniform across tools. "text" carries the summary.
        merged.append({
            "kind": "fact",
            "type": hit["type"],
            "name": hit["name"],
            "slug": hit.get("slug"),
            "path": hit.get("path"),
            "text": hit.get("summary") or "",
            "entity_summary": hit.get("summary") or "",
            "score": hit.get("score"),
        })

    sem = _semantic()
    sem.ensure_built()
    for hit in sem.search_entities(query, k=k):
        key = (hit["type"], hit["name"])
        if key in seen:
            continue
        seen.add(key)
        merged.append({
            "kind": "fact",
            "type": hit["type"],
            "name": hit["name"],
            "slug": hit.get("slug"),
            "path": hit.get("path"),
            "text": hit.get("summary") or "",
            "entity_summary": hit.get("summary") or "",
            "score": hit.get("score"),
        })
        if len(merged) >= k:
            break

    projected = _projection.project_hits(
        merged, k=k, verbose=verbose, debug=debug,
    )
    weak_match = not projected
    guidance = "The brain has no record of this." if weak_match else None
    env = _projection.envelope(
        query, projected,
        weak_match=weak_match, guidance=guidance, debug=debug,
    )
    return json.dumps(env, ensure_ascii=False)


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
def brain_notes(
    query: str,
    k: int = 8,
    verbose: bool = False,
    debug: bool = False,
) -> str:
    """Search user-written notes anywhere in the vault.

    Returns notes that the user typed directly into Obsidian (anywhere
    outside `entities/`). Results are hybrid (BM25 + semantic). The
    filename and the first heading both count as the title — so a file
    named `son dang o long xuyen.md` is findable even when its body is
    empty.

    Envelope: `{query, weak_match, guidance, hits}`. Default hit shape:
    `{kind="note", path, text}` where `text` is the note's snippet
    (body[:200] by default), subject to the same snippet cap as other
    tools. Verbose adds `{title, mtime}`.
    """
    if _projection.default_verbose() and not debug:
        verbose = True
    _ensure_fresh()
    k = max(1, min(int(k), 25))
    semantic = _semantic()
    semantic.ensure_built()
    # Prefer lexical (exact filename hits), then backfill with semantic.
    # Caller gets the union, deduped by path.
    seen = set()
    merged: list = []
    for hit in db.search_notes(query, k=k):
        if hit["path"] in seen:
            continue
        seen.add(hit["path"])
        merged.append({
            "kind": "note",
            "title": hit.get("title"),
            "path": hit["path"],
            "text": hit.get("snippet") or "",
            "mtime": hit.get("mtime"),
            "score": hit.get("score"),
        })
    for hit in semantic.search_notes(query, k=k):
        if hit["path"] in seen:
            continue
        seen.add(hit["path"])
        merged.append({
            "kind": "note",
            "title": hit.get("title"),
            "path": hit["path"],
            "text": hit.get("snippet") or "",
            "score": hit.get("score"),
        })
        if len(merged) >= k:
            break
    projected = _projection.project_hits(
        merged, k=k, verbose=verbose, debug=debug,
    )
    weak_match = not projected
    guidance = "The brain has no record of this." if weak_match else None
    env = _projection.envelope(
        query, projected,
        weak_match=weak_match, guidance=guidance, debug=debug,
    )
    return json.dumps(env, ensure_ascii=False)


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
def brain_note_add(text: str, tags: list[str] | None = None) -> str:
    """Append a knowledge bullet to today's journal file. THIS IS HOW AGENTS
    CAPTURE USER KNOWLEDGE — never use the generic Write tool to create
    one-off `.md` files under ~/.brain/, or the vault root fills with
    single-line orphans (the `messy vault` problem).

    Convention: one journal file per local day at
    `journal/YYYY-MM-DD.md`. Each call appends one line:
        - _HH:MM_ <text> #tag1 #tag2

    The line is the unit of fact-extraction provenance — keep it
    one-statement, plain prose. The extractor (note_extract.py) reads
    each new sha and turns it into entity facts on the next scheduler
    tick. Returns `{path, line, journal_existed}` so the agent knows
    what landed.

    `tags` — optional, gets joined as `#tag` tokens on the same line.
    Useful for steering extraction (e.g. ["people", "location"]).
    """
    text = (text or "").strip()
    if not text:
        return json.dumps({"error": "empty text"})
    # WS4: scrub the bullet before it lands on disk. Policy is
    # `journal` — user-authored content, so secrets REJECT and most
    # injection tripwires FLAG. A user who pastes an API key into
    # brain_note_add gets a redaction stub in its place; intentional
    # self-description passes untouched.
    try:
        from brain.sanitize import sanitize
        scrub = sanitize(text, source_kind="journal", source_path="brain_note_add")
        text = scrub.text.strip()
        if not text:
            return json.dumps({"error": "empty text after sanitize"})
    except Exception:
        pass
    # Local date — journal is for the user, not for UTC machines. A
    # journal entry written at 6 AM Vietnam time should land in today's
    # local file, not yesterday's UTC file.
    now = datetime.now()
    rel = f"journal/{now.strftime('%Y-%m-%d')}.md"
    path = config.BRAIN_DIR / rel
    existed = path.exists()
    bullet = f"- _{now.strftime('%H:%M')}_ {text}"
    if tags:
        clean = [t.strip().lstrip("#") for t in tags if t and t.strip()]
        if clean:
            bullet += " " + " ".join(f"#{t}" for t in clean)
    if existed:
        body = path.read_text(errors="replace")
        if not body.endswith("\n"):
            body += "\n"
        new_body = body + bullet + "\n"
    else:
        # First entry of the day — give the file a heading so it renders
        # nicely in Obsidian and gives note_extract a date anchor.
        header = f"# Journal — {now.strftime('%Y-%m-%d')}\n\n"
        new_body = header + bullet + "\n"
    from brain.io import atomic_write_text
    atomic_write_text(path, new_body)
    _audit_ledger.append("note_add", {
        "path": rel,
        "bullet_sha8": _sha8(bullet),
        "tag_count": len([t for t in (tags or []) if t and t.strip()]),
        "journal_existed": existed,
    })
    return json.dumps(
        {"path": rel, "line": bullet, "journal_existed": existed},
        ensure_ascii=False,
    )


@mcp.tool()
def brain_recent(hours: int = 48, type: str | None = None, k: int = 20) -> str:
    """List entities last_updated within the last N hours.

    Useful at session start: "what changed since I last worked?"
    """
    _ensure_fresh()
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


# ─────────────────────────────────────────────────────────────────────────
# Strict claim-mode recall — see docs/claim-lattice-strict-design.md
#
# When BRAIN_USE_CLAIMS=1 + BRAIN_STRICT_CLAIMS=1, brain_recall queries
# fact_claims directly and skips the entity/note RRF path entirely.
# Knowledge layer (claims) is the single source of truth for fact
# intent; notes layer remains queryable via brain_notes for content
# intent.
# ─────────────────────────────────────────────────────────────────────────
def _strict_claims_enabled() -> bool:
    use = os.environ.get("BRAIN_USE_CLAIMS", "0") == "1"
    strict = os.environ.get("BRAIN_STRICT_CLAIMS", "0") == "1"
    return use and strict


def _strict_claims_misconfigured() -> bool:
    use = os.environ.get("BRAIN_USE_CLAIMS", "0") == "1"
    strict = os.environ.get("BRAIN_STRICT_CLAIMS", "0") == "1"
    return strict and not use


def _claim_miss_threshold() -> float:
    try:
        return float(os.environ.get("BRAIN_CLAIM_MISS_THRESHOLD", "0.5"))
    except (ValueError, TypeError):
        return 0.5


def _recall_strict_claims(query: str, k: int, verbose: bool) -> str:
    """Query claim store only. No entity-file or note fallback."""
    from brain.claims import read as _claim_read
    hits = _claim_read.search_text(query, k=k)
    threshold = _claim_miss_threshold()
    weak = (not hits) or (hits[0].score < threshold)
    if not hits:
        guidance = (
            "the brain has no current claim matching this query in the "
            "strict claim store. Notes layer is not consulted in strict "
            "mode — call `brain_notes(query)` to search free-form note text."
        )
    elif weak:
        guidance = (
            "weak match in claim store; top score below threshold. "
            "Treat hits as topical hints, not authoritative answers."
        )
    else:
        guidance = None

    formatted_hits = []
    for h in hits:
        item: dict = {
            "kind": h.kind,
            "path": h.path,
            "text": h.text if verbose else (h.text or "")[:240],
            "name": h.name,
            "claim_id": h.claim_id,
        }
        # Spec §3.3: include entity_summary on the first hit per
        # entity (suppression already applied upstream by
        # claims.read.search_text — formatter just passes through).
        if h.entity_summary:
            item["entity_summary"] = h.entity_summary
        if verbose:
            item["score"] = h.score
        formatted_hits.append(item)

    return json.dumps({
        "query": query,
        "weak_match": weak,
        "guidance": guidance,
        "hits": formatted_hits,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def brain_recall(
    query: str,
    k: int = 8,
    type: str | None = None,
    verbose: bool = False,
    debug: bool = False,
) -> str:
    """Hybrid (BM25 + semantic) recall — RECOMMENDED DEFAULT.

    Searches across BOTH:
      - extracted facts/entities (`entities/<type>/*.md`)
      - free-form notes anywhere else in the vault (e.g. a root file
        named `son dang o long xuyen.md`)

    Catches paraphrases ("how do I avoid the freeze" → "dual-instance
    Mac freeze prevention") AND exact-keyword/filename hits in one
    ranked list.

    Envelope: `{query, weak_match, guidance, hits}`. Default hit shape
    is `{kind, path, text, name?, entity_summary?}` with `text` capped
    at `BRAIN_RECALL_SNIPPET_CHARS` (default 240, clamp [80, 2000]).
    `entity_summary` emits only on the FIRST hit per entity;
    canonical-fact-hash dedup runs at the envelope layer after
    reranking, so `k` counts unique hits only.

    Args:
      verbose: opt in to `{type, slug, source, date, status?}` + full
               untruncated text.
      debug: implies `verbose`; adds `{score, rrf, lexical_rank,
             semantic_rank, sem_score}` per hit plus envelope-level
             `{top_score, threshold, fetch_k, rerank_on,
             query_rewriter_on}`. Tuning signals, not for agents.

    Env overrides:
      BRAIN_RECALL_SNIPPET_CHARS  — snippet cap (default 240)
      BRAIN_MCP_DEFAULT_VERBOSE=1 — restore pre-WS2 verbose shape
                                    (migration grace).
      BRAIN_USE_CLAIMS=1 + BRAIN_STRICT_CLAIMS=1 — strict claim-store-only
                                    recall (skips entity/note RRF path).
    """
    if _strict_claims_misconfigured():
        return json.dumps({
            "error": "configuration_error",
            "detail": "BRAIN_STRICT_CLAIMS=1 requires BRAIN_USE_CLAIMS=1; "
                      "set both flags or unset both.",
        }, ensure_ascii=False, indent=2)
    if _strict_claims_enabled():
        return _recall_strict_claims(query, max(1, min(int(k), 25)), verbose)
    if _projection.default_verbose() and not debug:
        verbose = True
    k = max(1, min(int(k), 25))
    _ensure_fresh()
    semantic = _semantic()
    semantic.ensure_built()
    # Flags resolved per-call via the rewriter/reranker modules so a
    # test or admin env-flip takes effect without a server restart.
    # Defaults OFF per WS7b bench gate — see module docstrings for the
    # measurement outcome.
    from brain import query_rewriter as _qr
    from brain import reranker as _rr
    query_rewriter_on = _qr._enabled()
    rerank_on = _rr._enabled()
    # Match pre-WS2 fetch_k so weak_match is computed over the same pool
    # of candidates. Over-fetching here shifted a PM weak-anchor baseline
    # score by changing which hits ranked top. Envelope dedup instead
    # runs on whatever hybrid_search returns; if dupes drop it, k may
    # come back shorter — that's the Ontologist spec §5 "return shorter"
    # path.
    fetch_k = min(k * 4, 40) if rerank_on else k
    if query_rewriter_on:
        from brain import query_rewriter
        results = query_rewriter.expanded_hybrid_search(
            query, k=fetch_k, type=type,
            search_fn=semantic.hybrid_search,
        )
    else:
        results = semantic.hybrid_search(query, k=fetch_k, type=type)
    # Reranker is asked for fetch_k (not k) so envelope dedup has a
    # larger pool to select unique hits from.
    if rerank_on and results:
        from brain import reranker
        results = reranker.rerank(query, results, k=fetch_k)
    try:
        from brain import recall_metric
        recall_metric.log_live_recall(query)
    except Exception:
        pass

    # Enrich fact hits with entity_summary so agents can understand
    # context without a follow-up brain_get call. Projection handles
    # the first-per-entity suppression.
    fact_keys = list({
        (h["type"], h["name"])
        for h in results
        if h.get("kind") == "fact" and h.get("type") and h.get("name")
    })
    if fact_keys:
        summaries = db.get_entity_summaries(fact_keys)
        for h in results:
            if h.get("kind") == "fact":
                s = summaries.get((h.get("type"), h.get("name")))
                if s:
                    h["entity_summary"] = s

    # --- weak-match computation (mirrors benchmark.compute_weak_match).
    import os as _os
    try:
        threshold = float(_os.environ.get("BRAIN_RECALL_WEAK_RRF", "0.035"))
    except (ValueError, TypeError):
        threshold = 0.035
    # BM25 returns near-zero scores for non-ASCII queries; scale the
    # threshold so a correct semantic hit isn't classed as weak purely
    # due to query language. Tunable via BRAIN_RECALL_NON_ASCII_SCALE.
    if any(ord(c) > 127 for c in query):
        try:
            scale = float(_os.environ.get("BRAIN_RECALL_NON_ASCII_SCALE", "0.55"))
        except (ValueError, TypeError):
            scale = 0.55
        threshold *= scale

    top_score = max((h.get("rrf") or 0.0 for h in results), default=0.0)
    weak_match = top_score < threshold

    # Semantic-fallback rescue: low RRF but confident cosine → not weak.
    # Uses sem_score (true cosine) not `score` — on merged hits `score`
    # may be the BM25 branch's value (often negative).
    if weak_match and results:
        semantic_top = max(
            (h.get("sem_score") if h.get("sem_score") is not None
             else (h.get("score") or 0.0)
             for h in results
             if h.get("semantic_rank") is not None),
            default=0.0,
        )
        try:
            sem_fallback = float(
                _os.environ.get("BRAIN_RECALL_SEMANTIC_FALLBACK", "0.20")
            )
        except (ValueError, TypeError):
            sem_fallback = 0.20
        if semantic_top >= sem_fallback:
            weak_match = False

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

    projected = _projection.project_hits(
        results, k=k, verbose=verbose, debug=debug,
    )
    env = _projection.envelope(
        query, projected,
        weak_match=weak_match,
        guidance=guidance,
        debug=debug,
        top_score=top_score,
        threshold=threshold,
        fetch_k=fetch_k,
        rerank_on=rerank_on,
        query_rewriter_on=query_rewriter_on,
    )
    # indent=2 is gone — every byte it cost the caller had zero signal.
    return json.dumps(env, ensure_ascii=False)


@mcp.tool()
def brain_semantic(
    query: str,
    k: int = 8,
    type: str | None = None,
    verbose: bool = False,
    debug: bool = False,
) -> str:
    """Pure semantic (dense-vector) fact search.

    Use when you want paraphrase recall and don't care about exact
    keyword matches. For the recommended hybrid path with weak-match
    guidance, prefer `brain_recall`.

    Envelope: `{query, weak_match, guidance, hits}`. Default hit shape
    matches the other tools — `{kind, path, text, name?,
    entity_summary?}`.

    Strict-claim mode (BRAIN_USE_CLAIMS=1 + BRAIN_STRICT_CLAIMS=1):
    semantic search over entity .md files is NOT the claim store.
    Returns `strict_unsupported` error pointing the agent to
    `brain_recall` (which does lexical claim search). Claim-store
    semantic embeddings are deferred until claim count grows past
    10k.
    """
    if _strict_claims_misconfigured():
        return json.dumps({
            "error": "configuration_error",
            "detail": "BRAIN_STRICT_CLAIMS=1 requires BRAIN_USE_CLAIMS=1",
        }, ensure_ascii=False, indent=2)
    if _strict_claims_enabled():
        return json.dumps({
            "error": "strict_unsupported",
            "detail": (
                "brain_semantic embeds entity .md files (the projection "
                "layer), not the claim store. In strict mode, fact-intent "
                "queries should use brain_recall (lexical claim search). "
                "Claim-store semantic search is on the roadmap for >10k "
                "claims."
            ),
            "fallback_tool": "brain_recall",
        }, ensure_ascii=False, indent=2)
    if _projection.default_verbose() and not debug:
        verbose = True
    _ensure_fresh()
    semantic = _semantic()
    k = max(1, min(int(k), 25))
    semantic.ensure_built()
    fetch_k = min(k * 2, 25)
    results = semantic.search_facts(query, k=fetch_k, type=type) or []
    # Ensure kind is set for the projection layer.
    for h in results:
        h.setdefault("kind", "fact")
    try:
        from brain import recall_metric
        recall_metric.log_live_recall(query)
    except Exception:
        pass
    projected = _projection.project_hits(
        results, k=k, verbose=verbose, debug=debug,
    )
    weak_match = not projected
    guidance = "The brain has no record of this." if weak_match else None
    env = _projection.envelope(
        query, projected,
        weak_match=weak_match, guidance=guidance, debug=debug,
        fetch_k=fetch_k,
    )
    return json.dumps(env, ensure_ascii=False)


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
    memory layer for agents. Use this to see how an entity evolved over
    time — author, date, subject, and file diff stats — without leaving
    the brain MCP.

    Args:
      path: relative path under ~/.brain/ (e.g. `entities/people/madhav.md`)
      limit: max commits (default 10, capped at 50)

    Returns JSON list of {sha, date, author, subject, insertions, deletions}.
    """
    from brain.git_ops import entity_history
    return json.dumps(entity_history(path, limit), ensure_ascii=False, indent=2)


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
    from brain.live_sessions import list_live_sessions
    return json.dumps(
        list_live_sessions(active_within_sec=active_within_sec, include_self=include_self),
        ensure_ascii=False, indent=2,
    )


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
    from brain.live_sessions import tail_live_session
    return json.dumps(tail_live_session(session_id, n), ensure_ascii=False, indent=2)


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


def _resolve_audit_path(path: str):
    """Resolve an audit target path, confined to BRAIN_DIR. Returns
    (resolved_path, error_json). On success error_json is None."""
    from pathlib import Path
    p = Path(path)
    if not p.is_absolute():
        p = config.BRAIN_DIR / p
    try:
        p = p.resolve()
        p.relative_to(config.BRAIN_DIR.resolve())
    except (ValueError, OSError):
        return None, json.dumps({"error": f"path outside vault: {path}"})
    if not p.exists() or not p.is_file():
        return None, json.dumps({"error": f"not found: {path}"})
    return p, None


@mcp.tool()
def brain_mark_reviewed(path: str) -> str:
    """Confirm a single-source entity surfaced by `brain_audit` — stamps
    `reviewed: YYYY-MM-DD` into its frontmatter so audit stops nagging for
    ~90 days. Idempotent; re-stamping today is a no-op.

    `path` is the entity path from the audit item (relative to ~/.brain/,
    e.g. `entities/people/thuha.md`). Returns `{"changed": bool, "path": ...}`.
    """
    from brain import audit as audit_mod
    p, err = _resolve_audit_path(path)
    if err is not None:
        return err
    changed = audit_mod.mark_reviewed(p)
    rel = str(p.relative_to(config.BRAIN_DIR.resolve()))
    _audit_ledger.append("mark_reviewed", {"path": rel, "changed": changed})
    return json.dumps({"changed": changed, "path": rel})


@mcp.tool()
def brain_mark_contested(path: str) -> str:
    """Flag an entity as contested — flips `status: contested` into its
    frontmatter so audit surfaces it at the top. Use when the user rejects
    a single-source claim or when two sources disagree.

    `path` is relative to ~/.brain/. Returns `{"changed": bool, "path": ...}`;
    already-contested entities yield `changed: false` (no-op).
    """
    from brain import audit as audit_mod
    p, err = _resolve_audit_path(path)
    if err is not None:
        return err
    changed = audit_mod.mark_contested(p)
    rel = str(p.relative_to(config.BRAIN_DIR.resolve()))
    _audit_ledger.append("mark_contested", {"path": rel, "changed": changed})
    return json.dumps({"changed": changed, "path": rel})


@mcp.tool()
def brain_resolve_contested(path: str) -> str:
    """Clear a contested flag — drops the `status: contested` line from an
    entity's frontmatter. Use after the user resolves the underlying
    conflict (merged, corrected, or decided the claim is fine after all).

    `path` is relative to ~/.brain/. Returns `{"changed": bool, "path": ...}`;
    entities that weren't contested yield `changed: false`.
    """
    from brain import audit as audit_mod
    p, err = _resolve_audit_path(path)
    if err is not None:
        return err
    changed = audit_mod.resolve_contested(p)
    rel = str(p.relative_to(config.BRAIN_DIR.resolve()))
    _audit_ledger.append("resolve_contested", {"path": rel, "changed": changed})
    return json.dumps({"changed": changed, "path": rel})


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
    _audit_ledger.append("failure_record", {
        "id": fid,
        "source": source,
        "tool": tool,
        "tag_count": len(list(tags) if tags else []),
    })
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
def brain_learning_gaps(
    days: int = 14,
    min_count: int = 3,
    limit: int = 10,
) -> str:
    """Surface queries son has repeatedly asked the brain but that
    keep scoring below the miss threshold — the "close the loop"
    signal. Returns JSON list of `{query, miss_count, last_seen,
    best_score, recent_queries}` newest-first within each miss count.

    Usage: call this at session-start (or when son asks "what's the
    brain bad at?") to decide whether to prompt him to note something
    about those topics. DO NOT auto-generate entities to answer these
    gaps — that's the autoresearch fabrication trap. Surface only;
    son decides what becomes memory.

    Backed by `source=recall_miss` rows in `failures.jsonl`, which are
    appended automatically by the `brain_recall` path when a query's
    top score falls below `BRAIN_MISS_THRESHOLD`.
    """
    from brain import failures
    patterns = failures.list_miss_patterns(
        days=int(days),
        min_count=int(min_count),
        limit=int(limit),
    )
    return json.dumps(patterns, ensure_ascii=False, indent=2)


@mcp.tool()
def brain_retract_fact(
    entity_type: str,
    entity_name: str,
    fact_text: str,
) -> str:
    """Retract (supersede) a specific fact from an entity.

    Finds the first fact bullet in `entity_type/entity_name` whose text
    contains `fact_text` (case-insensitive substring match), wraps it in
    ~~strikethrough~~ so it stays visible as history, and removes it from
    the FTS + semantic indexes so it stops surfacing in recall.

    Use when the user says a brain fact is wrong and should be forgotten.
    Returns JSON: {"retracted": "<exact fact text>"} on success, or
    {"error": "..."} if the entity/fact is not found.

    Example:
        brain_retract_fact("people", "Son", "slippers are in the bedroom")
    """
    try:
        from brain import retract as retract_mod
        text = retract_mod.retract_fact(
            entity_type, entity_name, fact_text, retracted_by="user-correction"
        )
        _audit_ledger.append("retract_fact", {
            "entity": f"{entity_type}/{entity_name}",
            "match_sha8": _sha8(fact_text),
            "retracted_sha8": _sha8(text),
        })
        return json.dumps({"retracted": text}, ensure_ascii=False)
    except (ValueError, RuntimeError) as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def brain_correct_fact(
    entity_type: str,
    entity_name: str,
    wrong_fact: str,
    correct_fact: str,
) -> str:
    """Retract a wrong fact and immediately append the corrected one.

    Combines brain_retract_fact + appending a new fact bullet in one
    atomic step so the entity stays consistent. The new fact is sourced
    as "user-correction" with today's date.

    Returns JSON: {"retracted": "...", "appended": "..."} on success, or
    {"error": "..."} if the entity/fact is not found.

    Example:
        brain_correct_fact(
            "people", "Son",
            wrong_fact="currently in Long Xuyên",
            correct_fact="currently in Cần Thơ",
        )
    """
    try:
        from brain import retract as retract_mod
        result = retract_mod.correct_fact(
            entity_type, entity_name, wrong_fact, correct_fact,
            source="user-correction",
        )
        _audit_ledger.append("correct_fact", {
            "entity": f"{entity_type}/{entity_name}",
            "wrong_sha8": _sha8(wrong_fact),
            "correct_sha8": _sha8(correct_fact),
        })
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, RuntimeError) as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def brain_forget(
    text: str,
    entity_type: str | None = None,
    entity_name: str | None = None,
    reason: str | None = None,
) -> str:
    """Record a forget-intent (tombstone) for a claim.

    Use when the user says "don't remember this" / "I've decided to
    forget X" / "never bring this up again". Symmetric counterpart of
    `brain_note_add`: that pins a claim to memory, this pins a negation.

    Behaviour: a tombstone keyed on the canonical fact hash is written
    to the `tombstones` table. The extractor checks this table before
    promoting any new fact, so the claim cannot be resurrected by a
    later session mentioning it — no matter what the source. This is
    how retract becomes *sticky* instead of one-shot.

    Scope: pass `entity_type`+`entity_name` to narrow the tombstone to
    one entity (e.g. forget "slippers in bedroom" only for Son, leave
    intact for anyone else's claim text). Leave both None to forget
    globally — the safer default.

    Does NOT strikethrough existing facts on its own — for that, call
    `brain_retract_fact` (which also tombstones automatically).
    `brain_forget` is the "never re-ingest" primitive, useful when
    there is no existing fact yet but the user wants to pre-empt one.

    Returns JSON: {"tombstoned": bool, "claim": "...", "scope": ...}.
    """
    try:
        written = db.add_tombstone(
            text,
            entity_type=entity_type,
            entity_name=entity_name,
            reason=reason or "user-forget",
            created_by="user-forget",
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    scope = (
        f"{entity_type}/{entity_name}"
        if entity_type and entity_name
        else "global"
    )
    _audit_ledger.append("forget", {
        "scope": scope,
        "text_sha8": _sha8(text),
        "written": bool(written),
    })
    return json.dumps(
        {"tombstoned": written, "claim": (text or "").strip(), "scope": scope},
        ensure_ascii=False,
    )


@mcp.tool()
def brain_remember(
    text: str,
    entity_type: str | None = None,
    entity_name: str | None = None,
) -> str:
    """Lift a previously-written tombstone so the claim can be re-ingested.

    Use when the user says "actually that was true, start remembering it
    again". Removes the matching tombstone row; does NOT re-add the fact
    (the next extraction mentioning it will promote it normally). Match
    on canonical fact hash plus scope — call with the same `entity_type`
    / `entity_name` pair you used when forgetting.

    Returns JSON: {"removed": int, "claim": "...", "scope": ...}.
    """
    try:
        n = db.remove_tombstone(
            text, entity_type=entity_type, entity_name=entity_name
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    scope = (
        f"{entity_type}/{entity_name}"
        if entity_type and entity_name
        else "global"
    )
    _audit_ledger.append("remember", {
        "scope": scope,
        "text_sha8": _sha8(text),
        "removed": int(n),
    })
    return json.dumps(
        {"removed": int(n), "claim": (text or "").strip(), "scope": scope},
        ensure_ascii=False,
    )


@mcp.tool()
def brain_tombstones(limit: int = 20) -> str:
    """List the most recent tombstones (forget records) for audit.

    Surfaces what the brain has been asked to forget and why. Each row
    carries the original_text, scope (entity if any), reason, and who
    wrote it (`retract`, `correct`, `note-delete`, `user-forget`). Use
    this to verify a forget landed or to find a claim you want to
    `brain_remember`.

    Returns JSON list newest-first.
    """
    limit = max(1, min(int(limit), 200))
    try:
        rows = db.list_tombstones(limit=limit)
    except Exception as e:
        return json.dumps({"error": str(e)})
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


@mcp.tool()
def brain_graph_query(sparql: str) -> str:
    """Execute a SPARQL SELECT query against the brain's RDF triple store.

    The store holds typed relationships extracted from sessions and notes:
      worksAt, workedAt, knows, manages, reportsTo, partOf, locatedIn,
      builds, uses, involves, relatedTo, about, decidedOn, learnedFrom,
      contradicts

    Namespace prefix for convenience:
      PREFIX b: <http://brain.local/>
      Entities: b:e/<slug>   Predicates: b:p/<predicate>

    Example — who does Son work with?
      PREFIX b: <http://brain.local/>
      SELECT ?org WHERE { b:e/son b:p/worksAt ?org }

    Returns JSON list of binding dicts, or {error: ...} on bad SPARQL.
    """
    from brain.graph import query as gq
    return json.dumps(gq(sparql), ensure_ascii=False, indent=2)


@mcp.tool()
def brain_graph_neighbors(
    entity: str,
    predicate: str | None = None,
    depth: int = 1,
) -> str:
    """Return all triples reachable from `entity` (optionally filtered by predicate).

    Traverses the RDF graph up to `depth` hops (capped at 3). Returns
    JSON list of {subject, predicate, object} dicts.

    `entity` is the entity name or slug (e.g. "Son", "aitomatic").
    `predicate` filters to one relationship type (e.g. "worksAt").
    `depth=2` follows direct neighbours one more step (multi-hop).
    """
    from brain.graph import neighbors as gn
    return json.dumps(gn(entity, predicate=predicate, depth=depth),
                      ensure_ascii=False, indent=2)


@mcp.tool()
def brain_progress(format: str = "text") -> str:
    """Extraction pipeline progress: notes-progress bar, last-hour throughput,
    backlog, currently-extracting indicator, GREEN/YELLOW/RED health.

    Args:
      format: "text" (default) — return the human-readable block with
              the ASCII progress bar; agent should print verbatim
              without summarising. "json" — return the raw dict for
              programmatic consumption.

    Use this to answer "is brain keeping up with my notes?".
    """
    from brain.claims import progress as _progress
    p = _progress.extraction_progress()
    if (format or "").lower() == "json":
        return json.dumps(p, ensure_ascii=False, indent=2)
    return _progress.format_text(p)


@mcp.resource("brain://identity")
def identity_resource() -> str:
    return brain_identity()


# ─────────────────────────────────────────────────────────────────────────
# Realtime named-session messaging — see docs/realtime-named-sessions-design.md
#
# Three tools backed by brain.runtime/. Storage lives outside BRAIN_DIR;
# extraction of inter-session content happens via the existing harvest
# pipeline reading session transcript jsonl, not via a parallel path.
# ─────────────────────────────────────────────────────────────────────────


def _caller_cwd() -> str:
    """The cwd this MCP server was launched in. Overridden in tests."""
    return os.getcwd()


def _caller_project_for_uuid(uuid: str) -> str:
    """Map a session UUID to its project label.

    Reuses brain.live_sessions' project derivation so the answer matches
    what other brain tools see. Falls back to the basename of cwd.
    """
    from brain import live_sessions as _ls
    for row in _ls.list_live_sessions(include_self=True):
        if row.get("session_id") == uuid:
            return row.get("project") or os.path.basename(_caller_cwd())
    return os.path.basename(_caller_cwd())


def _live_uuids() -> set[str]:
    from brain import live_sessions as _ls
    return {row["session_id"] for row in _ls.list_live_sessions(include_self=True)}


def _detect_source_for_uuid(uuid: str) -> str:
    """Pick `claude` vs `cursor` for short-id derivation.

    Heuristic order:
      1. `cursor:<UUIDv4>` prefix → cursor (no PID mapping available
         on Cursor, so we use the first 8 chars of the UUID portion).
      2. `~/.claude/sessions/<ppid>.json` exists → claude.
      3. Default → claude (legacy fallback so existing sessions keep
         their PID-based short id).
    """
    if (uuid or "").startswith("cursor:"):
        return "cursor"
    try:
        from pathlib import Path
        ppid = os.getppid()
        if (Path.home() / ".claude" / "sessions" / f"{ppid}.json").exists():
            return "claude"
    except OSError:
        pass
    return "claude"


def _ensure_self_registered(uuid: str) -> None:
    """Lazy-create a default-name registry entry for `uuid` on first use."""
    from brain.runtime import names as _names
    from brain.runtime import session_id as _sid
    if _names.get(uuid):
        return
    project = _caller_project_for_uuid(uuid)
    source = _detect_source_for_uuid(uuid)
    short = _sid.short_id_for_default_name(uuid, source=source)
    _names.register(
        uuid=uuid,
        name=_names.default_name(project, short),
        project=project,
        cwd=_caller_cwd(),
        pid=int(short) if short.isdigit() else None,
    )


@mcp.tool()
def brain_set_name(name: str) -> str:
    """Set this session's human-readable name (per-project alias).

    Validation: lowercase, [a-z0-9-], 1-64 chars, not in
    {peer,self,all,me}, not already taken by another session in the
    same project.

    Returns JSON: {ok, uuid, name, project} on success;
    {ok: false, error, detail} on failure (codes: lowercase, length,
    chars, reserved, name_taken, no_session).
    """
    from brain.runtime import names as _names
    from brain.runtime import session_id as _sid
    uuid = _sid.detect_own_uuid()
    if not uuid:
        return json.dumps({"ok": False, "error": "no_session",
                           "detail": "could not detect own session UUID"})
    _ensure_self_registered(uuid)
    # Pass live_uuids so set_name can reclaim a slot held by a dead
    # session — name_taken should only mean "another *live* session
    # owns this name", not "some long-gone PID still has it on disk".
    err = _names.set_name(uuid, name, live_uuids=_live_uuids())
    if err:
        return json.dumps({"ok": False, "error": err})
    entry = _names.get(uuid) or {}
    return json.dumps({
        "ok": True,
        "uuid": uuid,
        "name": entry.get("name"),
        "project": entry.get("project"),
    })


@mcp.tool()
def brain_send(to: str, body: str) -> str:
    """Send a message to another live session by name or UUID.

    Resolution rules:
      1. UUIDv4 pattern → fire-and-forget (no liveness check).
      2. cursor:<UUIDv4> → MVP: rejected (deferred to v2).
      3. <project>/<name> → resolve `name` in that project's namespace.
      4. <name> → resolve in sender's own project.

    Error codes: name_not_found, ambiguous_name, recipient_dead,
    cursor_recipient_unsupported, invalid_recipient, body_too_large,
    no_session.
    """
    from brain.runtime import inbox as _inbox
    from brain.runtime import names as _names
    from brain.runtime import resolve as _resolve
    from brain.runtime import session_id as _sid

    sender_uuid = _sid.detect_own_uuid()
    if not sender_uuid:
        return json.dumps({"ok": False, "error": "no_session"})
    _ensure_self_registered(sender_uuid)

    sender_project = _caller_project_for_uuid(sender_uuid)
    decision = _resolve.resolve_recipient(
        to=to,
        sender_project=sender_project,
        live_uuids=_live_uuids(),
    )
    if not decision.ok:
        return json.dumps({
            "ok": False, "error": decision.error, "detail": decision.detail,
        })

    sender_entry = _names.get(sender_uuid) or {}
    try:
        env = _inbox.send(
            to_uuid=decision.uuid,
            from_uuid=sender_uuid,
            from_name_at_send=sender_entry.get("name") or sender_uuid[:8],
            to_name_at_send=decision.name_at_send,
            body=body,
        )
    except _inbox.BodyTooLarge as e:
        return json.dumps({"ok": False, "error": "body_too_large",
                           "detail": str(e)})

    # Best-effort throttled cleanup of dead-session names + delivered TTL.
    # Stale name slots otherwise sit on disk for 30 days, blocking name
    # reclaim for new sessions in the same project.
    try:
        from brain.runtime import gc as _gc
        _gc.maybe_run(_live_uuids())
    except Exception:
        pass

    return json.dumps({
        "ok": True,
        "message_id": env["id"],
        "to_uuid": env["to_uuid"],
        "to_name_at_send": env["to_name_at_send"],
    })


@mcp.tool()
def brain_inbox(unread_only: bool = True, limit: int = 50,
                mark_read: bool = False) -> str:
    """List own session's inbox.

    Default = peek (non-destructive). Pass mark_read=True to move
    listed messages from pending/ to delivered/. The
    UserPromptSubmit hook is the normal mark-read path; manual calls
    default to peek so user can inspect without consuming.
    """
    from brain.runtime import inbox as _inbox
    from brain.runtime import session_id as _sid
    own = _sid.detect_own_uuid()
    if not own:
        return json.dumps({"ok": False, "error": "no_session"})

    pending = _inbox.list_pending(own)
    delivered = _inbox.list_delivered(own)
    listed = pending if unread_only else pending + delivered
    listed = listed[: max(1, min(int(limit), 500))]

    if mark_read and pending:
        # Only mark the LISTED slice as delivered. Previous code passed
        # the full `pending` list, so brain_inbox(mark_read=True,
        # limit=N) silently moved every pending message to delivered/
        # even though the caller only saw N — a data-loss bug when N
        # was small relative to queue depth. Intersect listed ∩ pending
        # by id so we never try to re-mark already-delivered envelopes
        # when unread_only=False.
        pending_ids = {m["id"] for m in pending}
        to_mark = [m["id"] for m in listed if m["id"] in pending_ids]
        if to_mark:
            _inbox.mark_delivered(own, to_mark)
            # Recompute counts so they reflect the post-mark state.
            pending = _inbox.list_pending(own)
            delivered = _inbox.list_delivered(own)

    return json.dumps({
        "ok": True,
        "messages": listed,
        "pending_count": len(pending),
        "delivered_count": len(delivered),
    })


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
