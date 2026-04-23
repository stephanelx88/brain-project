"""Pre-filter raw session transcripts to drop low-signal tool noise.

`harvest_session.format_session_summary` ships entire transcripts (often
30+ KB each) including Read/Glob/Grep/Bash output that has zero entity
content. Stripping that before the LLM sees it cuts tokens 5-10x with
no recall loss.

Heuristics (tuned, conservative — when in doubt we keep the line):

  * keep all User turns (they're the ground-truth signal)
  * keep Claude prose lines outside of [tool: …] envelopes
  * collapse pure-tool turns (Read/Glob/Grep/Bash/LS/TodoWrite) to a
    one-line stub
  * keep Write/Edit tool calls but only their semantic header
  * cap any single line at 800 chars
  * drop the leading "###" speaker headers when the kept block is empty
"""

from __future__ import annotations

import re
from pathlib import Path

# Tools whose output is almost never an entity-bearing signal
_NOISY_TOOLS = {
    "Read", "Glob", "Grep", "LS", "Bash", "TodoWrite", "WebFetch",
    "WebSearch", "BashOutput", "KillShell", "ListMcpResources",
    "FetchMcpResource", "Task",
}
# Tools whose first line *is* a useful signal (file path / target)
_KEEP_HEADER_TOOLS = {"Write", "Edit", "EditNotebook", "StrReplace", "Delete"}

_TOOL_LINE_RE = re.compile(r"^\[tool:\s*(\w+)\b")
_SPEAKER_RE = re.compile(r"^### (User|Claude)\s*$")

_MAX_LINE = 800


def _filter_block(lines: list[str]) -> list[str]:
    """Filter the body of one speaker block."""
    out: list[str] = []
    skip_until_blank = False
    tool_stub_emitted = set()
    for raw in lines:
        line = raw.rstrip()
        if not line:
            if not (out and out[-1] == ""):
                out.append("")
            skip_until_blank = False
            continue

        m = _TOOL_LINE_RE.match(line.lstrip())
        if m:
            tool = m.group(1)
            if tool in _NOISY_TOOLS:
                if tool not in tool_stub_emitted:
                    out.append(f"[used: {tool}]")
                    tool_stub_emitted.add(tool)
                skip_until_blank = True
                continue
            if tool in _KEEP_HEADER_TOOLS:
                # keep just the first line of the tool block
                out.append(line[:_MAX_LINE])
                skip_until_blank = True
                continue
            out.append(line[:_MAX_LINE])
            continue

        if skip_until_blank:
            # we're inside a noisy tool's continuation — drop it
            continue

        out.append(line[:_MAX_LINE])

    # collapse trailing empties
    while out and out[-1] == "":
        out.pop()
    return out


def filter_session_text(text: str, *, source_path: str = "") -> str:
    """Apply the prefilter to a full session-summary markdown.

    WS4: before the tool-noise strip, run the sanitize pass to redact
    secrets, flag/reject injection payloads, and elide oversized lines.
    The order matters — we scrub BEFORE collapsing tool blocks so the
    injection-tripwire scope (tool_output vs user_turn) can still see
    the `[tool: X]` markers in the text.
    """
    from brain.sanitize import sanitize  # lazy to avoid import cycle at cold start
    text = sanitize(text, source_kind="session", source_path=source_path).text
    lines = text.split("\n")
    # Pass through the header (everything until first '## Conversation')
    out: list[str] = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if lines[i].strip() == "## Conversation":
            i += 1
            break
        i += 1

    # Now split remaining into speaker blocks and filter each
    block_lines: list[str] = []
    speaker = None

    def flush():
        nonlocal block_lines, speaker
        if speaker is None:
            return
        filtered = _filter_block(block_lines)
        if filtered:
            out.append(f"### {speaker}")
            out.extend(filtered)
            out.append("")
        block_lines = []

    while i < len(lines):
        line = lines[i]
        m = _SPEAKER_RE.match(line.strip())
        if m:
            flush()
            speaker = m.group(1)
        else:
            block_lines.append(line)
        i += 1
    flush()

    return "\n".join(out).rstrip() + "\n"


def filter_file(path: Path) -> tuple[int, int, str]:
    """Returns (orig_chars, new_chars, filtered_text)."""
    text = path.read_text(errors="replace")
    out = filter_session_text(text, source_path=str(path))
    return len(text), len(out), out


def main():
    """CLI for ad-hoc inspection: `python -m brain.prefilter raw/foo.md`."""
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m brain.prefilter <raw-session-file>")
        sys.exit(1)
    p = Path(sys.argv[1])
    a, b, text = filter_file(p)
    print(f"# {p.name}: {a} → {b} chars ({100*b//max(a,1)}%)")
    print(text)


if __name__ == "__main__":
    main()
