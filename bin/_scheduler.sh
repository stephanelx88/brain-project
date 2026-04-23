#!/usr/bin/env bash
# Scheduler dispatcher — sourced by install.sh / uninstall.sh.
#
# Exposes three functions that abstract over launchd (macOS) and
# systemd --user (Linux). Callers don't need to care which one runs.
#
#   scheduler_render_and_install "$PROJECT_DIR" "$USERNAME" "$BRAIN_DIR" "$PYTHON" "$TODAY"
#   scheduler_uninstall "$USERNAME"
#   scheduler_verify "$USERNAME"
#
# A caller must already have sourced bin/_render.sh so `render_template`
# is in scope before invoking scheduler_render_and_install.

# shellcheck disable=SC2034

scheduler_os() {
  case "$(uname -s)" in
    Darwin) echo "launchd" ;;
    Linux)  echo "systemd" ;;
    *)      echo "none" ;;
  esac
}

# ── launchd (macOS) ──────────────────────────────────────────────────

_launchd_render() {
  local project_dir="$1" home_dir="$2" user="$3" brain_dir="$4" python="$5" today="$6"
  local plist="$home_dir/Library/LaunchAgents/com.${user}.brain-auto-extract.plist"
  local sem_plist="$home_dir/Library/LaunchAgents/com.${user}.brain-semantic-worker.plist"
  mkdir -p "$(dirname "$plist")"
  render_template "$project_dir/templates" \
    "$project_dir/templates/launchd/brain-auto-extract.plist.tmpl" "$plist" \
    "$home_dir" "$user" "$project_dir" "$python" "$brain_dir" "$today"
  render_template "$project_dir/templates" \
    "$project_dir/templates/launchd/brain-semantic-worker.plist.tmpl" "$sem_plist" \
    "$home_dir" "$user" "$project_dir" "$python" "$brain_dir" "$today"
  SCHEDULER_PATHS=("$plist" "$sem_plist")
  SCHEDULER_LABELS=("com.${user}.brain-auto-extract" "com.${user}.brain-semantic-worker")
}

_launchd_load() {
  local plist
  for plist in "${SCHEDULER_PATHS[@]}"; do
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
  done
  # launchctl is async — poll briefly so the success message is honest.
  local label
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    local all_up=1
    for label in "${SCHEDULER_LABELS[@]}"; do
      launchctl list 2>/dev/null | grep -q "$label" || all_up=0
    done
    (( all_up == 1 )) && break
    sleep 0.3
  done
}

_launchd_verify() {
  local user="$1"
  local main_label="com.${user}.brain-auto-extract"
  local sem_label="com.${user}.brain-semantic-worker"
  if launchctl list 2>/dev/null | grep -q "$main_label"; then
    echo "      ✓ watcher live ($main_label)"
  else
    echo "      ✗ launchctl load returned 0 but '$main_label' is not visible."
    return 1
  fi
  if launchctl list 2>/dev/null | grep -q "$sem_label"; then
    echo "      ✓ semantic worker live"
  else
    echo "      ! semantic worker not visible — ingest will fall back to in-process embedding"
  fi
}

_launchd_uninstall() {
  local user="$1"
  local home_dir="$HOME"
  local plist="$home_dir/Library/LaunchAgents/com.${user}.brain-auto-extract.plist"
  local sem_plist="$home_dir/Library/LaunchAgents/com.${user}.brain-semantic-worker.plist"
  local legacy="$home_dir/Library/LaunchAgents/com.${user}.brain-autoresearch.plist"
  local entry label path
  for entry in \
      "com.${user}.brain-auto-extract:$plist" \
      "com.${user}.brain-semantic-worker:$sem_plist" \
      "com.${user}.brain-autoresearch:$legacy"; do
    label="${entry%%:*}"
    path="${entry#*:}"
    if launchctl list 2>/dev/null | grep -q "$label"; then
      launchctl unload "$path" 2>/dev/null || true
      echo "      ✓ unloaded $label"
    fi
    if [[ -f "$path" ]]; then
      rm -f "$path"
      echo "      ✓ removed $path"
    fi
  done
}

# ── systemd --user (Linux) ───────────────────────────────────────────

_systemd_unit_dir() {
  echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
}

_systemd_render() {
  local project_dir="$1" home_dir="$2" user="$3" brain_dir="$4" python="$5" today="$6"
  local unit_dir
  unit_dir="$(_systemd_unit_dir)"
  mkdir -p "$unit_dir"
  local svc="$unit_dir/brain-auto-extract.service"
  local timer="$unit_dir/brain-auto-extract.timer"
  local sem_svc="$unit_dir/brain-semantic-worker.service"
  render_template "$project_dir/templates" \
    "$project_dir/templates/systemd/brain-auto-extract.service.tmpl" "$svc" \
    "$home_dir" "$user" "$project_dir" "$python" "$brain_dir" "$today"
  render_template "$project_dir/templates" \
    "$project_dir/templates/systemd/brain-auto-extract.timer.tmpl" "$timer" \
    "$home_dir" "$user" "$project_dir" "$python" "$brain_dir" "$today"
  render_template "$project_dir/templates" \
    "$project_dir/templates/systemd/brain-semantic-worker.service.tmpl" "$sem_svc" \
    "$home_dir" "$user" "$project_dir" "$python" "$brain_dir" "$today"
  SCHEDULER_PATHS=("$svc" "$timer" "$sem_svc")
  SCHEDULER_LABELS=(
    "brain-auto-extract.service"
    "brain-auto-extract.timer"
    "brain-semantic-worker.service"
  )
}

_systemd_load() {
  # Pick up freshly-rendered units and enable the user-targeted timer +
  # worker service. Silent on reload failure (common on headless servers
  # without an active user session — `systemctl --user` still works but
  # `daemon-reload` may warn about the absence of a session manager).
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable --now brain-auto-extract.timer 2>/dev/null || {
    echo "      ! systemctl --user enable failed — on headless servers"
    echo "        run 'loginctl enable-linger $USER' once so user units run"
    echo "        outside of an interactive session, then re-run install."
  }
  systemctl --user enable --now brain-semantic-worker.service 2>/dev/null || true
}

_systemd_verify() {
  local unit
  for unit in brain-auto-extract.timer brain-semantic-worker.service; do
    if systemctl --user is-active "$unit" >/dev/null 2>&1; then
      echo "      ✓ $unit active"
    else
      echo "      ! $unit not active — check: systemctl --user status $unit"
    fi
  done
}

_systemd_uninstall() {
  local unit_dir
  unit_dir="$(_systemd_unit_dir)"
  local unit path
  for unit in brain-auto-extract.timer brain-auto-extract.service brain-semantic-worker.service; do
    systemctl --user disable --now "$unit" 2>/dev/null || true
    path="$unit_dir/$unit"
    if [[ -f "$path" ]]; then
      rm -f "$path"
      echo "      ✓ removed $path"
    fi
  done
  systemctl --user daemon-reload 2>/dev/null || true
}

# ── null (unsupported platform) ──────────────────────────────────────

_none_render() {
  SCHEDULER_PATHS=()
  SCHEDULER_LABELS=()
  echo "      ! platform $(uname -s) unsupported by the installer —"
  echo "        the Python package works, but no scheduler was registered"
  echo "        (auto-extract won't run periodically). PRs welcome."
}
_none_load() { :; }
_none_verify() { echo "      - no scheduler registered (platform unsupported)"; }
_none_uninstall() { :; }

# ── public dispatch ──────────────────────────────────────────────────

scheduler_render_and_install() {
  local project_dir="$1" home_dir="$2" user="$3" brain_dir="$4" python="$5" today="$6"
  local os
  os="$(scheduler_os)"
  case "$os" in
    launchd) _launchd_render "$project_dir" "$home_dir" "$user" "$brain_dir" "$python" "$today"; _launchd_load ;;
    systemd) _systemd_render "$project_dir" "$home_dir" "$user" "$brain_dir" "$python" "$today"; _systemd_load ;;
    *)       _none_render ;;
  esac
}

scheduler_verify() {
  local user="$1"
  case "$(scheduler_os)" in
    launchd) _launchd_verify "$user" ;;
    systemd) _systemd_verify ;;
    *)       _none_verify ;;
  esac
}

scheduler_uninstall() {
  local user="$1"
  case "$(scheduler_os)" in
    launchd) _launchd_uninstall "$user" ;;
    systemd) _systemd_uninstall ;;
    *)       _none_uninstall ;;
  esac
}
