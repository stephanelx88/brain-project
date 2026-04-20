#!/usr/bin/env bash
# brain uninstall — symmetric teardown of bin/install.sh.
#
# Removes:
#   - launchd job + plist
#   - Claude Code MCP registration
#   - Cursor MCP registration (brain entry in ~/.cursor/mcp.json)
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

echo "[1/5] launchd"
if launchctl list 2>/dev/null | grep -q "com\.${USERNAME}\.brain-auto-extract"; then
  launchctl unload "$PLIST" 2>/dev/null || true
  echo "      ✓ unloaded"
fi
if [[ -f "$PLIST" ]]; then
  rm -f "$PLIST"
  echo "      ✓ removed $PLIST"
fi

echo "[2/5] MCP registrations"
if command -v claude >/dev/null 2>&1; then
  claude mcp remove brain -s user >/dev/null 2>&1 || true
  echo "      ✓ Claude Code deregistered"
else
  echo "      - claude CLI not installed; skip"
fi

if [[ -n "$PYTHON" && -x "$PYTHON" ]]; then
  "$PYTHON" - <<'PY'
import json, os, sys

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
if "brain" not in servers:
    sys.exit(0)
del servers["brain"]
with open(cfg, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print("      ✓ Cursor deregistered (~/.cursor/mcp.json)")
PY
fi

echo "[3/5] generated files"
rm -f "$BRAIN_DIR/bin/auto-extract.sh" \
      "$BRAIN_DIR/bin/doctor.sh" \
      "$BRAIN_DIR/.brain.conf" \
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
