#!/bin/bash
# Runs brain auto-extract + reconcile. Invoked by launchd every N minutes.
# Guards against running inside an active Claude Code session via BRAIN_EXTRACTING.

set -euo pipefail

# Override BRAIN_SRC if your checkout is elsewhere.
BRAIN_SRC="${BRAIN_SRC:-$HOME/brain-project/src}"
export PYTHONPATH="$BRAIN_SRC"

LOG_DIR="$HOME/.brain/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/auto-extract.log"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) auto-extract run ==="
  python3 -m brain.auto_extract || echo "auto_extract exited $?"
  python3 -m brain.reconcile || echo "reconcile exited $?"
  python3 -m brain.clean --execute || echo "clean exited $?"
  echo ""
} >> "$LOG" 2>&1
