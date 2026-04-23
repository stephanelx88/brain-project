"""WS7a — subject-reject hard filter.

Eliminates the ``đôi-dép-tôi`` failure class (2026-04-21 incident):
a query subject ≠ hit subject must drop the hit at recall time, before
it reaches the ranker, the reranker, or the caller's token budget.

The pipeline:

    query (str)
      ↓  parse_query_subject(q)
    SubjectHint(subject_slug, subject_type, confidence, source)
      ↓  semantic.hybrid_search returns candidates (no change there)
    filter_hits(hits, hint)
      ↓
    filtered hits → ranker/reranker → envelope

If the parser can't detect a subject (generic question like "how does
TCP work"), the filter is a no-op and the caller sees today's
behaviour. Same goes for hits that carry no `slug` — legacy note hits,
facts that haven't flowed through WS6 backfill yet, etc.

Gate: `BRAIN_SUBJECT_REJECT=1`. Default 0 until we've measured the
bench delta, per PM 16:55.

Security spec: ``scratch/security-ws7a-subject-reject.md``.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import brain.config as config
from brain import db


# Default possessives. Matches the seed in the Security spec; users
# can override by writing `identity/possessives.jsonl` (one
# `{"lang", "pronouns", "possessive_particles"}` object per line).
_DEFAULT_POSSESSIVES: list[dict] = [
    {"lang": "vi",
     "pronouns": ["tôi", "mình", "tớ", "em"],
     "possessive_particles": ["của tôi", "của mình", "của tớ",
                              "của em", "nhà tôi", "nhà mình"]},
    {"lang": "en",
     "pronouns": ["i", "me", "my", "mine", "myself"],
     "possessive_particles": ["my", "mine", "of mine"]},
    {"lang": "fr",
     "pronouns": ["je", "moi"],
     "possessive_particles": ["mon", "ma", "mes", "le mien", "la mienne"]},
    {"lang": "es",
     "pronouns": ["yo", "mi", "mis"],
     "possessive_particles": ["mi", "mis", "mío", "mía"]},
    {"lang": "zh",
     "pronouns": ["我"],
     "possessive_particles": ["我的"]},
    {"lang": "ja",
     "pronouns": ["私", "僕", "俺"],
     "possessive_particles": ["私の", "僕の", "俺の"]},
]


@dataclass(frozen=True)
class SubjectHint:
    subject_slug: str | None = None
    subject_type: str | None = None
    confidence: float = 0.0
    source: str = "none"   # "proper_noun" | "possessive" | "none"
    ambiguous: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """Read `BRAIN_SUBJECT_REJECT` each call — lets tests flip it via
    `monkeypatch.setenv` without re-importing the module.

    **Default flipped to `1` on 2026-04-23** after WS1 golden-set expansion
    (n=60 + held-out n=20) showed the filter strictly improves weak-match
    detection (`weak_hit_rate 0.000 → 0.400` on held-out) without touching
    positive metrics (`p@1`, `MRR`, `hit_rate` unchanged both sets). Gate
    passed on the unseen held-out split so overfit is ruled out. Set
    `BRAIN_SUBJECT_REJECT=0` to disable if a regression surfaces.
    """
    return os.environ.get("BRAIN_SUBJECT_REJECT", "1") == "1"


def parse_query_subject(query: str) -> SubjectHint:
    """Classify a query's subject into one of three states:
    proper-noun, possessive (owner-self), or none.

    Proper-noun **wins** over possessive EXCEPT when the proper-noun
    match resolves to the vault owner's own entity — `son ăn gì hôm
    qua` is the owner asking about themselves, not a search for the
    entity named "son".
    """
    if not query or not query.strip():
        return SubjectHint()

    q_norm = _normalise(query)
    owner = _owner_slug()

    # Step 1: proper-noun scan.
    match = _longest_entity_match(q_norm)
    if match:
        length, slug, etype, was_alias, matched_text, span = match
        canonical = _canonical_slug(slug)
        if owner is not None and canonical == owner:
            # Owner self-reference — treat as possessive voice.
            return SubjectHint(
                subject_slug=owner,
                subject_type="people",
                confidence=0.9,
                source="possessive",
            )
        # Detect multi-subject queries (two different proper nouns):
        # the second match must (a) resolve to a DIFFERENT canonical
        # slug and (b) occupy a disjoint character range in the query
        # — otherwise a name-embedded-in-name case like "Long" inside
        # "Long Xuyen" wrongly flips the query to multi-subject.
        second = _longest_entity_match(q_norm, exclude_span=span)
        ambiguous = (
            second is not None
            and _canonical_slug(second[1]) != canonical
        )
        if ambiguous:
            return SubjectHint(ambiguous=True, source="multi_subject")
        return SubjectHint(
            subject_slug=canonical,
            subject_type=etype,
            confidence=1.0,
            source="proper_noun",
        )

    # Step 2: possessive scan. Any pronoun/particle hit → owner voice.
    if _has_possessive(q_norm) and owner is not None:
        return SubjectHint(
            subject_slug=owner,
            subject_type="people",
            confidence=0.9,
            source="possessive",
        )

    return SubjectHint()


def filter_hits(hits: Iterable[dict], hint: SubjectHint, *,
                query: str = "") -> list[dict]:
    """Drop hits whose subject_slug conflicts with the hint. Conservative
    by default: pass-through when the hint has no subject, the hit has
    no subject, or the hit is a note (free-form, no subject concept).

    An alias-aware comparison is used — a hit tagged ``son`` passes a
    query with subject_slug ``stephane`` if `aliases` maps them to the
    same entity.
    """
    hits = list(hits)
    if hint.subject_slug is None:
        return hits

    target = _canonical_slug(hint.subject_slug)
    kept: list[dict] = []
    dropped: list[tuple[str, str]] = []  # (hit_slug, reason)
    for h in hits:
        # Notes have no subject_slug concept — let them pass unchanged.
        # The subject-reject filter is a fact-level feature.
        if h.get("kind") == "note":
            kept.append(h)
            continue
        hit_slug = h.get("subject_slug") or h.get("slug")
        if not hit_slug:
            # Legacy / pre-WS6-backfill rows: unknown subject → PASS.
            kept.append(h)
            continue
        canonical_hit = _canonical_slug(hit_slug)
        if canonical_hit == target:
            kept.append(h)
            continue
        dropped.append((hit_slug, "subject_mismatch"))

    if dropped:
        _audit_rejects(query=query, hint=hint, dropped=dropped, hits=hits)
    return kept


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalise(s: str) -> str:
    """NFKC-fold + casefold. Leaves CJK characters intact."""
    return unicodedata.normalize("NFKC", s).casefold()


_OWNER_NAME_RE = re.compile(r"^[-*\s]*name\s*[:：]\s*(.+?)\s*$",
                            re.IGNORECASE | re.MULTILINE)

_owner_cache: tuple[str | None, float] | None = None
_OWNER_CACHE_TTL = 30.0  # seconds


def _owner_slug() -> str | None:
    """Parse the `Name:` line from `identity/who-i-am.md`. Cached for
    a short window so repeated filter calls don't re-read the file
    on every query."""
    global _owner_cache
    now = time.monotonic()
    if _owner_cache and (now - _owner_cache[1]) < _OWNER_CACHE_TTL:
        return _owner_cache[0]
    p = config.IDENTITY_DIR / "who-i-am.md"
    name: str | None = None
    try:
        text = p.read_text(errors="replace")
    except (OSError, FileNotFoundError):
        text = ""
    m = _OWNER_NAME_RE.search(text)
    if m:
        # Canonicalise: lowercase + replace spaces with hyphen (matches
        # slugify.slugify's basic contract for ascii names).
        name = m.group(1).strip().lower().replace(" ", "-")
        name = name.strip("-") or None
    _owner_cache = (name, now)
    return name


def _canonical_slug(slug: str) -> str:
    """Resolve aliases: the brain's `aliases` table maps (entity_id,
    alias) pairs. Two slugs that resolve to the same entity are
    considered the same subject. No-op when the DB is unreachable."""
    if not slug:
        return slug
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, slug FROM entities WHERE slug=? LIMIT 1",
                (slug,),
            ).fetchone()
            if row is None:
                # Maybe the caller passed an alias; look it up.
                alias_row = conn.execute(
                    "SELECT e.slug FROM aliases a JOIN entities e "
                    "ON e.id=a.entity_id WHERE lower(a.alias)=? LIMIT 1",
                    (slug.lower(),),
                ).fetchone()
                if alias_row:
                    return alias_row[0]
                return slug
            return row[1]
    except Exception:
        return slug


def _entity_name_table() -> list[tuple[str, str, str, bool]]:
    """Return [(match_text_lower, slug, type, is_alias)] for every
    entity name + every alias in the vault. Cheap: ~500 rows on a
    typical vault.

    Excludes very short matches (<2 chars) to avoid false positives
    like the substring 'a' inside any query.
    """
    try:
        with db.connect() as conn:
            names = conn.execute(
                "SELECT slug, type, name FROM entities"
            ).fetchall()
            aliases = conn.execute(
                "SELECT a.alias, e.slug, e.type "
                "FROM aliases a JOIN entities e ON e.id=a.entity_id"
            ).fetchall()
    except Exception:
        return []
    out: list[tuple[str, str, str, bool]] = []
    for slug, etype, name in names:
        if not name or len(name) < 2:
            continue
        out.append((_normalise(name), slug, etype, False))
    for alias, slug, etype in aliases:
        if not alias or len(alias) < 2:
            continue
        out.append((_normalise(alias), slug, etype, True))
    return out


def _longest_entity_match(
    q_norm: str,
    *,
    exclude_span: tuple[int, int] | None = None,
) -> tuple[int, str, str, bool, str, tuple[int, int]] | None:
    """Return (length, slug, type, is_alias, matched_text, (start, end))
    for the longest entity name/alias appearing in ``q_norm``.

    ``exclude_span`` (optional) rejects any candidate whose matched
    character range overlaps with the given (start, end) tuple. Used
    to detect multi-subject queries without false-firing on embedded
    names (e.g. 'Long' inside 'Long Xuyen').

    Ties broken by: (a) non-alias over alias, (b) 'people' type over
    others.
    """
    best: tuple[int, str, str, bool, str, tuple[int, int]] | None = None
    for match_text, slug, etype, is_alias in _entity_name_table():
        span = _bounded_substring_span(q_norm, match_text)
        if span is None:
            continue
        if exclude_span is not None and _spans_overlap(span, exclude_span):
            continue
        candidate = (len(match_text), slug, etype, is_alias, match_text, span)
        if best is None:
            best = candidate
            continue
        if candidate[0] > best[0]:
            best = candidate
            continue
        if candidate[0] < best[0]:
            continue
        if best[3] and not candidate[3]:
            best = candidate
            continue
        if best[2] != "people" and candidate[2] == "people":
            best = candidate
    return best


def _spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] <= b[0] or b[1] <= a[0])


_WORD_CHAR = re.compile(r"[\w一-鿿぀-ヿÀ-ɏ]")


def _bounded_substring_span(haystack: str, needle: str
                             ) -> tuple[int, int] | None:
    """Like `_bounded_substring` but returns the (start, end) char
    range of the first bounded hit. Returns None when no hit."""
    if not needle:
        return None
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return None
        # CJK scripts have no word boundaries — any substring hit counts.
        if _is_cjk(needle[0]) or _is_cjk(needle[-1]):
            return (idx, idx + len(needle))
        left = haystack[idx - 1] if idx > 0 else ""
        right = (haystack[idx + len(needle)]
                 if idx + len(needle) < len(haystack) else "")
        left_ok = not left or not _WORD_CHAR.match(left)
        right_ok = not right or not _WORD_CHAR.match(right)
        if left_ok and right_ok:
            return (idx, idx + len(needle))
        start = idx + 1  # overlapping-safe; look for next occurrence


def _bounded_substring(haystack: str, needle: str) -> bool:
    return _bounded_substring_span(haystack, needle) is not None


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or
            0x3040 <= cp <= 0x30FF or
            0x3400 <= cp <= 0x4DBF)


_possessives_cache: tuple[list[dict], float] | None = None


def _load_possessives() -> list[dict]:
    """Seed list + optional overrides from
    `$BRAIN_DIR/identity/possessives.jsonl`. Cached for 30 s so a
    burst of recall calls doesn't reparse the file repeatedly.
    """
    global _possessives_cache
    now = time.monotonic()
    if _possessives_cache and (now - _possessives_cache[1]) < 30.0:
        return _possessives_cache[0]

    data = [dict(row) for row in _DEFAULT_POSSESSIVES]
    override = config.IDENTITY_DIR / "possessives.jsonl"
    try:
        if override.exists():
            by_lang = {row["lang"]: row for row in data}
            for line in override.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                lang = row.get("lang")
                if not lang:
                    continue
                by_lang[lang] = {
                    "lang": lang,
                    "pronouns": list(row.get("pronouns") or []),
                    "possessive_particles": list(
                        row.get("possessive_particles") or []
                    ),
                }
            data = list(by_lang.values())
    except OSError:
        pass

    _possessives_cache = (data, now)
    return data


def _has_possessive(q_norm: str) -> bool:
    for lang in _load_possessives():
        # Check multi-token particles first (e.g. "của tôi") so "tôi"
        # inside "của tôi" doesn't double-count — doesn't matter for
        # correctness (both still fire), just cleaner.
        for particle in lang.get("possessive_particles", []):
            p_norm = _normalise(particle)
            if _bounded_substring(q_norm, p_norm):
                return True
        for pronoun in lang.get("pronouns", []):
            p_norm = _normalise(pronoun)
            if _bounded_substring(q_norm, p_norm):
                return True
    return False


def _audit_rejects(*, query: str, hint: SubjectHint,
                   dropped: list[tuple[str, str]], hits: list[dict]) -> None:
    """Append one JSONL line per rejected hit. Silent-fail on OSError."""
    audit_dir = config.BRAIN_DIR / ".audit"
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    path = audit_dir / "subject_reject.jsonl"
    try:
        import hashlib
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as f:
            for hit_slug, reason in dropped:
                text_sample = ""
                for h in hits:
                    if (h.get("slug") == hit_slug
                            or h.get("subject_slug") == hit_slug):
                        text_sample = (h.get("text") or "")[:120]
                        break
                sha8 = hashlib.sha256(
                    text_sample.encode("utf-8")
                ).hexdigest()[:8] if text_sample else None
                f.write(json.dumps({
                    "ts": ts,
                    "query": query,
                    "query_subject_slug": hint.subject_slug,
                    "query_subject_source": hint.source,
                    "hit_subject_slug": hit_slug,
                    "hit_fact_sha8": sha8,
                    "reason": reason,
                }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def reset_caches() -> None:
    """Test helper — drop module-level caches so tests that monkeypatch
    `config.IDENTITY_DIR` or write override files don't see stale
    results from a previous test case."""
    global _owner_cache, _possessives_cache
    _owner_cache = None
    _possessives_cache = None
