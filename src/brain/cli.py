"""Top-level `brain` CLI dispatcher.

    brain init                 interactive setup wizard
    brain status               show vault stats
    brain doctor               run diagnostics (delegates to bin/doctor.sh)
    brain config               print resolved brain-config.yaml
    brain failure record ...   append a row to the failure ledger
    brain failure list [...]   list recorded failures (newest first)
    brain failure resolve ...  mark a failure as resolved
    brain --version            print version

This is intentionally thin — each subcommand lives in its own module.
"""

from __future__ import annotations

import argparse
import json
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


def _cmd_failure_record(args: argparse.Namespace) -> int:
    """`brain failure record` — append one row to the ledger, print id.

    Thin wrapper: the argparse namespace carries whatever the caller
    supplied; `None` is the right default for every optional field so
    `record_failure` stores an explicit null rather than the empty string.
    """
    from brain import failures
    fid = failures.record_failure(
        source=args.source,
        tool=args.tool,
        query=args.query,
        result_digest=args.result_digest,
        user_correction=args.correction,
        tags=args.tag or [],
        session_id=args.session_id,
    )
    print(fid)
    return 0


def _cmd_failure_list(args: argparse.Namespace) -> int:
    """`brain failure list` — pretty-print (or JSON) the ledger.

    --json is the primary interface for tests and scripting; the text
    form is a best-effort single-line summary per row to keep humans
    oriented without reimplementing a full formatter."""
    from brain import failures
    rows = failures.list_failures(
        source=args.source,
        tag=args.tag,
        unresolved_only=args.unresolved,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no failures recorded)")
        return 0
    for r in rows:
        status = "RESOLVED" if r.get("resolution") else "open"
        tags = ",".join(r.get("tags") or []) or "-"
        q = (r.get("query") or "")[:60]
        print(f"{r.get('id','?')}  {r.get('ts','?')}  [{status}]  "
              f"{r.get('source','?')}/{r.get('tool') or '-'}  "
              f"tags={tags}  q={q!r}")
    return 0


def _cmd_failure_resolve(args: argparse.Namespace) -> int:
    """`brain failure resolve <id>` — rewrite the matching row's resolution."""
    from brain import failures
    ok = failures.resolve_failure(
        args.id,
        patch_ref=args.patch,
        outcome=args.outcome,
    )
    if not ok:
        print(f"No failure with id {args.id!r}", file=sys.stderr)
        return 1
    print(f"resolved {args.id}")
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

    # `brain failure {record,list,resolve}` — structured failure ledger.
    # Substrate only; consumers (extraction DLQ, drift detector, ...) ship
    # in later waves.
    p_fail = sub.add_parser("failure", help="Structured failure ledger")
    fail_sub = p_fail.add_subparsers(dest="failure_cmd")
    # If the user types just `brain failure`, print the subcommand help
    # rather than silently no-op'ing.
    p_fail.set_defaults(func=lambda _a: (p_fail.print_help() or 0))

    p_fr = fail_sub.add_parser("record", help="Append a failure row")
    p_fr.add_argument("--source", required=True,
                      help="Failure source (e.g. recall, extraction, template_drift, manual)")
    p_fr.add_argument("--tool", default=None, help="MCP/CLI tool involved")
    p_fr.add_argument("--query", default=None, help="The user query that triggered the failure")
    p_fr.add_argument("--result-digest", dest="result_digest", default=None,
                      help="Short hash or summary of what brain returned")
    p_fr.add_argument("--correction", dest="correction", default=None,
                      help="User-supplied correction / ground truth")
    p_fr.add_argument("--tag", action="append", default=[],
                      help="Tag (repeatable)")
    p_fr.add_argument("--session-id", dest="session_id", default=None,
                      help="Originating session id, if known")
    p_fr.set_defaults(func=_cmd_failure_record)

    p_fl = fail_sub.add_parser("list", help="List recorded failures (newest first)")
    p_fl.add_argument("--source", default=None, help="Filter by source")
    p_fl.add_argument("--tag", default=None, help="Filter by tag")
    p_fl.add_argument("--unresolved", action="store_true",
                      help="Only list rows without a resolution")
    p_fl.add_argument("-n", "--limit", type=int, default=50,
                      help="Max rows to return (default 50)")
    p_fl.add_argument("--json", action="store_true",
                      help="Emit JSON instead of text")
    p_fl.set_defaults(func=_cmd_failure_list)

    p_fx = fail_sub.add_parser("resolve", help="Mark a failure as resolved")
    p_fx.add_argument("id", help="Failure id (12-hex short uuid)")
    p_fx.add_argument("--patch", required=True,
                      help="Patch reference (commit sha, PR url, file path)")
    p_fx.add_argument("--outcome", required=True,
                      help="Outcome label (e.g. fixed, wontfix, duplicate)")
    p_fx.set_defaults(func=_cmd_failure_resolve)

    p_ac = sub.add_parser("auto-clean",
                          help="Apply auto-clean rules to brain entities")
    p_ac.add_argument("--dry-run", action="store_true",
                      help="Show what would be deleted without deleting.")
    p_ac.add_argument("--rules-file", metavar="PATH",
                      help="Override rules file path.")
    p_ac.set_defaults(func=lambda a: __import__(
        "brain.auto_clean", fromlist=["main"]).main(
        (["--dry-run"] if a.dry_run else []) +
        (["--rules-file", a.rules_file] if a.rules_file else [])))

    p_vf = sub.add_parser("verify",
                          help="GC phantom index entries and detect stale facts")
    p_vf.add_argument("--gc-only", action="store_true",
                      help="Only run GC pass.")
    p_vf.add_argument("--stale-only", action="store_true",
                      help="Only report stale/orphaned facts.")
    p_vf.set_defaults(func=lambda a: __import__(
        "brain.verify", fromlist=["main"]).main(
        (["--gc-only"] if a.gc_only else []) +
        (["--stale-only"] if a.stale_only else [])))

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
