#!/usr/bin/env bash
# brain uninstall — symmetric teardown of bin/install.sh.
#
# Removes:
#   - launchd job + plist
#   - Claude Code MCP registration
#   - Claude SessionStart hook entry in ~/.claude/settings.json
#   - Cursor MCP registration (brain entry in ~/.cursor/mcp.json)
#   - Cursor sessionStart hook entry in ~/.cursor/hooks.json
#   - generated scripts in ~/.brain/bin/
#   - generated CLAUDE.md (restoring backup if one exists)
#   - generated cursor-user-rules.md
#   - editable pip install
#
# Preserves (your data, on purpose):
#   - ~/.brain/         — entire vault
#   - ~/.brain/.git     — version history
#   - ~/.brain/identity — your personal identity files
#
# Pass --purge to also delete ~/.brain (asks confirmation).

set -uo pipefail

BRAIN_DIR="$HOME/.brain"
CONF="$BRAIN_DIR/.brain.conf"
USERNAME="${USER:-$(whoami)}"
PYTHON=""
PROJECT_DIR=""

if [[ -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  source "$CONF"
fi
PYTHON="${PYTHON:-$(command -v python3 || true)}"

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

echo "── brain uninstall ───────────────────────────────────"
echo "  USER       : $USERNAME"
echo "  PROJECT    : ${PROJECT_DIR:-<unknown>}"
echo "  PYTHON     : ${PYTHON:-<unknown>}"
echo "  PURGE VAULT: $PURGE"
echo

PLIST="$HOME/Library/LaunchAgents/com.${USERNAME}.brain-auto-extract.plist"
SEM_PLIST="$HOME/Library/LaunchAgents/com.${USERNAME}.brain-semantic-worker.plist"
AR_PLIST="$HOME/Library/LaunchAgents/com.${USERNAME}.brain-autoresearch.plist"

echo "[1/5] launchd"
for label_plist in \
    "com.${USERNAME}.brain-auto-extract:$PLIST" \
    "com.${USERNAME}.brain-semantic-worker:$SEM_PLIST" \
    "com.${USERNAME}.brain-autoresearch:$AR_PLIST"; do
  label="${label_plist%%:*}"
  plist="${label_plist#*:}"
  if launchctl list 2>/dev/null | grep -q "$label"; then
    launchctl unload "$plist" 2>/dev/null || true
    echo "      ✓ unloaded $label"
  fi
  if [[ -f "$plist" ]]; then
    rm -f "$plist"
    echo "      ✓ removed $plist"
  fi
done

echo "[2/5] MCP registrations + SessionStart hooks"
if command -v claude >/dev/null 2>&1; then
  claude mcp remove brain -s user >/dev/null 2>&1 || true
  echo "      ✓ Claude Code MCP deregistered"
else
  echo "      - claude CLI not installed; skip"
fi

if [[ -n "$PYTHON" && -x "$PYTHON" ]]; then
  # Cursor MCP registration is independent of hooks — drop it inline
  # (uses the same JSON tools as brain.install_hooks; kept here so this
  # script still works after `pip uninstall brain` runs in step [4/5]).
  "$PYTHON" - <<'PY'
import json, os, shutil, sys, time

cfg = os.path.expanduser("~/.cursor/mcp.json")
if not os.path.exists(cfg):
    sys.exit(0)
try:
    with open(cfg) as f:
        data = json.load(f)
except json.JSONDecodeError:
    print("      ! ~/.cursor/mcp.json not valid JSON; skip")
    sys.exit(0)
servers = data.get("mcpServers", {})
if "brain" in servers:
    shutil.copy2(cfg, f"{cfg}.bak.{time.strftime('%Y%m%d%H%M%S')}")
    del servers["brain"]
    with open(cfg, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"      ✓ Cursor MCP deregistered ({cfg})")
PY

  # SessionStart hooks — drop brain entries from both Claude + Cursor
  # using the same module that wired them. Safe even if brain itself is
  # already pip-uninstalled (PYTHONPATH points at the source tree).
  if [[ -d "${PROJECT_DIR:-}/src/brain" ]]; then
    PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$PYTHON" -m brain.install_hooks remove
  else
    "$PYTHON" -m brain.install_hooks remove 2>/dev/null || \
      echo "      - brain module unavailable; SessionStart hooks left in place (edit ~/.claude/settings.json + ~/.cursor/hooks.json by hand)"
  fi
fi

echo "[3/5] generated files"
rm -f "$BRAIN_DIR/bin/auto-extract.sh" \
      "$BRAIN_DIR/bin/autoresearch-tick.sh" \
      "$BRAIN_DIR/bin/cursor-session-start.sh" \
      "$BRAIN_DIR/bin/doctor.sh" \
      "$BRAIN_DIR/.brain.conf" \
      "$BRAIN_DIR/.claude-settings.brain.json" \
      "$BRAIN_DIR/.cursor-hooks.brain.json" \
      "$BRAIN_DIR/cursor-user-rules.md"
echo "      ✓ removed scripts + conf"

CLAUDE_MD="$HOME/.claude/CLAUDE.md"
if [[ -f "$CLAUDE_MD" ]] && grep -q "Personal Brain — Mandatory Use" "$CLAUDE_MD" 2>/dev/null; then
  rm -f "$CLAUDE_MD"
  if [[ -f "$CLAUDE_MD.pre-brain.bak" ]]; then
    mv "$CLAUDE_MD.pre-brain.bak" "$CLAUDE_MD"
    echo "      ✓ CLAUDE.md restored from backup"
  else
    echo "      ✓ CLAUDE.md removed (no backup found)"
  fi
fi

echo "[4/5] pip uninstall"
if [[ -n "$PYTHON" && -x "$PYTHON" ]]; then
  "$PYTHON" -m pip uninstall -y brain >/dev/null 2>&1 && echo "      ✓ brain package uninstalled" || echo "      - brain not installed"
fi

echo "[5/5] vault"
if (( PURGE == 1 )); then
  read -r -p "  Really delete $BRAIN_DIR (your entire knowledge vault)? Type 'yes' to confirm: " ans
  if [[ "$ans" == "yes" ]]; then
    rm -rf "$BRAIN_DIR"
    echo "      ✓ $BRAIN_DIR deleted"
  else
    echo "      - kept $BRAIN_DIR"
  fi
else
  echo "      - kept $BRAIN_DIR (use --purge to delete)"
fi

echo
echo "── uninstall complete ────────────────────────────────"
