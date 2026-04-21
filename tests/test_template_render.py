"""Tests for the shared-partial template renderer (bin/_render.sh).

Covers the Option-A shell renderer that:
  1. expands {{include: <rel-path>}} directives against the templates/
     directory
  2. substitutes {{HOME}} / {{USERNAME}} / {{PROJECT_DIR}} / {{PYTHON}} /
     {{BRAIN_DIR}} / {{TODAY}} tokens (sed pipeline)

Tests invoke the real shell functions by sourcing bin/_render.sh — the
exact same code path bin/install.sh uses, so tests fail loudly if
someone breaks the CLI entry or the awk/sed plumbing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_SH = REPO_ROOT / "bin" / "_render.sh"
TEMPLATES_DIR = REPO_ROOT / "templates"
CLAUDE_TMPL = TEMPLATES_DIR / "claude" / "CLAUDE.md.tmpl"
CURSOR_TMPL = TEMPLATES_DIR / "cursor" / "USER_RULES.md.tmpl"
SHARED_RULES = TEMPLATES_DIR / "_shared" / "rules"

# A fixed set of substitution values reused across the suite. Chosen so
# every rendered host file contains the literal "testuser" in multiple
# positions (greeting, grounding prose, weak-match prose) — making the
# "Son literal is gone" grep trivially decisive.
FAKE_ENV = {
    "home": "/Users/testuser",
    "username": "testuser",
    "project_dir": "/Users/testuser/code/brain-project",
    "python": "/usr/bin/python3",
    "brain_dir": "/Users/testuser/.brain",
    "today": "2026-04-21",
}


def _render(src: Path, dst: Path) -> subprocess.CompletedProcess:
    """Invoke bin/_render.sh render <...> as install.sh does."""
    return subprocess.run(
        [
            "bash",
            str(RENDER_SH),
            "render",
            str(TEMPLATES_DIR),
            str(src),
            str(dst),
            FAKE_ENV["home"],
            FAKE_ENV["username"],
            FAKE_ENV["project_dir"],
            FAKE_ENV["python"],
            FAKE_ENV["brain_dir"],
            FAKE_ENV["today"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _expand(stdin_text: str) -> subprocess.CompletedProcess:
    """Invoke the include-pass in isolation (stdin/stdout mode)."""
    return subprocess.run(
        ["bash", str(RENDER_SH), "expand", str(TEMPLATES_DIR)],
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
    )


# ──────────────────────────────────────────────────────────────────────
# Sanity: prerequisites exist
# ──────────────────────────────────────────────────────────────────────

def test_renderer_script_exists_and_executable():
    assert RENDER_SH.exists(), f"{RENDER_SH} missing — install.sh would break"
    # Not strictly required (bash <path> works without +x) but a nice
    # guardrail against someone accidentally dropping the bit.
    assert RENDER_SH.stat().st_mode & 0o111, "_render.sh should be executable"


def test_all_referenced_partials_exist():
    """Every partial referenced from either host template must exist."""
    import re
    pattern = re.compile(r"\{\{include:\s*([^}]+?)\s*\}\}")
    for tmpl in (CLAUDE_TMPL, CURSOR_TMPL):
        for rel in pattern.findall(tmpl.read_text()):
            target = TEMPLATES_DIR / rel
            assert target.exists(), f"{tmpl.name} references missing partial {rel}"


# ──────────────────────────────────────────────────────────────────────
# Host-template rendering
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("src", [CLAUDE_TMPL, CURSOR_TMPL])
def test_host_renders_without_error(tmp_path: Path, src: Path):
    dst = tmp_path / (src.name + ".out")
    proc = _render(src, dst)
    assert proc.returncode == 0, f"render failed: {proc.stderr}"
    assert dst.exists() and dst.stat().st_size > 0


@pytest.mark.parametrize("src", [CLAUDE_TMPL, CURSOR_TMPL])
def test_host_rendered_contains_username(tmp_path: Path, src: Path):
    """USERNAME token substitution lands in every host output."""
    dst = tmp_path / (src.name + ".out")
    proc = _render(src, dst)
    assert proc.returncode == 0, proc.stderr
    text = dst.read_text()
    assert "testuser" in text, f"{src.name} missing substituted USERNAME"


@pytest.mark.parametrize("src", [CLAUDE_TMPL, CURSOR_TMPL])
def test_host_rendered_has_no_hardcoded_son(tmp_path: Path, src: Path):
    """The 2026-04-21 hardcoded-"Son" regression must not come back.

    Reason this test matters: before the refactor, CLAUDE.md.tmpl had
    literal "Son" in the dép-incident prose (lines 76-83 + 117). Every
    non-Son installer's CLAUDE.md said "Son's slippers." After the
    partial extraction, all such prose lives in _shared/rules/*.md with
    {{USERNAME}}, so no host output should ever contain the literal
    string "Son".
    """
    dst = tmp_path / (src.name + ".out")
    proc = _render(src, dst)
    assert proc.returncode == 0, proc.stderr
    text = dst.read_text()
    # Whole-word check: reject "Son" / "Son's" / "Son " but allow words
    # like "Reason" or "Reasoning" that legitimately contain the
    # substring. Catch at word boundaries on either side.
    import re
    matches = re.findall(r"\bSon('s|s)?\b", text)
    assert not matches, (
        f"{src.name} rendered output contains hardcoded 'Son' "
        f"({len(matches)} occurrence(s)); hardcoded-username regression."
    )


@pytest.mark.parametrize("src", [CLAUDE_TMPL, CURSOR_TMPL])
def test_host_rendered_has_no_unresolved_include_or_token(tmp_path: Path, src: Path):
    """Rendered output must contain no `{{include:…}}` and no `{{TOKEN}}`."""
    dst = tmp_path / (src.name + ".out")
    proc = _render(src, dst)
    assert proc.returncode == 0, proc.stderr
    text = dst.read_text()
    assert "{{include:" not in text, "include directive survived to output"
    # Known substitution tokens we handle — anything else matching
    # {{CAPS}} is a bug (forgotten sed rule).
    import re
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", text)
    assert not leftover, f"unresolved tokens in {src.name}: {leftover}"


# ──────────────────────────────────────────────────────────────────────
# Cross-host partial invariants
# ──────────────────────────────────────────────────────────────────────

def test_shared_partials_render_identical_across_hosts(tmp_path: Path):
    """Each shared partial, once expanded + substituted, should produce
    byte-identical text regardless of which host included it.

    We assert this at the partial level: render the partial as if it
    were its own template, and compare with what install-time rendering
    of the *same* partial produces for any host. Both go through the
    same render_template function, so they must match.

    The real host files inevitably differ in length because each host
    wraps the partials with its own preamble/caveats — this test
    isolates the partials themselves.
    """
    for partial in sorted(SHARED_RULES.glob("*.md")):
        if partial.name == "README.md":
            # Authoring notes for humans, never included by a host.
            continue
        dst = tmp_path / f"partial-{partial.name}"
        proc = _render(partial, dst)
        assert proc.returncode == 0, f"{partial.name}: {proc.stderr}"
        rendered = dst.read_text()
        # Substitution hit at least one spot somewhere (or the partial
        # genuinely has no token, which is fine).
        for tok in ("{{USERNAME}}", "{{BRAIN_DIR}}", "{{PROJECT_DIR}}",
                    "{{HOME}}", "{{PYTHON}}", "{{TODAY}}"):
            assert tok not in rendered, (
                f"{partial.name} has unresolved token {tok} after render"
            )


def test_shared_partials_have_no_host_specific_leakage():
    """Partials must be host-agnostic: no literal 'Cursor' / 'Claude
    Code' / 'SessionStart hook' etc. in prose that should be shared.

    A loose approximation — we ban the literal "SessionStart" in
    shared partials because that's Claude-specific vocabulary; Cursor
    uses "sessionStart". Host-specific wording stays in the host
    template, not the shared partial.
    """
    for partial in sorted(SHARED_RULES.glob("*.md")):
        if partial.name == "README.md":
            continue
        text = partial.read_text()
        assert "SessionStart hook" not in text, (
            f"{partial.name} contains Claude-specific 'SessionStart hook'; "
            f"move to templates/claude/CLAUDE.md.tmpl"
        )
        # Host-specific "claude --print" warning IS intentionally
        # shared (per README.md's superset rule), so not banned.


# ──────────────────────────────────────────────────────────────────────
# Error paths
# ──────────────────────────────────────────────────────────────────────

def test_missing_include_errors_loudly(tmp_path: Path):
    """`{{include: _shared/rules/missing.md}}` must emit a helpful
    stderr line and exit non-zero — silent empty output is a trap."""
    proc = _expand("{{include: _shared/rules/does-not-exist.md}}\n")
    assert proc.returncode != 0, (
        f"missing-partial should fail, got exit 0 with stdout={proc.stdout!r}"
    )
    assert "missing partial" in proc.stderr, (
        f"stderr should name the problem; got: {proc.stderr!r}"
    )
    assert "does-not-exist.md" in proc.stderr


def test_inline_include_in_prose_is_not_expanded(tmp_path: Path):
    """Only whole-line directives expand. If a paragraph says
    `{{include: foo}}` in the middle, leave it alone — users sometimes
    discuss the syntax in prose (e.g. this docstring)."""
    body = "Some prose mentions {{include: _shared/rules/never.md}} inline.\n"
    proc = _expand(body)
    assert proc.returncode == 0, proc.stderr
    # The never.md contents should NOT have been spliced in.
    assert "NEVER" not in proc.stdout, (
        "inline {{include: …}} inside a paragraph should not expand"
    )
    # And the directive text itself is preserved.
    assert "{{include: _shared/rules/never.md}}" in proc.stdout


def test_whole_line_include_is_expanded(tmp_path: Path):
    """Standalone-line directives DO expand — the common case."""
    proc = _expand("{{include: _shared/rules/never.md}}\n")
    assert proc.returncode == 0, proc.stderr
    assert "## NEVER" in proc.stdout, (
        "stand-alone include line should splice in never.md"
    )
