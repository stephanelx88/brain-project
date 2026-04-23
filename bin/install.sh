#!/usr/bin/env bash
# brain install — single-shot setup. Idempotent: safe to re-run.
#
# Steps:
#   1. detect $HOME, $USER, the right python (3.11+), the project location
#   2. pip install -e .  (so source edits take effect immediately)
#   3. ensure ~/.brain/ exists with seed dirs
#   4. render templates → ~/.brain/bin/, identity/*, ~/Library/LaunchAgents/, ~/.claude/
#   5. download embedding model (~120 MB, one-time)
#   6. register the MCP server with Claude Code (and Cursor, if installed)
#   7. load the launchd job
#   8. run doctor.sh to verify everything green
#
# Re-run after editing templates/ to regenerate the deployed scripts.

set -euo pipefail

# Optional flags
ALLOW_SANDBOX=0
for arg in "$@"; do
  case "$arg" in
    --allow-sandbox) ALLOW_SANDBOX=1 ;;
    -h|--help)
      sed -n '2,15p' "$0" | sed 's/^# \?//'
      echo
      echo "Flags:"
      echo "  --allow-sandbox   override the HOME-mismatch safety check (advanced)"
      exit 0
      ;;
  esac
done

# ──────────────────────────────────────────────────────────────────────
# Detect environment
# ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOME_DIR="$HOME"
USERNAME="${USER:-$(whoami)}"
# BRAIN_DIR is the user's vault. `brain init` exports it before invoking
# this script; falls back to ~/.brain for direct/legacy invocations.
# Exported so every Python subprocess we spawn (db rebuild, semantic
# ensure, brain.mcp_server when test-launched) reads the right vault
# via brain.config._resolve_brain_dir().
export BRAIN_DIR="${BRAIN_DIR:-$HOME_DIR/.brain}"
TODAY="$(date +%Y-%m-%d)"

# ──────────────────────────────────────────────────────────────────────
# Pre-flight: refuse to run with a faked $HOME unless explicitly opted in.
#
# Why: pip install -e . is *per Python interpreter*, not per HOME. If you
# point HOME at /tmp/sandbox to "test a fresh install", pip will happily
# overwrite the editable install in your real Python — silently breaking
# your existing brain. Discovered the hard way 2026-04-19.
#
# `dscl` is macOS-only; on Linux we read /etc/passwd (or NSS) via
# `getent`. Either way the check is advisory and skipped when the lookup
# fails.
# ──────────────────────────────────────────────────────────────────────
OS="$(uname -s)"
REAL_HOME=""
case "$OS" in
  Darwin) REAL_HOME="$(dscl . -read "/Users/$USERNAME" NFSHomeDirectory 2>/dev/null | awk '{print $2}')" ;;
  Linux)  REAL_HOME="$(getent passwd "$USERNAME" 2>/dev/null | cut -d: -f6)" ;;
esac
if [[ -n "$REAL_HOME" && "$REAL_HOME" != "$HOME_DIR" && "$ALLOW_SANDBOX" -ne 1 ]]; then
  echo "✗ \$HOME ($HOME_DIR) does not match $USERNAME's real home ($REAL_HOME)."
  echo
  echo "  Running install.sh under a faked \$HOME will mutate the real Python's"
  echo "  editable install of 'brain', silently breaking your live setup."
  echo
  echo "  If you really want to test a fresh-clone install in a sandbox:"
  echo "    1. Use a separate Python (venv or pyenv-virtualenv), then"
  echo "    2. HOME=/your/sandbox $0 --allow-sandbox"
  exit 1
fi

case "$OS" in
  Darwin|Linux) : ;;
  *)
    echo "✗ Unsupported platform: $OS."
    echo "  The Python package works on any Unix, but the scheduler install"
    echo "  flow (launchd / systemd) has no backend for this OS."
    exit 1
    ;;
esac

# ──────────────────────────────────────────────────────────────────────
# Pick python — prefer existing brain venv, then pyenv, then system 3.11+
#
# Why the brain-venv is first: on re-installs we always want the same
# interpreter the MCP server is already registered against. Also avoids
# PEP 668 and missing-pip issues (see python_ensure_installable below).
# ──────────────────────────────────────────────────────────────────────
BRAIN_VENV="$HOME_DIR/.brain-venv"
PYTHON=""
for candidate in \
    "$BRAIN_VENV/bin/python3" \
    "$HOME_DIR/.pyenv/versions/3.12.12/bin/python3" \
    "$HOME_DIR/.pyenv/shims/python3" \
    "$(command -v python3.12 || true)" \
    "$(command -v python3.11 || true)" \
    "$(command -v python3 || true)"; do
  if [[ -x "$candidate" ]] && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "✗ No Python ≥ 3.11 found."
  echo "  Install one of:"
  echo "    - Debian/Ubuntu: sudo apt install python3.12 python3.12-venv"
  echo "    - macOS:         brew install python@3.12"
  echo "    - Anywhere:      curl -LsSf https://astral.sh/uv/install.sh | sh  (then 'uv python install 3.12')"
  exit 1
fi
PYVER="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"

# ──────────────────────────────────────────────────────────────────────
# Bootstrap pip + sidestep PEP 668
#
# Common failure modes this handles:
#   - Debian/Ubuntu system python3 ships without pip/ensurepip (apt
#     splits it into python3-pip). "No module named pip" on step [1].
#   - Debian 12+, Ubuntu 23.04+, Homebrew python mark the interpreter
#     EXTERNALLY-MANAGED (PEP 668). pip install -e . refuses with
#     "error: externally-managed-environment" unless you use a venv
#     or --break-system-packages (which we won't — it pollutes the OS
#     python the distro relies on).
#
# Strategy: if the chosen python can't pip-install editable, create a
# dedicated venv at ~/.brain-venv and repoint PYTHON at it. Try
# `python -m venv` first (stdlib, works when python3-venv is installed),
# fall back to `uv venv` if available, and finally emit a distro-
# specific suggestion if both paths fail.
# ──────────────────────────────────────────────────────────────────────

# Install pip into $1 (a python executable). Prints nothing on success.
python_ensure_pip() {
  local py="$1"
  if "$py" -m pip --version >/dev/null 2>&1; then return 0; fi
  # ensurepip is the stdlib bootstrap — absent on Debian/Ubuntu system python
  # unless python3-venv is installed.
  if "$py" -m ensurepip --default-pip >/dev/null 2>&1; then return 0; fi
  # uv can install pip into any interpreter without needing pip itself.
  if command -v uv >/dev/null 2>&1; then
    if uv pip install --quiet --python "$py" pip >/dev/null 2>&1; then return 0; fi
  fi
  return 1
}

# Return 0 if $1 (a python executable) can do `pip install` without PEP 668
# blocking. Detection is probe-based: install a harmless no-op package with
# --dry-run and grep for the marker.
python_can_pip_install() {
  local py="$1"
  local out
  out=$("$py" -m pip install --dry-run --quiet pip 2>&1 || true)
  if echo "$out" | grep -qi "externally-managed"; then return 1; fi
  return 0
}

# Create (or reuse) $BRAIN_VENV, repoint PYTHON at it. On failure, exit.
python_create_brain_venv() {
  if [[ -x "$BRAIN_VENV/bin/python3" ]] && \
     "$BRAIN_VENV/bin/python3" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    :  # reuse
  elif "$PYTHON" -m venv "$BRAIN_VENV" >/dev/null 2>&1; then
    :  # stdlib venv worked
  elif command -v uv >/dev/null 2>&1 && uv venv --quiet --python ">=3.11" "$BRAIN_VENV" >/dev/null 2>&1; then
    :  # uv venv worked
  else
    echo "✗ Could not create a Python venv at $BRAIN_VENV."
    echo "  Install one of:"
    echo "    - Debian/Ubuntu: sudo apt install python3-venv  (or python3.12-venv)"
    echo "    - macOS:         brew install python@3.12"
    echo "    - Anywhere:      curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
  fi
  PYTHON="$BRAIN_VENV/bin/python3"
}

# If $PYTHON can't pip-install editable, switch to a dedicated venv.
if ! python_ensure_pip "$PYTHON" || ! python_can_pip_install "$PYTHON"; then
  echo "  ! $PYTHON can't pip-install here (missing pip or PEP 668-managed)"
  echo "  ! creating dedicated venv at $BRAIN_VENV"
  python_create_brain_venv
  python_ensure_pip "$PYTHON" || {
    echo "✗ Failed to install pip into $BRAIN_VENV."
    exit 1
  }
  PYVER="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
fi

# ──────────────────────────────────────────────────────────────────────
# Pre-flight: on macOS, project must NOT live under
# ~/Desktop, ~/Documents, ~/Downloads — macOS TCC denies launchd-spawned
# processes read access there. Linux has no equivalent restriction, so
# the check is scoped to Darwin.
# ──────────────────────────────────────────────────────────────────────
if [[ "$OS" == "Darwin" ]]; then
  case "$PROJECT_DIR" in
    "$HOME_DIR/Desktop"/*|"$HOME_DIR/Documents"/*|"$HOME_DIR/Downloads"/*)
      echo "✗ Project lives at $PROJECT_DIR, which macOS TCC blocks from launchd."
      echo "  Move it: mv \"$PROJECT_DIR\" \"$HOME_DIR/code/$(basename "$PROJECT_DIR")\""
      echo "  Then re-run this installer."
      exit 1
      ;;
  esac
fi

echo "── brain install ─────────────────────────────────────"
echo "  HOME       : $HOME_DIR"
echo "  USER       : $USERNAME"
echo "  PROJECT    : $PROJECT_DIR"
echo "  PYTHON     : $PYTHON ($PYVER)"
echo "  BRAIN_DIR  : $BRAIN_DIR"
echo

# ──────────────────────────────────────────────────────────────────────
# 1. pip install -e .  (editable, so source edits take immediate effect)
# ──────────────────────────────────────────────────────────────────────
echo "[1/8] pip install -e . (editable)"
"$PYTHON" -m pip install --quiet --upgrade pip >/dev/null
"$PYTHON" -m pip install --quiet -e "$PROJECT_DIR"
echo "      ✓ brain installed"

# ──────────────────────────────────────────────────────────────────────
# 2. Ensure brain dir + seed structure
# ──────────────────────────────────────────────────────────────────────
echo "[2/8] vault skeleton at $BRAIN_DIR"
mkdir -p \
  "$BRAIN_DIR/bin" \
  "$BRAIN_DIR/identity" \
  "$BRAIN_DIR/raw" \
  "$BRAIN_DIR/logs" \
  "$BRAIN_DIR/entities/people" \
  "$BRAIN_DIR/entities/projects" \
  "$BRAIN_DIR/entities/domains"
if [[ ! -d "$BRAIN_DIR/.git" ]]; then
  git -C "$BRAIN_DIR" init --quiet
fi
echo "      ✓ vault ready"

# ──────────────────────────────────────────────────────────────────────
# 3. Render templates with sed substitution
#
# Two-pass render (see bin/_render.sh for the impl — sourced so tests
# can exercise the same functions install does):
#   Pass 1: expand {{include: <path-relative-to-templates/>}} directives
#           (single pass, no nesting — partials are pure prose today).
#           See templates/_shared/rules/README.md for the "why".
#   Pass 2: the original token substitution.
# ──────────────────────────────────────────────────────────────────────
TEMPLATES_DIR="$PROJECT_DIR/templates"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_render.sh"

render() {
  local src="$1" dst="$2"
  render_template "$TEMPLATES_DIR" "$src" "$dst" \
    "$HOME_DIR" "$USERNAME" "$PROJECT_DIR" "$PYTHON" "$BRAIN_DIR" "$TODAY"
}

echo "[3/8] render scripts + plist + CLAUDE.md + session-start hooks"
render "$PROJECT_DIR/templates/scripts/auto-extract.sh.tmpl" "$BRAIN_DIR/bin/auto-extract.sh"
chmod +x "$BRAIN_DIR/bin/auto-extract.sh"

# Cursor sessionStart hook script — invoked by ~/.cursor/hooks.json on
# every new composer session. Mirrors what Claude's SessionStart hook
# does (harvest + audit), so onboarding doesn't require any manual
# settings.json / hooks.json editing on either side.
render "$PROJECT_DIR/templates/cursor/hooks/session-start.sh.tmpl" "$BRAIN_DIR/bin/cursor-session-start.sh"
chmod +x "$BRAIN_DIR/bin/cursor-session-start.sh"

# Persist resolved paths so doctor.sh + future re-installs find them.
cat > "$BRAIN_DIR/.brain.conf" <<EOF
# Generated by brain install.sh — do not edit by hand. Re-run install to update.
PYTHON="$PYTHON"
PROJECT_DIR="$PROJECT_DIR"
USERNAME="$USERNAME"
BRAIN_DIR="$BRAIN_DIR"
EOF

# Doctor lives in the source repo so it tracks code changes; just symlink it.
ln -sf "$PROJECT_DIR/bin/doctor.sh" "$BRAIN_DIR/bin/doctor.sh"

# Scheduler units (launchd plist on macOS, systemd --user units on Linux)
# are rendered + installed in step [7/8] below via the scheduler dispatcher.
# Keeping it out of step [3/8] so the MCP registration / hook wiring can
# still proceed on platforms with no supported scheduler backend.

CLAUDE_MD="$HOME_DIR/.claude/CLAUDE.md"
mkdir -p "$(dirname "$CLAUDE_MD")"
if [[ -f "$CLAUDE_MD" ]] && ! grep -q "Personal Brain — Mandatory Use" "$CLAUDE_MD" 2>/dev/null; then
  cp "$CLAUDE_MD" "$CLAUDE_MD.pre-brain.bak"
  echo "      ! existing $CLAUDE_MD backed up to $CLAUDE_MD.pre-brain.bak"
fi
render "$PROJECT_DIR/templates/claude/CLAUDE.md.tmpl" "$CLAUDE_MD"

# Cursor has no global rules file — render a copy-paste-ready document
# so the user can drop it into Cursor → Settings → Rules → User Rules.
CURSOR_RULES="$BRAIN_DIR/cursor-user-rules.md"
render "$PROJECT_DIR/templates/cursor/USER_RULES.md.tmpl" "$CURSOR_RULES"

# Identity files: only seed if missing — never overwrite the user's own data.
for name in who-i-am preferences corrections; do
  dst="$BRAIN_DIR/identity/$name.md"
  if [[ ! -f "$dst" ]]; then
    render "$PROJECT_DIR/templates/identity/$name.md.tmpl" "$dst"
    echo "      + seeded identity/$name.md"
  fi
done
echo "      ✓ files rendered"

# ──────────────────────────────────────────────────────────────────────
# 4. Download embedding model (one-time, ~120 MB)
# ──────────────────────────────────────────────────────────────────────
echo "[4/8] download embedding model (one-time, ~120 MB)"
"$PYTHON" - <<'PY'
import os
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
from sentence_transformers import SentenceTransformer
SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
print("      ✓ model cached")
PY

# ──────────────────────────────────────────────────────────────────────
# 5. Register MCP server with Claude Code + Cursor (both idempotent)
# ──────────────────────────────────────────────────────────────────────
echo "[5/8] register MCP servers (read + write split) + SessionStart hooks"
# WS5 (2026-04-23): brain is now two MCP servers — brain-read (safe on
# every host) and brain-write (primary host only). The legacy single
# `brain` server is still registered here for one release cycle so
# existing CLAUDE.md references keep resolving; new agents should
# prefer the split entries. `brain doctor` warns if a host is half-wired.
#
# Writes are env-gated: BRAIN_WRITE=1 → brain-write exposes 9 mutation
# tools; BRAIN_WRITE=0 → server starts with zero tools (still dial-
# testable, but can't mutate). Primary host is the one running
# install.sh; all wiring here sets BRAIN_WRITE=1. Remote hosts should
# register brain-read without the write twin.
if command -v claude >/dev/null 2>&1; then
  for name in brain brain-read brain-write; do
    claude mcp remove "$name" -s user >/dev/null 2>&1 || true
  done
  # 1. legacy aggregate — delete after one release cycle
  claude mcp add brain -s user \
    -e "PYTHONPATH=$PROJECT_DIR/src" \
    -e "BRAIN_DIR=$BRAIN_DIR" \
    -- "$PYTHON" -m brain.mcp_server >/dev/null
  # 2. read-only split — safe everywhere
  claude mcp add brain-read -s user \
    -e "PYTHONPATH=$PROJECT_DIR/src" \
    -e "BRAIN_DIR=$BRAIN_DIR" \
    -- "$PYTHON" -m brain.mcp_server_read >/dev/null
  # 3. write split — this host is primary, flag on
  claude mcp add brain-write -s user \
    -e "PYTHONPATH=$PROJECT_DIR/src" \
    -e "BRAIN_DIR=$BRAIN_DIR" \
    -e "BRAIN_WRITE=1" \
    -- "$PYTHON" -m brain.mcp_server_write >/dev/null
  echo "      ✓ Claude Code registered: brain (legacy) + brain-read + brain-write (BRAIN_DIR=$BRAIN_DIR)"
else
  echo "      - 'claude' CLI not found — Claude Code skipped."
fi

# Cursor has no MCP CLI; merge into ~/.cursor/mcp.json by hand using Python
# (already a hard dep). Preserves any sibling servers, backs up first.
"$PYTHON" - "$PYTHON" "$PROJECT_DIR" "$BRAIN_DIR" <<'PY'
import json, os, shutil, sys, time

py, proj, brain_dir = sys.argv[1], sys.argv[2], sys.argv[3]
home = os.path.expanduser("~")
cursor_dir = os.path.join(home, ".cursor")
cfg = os.path.join(cursor_dir, "mcp.json")

if not os.path.isdir(cursor_dir):
    print("      - ~/.cursor not found — Cursor skipped.")
    sys.exit(0)

data = {}
if os.path.exists(cfg):
    try:
        with open(cfg) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        print(f"      ! {cfg} is not valid JSON — Cursor skipped (fix manually).")
        sys.exit(0)
    shutil.copy2(cfg, f"{cfg}.bak.{time.strftime('%Y%m%d%H%M%S')}")

servers = data.setdefault("mcpServers", {})
# WS5 (2026-04-23): same split pattern as Claude above.
common_env = {
    "PYTHONPATH": os.path.join(proj, "src"),
    "BRAIN_DIR": brain_dir,
}
servers["brain"] = {          # legacy aggregate — delete after one release
    "name": "brain",
    "transport": "stdio",
    "command": py,
    "args": ["-m", "brain.mcp_server"],
    "env": dict(common_env),
}
servers["brain-read"] = {
    "name": "brain-read",
    "transport": "stdio",
    "command": py,
    "args": ["-m", "brain.mcp_server_read"],
    "env": dict(common_env),
}
servers["brain-write"] = {
    "name": "brain-write",
    "transport": "stdio",
    "command": py,
    "args": ["-m", "brain.mcp_server_write"],
    "env": {**common_env, "BRAIN_WRITE": "1"},
}

with open(cfg, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"      ✓ Cursor registered: brain (legacy) + brain-read + brain-write (BRAIN_DIR={brain_dir})")
PY

# Render per-machine hook config files, then delegate the JSON merge
# to brain.install_hooks (testable, used by uninstall.sh too). Quietly
# skips Claude or Cursor if the corresponding app dir is missing.
SETTINGS_RENDERED="$BRAIN_DIR/.claude-settings.brain.json"
HOOKS_RENDERED="$BRAIN_DIR/.cursor-hooks.brain.json"
render "$PROJECT_DIR/templates/claude/settings.json.tmpl" "$SETTINGS_RENDERED"
render "$PROJECT_DIR/templates/cursor/hooks.json.tmpl"   "$HOOKS_RENDERED"
"$PYTHON" -m brain.install_hooks install "$SETTINGS_RENDERED" "$HOOKS_RENDERED"

# ──────────────────────────────────────────────────────────────────────
# 6. Build the search index (so first query is instant)
# ──────────────────────────────────────────────────────────────────────
echo "[6/8] build database + semantic index"
"$PYTHON" -m brain.db rebuild >/dev/null 2>&1 || echo "      ! db rebuild failed (will retry on first ingest)"
"$PYTHON" -m brain.semantic ensure >/dev/null 2>&1 || echo "      ! semantic build failed (will retry on first use)"
echo "      ✓ index ready"

# ──────────────────────────────────────────────────────────────────────
# 7. Install scheduler units (launchd on macOS, systemd --user on Linux)
# ──────────────────────────────────────────────────────────────────────
echo "[7/8] install scheduler ($(uname -s))"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_scheduler.sh"
scheduler_render_and_install \
  "$PROJECT_DIR" "$HOME_DIR" "$USERNAME" "$BRAIN_DIR" "$PYTHON" "$TODAY"
scheduler_verify "$USERNAME"

# ──────────────────────────────────────────────────────────────────────
# 7b. Export BRAIN_DIR into the user's shell rc (idempotent).
#
# The brain CLI and any script run outside Claude Code / Cursor looks up
# BRAIN_DIR from the env. Without this, users have to type `BRAIN_DIR=…
# brain …` forever. We touch zsh/bash/fish only if their rc file exists
# (so we don't create shells the user doesn't use) and we use a marker
# comment so re-runs don't duplicate the line.
# ──────────────────────────────────────────────────────────────────────
append_brain_dir_export() {
  local rc="$1" line="$2"
  [[ -f "$rc" ]] || return 0
  if grep -q "# brain: BRAIN_DIR" "$rc" 2>/dev/null; then
    return 0  # already present
  fi
  printf '\n# brain: BRAIN_DIR (managed by brain install.sh)\n%s\n' "$line" >> "$rc"
  echo "      + appended BRAIN_DIR export to $rc"
}
append_brain_dir_export "$HOME_DIR/.zshrc"  "export BRAIN_DIR=\"$BRAIN_DIR\""
append_brain_dir_export "$HOME_DIR/.bashrc" "export BRAIN_DIR=\"$BRAIN_DIR\""
append_brain_dir_export "$HOME_DIR/.config/fish/config.fish" "set -gx BRAIN_DIR \"$BRAIN_DIR\""

# ──────────────────────────────────────────────────────────────────────
# 8. Doctor — verify everything green
# ──────────────────────────────────────────────────────────────────────
echo "[8/8] doctor"
echo
if "$BRAIN_DIR/bin/doctor.sh"; then
  echo
  echo "── install complete ──────────────────────────────────"
  echo "  Restart Claude Code / Cursor to pick up the brain MCP tools."
  echo "  SessionStart hooks are auto-wired in both — no manual config needed."
  echo
  echo "  Cursor extra step (one-time, optional): paste"
  echo "    $BRAIN_DIR/cursor-user-rules.md"
  echo "  into Cursor → Settings → Rules → User Rules so the agent prefers"
  echo "  brain_recall over guessing. (Cursor still has no global rules file.)"
  echo
  echo "  Run anytime: $BRAIN_DIR/bin/doctor.sh"
  exit 0
else
  echo
  echo "── install finished with warnings ────────────────────"
  echo "  Doctor reported failures above. Re-run after fixing them."
  exit 1
fi
