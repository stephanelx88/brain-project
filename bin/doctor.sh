#!/bin/bash
# brain doctor — single-shot health check.
#
# Catches the silent-failure modes that cost trust:
#   - launchd loaded but every run fails
#   - python imports brain from a stale (non-editable) install
#   - MCP server can't boot
#   - semantic index is missing/stale
#   - notes ledger out of sync with disk (deletions not propagated)
#
# Exit code: 0 if green, 1 if any check failed.
#
# Configuration is loaded from $BRAIN_DIR/.brain.conf (written by install.sh).
# That file holds PYTHON, PROJECT_DIR, USERNAME — all the things that vary
# per machine. Falls back to autodetect so this script also works pre-install.

set -uo pipefail

# Snapshot env BRAIN_DIR BEFORE .brain.conf can override it. Needed so we
# can detect (and warn about) drift between the shell env the user thinks
# they configured and the value silently loaded from the vault's conf file.
_ENV_BRAIN_DIR="${BRAIN_DIR:-}"

BRAIN_DIR="${BRAIN_DIR:-$HOME/.brain}"
CONF="$BRAIN_DIR/.brain.conf"
_CONF_BRAIN_DIR=""
if [[ -f "$CONF" ]]; then
  # Record the conf's view of BRAIN_DIR before sourcing so a drift warning
  # can cite both sides.
  _CONF_BRAIN_DIR=$(sed -n 's/^BRAIN_DIR="\?\([^"]*\)"\?/\1/p' "$CONF" | tail -1)
  # shellcheck disable=SC1090
  source "$CONF"
fi

PYTHON="${PYTHON:-$(command -v python3 || true)}"
USERNAME="${USERNAME:-${USER:-$(whoami)}}"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$(readlink "${BASH_SOURCE[0]}" || echo "${BASH_SOURCE[0]}")")/.." && pwd)}"
LOG="$BRAIN_DIR/logs/auto-extract.log"
PLIST="$HOME/Library/LaunchAgents/com.${USERNAME}.brain-auto-extract.plist"
DB="$BRAIN_DIR/.brain.db"
LOG_TAIL=40

PASS=0
FAIL=0
WARN=0

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$*"; FAIL=$((FAIL+1)); }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; WARN=$((WARN+1)); }
hdr()  { printf "\n\033[1m%s\033[0m\n" "$*"; }

# ──────────────────────────────────────────────────────────────────────
# 0. BRAIN_DIR resolution (ALWAYS first). Every downstream check assumes
# BRAIN_DIR is a real, writable directory; if it isn't, running the rest
# produces misleading errors (e.g. "note not searchable" when the true
# cause is that BRAIN_DIR never existed). We surface provenance + drift
# here so the user sees the root cause immediately.
# ──────────────────────────────────────────────────────────────────────
hdr "0. BRAIN_DIR resolution"
if [[ -n "$_ENV_BRAIN_DIR" && -n "$_CONF_BRAIN_DIR" && "$_ENV_BRAIN_DIR" != "$_CONF_BRAIN_DIR" ]]; then
  warn "env BRAIN_DIR=$_ENV_BRAIN_DIR overridden by $CONF → $_CONF_BRAIN_DIR"
  warn "  if this is unintentional:"
  warn "    – remove the BRAIN_DIR= line from $CONF, or"
  warn "    – unset the env var + remove 'export BRAIN_DIR' from ~/.zshrc ~/.bashrc"
fi
if [[ -n "$_ENV_BRAIN_DIR" ]]; then
  ok "BRAIN_DIR = $BRAIN_DIR (source: env)"
elif [[ -n "$_CONF_BRAIN_DIR" ]]; then
  ok "BRAIN_DIR = $BRAIN_DIR (source: $CONF)"
else
  ok "BRAIN_DIR = $BRAIN_DIR (source: default \$HOME/.brain)"
fi
if [[ ! -d "$BRAIN_DIR" ]]; then
  bad "BRAIN_DIR is not an existing directory"
  warn "  every downstream check would now fail misleadingly; bailing out."
  warn "  fix A (use this path): cd <brain-project repo> && BRAIN_DIR='$BRAIN_DIR' bash bin/install.sh"
  warn "  fix B (reset to default): unset BRAIN_DIR; remove any 'export BRAIN_DIR' from ~/.zshrc ~/.bashrc; re-run install"
  printf "\n\033[1msummary\033[0m\n  %d passed, %d warnings, %d failures\n" "$PASS" "$WARN" "$FAIL"
  exit 1
fi
if [[ ! -w "$BRAIN_DIR" ]]; then
  bad "BRAIN_DIR not writable: $BRAIN_DIR"
  warn "  ingest + auto-extract will both fail silently; bailing out."
  printf "\n\033[1msummary\033[0m\n  %d passed, %d warnings, %d failures\n" "$PASS" "$WARN" "$FAIL"
  exit 1
fi

hdr "1. python + package install"
if [[ -x "$PYTHON" ]]; then
  ok "python found: $PYTHON"
else
  bad "python missing: $PYTHON"
fi

PIP_INFO=$("$PYTHON" -m pip show brain 2>/dev/null)
if [[ -z "$PIP_INFO" ]]; then
  bad "brain package not installed in $PYTHON"
elif echo "$PIP_INFO" | grep -q "Editable project location"; then
  ok "brain installed editable ($(echo "$PIP_INFO" | awk -F': ' '/Editable/ {print $2}'))"
else
  warn "brain installed NON-editable — code edits in $PROJECT_DIR won't take effect until reinstall"
  warn "  fix: cd $PROJECT_DIR && $PYTHON -m pip install -e ."
fi

if "$PYTHON" -c "import brain, brain.mcp_server, brain.ingest_notes, brain.semantic" 2>/dev/null; then
  ok "all brain submodules importable"
else
  bad "brain submodule import failed"
fi

DOCTOR_OS="$(uname -s)"
hdr "2. scheduler ($DOCTOR_OS)"
case "$DOCTOR_OS" in
  Darwin)
    # Snapshot once to avoid `launchctl list | grep -q` which trips SIGPIPE
    # under `set -o pipefail` (launchctl gets killed before its output
    # drains, pipeline exit=141, `if` branch treats it as false even when
    # the pattern matched). Observed on darwin 24 with long job lists.
    LAUNCHCTL_LIST=$(launchctl list 2>/dev/null || true)
    if echo "$LAUNCHCTL_LIST" | grep -q "com\.${USERNAME}\.brain-auto-extract"; then
      ok "auto-extract launchd job loaded"
    else
      bad "auto-extract launchd job not loaded"
      warn "  fix: launchctl load $PLIST"
    fi
    if echo "$LAUNCHCTL_LIST" | grep -q "com\.${USERNAME}\.brain-semantic-worker"; then
      ok "semantic-worker launchd job loaded"
    else
      warn "semantic-worker not loaded — ingest will cold-start the model each run"
    fi
    ;;
  Linux)
    # systemctl --user returns nonzero for inactive units and we don't
    # want `set -e` (if ever enabled) to trip — keep the `|| true` guard.
    if systemctl --user is-active brain-auto-extract.timer >/dev/null 2>&1; then
      ok "auto-extract systemd timer active"
    else
      bad "brain-auto-extract.timer not active"
      warn "  fix: systemctl --user enable --now brain-auto-extract.timer"
      warn "  (headless? run 'loginctl enable-linger $USERNAME' first)"
    fi
    if systemctl --user is-active brain-semantic-worker.service >/dev/null 2>&1; then
      ok "semantic-worker systemd service active"
    else
      warn "semantic-worker not active — ingest will cold-start the model each run"
    fi
    ;;
  *)
    warn "scheduler checks skipped — platform $DOCTOR_OS not supported"
    ;;
esac

if [[ -f "$LOG" ]]; then
  LAST_RUN=$(grep "auto-extract run" "$LOG" | tail -1 | awk '{print $2}')
  if [[ -z "$LAST_RUN" ]]; then
    warn "no launchd runs ever logged"
  else
    ok "last run: $LAST_RUN"
  fi
  RECENT_ERRORS=$(tail -"$LOG_TAIL" "$LOG" | grep -cE "(Error|exited [1-9]|ModuleNotFoundError)")
  RECENT_ERRORS=${RECENT_ERRORS//[^0-9]/}
  if [[ "${RECENT_ERRORS:-0}" -gt 0 ]]; then
    bad "$RECENT_ERRORS errors in last $LOG_TAIL log lines — check $LOG"
    tail -"$LOG_TAIL" "$LOG" | grep -E "(Error|exited [1-9]|ModuleNotFoundError)" | head -3 | sed 's/^/      /'
  else
    ok "no errors in last $LOG_TAIL log lines"
  fi
else
  warn "log file does not exist yet: $LOG"
fi

hdr "3. mcp server"
MCP_OUT=$("$PYTHON" - <<'PY' 2>&1
import asyncio, sys
async def main():
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp import ClientSession
    p = StdioServerParameters(command=sys.executable, args=['-m','brain.mcp_server'],
        env={'BRAIN_WARMUP':'0'})
    async with stdio_client(p) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print(f"OK {len(tools.tools)}")
try:
    asyncio.run(asyncio.wait_for(main(), timeout=15))
except Exception as e:
    print(f"FAIL: {e}")
PY
)
if echo "$MCP_OUT" | grep -q "^OK "; then
  COUNT=$(echo "$MCP_OUT" | grep "^OK " | awk '{print $2}')
  ok "MCP server boots, $COUNT tools registered"
else
  bad "MCP server failed to boot"
  echo "$MCP_OUT" | tail -3 | sed 's/^/      /'
fi

if [[ -f "$HOME/.claude.json" ]]; then
  CLAUDE_HAS_READ=0
  CLAUDE_HAS_WRITE=0
  CLAUDE_HAS_LEGACY=0
  grep -q '"brain-read"'  "$HOME/.claude.json" && CLAUDE_HAS_READ=1
  grep -q '"brain-write"' "$HOME/.claude.json" && CLAUDE_HAS_WRITE=1
  grep -q '"brain"'       "$HOME/.claude.json" && CLAUDE_HAS_LEGACY=1
  if [[ $CLAUDE_HAS_READ -eq 1 && $CLAUDE_HAS_WRITE -eq 1 ]]; then
    ok "brain-read + brain-write registered in Claude Code (WS5 split)"
  elif [[ $CLAUDE_HAS_READ -eq 1 && $CLAUDE_HAS_WRITE -eq 0 ]]; then
    warn "brain-read registered but brain-write missing — this host can recall but not mutate"
    warn "  if this is the primary host, rewire via: cd $PROJECT_DIR && bash bin/install.sh"
    warn "  if this is intentional (untrusted host), you can ignore this."
  elif [[ $CLAUDE_HAS_READ -eq 0 && $CLAUDE_HAS_WRITE -eq 1 ]]; then
    bad "brain-write registered WITHOUT brain-read — anomaly (write without query)"
    warn "  fix: cd $PROJECT_DIR && bash bin/install.sh"
  elif [[ $CLAUDE_HAS_LEGACY -eq 1 ]]; then
    warn "legacy 'brain' server registered; WS5 split not wired on this host"
    warn "  fix: cd $PROJECT_DIR && bash bin/install.sh  (will register brain-read + brain-write)"
  else
    warn "brain NOT registered in Claude Code"
    warn "  fix: cd $PROJECT_DIR && bash bin/install.sh"
  fi
fi

# SessionStart hooks: both files are JSON-merged by install.sh, so a
# missing brain entry means the install ran before hook support shipped
# OR the user nuked it. Either way doctor flags it; uninstall sets the
# expected state to "absent" so we don't false-positive after teardown.
SETTINGS="$HOME/.claude/settings.json"
if [[ -f "$SETTINGS" ]] && grep -q "brain.audit" "$SETTINGS"; then
  ok "Claude SessionStart hook wired (audit + harvest)"
elif [[ -d "$HOME/.claude" ]]; then
  warn "Claude SessionStart hook NOT wired in $SETTINGS"
  warn "  fix: cd $PROJECT_DIR && bash bin/install.sh"
fi

CURSOR_HOOKS="$HOME/.cursor/hooks.json"
if [[ -f "$CURSOR_HOOKS" ]] && grep -q "cursor-session-start.sh" "$CURSOR_HOOKS"; then
  ok "Cursor sessionStart hook wired (audit + harvest)"
  if [[ ! -x "$BRAIN_DIR/bin/cursor-session-start.sh" ]]; then
    bad "  but $BRAIN_DIR/bin/cursor-session-start.sh is missing or not executable"
    warn "  fix: cd $PROJECT_DIR && bash bin/install.sh  (re-renders + chmods the hook)"
  fi
elif [[ -d "$HOME/.cursor" ]]; then
  warn "Cursor sessionStart hook NOT wired in $CURSOR_HOOKS"
  warn "  fix: cd $PROJECT_DIR && bash bin/install.sh"
fi

# Cursor user rules: stored opaquely in app settings, so we can't verify
# the user actually pasted them. Best-effort: warn if the rendered file is
# newer than the template (template just got an edit + re-render but
# user hasn't re-pasted yet) OR newer than a 7-day cutoff (gentle nudge).
CURSOR_RULES_RENDERED="$BRAIN_DIR/cursor-user-rules.md"
CURSOR_RULES_TMPL="$PROJECT_DIR/templates/cursor/USER_RULES.md.tmpl"
if [[ -d "$HOME/.cursor" && -f "$CURSOR_RULES_RENDERED" && -f "$CURSOR_RULES_TMPL" ]]; then
  if [[ "$CURSOR_RULES_TMPL" -nt "$CURSOR_RULES_RENDERED" ]]; then
    warn "Cursor user rules template updated since last render — re-run install.sh"
  else
    # Cursor stores user rules in opaque app state; we can only remind.
    ok "Cursor user rules rendered ($CURSOR_RULES_RENDERED)"
    warn "  reminder: paste into Cursor → Settings → Rules → User Rules if not done"
    # Clipboard copy command differs by platform.
    case "$DOCTOR_OS" in
      Darwin) warn "  copy: pbcopy < $CURSOR_RULES_RENDERED" ;;
      Linux)  warn "  copy: xclip -selection clipboard < $CURSOR_RULES_RENDERED  (or wl-copy)" ;;
    esac
  fi
fi

hdr "4. data integrity"
if [[ -f "$DB" ]]; then
  ROWS=$("$PYTHON" -c "import sqlite3; c=sqlite3.connect('$DB'); print(c.execute('SELECT COUNT(*) FROM entities').fetchone()[0], c.execute('SELECT COUNT(*) FROM facts').fetchone()[0], c.execute('SELECT COUNT(*) FROM notes').fetchone()[0])" 2>/dev/null)
  if [[ -n "$ROWS" ]]; then
    read -r ENTS FACTS NOTES <<<"$ROWS"
    ok "db: $ENTS entities, $FACTS facts, $NOTES notes"
  else
    bad "db unreadable: $DB"
  fi
else
  bad "db missing: $DB"
fi

VEC="$BRAIN_DIR/.vec/meta.json"
if [[ -f "$VEC" ]]; then
  ok "semantic index present"
  # Stale-embedding ratio check (incident 2026-04-23): meta.fact_hash
  # vs db.facts.text. stale = text mutated on disk after embed; orphan
  # = rowid missing from DB. Warn at >1%, bad at >5% — tuned so a
  # normal retract cycle doesn't noise-page the user.
  STALE_JSON=$("$PYTHON" -c "
import json, sys
sys.path.insert(0, '$PROJECT_DIR/src')
from brain import semantic
print(json.dumps(semantic.count_stale_fact_meta()))
" 2>/dev/null)
  if [[ -n "$STALE_JSON" ]]; then
    STALE_RATIO=$(echo "$STALE_JSON" | "$PYTHON" -c "import json, sys; d=json.load(sys.stdin); print(f\"{d['ratio']:.3f} {d['stale']} {d['orphan']} {d['total']}\")")
    read -r RATIO STALE ORPHAN TOTAL <<<"$STALE_RATIO"
    if [[ "$TOTAL" == "0" ]]; then
      :  # empty vec; skip
    elif awk "BEGIN{exit !($RATIO > 0.05)}"; then
      bad ".vec stale-embedding ratio $RATIO ($STALE stale + $ORPHAN orphan / $TOTAL) > 5%"
      warn "  fix: $PYTHON -m brain.semantic build  (full re-embed)"
    elif awk "BEGIN{exit !($RATIO > 0.01)}"; then
      warn ".vec stale-embedding ratio $RATIO ($STALE stale + $ORPHAN orphan / $TOTAL) — guard is catching these"
      warn "  refresh when convenient: $PYTHON -m brain.semantic build"
    else
      ok ".vec freshness ratio $RATIO ($STALE stale + $ORPHAN orphan / $TOTAL)"
    fi
  fi
else
  warn "semantic index missing — first brain_recall will be slow"
  warn "  fix: $PYTHON -m brain.semantic build"
fi

hdr "5. audit ledger (WS5 hash chain)"
LEDGER="$BRAIN_DIR/.audit/ledger.jsonl"
if [[ ! -f "$LEDGER" ]]; then
  ok "audit ledger empty (no write tools called yet)"
else
  LEDGER_OUT=$("$PYTHON" -m brain._audit_ledger validate 2>&1)
  if echo "$LEDGER_OUT" | grep -q "^ok=True"; then
    ROWS=$(echo "$LEDGER_OUT" | sed -n 's/.*rows=\([0-9]*\).*/\1/p')
    ok "audit chain validates ($ROWS rows, head at $LEDGER)"
  else
    bad "audit chain BROKEN: $LEDGER_OUT"
    warn "  fix: inspect $LEDGER around the first_bad row; do NOT try to 'repair' — truncation is a last resort"
  fi
fi

hdr "6. ingest round-trip (write → wait → verify → cleanup)"
TESTFILE="$BRAIN_DIR/doctor-roundtrip-$$.md"
TESTQUERY="DOCTORTOKEN$$"
# Attribute the actual failure: if the write itself dies, previous doctor
# versions let bash emit a cryptic "No such file or directory" and then
# reported "note written but not searchable" — false. Check write first.
if ! echo "$TESTQUERY" > "$TESTFILE" 2>/dev/null; then
  bad "cannot write test note to $TESTFILE"
  warn "  BRAIN_DIR may be read-only or missing — see step 0"
else
  sleep 1
  "$PYTHON" -m brain.ingest_notes >/dev/null 2>&1
  HIT=$("$PYTHON" -m brain.db notes "$TESTQUERY" 2>/dev/null | grep -c "$TESTQUERY")
  HIT=${HIT//[^0-9]/}
  rm -f "$TESTFILE"
  "$PYTHON" -m brain.ingest_notes >/dev/null 2>&1
  if [[ "${HIT:-0}" -gt 0 ]]; then
    ok "wrote, ingested, and recalled a test note end-to-end"
  else
    bad "ingest round-trip failed — note written but not searchable"
  fi
fi

hdr "summary"
printf "  %d passed, %d warnings, %d failures\n" "$PASS" "$WARN" "$FAIL"
exit $(( FAIL > 0 ? 1 : 0 ))
