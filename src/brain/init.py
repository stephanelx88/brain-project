"""Interactive `brain init` wizard.

Collects persona (preset, name, role, optional LLM config), writes the
result into `~/.brain/brain-config.yaml`, then delegates the mechanical
setup (vault skeleton, MCP registration, launchd, model download) to
`bin/install.sh`. Persona-specific entity folders and a rendered
`identity/who-i-am.md` are applied on top.

Usage
-----
    brain init                       # interactive
    brain init --preset doctor       # skip preset prompt
    brain init --no-install          # write config + identity, skip install.sh
    brain init --yes                 # accept all defaults (uses developer preset)

The wizard is idempotent: re-running it preserves existing identity files
unless `--force-identity` is passed and merges into existing
brain-config.yaml without clobbering unrelated keys.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from brain.io import atomic_write_text
from brain.presets import list_presets, load_preset

BRAIN_DIR = Path.home() / ".brain"
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # repo root
CONFIG_PATH = BRAIN_DIR / "brain-config.yaml"


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
# Wizard steps
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
            title=f"{p['display_name']:<32} — {p['description']}",
            value=p["_slug"],
        )
        for p in presets
    ]
    slug = q.select(
        "What's your field?",
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


def _collect_identity(q, preset: dict[str, Any]) -> dict[str, str]:
    name = q.text("Your name:", default=_detect_default_name()).unsafe_ask()
    role = q.text(
        "Your role:",
        default=preset["identity"].get("role_hint", ""),
    ).unsafe_ask()
    return {"name": name.strip(), "role": role.strip(), "field": preset["identity"].get("field", "")}


def _collect_llm(q) -> dict[str, str]:
    provider = q.select(
        "LLM provider for extraction (you can change later):",
        choices=[
            q.Choice("Claude (Anthropic) — recommended", value="claude"),
            q.Choice("OpenAI", value="openai"),
            q.Choice("Local (Ollama)", value="ollama"),
            q.Choice("Skip — set up later", value="skip"),
        ],
    ).unsafe_ask()
    return {"provider": provider}


# ─────────────────────────────────────────────────────────────────────────
# Apply collected data to disk
# ─────────────────────────────────────────────────────────────────────────
def _merge_config(
    preset: dict[str, Any],
    entity_types: list[dict[str, str]],
    identity: dict[str, str],
    llm: dict[str, str],
) -> dict[str, Any]:
    """Read existing brain-config.yaml (if any) and overlay persona keys.
    Never drops unrelated keys."""
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            loaded = yaml.safe_load(CONFIG_PATH.read_text())
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            existing = loaded
        else:
            # Unparseable OR parsed to a non-dict (e.g. accidental list/string).
            # Either way it's unusable; back it up and start fresh.
            _warn(f"existing {CONFIG_PATH} unusable — backing up to .bak")
            CONFIG_PATH.rename(CONFIG_PATH.with_suffix(".yaml.bak"))
            existing = {}

    existing.setdefault("version", "0.1.0")
    existing.setdefault("reconciliation_interval_hours", 2)
    existing.setdefault("auto_commit", True)
    if llm["provider"] != "skip":
        existing["llm_provider"] = llm["provider"]
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


def _write_config(cfg: dict[str, Any]) -> None:
    atomic_write_text(
        CONFIG_PATH,
        "# Generated and updated by `brain init`. Hand-edits preserved on re-run.\n"
        + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False),
    )
    _ok(f"wrote {CONFIG_PATH}")


def _create_entity_dirs(types: list[dict[str, str]]) -> None:
    ent = BRAIN_DIR / "entities"
    ent.mkdir(parents=True, exist_ok=True)
    for t in types:
        (ent / t["name"]).mkdir(exist_ok=True)
    _ok(f"created {len(types)} entity folders under entities/")


def _render_who_i_am(
    preset: dict[str, Any],
    identity: dict[str, str],
    force: bool,
) -> None:
    """Write identity/who-i-am.md from the preset. Skip if file already
    exists and `--force-identity` was not passed."""
    dst = BRAIN_DIR / "identity" / "who-i-am.md"
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


def _run_install_sh() -> int:
    sh = PROJECT_DIR / "bin" / "install.sh"
    if not sh.exists():
        _warn(f"{sh} not found — skipping mechanical install")
        return 0
    _info("delegating to bin/install.sh for vault + MCP + launchd setup …")
    print()
    return subprocess.call(["bash", str(sh)])


# ─────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="brain init",
        description="Interactive setup wizard for the brain.",
    )
    p.add_argument("--preset", help="Skip preset prompt (e.g. developer, doctor).")
    p.add_argument("--no-install", action="store_true",
                   help="Write config + identity, skip bin/install.sh.")
    p.add_argument("--force-identity", action="store_true",
                   help="Overwrite identity/who-i-am.md even if it exists.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Non-interactive: accept defaults (developer preset).")
    args = p.parse_args(argv)

    _header("👋  brain init  —  set up your second brain")
    _info(f"vault    : {BRAIN_DIR}")
    _info(f"project  : {PROJECT_DIR}")

    if BRAIN_DIR.exists() and any(BRAIN_DIR.iterdir()):
        _warn(f"{BRAIN_DIR} already exists. Re-running is safe — existing data is preserved.")

    # ── Non-interactive shortcut ──────────────────────────────────────
    if args.yes:
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
            _header("1. Pick a profile")
            preset = _pick_preset(q, args.preset)

            if preset["_slug"] == "custom" or not preset["entity_types"]:
                _header("2. Custom entity types")
                types = _collect_custom_types(q)
            else:
                types = preset["entity_types"]

            _header("3. Identity")
            identity = _collect_identity(q, preset)

            _header("4. LLM")
            llm = _collect_llm(q)
        except KeyboardInterrupt:
            _err("aborted")
            return 130

    # ── Apply ─────────────────────────────────────────────────────────
    _header("Applying configuration")
    cfg = _merge_config(preset, types, identity, llm)
    _write_config(cfg)
    _create_entity_dirs(types)
    _render_who_i_am(preset, identity, force=args.force_identity)

    if not args.no_install:
        _header("Running installer")
        rc = _run_install_sh()
        if rc != 0:
            _err(f"bin/install.sh exited {rc}")
            return rc

    _header("Done")
    _ok("Restart Claude Code / Cursor to pick up the brain MCP tools.")
    _info(f"Re-run anytime: brain init   ·   diagnose: brain doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
