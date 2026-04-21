"""Interactive `brain init` wizard.

Two prompts only — profile + vault path. Everything else (name, role, LLM
provider, MCP registration, launchd watcher, embedding model download)
is auto-configured.

The vault path is what the user supplies — typically an Obsidian vault.
Brain creates `entities/`, `raw/`, `identity/`, `logs/` etc. inside it
(side-by-side with the user's existing notes; they coexist).

The chosen path is persisted three ways:
  1. `BRAIN_DIR` export appended to the user's shell rc (~/.zshrc etc.)
  2. Recorded in `<vault>/.brain.conf` by `bin/install.sh`
  3. Used as the `BRAIN_DIR` env var when spawning the MCP server (so
     Claude Code / Cursor read the right vault).

Usage
-----
    brain init                       # 2 prompts (profile + vault)
    brain init --preset doctor       # skip profile prompt
    brain init --no-install          # write config + identity, skip install.sh
    brain init --yes                 # CI mode — needs BRAIN_DIR env var

Re-running is safe: existing identity files are preserved unless
`--force-identity` is passed; brain-config.yaml is merged, not clobbered.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from brain.io import atomic_write_text
from brain.presets import list_presets, load_preset

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # repo root


# ─────────────────────────────────────────────────────────────────────────
# Pretty output (no third-party dep)
# ─────────────────────────────────────────────────────────────────────────
def _info(msg: str) -> None:
    print(f"  {msg}")


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def _warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m {msg}")


def _err(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}", file=sys.stderr)


def _header(msg: str) -> None:
    print(f"\n\033[1m{msg}\033[0m")


# ─────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────
def _ensure_questionary():
    """Import questionary lazily so the module loads even when the dep is
    missing (used by tests that exercise non-interactive paths)."""
    try:
        import questionary  # noqa: F401
        return questionary
    except ImportError as e:
        _err(
            "questionary is required for interactive `brain init`.\n"
            "  Install with: pip install 'brain[init]'  (or: pip install questionary)\n"
            f"  Original error: {e}"
        )
        sys.exit(1)


def _detect_default_name() -> str:
    """Best-effort guess: git user.name → $USER → 'You'."""
    try:
        out = subprocess.run(
            ["git", "config", "--global", "--get", "user.name"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return os.environ.get("USER", "You")


def _pick_preset(q, forced_slug: str | None) -> dict[str, Any]:
    if forced_slug:
        return load_preset(forced_slug)
    presets = list_presets()
    choices = [
        q.Choice(
            title=f"{p['display_name']:<24} — {p['description']}",
            value=p["_slug"],
        )
        for p in presets
    ]
    slug = q.select(
        "Pick your profile:",
        choices=choices,
        instruction="(↑↓ to move, ⏎ to select)",
    ).unsafe_ask()
    return load_preset(slug)


def _collect_custom_types(q) -> list[dict[str, str]]:
    raw = q.text(
        "Entity types (comma-separated, lowercase-plural):",
        default="people, projects, decisions, insights",
        validate=lambda v: bool(v.strip()) or "Need at least one type",
    ).unsafe_ask()
    return [
        {"name": t.strip().lower().replace(" ", "-"), "hint": ""}
        for t in raw.split(",")
        if t.strip()
    ]


def _ask_vault_path(q) -> Path:
    """Prompt for vault path. Required, no default — user must supply.

    Accepts ~ and $VARS. Creates the folder if missing. Returns the
    resolved absolute Path.
    """
    def _validate(v: str) -> bool | str:
        v = v.strip()
        if not v:
            return "Vault path is required."
        # Don't fail-fast on non-existence — we'll create it. Just sanity-check
        # that the parent is reachable.
        try:
            p = Path(os.path.expandvars(os.path.expanduser(v)))
            if not p.parent.exists():
                return f"Parent folder doesn't exist: {p.parent}"
        except Exception as e:
            return f"Invalid path: {e}"
        return True

    raw = q.text(
        "Vault path (your Obsidian vault, or any folder):",
        instruction="(absolute path, e.g. ~/Documents/MyVault)",
        validate=_validate,
    ).unsafe_ask()
    p = Path(os.path.expandvars(os.path.expanduser(raw.strip())))
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


# ─────────────────────────────────────────────────────────────────────────
# Persistence: brain-config.yaml, shell rc, identity
# ─────────────────────────────────────────────────────────────────────────
def _merge_config(
    vault: Path,
    preset: dict[str, Any],
    entity_types: list[dict[str, str]],
    identity: dict[str, str],
    llm: dict[str, str],
) -> dict[str, Any]:
    """Read existing brain-config.yaml (if any) and overlay persona keys.
    Never drops unrelated keys."""
    vault.mkdir(parents=True, exist_ok=True)
    config_path = vault / "brain-config.yaml"
    existing: dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text())
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            existing = loaded
        else:
            _warn(f"existing {config_path} unusable — backing up to .bak")
            config_path.rename(config_path.with_suffix(".yaml.bak"))
            existing = {}

    existing.setdefault("version", "0.1.0")
    existing.setdefault("reconciliation_interval_hours", 2)
    existing.setdefault("auto_commit", True)
    existing.setdefault("llm_provider", llm["provider"])
    existing.setdefault("models", {
        "extraction": "sonnet",
        "reconciliation": "sonnet",
        "ingestion": "sonnet",
        "work_sessions": "opus",
    })
    existing["preset"] = preset["_slug"]
    existing["entity_types"] = [t["name"] for t in entity_types]
    existing["identity"] = {
        "name": identity["name"],
        "role": identity["role"],
        "field": identity["field"],
    }
    return existing


def _write_config(vault: Path, cfg: dict[str, Any]) -> None:
    config_path = vault / "brain-config.yaml"
    atomic_write_text(
        config_path,
        "# Generated and updated by `brain init`. Hand-edits preserved on re-run.\n"
        + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False),
    )
    _ok(f"wrote {config_path}")


def _write_auto_clean(vault: Path) -> None:
    """Copy the default auto_clean.yaml into the vault (skip if already present).

    The vault copy is the live rules file — users edit it to customise their
    own patterns. We never overwrite an existing file so hand-edits survive
    `brain init` re-runs.
    """
    dst = vault / "auto_clean.yaml"
    if dst.exists():
        _info(f"skipped {dst} (exists; edit to customise auto-clean rules)")
        return
    src = Path(__file__).parent / "presets" / "auto_clean.yaml"
    if not src.exists():
        _warn("presets/auto_clean.yaml not found — skipping auto-clean setup")
        return
    atomic_write_text(dst, src.read_text())
    _ok(f"wrote {dst}")


def _create_entity_dirs(vault: Path, types: list[dict[str, str]]) -> None:
    ent = vault / "entities"
    ent.mkdir(parents=True, exist_ok=True)
    for t in types:
        (ent / t["name"]).mkdir(exist_ok=True)
    _ok(f"created {len(types)} entity folders under {ent}/")


def _render_who_i_am(
    vault: Path,
    preset: dict[str, Any],
    identity: dict[str, str],
    force: bool,
) -> None:
    """Write identity/who-i-am.md from the preset. Skip if file already
    exists and `--force-identity` was not passed."""
    dst = vault / "identity" / "who-i-am.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not force:
        _info(f"skipped {dst} (exists; pass --force-identity to overwrite)")
        return

    how_i_work = preset["identity"].get("how_i_work", []) or []
    what_matters = preset["identity"].get("what_matters", []) or []
    body = [
        "---",
        "type: identity",
        f"last_updated: {date.today().isoformat()}",
        "---",
        "",
        "# Who I Am",
        "",
        "## Identity",
        f"- Name: {identity['name']}",
        f"- Role: {identity['role']}",
        f"- Field: {identity['field']}",
        "",
        "## How I Work",
        *(f"- {line}" for line in how_i_work),
        "",
        "## What Matters",
        *(f"- {line}" for line in what_matters),
        "",
        "<!--",
        "Edit freely. The brain reads this on every session via brain_identity.",
        "-->",
        "",
    ]
    atomic_write_text(dst, "\n".join(body))
    _ok(f"wrote {dst}")


# ─────────────────────────────────────────────────────────────────────────
# Shell rc: persist `export BRAIN_DIR=…` so future shells see it
# ─────────────────────────────────────────────────────────────────────────
_RC_BEGIN = "# >>> brain vault >>>"
_RC_END = "# <<< brain vault <<<"


def _shell_rc_path() -> Path:
    shell = os.environ.get("SHELL", "/bin/zsh").rsplit("/", 1)[-1]
    home = Path.home()
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        return home / ".bashrc"
    return home / ".profile"


def _persist_brain_dir_to_shell_rc(vault: Path) -> Path:
    """Append (or replace) `export BRAIN_DIR="…"` block in the user's
    shell rc. Returns the rc path so we can tell the user."""
    rc = _shell_rc_path()
    line = f'export BRAIN_DIR="{vault}"'
    block = f"{_RC_BEGIN}\n{line}\n{_RC_END}\n"

    if rc.exists() and _RC_BEGIN in rc.read_text():
        text = rc.read_text()
        text = re.sub(
            rf"{re.escape(_RC_BEGIN)}.*?{re.escape(_RC_END)}\n",
            block,
            text,
            flags=re.S,
        )
        rc.write_text(text)
        _ok(f"updated BRAIN_DIR in {rc}")
    else:
        with rc.open("a") as f:
            f.write(f"\n{block}")
        _ok(f"added BRAIN_DIR export to {rc}")
    return rc


# ─────────────────────────────────────────────────────────────────────────
# Detect & confirm an existing vault
# ─────────────────────────────────────────────────────────────────────────
def _detect_existing_vault() -> Path | None:
    """Return the vault path from a prior install, or None.

    Resolution order:
      1. $BRAIN_DIR env var (current shell)
      2. shell rc block we wrote previously
    """
    raw = os.environ.get("BRAIN_DIR")
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    rc = _shell_rc_path()
    if rc.exists():
        m = re.search(r'export BRAIN_DIR="([^"]+)"', rc.read_text())
        if m:
            return Path(os.path.expandvars(os.path.expanduser(m.group(1)))).resolve()
    return None


# ─────────────────────────────────────────────────────────────────────────
# install.sh hand-off
# ─────────────────────────────────────────────────────────────────────────
def _run_install_sh(vault: Path) -> int:
    sh = PROJECT_DIR / "bin" / "install.sh"
    if not sh.exists():
        _warn(f"{sh} not found — skipping mechanical install")
        return 0
    _info("delegating to bin/install.sh for vault + MCP + launchd setup …")
    print()
    env = {**os.environ, "BRAIN_DIR": str(vault)}
    return subprocess.call(["bash", str(sh)], env=env)


# ─────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="brain init",
        description="Interactive setup wizard for the brain.",
    )
    p.add_argument("--preset", help="Skip preset prompt (e.g. developer, doctor).")
    p.add_argument("--vault", help="Vault path (skips the vault prompt).")
    p.add_argument("--no-install", action="store_true",
                   help="Write config + identity, skip bin/install.sh.")
    p.add_argument("--force-identity", action="store_true",
                   help="Overwrite identity/who-i-am.md even if it exists.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Non-interactive (CI). Requires --vault or $BRAIN_DIR.")
    args = p.parse_args(argv)

    _header("👋  brain init  —  set up your second brain")

    # ── Resolve vault: --vault flag > $BRAIN_DIR/rc > prompt ─────────
    vault: Path | None = None
    if args.vault:
        vault = Path(os.path.expandvars(os.path.expanduser(args.vault))).resolve()
    elif args.yes:
        existing = _detect_existing_vault()
        if not existing:
            _err("--yes requires --vault or $BRAIN_DIR set.")
            return 2
        vault = existing

    # ── Non-interactive shortcut ──────────────────────────────────────
    if args.yes:
        assert vault is not None
        preset_slug = args.preset or "developer"
        preset = load_preset(preset_slug)
        identity = {
            "name": _detect_default_name(),
            "role": preset["identity"].get("role_hint", ""),
            "field": preset["identity"].get("field", ""),
        }
        llm = {"provider": "claude"}
        types = preset["entity_types"] or [{"name": "notes", "hint": ""}]
    else:
        q = _ensure_questionary()
        try:
            # Existing-vault confirmation (only if user didn't pass --vault).
            # If the user declines the detected vault, ask for the new path
            # immediately — don't make them sit through the Profile step
            # wondering where their "No" went.
            step = 1
            if vault is None:
                detected = _detect_existing_vault()
                if detected and detected.exists():
                    keep = q.confirm(
                        f"Found existing vault at {detected}. Keep using it?",
                        default=True,
                    ).unsafe_ask()
                    if keep:
                        vault = detected
                    else:
                        _header(f"{step}. Vault")
                        vault = _ask_vault_path(q)
                        step += 1

            _header(f"{step}. Profile")
            preset = _pick_preset(q, args.preset)
            step += 1

            if preset["_slug"] == "custom" or not preset["entity_types"]:
                _header(f"{step}. Custom entity types")
                types = _collect_custom_types(q)
                step += 1
            else:
                types = preset["entity_types"]

            if vault is None:
                _header(f"{step}. Vault")
                vault = _ask_vault_path(q)

            identity = {
                "name": _detect_default_name(),
                "role": preset["identity"].get("role_hint", ""),
                "field": preset["identity"].get("field", ""),
            }
            llm = {"provider": "claude"}
        except KeyboardInterrupt:
            _err("aborted")
            return 130

    assert vault is not None

    # ── Apply ─────────────────────────────────────────────────────────
    _header("Applying configuration")
    _info(f"vault    : {vault}")
    _info(f"profile  : {preset['_slug']} ({preset.get('display_name', '')})")
    _info(f"identity : {identity['name']} — {identity['role']}")

    cfg = _merge_config(vault, preset, types, identity, llm)
    _write_config(vault, cfg)
    _create_entity_dirs(vault, types)
    _render_who_i_am(vault, preset, identity, force=args.force_identity)
    _write_auto_clean(vault)
    _persist_brain_dir_to_shell_rc(vault)

    if not args.no_install:
        _header("Running installer")
        rc = _run_install_sh(vault)
        if rc != 0:
            _err(f"bin/install.sh exited {rc}")
            return rc

    _header("Done")
    _ok("Restart Claude Code / Cursor to pick up the brain MCP tools.")
    _info(f"Open a new shell (or `source {_shell_rc_path()}`) so $BRAIN_DIR is loaded.")
    _info("Re-run anytime: brain init   ·   diagnose: brain doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
