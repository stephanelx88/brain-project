#!/usr/bin/env bash
# brain template renderer — shared by install.sh and tests.
#
# Exposes two functions once sourced:
#   expand_includes <templates_dir>          — awk pass, stdin → stdout,
#                                              expands {{include: rel}}
#                                              directives (single pass,
#                                              no nesting).
#   render_template <templates_dir> <src> <dst> <home> <username> \
#                   <project_dir> <python> <brain_dir> <today>
#     — runs expand_includes + the sed token substitution used across
#       every templated file.
#
# Called as a script (argv mode), behaves as a tiny CLI so tests can
# invoke it directly without sourcing:
#   _render.sh expand <templates_dir>          # stdin/stdout
#   _render.sh render <templates_dir> <src> <dst> \
#              <home> <username> <project_dir> <python> <brain_dir> <today>
#
# Chosen shape: Option A in refactor/shared-rule-partials — install.sh's
# existing `render()` was a 6-line sed pipeline; adding an awk pre-pass
# keeps everything in shell and ~40 LOC. Python renderer (Option B) was
# rejected as unnecessary bulk given install.sh already owns render.

set -euo pipefail

expand_includes() {
  # Reads template on stdin, emits include-expanded Markdown on stdout.
  # On missing partial, writes a helpful error to stderr and exits 2.
  # Only whole-line directives are expanded (inline `{{include:…}}` in
  # prose is left untouched — the regex anchors at ^\s* and $).
  local templates_dir="$1"
  awk -v tdir="$templates_dir" '
    /^[[:space:]]*\{\{include:[[:space:]]*[^}]+\}\}[[:space:]]*$/ {
      match($0, /\{\{include:[[:space:]]*[^}]+\}\}/)
      directive = substr($0, RSTART, RLENGTH)
      sub(/^\{\{include:[[:space:]]*/, "", directive)
      sub(/[[:space:]]*\}\}$/, "", directive)
      path = tdir "/" directive
      if ((getline line < path) < 0) {
        printf("✗ render error: missing partial %s (referenced by {{include: %s}})\n", path, directive) > "/dev/stderr"
        exit 2
      }
      print line
      while ((getline line < path) > 0) print line
      close(path)
      next
    }
    { print }
  '
}

render_template() {
  local templates_dir="$1" src="$2" dst="$3"
  local home_dir="$4" username="$5" project_dir="$6"
  local python="$7" brain_dir="$8" today="$9"
  expand_includes "$templates_dir" < "$src" \
    | sed -e "s|{{HOME}}|$home_dir|g" \
          -e "s|{{USERNAME}}|$username|g" \
          -e "s|{{PROJECT_DIR}}|$project_dir|g" \
          -e "s|{{PYTHON}}|$python|g" \
          -e "s|{{BRAIN_DIR}}|$brain_dir|g" \
          -e "s|{{TODAY}}|$today|g" \
    > "$dst"
}

# CLI mode — only fires if this file is executed, not sourced.
# BASH_SOURCE[0] == $0 exactly when invoked as a script.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  cmd="${1:-}"
  case "$cmd" in
    expand)
      shift
      expand_includes "$@"
      ;;
    render)
      shift
      render_template "$@"
      ;;
    *)
      echo "usage: $0 expand <templates_dir>                                     (stdin→stdout)" >&2
      echo "       $0 render <templates_dir> <src> <dst> <home> <user> <proj> <py> <brain> <today>" >&2
      exit 64
      ;;
  esac
fi
