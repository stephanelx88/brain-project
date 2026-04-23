"""Pre-LLM secret + injection scrubber (WS4).

Four passes, applied in order to any text entering the pipeline from
an untrusted source (session transcripts, notes, tool outputs):

  1. Secret redaction  — named high-confidence patterns → [REDACTED:KIND:sha8]
  2. Injection tripwire — prompt-injection patterns     → flag or reject by source_kind
  3. Entropy catch-all — unnamed high-entropy tokens    → [REDACTED:HIGH_ENTROPY:sha8:Nch]
  4. Length elision    — oversized lines / blocks       → [ELIDED:Nch:sha8]

Order rationale: injection runs BEFORE entropy because many attack
signatures (exfil URLs, base64 payloads, CHATML markers) are
themselves high-entropy tokens. If entropy redacts them first, the
tripwire regex can't match the attack structure. Secret redaction
runs first because named-pattern matches are strictly higher
confidence than either of the other passes.

Call sites (pinned):
  * brain.prefilter.filter_session_text       (source_kind="session")
  * brain.note_extract._build_prompt          (source_kind="note")
  * brain.ingest_notes.ingest_all             (source_kind="note")
  * brain.mcp_server.brain_note_add           (source_kind="journal")

Every call writes a `SanitizeReport` row to
`~/.brain/.audit/sanitize.jsonl` (append-only). Counters only — never
the raw redacted value. The sha8 correlation key lets us audit rotation
events without leaking secrets.

Scope limits (intentional, see scratch/security-ws4-scrubber.md):
  * forward-only — WS4 does not rewrite git history
  * detection-only for `low-entropy` secrets (see false-positive carveouts)
  * CHATML / TOOL_CALL_FORGERY / ZWJ tripwires reject regardless of scope
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import brain.config as config


# ---------------------------------------------------------------------------
# scrubber version
# ---------------------------------------------------------------------------

# Bump when ANY of the following change materially:
#   * the set or order of passes in `sanitize()`
#   * the secret-regex table (_SECRET_PATTERNS)
#   * the injection-tripwire table (_INJECTION_RULES)
#   * the entropy thresholds or carve-outs
#
# Consumers (`.vec` bundle build, `semantic.ensure_built`) compare
# their stored `scrub_tag` against this constant. A mismatch means the
# embedded text was produced by an older ruleset — the downstream
# pipeline may be holding content the current scrubber would have
# redacted/rejected. Treated as a forced full re-ingest trigger.
#
# Format: `ws4-vN` (lexicographically orderable; allows simple
# "newer than" checks with tuple comparison on the suffix). History:
#   v1 — initial (2026-04-23): 22 secret regex + 11 injection rules +
#        4-pass order (secret → injection → entropy → length-elide).
VERSION = "ws4-v1"


# ---------------------------------------------------------------------------
# report shape
# ---------------------------------------------------------------------------


@dataclass
class SanitizeReport:
    """Structured outcome of one sanitize() call.

    `text` is the cleaned output. Every list holds (kind/rule, sha8)
    pairs — sha8 is the first 8 hex chars of sha256 over the original
    (secret / block / line) value. It is enough to correlate rotations
    and dedup repeated leaks within one vault; insufficient to
    brute-force-recover the secret without the original in hand.
    """

    text: str
    redactions: list[tuple[str, str]] = field(default_factory=list)
    rejections: list[tuple[str, str]] = field(default_factory=list)
    flags: list[tuple[str, str]] = field(default_factory=list)
    elisions: list[tuple[str, str]] = field(default_factory=list)

    def any_hit(self) -> bool:
        return bool(self.redactions or self.rejections or self.flags or self.elisions)


def _sha8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# pass 1 — named secret patterns (21 rules, REJECT always)
# ---------------------------------------------------------------------------


# Ordered list so overlapping patterns (e.g. PEM vs BASIC_AUTH) resolve
# deterministically. Each entry is (kind, compiled_regex).
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("PEM_PRIVATE_KEY", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA |ENCRYPTED )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END [^-]+PRIVATE KEY-----",
    )),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS_SECRET_KEY", re.compile(
        r"(?i)aws[_-]?secret[_-]?access[_-]?key[\"'\s:=]+([A-Za-z0-9/+=]{40})\b",
    )),
    ("GH_PAT", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("GH_OAUTH", re.compile(r"\bgho_[A-Za-z0-9]{36}\b")),
    ("GH_SERVER_APP", re.compile(r"\bghs_[A-Za-z0-9]{36}\b")),
    ("GH_USER_APP", re.compile(r"\bghu_[A-Za-z0-9]{36}\b")),
    ("GH_REFRESH", re.compile(r"\bghr_[A-Za-z0-9]{36}\b")),
    ("ANTHROPIC_KEY", re.compile(
        r"\bsk-ant-(?:api|admin|rt)[0-9]{2}-[A-Za-z0-9_\-]{90,}\b",
    )),
    # OPENAI_KEY must come AFTER ANTHROPIC_KEY — sk-ant-... also starts with sk-.
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("GCP_SERVICE_ACCT", re.compile(
        r'"private_key_id"\s*:\s*"[0-9a-f]{32,}"',
    )),
    ("SLACK_TOKEN", re.compile(
        r"\bxox[abpr]-[0-9]{10,}-[0-9]{10,}-[0-9]{10,}-[a-f0-9]{24,}\b",
    )),
    ("STRIPE_LIVE", re.compile(r"\b(?:sk|rk|pk)_live_[A-Za-z0-9]{24,}\b")),
    ("STRIPE_TEST", re.compile(r"\b(?:sk|rk|pk)_test_[A-Za-z0-9]{24,}\b")),
    ("HF_TOKEN", re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    ("NPM_TOKEN", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("PYPI_TOKEN", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{70,}\b")),
    ("DISCORD_BOT", re.compile(r"\b[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27,38}\b")),
    ("JWT", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b",
    )),
    ("BASIC_AUTH_URL", re.compile(r"https?://[^\s:@/]+:[^\s@/]+@[^\s/]+")),
    # ENV_FILE_LINE is deliberately last: it's the broadest pattern, so
    # everything with a specific kind wins first.
    ("ENV_FILE_LINE", re.compile(
        r"(?mi)^(?:export\s+)?[A-Z][A-Z0-9_]{2,}\s*=\s*['\"]?[A-Za-z0-9/+=_\-]{24,}['\"]?$",
    )),
]


def _stub_for_secret(kind: str, value: str) -> str:
    sha = _sha8(value)
    # Multi-line secrets (PEM) get a length tail so auditors can see
    # the block was a big blob without needing the original.
    if "\n" in value and len(value) > 1024:
        return f"[REDACTED:{kind}:{sha}:{len(value)}b]"
    return f"[REDACTED:{kind}:{sha}]"


def _redact_secrets(text: str) -> tuple[str, list[tuple[str, str]]]:
    redactions: list[tuple[str, str]] = []
    for kind, pattern in _SECRET_PATTERNS:
        def _sub(m: re.Match, _kind: str = kind) -> str:
            value = m.group(0)
            redactions.append((_kind, _sha8(value)))
            return _stub_for_secret(_kind, value)

        text = pattern.sub(_sub, text)
    return text, redactions


# ---------------------------------------------------------------------------
# pass 2 — entropy catch-all (REJECT conditional)
# ---------------------------------------------------------------------------


_ENTROPY_TOKEN_RE = re.compile(r"\S+")

# False-positive carve-outs — tokens matching these are NOT candidates
# even if they pass entropy gates.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_NIX_STORE_RE = re.compile(r"^/nix/store/[a-z0-9]{32}-")
# Tokens preceded by "sha256:", "sha1:", "md5:" on the same line are
# asserted-public hashes; we skip them.
_ASSERTED_HASH_RE = re.compile(r"(?i)(?:sha(?:256|1|512)|md5|commit|ref)\s*[:=]\s*$")

_MIN_TOKEN_LEN_TIGHT = 28
_MIN_TOKEN_LEN_LOOSE = 40
_ENTROPY_TIGHT = 4.5
_ENTROPY_LOOSE = 4.0


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    counts = Counter(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _char_class_count(s: str) -> int:
    classes = 0
    if any(c.isupper() for c in s):
        classes += 1
    if any(c.islower() for c in s):
        classes += 1
    if any(c.isdigit() for c in s):
        classes += 1
    if any(not c.isalnum() for c in s):
        classes += 1
    return classes


def _is_asserted_public_hash(text: str, start: int) -> bool:
    # Look back on the same line for `sha256:`, `commit:`, `ref:`, etc.
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    return bool(_ASSERTED_HASH_RE.search(prefix))


def _token_is_likely_secret(tok: str) -> bool:
    if _UUID_RE.match(tok) or _GIT_SHA_RE.match(tok) or _NIX_STORE_RE.match(tok):
        return False
    # Don't re-redact our own stubs. Pass 1 and Pass 2 (injection) may
    # have already produced stubs like `[REDACTED:...]` or
    # `[REJECTED:INJECTION:...]` whose tokens can themselves look
    # high-entropy. Leaving them alone preserves the audit trail.
    if tok.startswith(("[REDACTED:", "[REJECTED:", "[FLAG:", "[ELIDED:")):
        return False
    if _char_class_count(tok) < 2:
        return False
    if len(tok) >= _MIN_TOKEN_LEN_TIGHT and _shannon_entropy(tok) >= _ENTROPY_TIGHT:
        return True
    if len(tok) >= _MIN_TOKEN_LEN_LOOSE and _shannon_entropy(tok) >= _ENTROPY_LOOSE:
        return True
    return False


def _redact_high_entropy(text: str) -> tuple[str, list[tuple[str, str]]]:
    redactions: list[tuple[str, str]] = []
    out: list[str] = []
    cursor = 0
    for m in _ENTROPY_TOKEN_RE.finditer(text):
        tok = m.group(0)
        start, end = m.span()
        if not _token_is_likely_secret(tok):
            continue
        if _is_asserted_public_hash(text, start):
            continue
        out.append(text[cursor:start])
        sha = _sha8(tok)
        out.append(f"[REDACTED:HIGH_ENTROPY:{sha}:{len(tok)}ch]")
        redactions.append(("HIGH_ENTROPY", sha))
        cursor = end
    out.append(text[cursor:])
    return "".join(out), redactions


# ---------------------------------------------------------------------------
# pass 3 — injection tripwires (context-dependent policy)
# ---------------------------------------------------------------------------


# (rule_name, pattern, always_reject) — always_reject overrides the
# scope policy below (CHATML / TOOL_CALL_FORGERY / ZWJ).
_INJECTION_RULES: list[tuple[str, re.Pattern, bool]] = [
    ("CHATML_MARKER", re.compile(
        r"<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>",
    ), True),
    ("TOOL_CALL_FORGERY", re.compile(
        r"(?i)(?:<function_calls>|<invoke\s+name=|<tool_call\b)",
    ), True),
    ("ZWJ_HIDING", re.compile(
        "[​-‏‪-‮⁦-⁩]{3,}",
    ), True),
    ("IGNORE_PRIOR", re.compile(
        r"(?i)\b(?:ignore|disregard|forget)\s+"
        r"(?:all\s+|any\s+|the\s+|your\s+)?"
        r"(?:previous|prior|above|earlier|system|initial)\s+"
        r"(?:instructions?|prompts?|rules?|messages?|context)\b",
    ), False),
    ("ROLE_OVERRIDE", re.compile(
        r"(?i)\byou\s+are\s+(?:now\s+)?(?:a\s+|an\s+)?"
        r"(?:different|new|helpful|unrestricted|unfiltered|uncensored|dan|jailbroken)\b",
    ), False),
    ("SYSTEM_ROLE_LEAK", re.compile(
        r"(?im)^\s*(?:system|assistant|user)\s*:\s*",
    ), False),
    ("INSTR_TAG_INJECT", re.compile(
        r"(?i)<(?:instructions?|system|rules?|policy)>"
        r"[\s\S]{0,4000}?"
        r"</(?:instructions?|system|rules?|policy)>",
    ), False),
    ("NEW_RULES", re.compile(
        r"(?i)\b(?:new|updated|revised)\s+"
        r"(?:rules?|instructions?|system\s+prompt)\s*(?::|are|follow)",
    ), False),
    ("SELF_EXFIL", re.compile(
        r"(?i)\b(?:send|post|upload|email|transmit|curl|fetch)\s+"
        r"(?:the\s+)?(?:brain|vault|\.env|credentials|api[_\s-]?key)\b",
    ), False),
    ("MARKDOWN_IMG_EXFIL", re.compile(
        r"!\[[^\]]*\]\(https?://"
        r"(?!(?:github\.com|raw\.githubusercontent\.com|obsidian\.md)/)"
        r"[^)]+\?[^)]*=",
    ), False),
    ("IDENTITY_CLAIM", re.compile(
        r"(?i)\b(?:stephane|son)\s+"
        r"(?:is|was|lives|works|loves|hates|prefers|owns|uses)\b",
    ), False),
]

# For session transcripts we split on `### User` / `### Claude` markers
# and apply per-block policy. Everywhere else, policy is uniform per
# source_kind.
_SPEAKER_RE = re.compile(r"^### (User|Claude)\s*$")
_TOOL_LINE_RE = re.compile(r"^\s*\[tool:\s*(\w+)")

_SOURCE_KINDS = {"session", "tool_output", "user_turn", "webfetch", "note", "journal"}


def _scope_policy(source_kind: str, rule: str, always_reject: bool) -> str:
    """Return 'reject' | 'flag' | 'pass' for this rule in this scope."""
    if always_reject:
        return "reject"
    # tool_output and webfetch are the most paranoid scopes: reject.
    if source_kind in ("tool_output", "webfetch"):
        if rule == "IDENTITY_CLAIM" and source_kind == "webfetch":
            return "flag"
        return "reject"
    # user_turn / note / journal — user-authored or user-originating.
    if source_kind in ("user_turn", "note", "journal"):
        if rule == "IDENTITY_CLAIM":
            # user self-description is legitimate; don't even flag.
            return "pass"
        return "flag"
    # session mode delegates to block-level decisions below.
    return "flag"


def _reject_stub(rule: str, block: str) -> str:
    return f"[REJECTED:INJECTION:{rule}:{_sha8(block)}]"


def _apply_tripwires_block(
    text: str, source_kind: str,
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """Apply tripwires to a block treated as one scope. Returns
    (text, rejections, flags)."""
    rejections: list[tuple[str, str]] = []
    flags: list[tuple[str, str]] = []

    # First pass: does any "reject" rule fire? If yes, short-circuit
    # and replace the whole block.
    for rule, pattern, always_reject in _INJECTION_RULES:
        if not pattern.search(text):
            continue
        policy = _scope_policy(source_kind, rule, always_reject)
        if policy == "reject":
            sha = _sha8(text)
            rejections.append((rule, sha))
            return _reject_stub(rule, text), rejections, flags

    # No reject fired — apply flag markers line-by-line.
    lines = text.split("\n")
    out_lines: list[str] = []
    for line in lines:
        flagged_rules: list[str] = []
        for rule, pattern, always_reject in _INJECTION_RULES:
            if not pattern.search(line):
                continue
            policy = _scope_policy(source_kind, rule, always_reject)
            if policy == "flag":
                flagged_rules.append(rule)
                flags.append((rule, _sha8(line)))
        if flagged_rules:
            prefix = "".join(f"[FLAG:INJECTION:{r}]" for r in flagged_rules)
            out_lines.append(f"{prefix} {line}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines), rejections, flags


def _apply_tripwires_session(
    text: str,
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """Session mode: walk `### User` / `### Claude` blocks. Inside a
    Claude block, any `[tool: X]` output region is treated as
    tool_output; plain prose is treated as user_turn (Claude prose is
    high-trust). User blocks are treated as user_turn throughout."""
    lines = text.split("\n")
    # For each line decide the active scope.
    scope_per_line: list[str] = []
    current_speaker: str | None = None
    in_tool_block = False
    for line in lines:
        m = _SPEAKER_RE.match(line.strip())
        if m:
            current_speaker = m.group(1)
            in_tool_block = False
            scope_per_line.append("user_turn")  # header line itself
            continue
        if current_speaker == "Claude" and _TOOL_LINE_RE.match(line):
            in_tool_block = True
            scope_per_line.append("tool_output")
            continue
        if in_tool_block:
            if not line.strip():
                in_tool_block = False
                scope_per_line.append("user_turn")
                continue
            scope_per_line.append("tool_output")
            continue
        scope_per_line.append("user_turn")

    # Coalesce into contiguous scope segments so a reject replaces the
    # whole segment (e.g. the whole `[tool: WebFetch]` body), not a
    # single line.
    segments: list[tuple[str, list[int]]] = []
    for i, scope in enumerate(scope_per_line):
        if segments and segments[-1][0] == scope:
            segments[-1][1].append(i)
        else:
            segments.append((scope, [i]))

    rejections: list[tuple[str, str]] = []
    flags: list[tuple[str, str]] = []
    out_lines: list[str | None] = list(lines)
    for scope, idxs in segments:
        chunk = "\n".join(lines[i] for i in idxs)
        cleaned, r, f = _apply_tripwires_block(chunk, scope)
        rejections.extend(r)
        flags.extend(f)
        cleaned_lines = cleaned.split("\n")
        # If the segment was replaced whole (stub), put the stub on
        # the first idx and blank the rest so we preserve line count
        # semantics approximately.
        if len(cleaned_lines) != len(idxs):
            out_lines[idxs[0]] = cleaned
            for j in idxs[1:]:
                out_lines[j] = None
        else:
            for j, cline in zip(idxs, cleaned_lines):
                out_lines[j] = cline

    return "\n".join(l for l in out_lines if l is not None), rejections, flags


def _apply_injection_tripwires(
    text: str, source_kind: str,
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    if source_kind == "session":
        return _apply_tripwires_session(text)
    return _apply_tripwires_block(text, source_kind)


# ---------------------------------------------------------------------------
# pass 4 — length elision
# ---------------------------------------------------------------------------


_MAX_LINE_LEN = 1200
_MAX_BLOCK_KB = 8


def _elide_long_lines(text: str) -> tuple[str, list[tuple[str, str]]]:
    elisions: list[tuple[str, str]] = []
    out_lines: list[str] = []
    for line in text.split("\n"):
        if len(line) > _MAX_LINE_LEN:
            sha = _sha8(line)
            elisions.append(("LONG_LINE", sha))
            out_lines.append(f"[ELIDED:{len(line)}ch:{sha}]")
        else:
            out_lines.append(line)
    return "\n".join(out_lines), elisions


# ---------------------------------------------------------------------------
# public entrypoint
# ---------------------------------------------------------------------------


def sanitize(
    text: str,
    *,
    source_kind: str,
    source_path: str = "",
    emit_audit: bool = True,
) -> SanitizeReport:
    """Run all four passes on `text` and return a SanitizeReport.

    `source_kind` controls injection-tripwire policy; must be one of
    `session`, `tool_output`, `user_turn`, `webfetch`, `note`, `journal`.

    When `emit_audit` is True and any pass fired, a one-line JSON entry
    is appended to `~/.brain/.audit/sanitize.jsonl` with counters only
    (no content, no raw secrets).
    """
    if source_kind not in _SOURCE_KINDS:
        raise ValueError(
            f"unknown source_kind: {source_kind!r} "
            f"(expected one of {sorted(_SOURCE_KINDS)})"
        )

    if not text:
        return SanitizeReport(text="")

    # 1. named secret patterns (REJECT always — highest confidence)
    text, redactions = _redact_secrets(text)
    # 2. injection tripwires (scope-dependent) — must run before entropy
    # so attack signatures aren't pre-munged into opaque REDACTED stubs.
    text, rejections, flags = _apply_injection_tripwires(text, source_kind)
    # 3. high-entropy catch-all (REJECT) — fallback for secrets that
    # don't match a named pattern.
    text, entropy_hits = _redact_high_entropy(text)
    redactions.extend(entropy_hits)
    # 4. length elision
    text, elisions = _elide_long_lines(text)

    report = SanitizeReport(
        text=text,
        redactions=redactions,
        rejections=rejections,
        flags=flags,
        elisions=elisions,
    )

    if emit_audit and report.any_hit():
        try:
            _write_audit(report, source_kind, source_path)
        except Exception:
            # audit write failure never blocks ingest
            pass

    return report


# ---------------------------------------------------------------------------
# audit ledger — append-only JSONL, counters only
# ---------------------------------------------------------------------------


_AUDIT_DIR_NAME = ".audit"
_AUDIT_FILE_NAME = "sanitize.jsonl"


def _audit_path() -> Path:
    # Resolve at call time so tests / multi-vault setups that flip
    # BRAIN_DIR mid-process land their audit lines in the right vault.
    base = Path(os.environ.get("BRAIN_DIR") or config.BRAIN_DIR)
    return base / _AUDIT_DIR_NAME / _AUDIT_FILE_NAME


def _summarise(pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind, _sha in pairs:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _write_audit(report: SanitizeReport, source_kind: str, source_path: str) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        # Which scrubber ruleset produced this row. Consumers of this
        # ledger (e.g. `semantic.ensure_built`) compare against
        # `sanitize.VERSION` to detect rows scrubbed by an older
        # ruleset and force re-ingest on bump.
        "scrub_tag": VERSION,
        "source_kind": source_kind,
        "source_path": source_path,
        "redactions": _summarise(report.redactions),
        "rejections": _summarise(report.rejections),
        "flags": _summarise(report.flags),
        "elisions": _summarise(report.elisions),
    }
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    # Append with O_APPEND so concurrent writers don't clobber. One
    # short line per sanitize call is well below the pipe-atomic limit
    # on Linux (4 KB), so single write() is safe without locking.
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
