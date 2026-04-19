#!/bin/bash
# Brain onboarding — one-shot setup.
#
# Usage:
#   ./scripts/onboard.sh                          # interactive prompt
#   ./scripts/onboard.sh /path/to/your/vault      # non-interactive
#
# What it does (all idempotent — safe to re-run):
#   1. Resolves your vault path (arg or prompt)
#   2. Creates the folder if missing, initializes git if needed
#   3. Scaffolds identity/ files (only if missing)
#   4. Persists BRAIN_DIR export in your shell rc file
#   5. Wires the SessionStart hook into ~/.claude/settings.json
#   6. Runs a test harvest + extract so you can see it work

set -euo pipefail

# ---- pretty output ----
if [ -t 1 ]; then
  BOLD='\033[1m'; DIM='\033[2m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; RESET='\033[0m'
else
  BOLD=''; DIM=''; GREEN=''; YELLOW=''; RED=''; RESET=''
fi

say()   { printf "${BOLD}==>${RESET} %s\n" "$*"; }
ok()    { printf "${GREEN}✓${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}!${RESET} %s\n" "$*"; }
die()   { printf "${RED}✗${RESET} %s\n" "$*" >&2; exit 1; }

# ---- locate this repo ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$REPO_DIR/src"

[ -d "$SRC_DIR/brain" ] || die "Could not find $SRC_DIR/brain — is the repo intact?"

# ---- 1. resolve vault path ----
VAULT="${1:-}"
if [ -z "$VAULT" ]; then
  printf "${BOLD}Brain vault path${RESET} (where your memory will live, e.g. an Obsidian vault):\n> "
  read -r VAULT
fi

# expand ~ and env vars
VAULT="${VAULT/#\~/$HOME}"
VAULT="$(eval echo "$VAULT")"

[ -n "$VAULT" ] || die "No vault path provided."

say "Using vault: $VAULT"

# ---- 2. create + git init ----
mkdir -p "$VAULT"
VAULT="$(cd "$VAULT" && pwd)"   # absolute path

if [ ! -d "$VAULT/.git" ]; then
  (cd "$VAULT" && git init -q)
  ok "Initialized git repo at $VAULT"
else
  ok "Vault already a git repo"
fi

# ---- 3. pick a role + scaffold identity files ----
mkdir -p "$VAULT/identity"

ROLES=(
  "Software Engineer"
  "Product Manager"
  "Designer"
  "Data Scientist"
  "Researcher"
  "Founder / CEO"
  "Student"
  "Other"
)

if [ ! -f "$VAULT/identity/who-i-am.md" ]; then
  echo ""
  printf "${BOLD}What's your role?${RESET}\n"
  i=1
  for r in "${ROLES[@]}"; do
    printf "  %d) %s\n" "$i" "$r"
    i=$((i+1))
  done
  printf "> "
  read -r CHOICE

  case "$CHOICE" in
    ''|*[!0-9]*) ROLE="Software Engineer" ;;
    *)
      if [ "$CHOICE" -ge 1 ] && [ "$CHOICE" -le "${#ROLES[@]}" ]; then
        ROLE="${ROLES[$((CHOICE-1))]}"
      else
        ROLE="Software Engineer"
      fi
      ;;
  esac

  if [ "$ROLE" = "Other" ]; then
    printf "Type your role: "
    read -r CUSTOM_ROLE
    [ -n "$CUSTOM_ROLE" ] && ROLE="$CUSTOM_ROLE"
  fi

  cat > "$VAULT/identity/who-i-am.md" <<EOF
---
type: identity
---

# Who I Am

- Role: $ROLE
EOF
  ok "Created identity/who-i-am.md (Role: $ROLE)"
else
  ok "identity/who-i-am.md already exists"
fi

if [ ! -f "$VAULT/identity/preferences.md" ]; then
  cat > "$VAULT/identity/preferences.md" <<'EOF'
---
type: identity
---

# Preferences

- Communication style:
- Coding style:
- Tools:
EOF
  ok "Created identity/preferences.md"
fi

if [ ! -f "$VAULT/identity/corrections.md" ]; then
  cat > "$VAULT/identity/corrections.md" <<'EOF'
---
type: identity
---

# Corrections

Things I've corrected before — don't repeat these mistakes.
EOF
  ok "Created identity/corrections.md"
fi

# ---- 4. persist BRAIN_DIR in shell rc ----
SHELL_NAME="$(basename "${SHELL:-/bin/zsh}")"
case "$SHELL_NAME" in
  zsh)  RC="$HOME/.zshrc" ;;
  bash) RC="$HOME/.bashrc" ;;
  *)    RC="$HOME/.profile" ;;
esac

EXPORT_LINE="export BRAIN_DIR=\"$VAULT\""
MARKER="# >>> brain vault >>>"

if [ -f "$RC" ] && grep -q "$MARKER" "$RC"; then
  # replace existing block
  python3 - "$RC" "$MARKER" "$EXPORT_LINE" <<'PY'
import sys, re, pathlib
rc, marker, line = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(rc)
text = p.read_text()
end = "# <<< brain vault <<<"
block = f"{marker}\n{line}\n{end}\n"
text = re.sub(rf"{re.escape(marker)}.*?{re.escape(end)}\n", block, text, flags=re.S)
p.write_text(text)
PY
  ok "Updated BRAIN_DIR export in $RC"
else
  {
    echo ""
    echo "$MARKER"
    echo "$EXPORT_LINE"
    echo "# <<< brain vault <<<"
  } >> "$RC"
  ok "Added BRAIN_DIR export to $RC"
fi

# make it active for the rest of this script
export BRAIN_DIR="$VAULT"

# ---- 5. wire SessionStart hook into ~/.claude/settings.json ----
CLAUDE_DIR="$HOME/.claude"
SETTINGS="$CLAUDE_DIR/settings.json"
mkdir -p "$CLAUDE_DIR"

HOOK_CMD="cd $SRC_DIR && BRAIN_DIR=\"$VAULT\" python3 -m brain.harvest_session && BRAIN_DIR=\"$VAULT\" python3 -m brain.auto_extract"

python3 - "$SETTINGS" "$HOOK_CMD" <<'PY'
import json, sys, pathlib, datetime, shutil

path = pathlib.Path(sys.argv[1])
cmd = sys.argv[2]

if path.exists():
    backup = path.with_suffix(f".json.bak.{datetime.datetime.now():%Y%m%d%H%M%S}")
    shutil.copy(path, backup)
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        print(f"  (existing settings.json is invalid JSON; backed up to {backup})")
        data = {}
else:
    data = {}

data.setdefault("hooks", {})
hooks_list = data["hooks"].setdefault("SessionStart", [])

# remove any prior brain hook (look for our marker)
def is_brain_hook(group):
    for h in group.get("hooks", []):
        if "brain.harvest_session" in h.get("command", "") or "brain.auto_extract" in h.get("command", ""):
            return True
    return False

hooks_list[:] = [g for g in hooks_list if not is_brain_hook(g)]

hooks_list.append({
    "hooks": [
        {
            "type": "command",
            "command": cmd,
            "timeout": 60000,
        }
    ]
})

path.write_text(json.dumps(data, indent=2) + "\n")
print(f"  Wrote {path}")
PY
ok "Wired SessionStart hook into $SETTINGS"

# ---- 6. preflight checks (before we try to run anything) ----
say "Preflight checks..."

PREFLIGHT_FAIL=0

# Python 3.11+
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
  ok "python3 $(python3 -c 'import sys; print(".".join(map(str,sys.version_info[:3])))')"
else
  warn "python3 is older than 3.11 — required by pyproject.toml"
  PREFLIGHT_FAIL=1
fi

# claude CLI (needed by auto_extract)
if command -v claude >/dev/null 2>&1; then
  ok "claude CLI found at $(command -v claude)"
else
  warn "claude CLI not found on PATH — auto_extract will fail until it's installed"
  PREFLIGHT_FAIL=1
fi

# vault writable
if touch "$VAULT/.brain_write_test" 2>/dev/null; then
  rm "$VAULT/.brain_write_test"
  ok "Vault is writable"
else
  warn "Vault is NOT writable: $VAULT"
  PREFLIGHT_FAIL=1
fi

# BRAIN_DIR resolves correctly inside the python module
RESOLVED=$(cd "$SRC_DIR" && BRAIN_DIR="$VAULT" python3 -c 'from brain.config import BRAIN_DIR; print(BRAIN_DIR)')
if [ "$RESOLVED" = "$VAULT" ]; then
  ok "brain.config resolves BRAIN_DIR correctly"
else
  warn "brain.config resolved $RESOLVED (expected $VAULT)"
  PREFLIGHT_FAIL=1
fi

# settings.json is valid JSON
if python3 -c "import json,sys; json.load(open('$SETTINGS'))" 2>/dev/null; then
  ok "$SETTINGS is valid JSON"
else
  warn "$SETTINGS is NOT valid JSON"
  PREFLIGHT_FAIL=1
fi

# ---- 7. ask before running the live test ----
echo ""
if [ "$PREFLIGHT_FAIL" -eq 0 ]; then
  printf "${BOLD}Run a live test (harvest + extract using your real Claude sessions)? [Y/n]${RESET} "
else
  printf "${YELLOW}Some checks failed above. Run live test anyway? [y/N]${RESET} "
fi

if [ -t 0 ]; then
  read -r RUN_TEST
else
  RUN_TEST=""   # non-interactive: skip
fi

case "${RUN_TEST:-}" in
  ""|y|Y|yes|YES)
    if [ "$PREFLIGHT_FAIL" -ne 0 ] && [ -z "${RUN_TEST:-}" ]; then
      warn "Skipping live test (preflight had failures and no input given)"
    else
      say "Running test harvest + extract..."
      RAW_BEFORE=$(find "$VAULT/raw" -type f 2>/dev/null | wc -l | tr -d ' ')
      ENT_BEFORE=$(find "$VAULT/entities" -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')

      (
        cd "$SRC_DIR"
        python3 -m brain.harvest_session || warn "harvest_session exited non-zero"
        python3 -m brain.auto_extract    || warn "auto_extract exited non-zero"
      )

      RAW_AFTER=$(find "$VAULT/raw" -type f 2>/dev/null | wc -l | tr -d ' ')
      ENT_AFTER=$(find "$VAULT/entities" -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')

      echo ""
      say "Result:"
      echo "  raw/      $RAW_BEFORE → $RAW_AFTER files"
      echo "  entities/ $ENT_BEFORE → $ENT_AFTER files"
      if [ "$ENT_AFTER" -gt "$ENT_BEFORE" ] || [ "$RAW_AFTER" -gt "$RAW_BEFORE" ]; then
        ok "Pipeline produced new content — extraction is working"
      elif [ "$ENT_AFTER" -gt 0 ]; then
        ok "Vault already has $ENT_AFTER entity files (nothing new to extract right now)"
      else
        warn "No new content produced. Either no past sessions exist, or extraction failed."
      fi
    fi
    ;;
  *)
    warn "Skipped live test"
    ;;
esac

# ---- summary ----
echo ""
if [ "$PREFLIGHT_FAIL" -eq 0 ]; then
  say "${GREEN}${BOLD}Setup complete.${RESET}"
else
  say "${YELLOW}${BOLD}Setup finished, but some checks failed — review warnings above.${RESET}"
fi
echo ""
echo "  Vault:    $VAULT"
echo "  Shell rc: $RC      ${DIM}(BRAIN_DIR exported)${RESET}"
echo "  Hook:     $SETTINGS"
echo ""
echo "Next:"
echo "  1. Open a new terminal (or 'source $RC') so BRAIN_DIR is loaded"
echo "  2. Start Claude Code — the brain will run automatically"
echo ""
echo "Manual file ingest:"
echo "  cd $SRC_DIR && python3 -m brain.ingest /path/to/file.md"
