"""Regression test for the launchd auto-extract template.

Bug (incident 2026-04-27): `templates/scripts/auto-extract.sh.tmpl` exported
`BRAIN_DIR` but never sourced `<vault>/.brain.conf`. Because launchd does
not read the user's shell rc, every persisted env var (notably
`BRAIN_USE_CLAIMS=1` and `BRAIN_STRICT_CLAIMS=1`) was missing in the
extract subprocess. Result: dual-write to `fact_claims` was silently
skipped — facts landed in the legacy `facts` table only, while
`fact_claims` was supposed to be the strict-mode source of truth.

These tests lock in two things:
  1. The raw template literally contains the `. "$BRAIN_DIR/.brain.conf"`
     source line (cheap structural check — fails fast even without
     bash on PATH).
  2. The fully rendered script (via `bin/_render.sh`) contains the same
     line with the substituted `BRAIN_DIR`, so a future renderer
     regression that drops the line is caught at the same gate.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "templates" / "scripts" / "auto-extract.sh.tmpl"
RENDER_SH = REPO_ROOT / "bin" / "_render.sh"
TEMPLATES_DIR = REPO_ROOT / "templates"


def test_template_sources_brain_conf():
    """Raw template must source `.brain.conf` so launchd-driven runs
    pick up `BRAIN_USE_CLAIMS=1` / `BRAIN_STRICT_CLAIMS=1`.

    Without this line, the extract subprocess inherits only the launchd
    plist env (just `BRAIN_DIR`) and the dual-write to `fact_claims` is
    silently disabled — see incident 2026-04-27.
    """
    body = TEMPLATE.read_text()
    # The exact pattern install.sh ships. Match `. "$BRAIN_DIR/.brain.conf"`
    # with the standard `[ -f ... ] &&` guard so an absent conf file is a
    # no-op rather than a `set -e` exit.
    pattern = re.compile(
        r'\[\s*-f\s+"\$BRAIN_DIR/\.brain\.conf"\s*\]\s*&&\s*\.\s+"\$BRAIN_DIR/\.brain\.conf"'
    )
    assert pattern.search(body), (
        "auto-extract.sh.tmpl must source $BRAIN_DIR/.brain.conf so "
        "launchd-driven extracts inherit BRAIN_USE_CLAIMS / "
        "BRAIN_STRICT_CLAIMS. Missing source line — see incident "
        "2026-04-27."
    )


def test_template_sources_conf_after_brain_dir_export():
    """Order matters: the source line uses `$BRAIN_DIR`, so the export
    must come first."""
    body = TEMPLATE.read_text()
    export_idx = body.find('export BRAIN_DIR=')
    source_idx = body.find('"$BRAIN_DIR/.brain.conf"')
    assert export_idx != -1, "BRAIN_DIR export missing from template"
    assert source_idx != -1, ".brain.conf source line missing from template"
    assert export_idx < source_idx, (
        ".brain.conf must be sourced AFTER `export BRAIN_DIR=...` — "
        "otherwise the path expands to an empty string."
    )


@pytest.mark.skipif(not RENDER_SH.exists(), reason="bin/_render.sh missing")
def test_rendered_script_contains_source_line(tmp_path):
    """Render the template the way `bin/install.sh` does and assert the
    output retains the conf-source line with `$BRAIN_DIR` literal
    (not `{{BRAIN_DIR}}` — that would mean the renderer broke).
    """
    dst = tmp_path / "auto-extract.sh"
    proc = subprocess.run(
        [
            "bash",
            str(RENDER_SH),
            "render",
            str(TEMPLATES_DIR),
            str(TEMPLATE),
            str(dst),
            "/Users/testuser",                         # HOME
            "testuser",                                # USERNAME
            "/Users/testuser/code/brain-project",      # PROJECT_DIR
            "/usr/bin/python3",                        # PYTHON
            "/Users/testuser/.brain",                  # BRAIN_DIR
            "2026-04-27",                              # TODAY
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"renderer failed: rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    rendered = dst.read_text()
    # The renderer leaves `$BRAIN_DIR` alone (it's a shell var ref, not
    # a Mustache token), so the literal `$BRAIN_DIR/.brain.conf` must
    # survive into the deployed script verbatim.
    assert '. "$BRAIN_DIR/.brain.conf"' in rendered, (
        "rendered auto-extract.sh missing `. \"$BRAIN_DIR/.brain.conf\"` — "
        "the conf-source line must survive template rendering, otherwise "
        "launchd extracts run without BRAIN_USE_CLAIMS."
    )
    # And the BRAIN_DIR export should have been substituted to the test
    # value, proving we tested the fully-rendered path (not just a
    # static template grep).
    assert 'export BRAIN_DIR="/Users/testuser/.brain"' in rendered
