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
    from brain import status as status_mod
    report = status_mod.gather()
    if getattr(args, "json", False):
        print(status_mod.to_json(report))
        return 0
    print(status_mod.format_text(report))
    if getattr(args, "verbose", False):
        print("\nEntity types:")
        for name, n in sorted(report.vault["by_type"].items()):
            print(f"  - {name:<20} {n:>4}")
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

    p_status = sub.add_parser("status", help="Operational + vault dashboard")
    p_status.add_argument("--json", action="store_true",
                          help="Emit JSON instead of the text dashboard")
    p_status.add_argument("--verbose", "-v", action="store_true",
                          help="Also print per-type entity counts")
    p_status.set_defaults(func=_cmd_status)
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
