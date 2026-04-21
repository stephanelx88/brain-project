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
# ──────────────────────────────────────────────────────────────────────
REAL_HOME="$(dscl . -read "/Users/$USERNAME" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
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

OS="$(uname -s)"
if [[ "$OS" != "Darwin" ]]; then
  echo "✗ Only macOS is supported by this installer (detected: $OS)."
  echo "  The Python package itself is portable; only the launchd plist + MCP"
  echo "  registration are macOS-specific. PRs for systemd-user welcome."
  exit 1
fi

# ──────────────────────────────────────────────────────────────────────
# Pick python — prefer pyenv if present, else system python3.11+
# ──────────────────────────────────────────────────────────────────────
PYTHON=""
for candidate in \
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
  echo "✗ No Python ≥ 3.11 found. Install pyenv or python3.11+ first."
  exit 1
fi
PYVER="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"

# ──────────────────────────────────────────────────────────────────────
# Pre-flight: project must NOT live under ~/Desktop, ~/Documents, ~/Downloads
# (macOS TCC denies launchd-spawned processes read access there).
# ──────────────────────────────────────────────────────────────────────
case "$PROJECT_DIR" in
  "$HOME_DIR/Desktop"/*|"$HOME_DIR/Documents"/*|"$HOME_DIR/Downloads"/*)
    echo "✗ Project lives at $PROJECT_DIR, which macOS TCC blocks from launchd."
    echo "  Move it: mv \"$PROJECT_DIR\" \"$HOME_DIR/code/$(basename "$PROJECT_DIR")\""
    echo "  Then re-run this installer."
    exit 1
    ;;
esac

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

PLIST="$HOME_DIR/Library/LaunchAgents/com.${USERNAME}.brain-auto-extract.plist"
mkdir -p "$(dirname "$PLIST")"
render "$PROJECT_DIR/templates/launchd/brain-auto-extract.plist.tmpl" "$PLIST"

# Persistent semantic-embedding worker — keeps sentence-transformers warm
# so each ingest pays ~0.5 s instead of ~10 s cold-start. Optional, but
# the only way to hit the ≤10 s sync goal.
SEM_PLIST="$HOME_DIR/Library/LaunchAgents/com.${USERNAME}.brain-semantic-worker.plist"
render "$PROJECT_DIR/templates/launchd/brain-semantic-worker.plist.tmpl" "$SEM_PLIST"

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
echo "[5/8] register MCP server + SessionStart hooks (Claude Code + Cursor)"
if command -v claude >/dev/null 2>&1; then
  # `claude mcp add` errors if name exists — remove first to be idempotent.
  claude mcp remove brain -s user >/dev/null 2>&1 || true
  claude mcp add brain -s user \
    -e "PYTHONPATH=$PROJECT_DIR/src" \
    -e "BRAIN_DIR=$BRAIN_DIR" \
    -- "$PYTHON" -m brain.mcp_server >/dev/null
  echo "      ✓ Claude Code registered (BRAIN_DIR=$BRAIN_DIR)"
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
servers["brain"] = {
    "name": "brain",
    "transport": "stdio",
    "command": py,
    "args": ["-m", "brain.mcp_server"],
    "env": {
        "PYTHONPATH": os.path.join(proj, "src"),
        "BRAIN_DIR": brain_dir,
    },
}

with open(cfg, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"      ✓ Cursor registered (~/.cursor/mcp.json, BRAIN_DIR={brain_dir})")
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
# 7. Load launchd job (unload first so re-runs pick up plist changes)
# ──────────────────────────────────────────────────────────────────────
echo "[7/8] load launchd watcher + semantic worker"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
launchctl unload "$SEM_PLIST" 2>/dev/null || true
launchctl load "$SEM_PLIST"
# launchctl load is asynchronous — the job may take a beat to appear in
# `launchctl list`. Poll briefly so the success message is honest.
LAUNCHD_LABEL="com.${USERNAME}.brain-auto-extract"
SEM_LABEL="com.${USERNAME}.brain-semantic-worker"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if launchctl list | grep -q "$LAUNCHD_LABEL" \
     && launchctl list | grep -q "$SEM_LABEL"; then break; fi
  sleep 0.3
done
if launchctl list | grep -q "$LAUNCHD_LABEL"; then
  echo "      ✓ watcher live (1 s throttle on $BRAIN_DIR + ~/.claude/projects + ~/.cursor/projects)"
else
  echo "      ✗ launchctl load returned 0 but '$LAUNCHD_LABEL' is not visible."
  echo "        Check $PLIST then run: launchctl bootstrap gui/\$(id -u) $PLIST"
  exit 1
fi
if launchctl list | grep -q "$SEM_LABEL"; then
  echo "      ✓ semantic worker live (cold-start once, warm forever)"
else
  echo "      ! semantic worker not visible — ingest will fall back to in-process embedding"
fi

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
