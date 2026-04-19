"""Top-level `brain` CLI dispatcher.

    brain init             interactive setup wizard
    brain status           show vault stats
    brain doctor           run diagnostics (delegates to bin/doctor.sh)
    brain config           print resolved brain-config.yaml
    brain --version        print version

This is intentionally thin — each subcommand lives in its own module.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import brain.config as config
from brain import init as init_mod

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent


def _cmd_status(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    types = config._discover_entity_types()
    print(f"Brain: {config.BRAIN_DIR}")
    print(f"Index: {config.INDEX_FILE}")
    print(f"Types ({len(types)}):")
    for name, path in sorted(types.items()):
        n = sum(1 for p in path.glob("*.md") if not p.name.startswith("_"))
        print(f"  - {name:<20} {n:>4} entities")
    raw = config.RAW_DIR
    raw_count = sum(1 for _ in raw.glob("*")) if raw.exists() else 0
    print(f"Raw pending: {raw_count}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    sh = PROJECT_DIR / "bin" / "doctor.sh"
    if not sh.exists():
        print(f"doctor.sh not found at {sh}", file=sys.stderr)
        return 1
    return subprocess.call(["bash", str(sh)])


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = config.BRAIN_DIR / "brain-config.yaml"
    if not cfg.exists():
        print(f"No config at {cfg} — run `brain init` first.", file=sys.stderr)
        return 1
    print(cfg.read_text(), end="")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Translate parsed args back into argv form so init.main() can stay
    a self-contained, separately runnable module (`python3 -m brain.init`)."""
    forward: list[str] = []
    if args.preset:
        forward += ["--preset", args.preset]
    if args.yes:
        forward.append("--yes")
    if args.no_install:
        forward.append("--no-install")
    if args.force_identity:
        forward.append("--force-identity")
    return init_mod.main(forward)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain", description="Personal brain CLI.")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="Interactive setup wizard")
    p_init.add_argument("--preset", help="Skip preset prompt (e.g. developer, doctor).")
    p_init.add_argument("--yes", "-y", action="store_true",
                        help="Non-interactive: accept defaults (developer preset).")
    p_init.add_argument("--no-install", action="store_true",
                        help="Write config + identity, skip bin/install.sh.")
    p_init.add_argument("--force-identity", action="store_true",
                        help="Overwrite identity/who-i-am.md even if it exists.")
    p_init.set_defaults(func=_cmd_init)

    sub.add_parser("status", help="Vault stats").set_defaults(func=_cmd_status)
    sub.add_parser("doctor", help="Diagnostics (bin/doctor.sh)").set_defaults(func=_cmd_doctor)
    sub.add_parser("config", help="Print brain-config.yaml").set_defaults(func=_cmd_config)

    args = p.parse_args(argv)
    if args.version:
        try:
            from importlib.metadata import version
            print(version("brain"))
        except Exception:
            print("brain (dev)")
        return 0
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
