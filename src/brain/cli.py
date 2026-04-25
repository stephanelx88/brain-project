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


def _cmd_progress(args: argparse.Namespace) -> int:
    """`brain progress` — show extraction pipeline progress + health."""
    from brain.claims import progress as _progress
    p = _progress.extraction_progress()
    if getattr(args, "json", False):
        import json as _json
        print(_json.dumps(p, indent=2, ensure_ascii=False))
    else:
        print(_progress.format_text(p))
    return 0


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


def _cmd_bench(args: argparse.Namespace) -> int:
    """`brain bench` — run the golden-set benchmark against the vault.

    Text mode prints the `headline()` plus, in verbose mode, one line
    per query with its rank (or weak_expected/weak_observed pair). JSON
    mode is the machine-readable surface for CI comparisons + the
    nightly report consumer (follow-up workstream).
    """
    from brain import benchmark
    queries = benchmark.load_golden_yaml(args.yaml)
    if not queries:
        print(
            f"no golden queries loaded from {args.yaml or benchmark.DEFAULT_GOLDEN_PATH}",
            file=sys.stderr,
        )
        return 1
    report = benchmark.run_benchmark(queries, k=args.k)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print(report.headline())
    if not args.verbose:
        return 0
    # One line per query, rank or weak-match outcome.
    for row in report.per_query:
        q = row.get("query", "")
        if row.get("weak_expected"):
            tag = "WEAK_OK" if row.get("weak_observed") else "WEAK_FAIL"
            print(f"  [{tag}] top={row.get('top_score')} thr={row.get('threshold')}  {q!r}")
        elif row.get("error"):
            print(f"  [ERR]     err={row['error']!r}  {q!r}")
        elif row.get("hit"):
            print(f"  [hit@{row['rank']:<2}]  {q!r}  -> {row['top_identifiers'][:1]}")
        else:
            print(f"  [miss]    {q!r}  -> {row['top_identifiers'][:3]}")
    return 0


def _cmd_consolidate(args: argparse.Namespace) -> int:
    """`brain consolidate` — one-shot WS8 promotion worker.

    Scans fact_claims for episodic triples that have ≥2 independent
    agreeing episodes + pass the scrub/trust/contested/salience
    gates, and (with ``--apply``) promotes them to semantic. Default
    dry-run prints a summary without touching the DB.
    """
    from brain import consolidation
    summary = consolidation.promote_episodic_ready(
        apply=args.apply,
        max_promotions=args.max,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    mode = "APPLY" if args.apply else "DRY-RUN"
    status_note = summary.get("status")
    print(
        f"[{mode}] consolidate: "
        f"checked={summary['checked_groups']} "
        f"eligible={summary['eligible']} "
        f"promoted={summary['promoted']} "
        f"blocked(contested={summary['blocked_contested']}, "
        f"salience={summary['blocked_salience']}, "
        f"scrub={summary['blocked_scrub']}, "
        f"trust={summary['blocked_trust']}, "
        f"age={summary['blocked_age']}, "
        f"disagree={summary['blocked_disagreement']}) "
        f"budget={summary['budget_remaining']}"
        + (f" status={status_note}" if status_note else "")
    )
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """`brain watch` — fs-event watcher daemon.

    Without flags: runs the watcher in the foreground (Ctrl-C to exit).
    `--install-unit`: renders the systemd user unit into
        `$XDG_CONFIG_HOME/systemd/user/brain-watcher.service`,
        reloads daemon, and enables+starts it. Linux only.
    """
    from brain import watcher
    if getattr(args, "install_unit", False):
        return watcher.install_unit(enable=not args.no_enable)
    return watcher.watch_vault(verbose=args.verbose)


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

    p_prog = sub.add_parser("progress", help="Extraction pipeline progress + health")
    p_prog.add_argument("--json", action="store_true",
                         help="Emit JSON instead of the text progress block")
    p_prog.set_defaults(func=_cmd_progress)

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

    p_bench = sub.add_parser(
        "bench",
        help="Run the golden-set recall benchmark against the vault",
    )
    p_bench.add_argument(
        "--yaml", default=None,
        help="Path to the golden yaml (defaults to tests/golden/recall.yaml in the repo)",
    )
    p_bench.add_argument(
        "-k", "--k", type=int, default=10,
        help="Top-k cutoff for hit detection (default 10)",
    )
    p_bench.add_argument("--json", action="store_true",
                         help="Emit the full report as JSON")
    p_bench.add_argument("-v", "--verbose", action="store_true",
                         help="Print one line per query with rank / weak outcome")
    p_bench.set_defaults(func=_cmd_bench)

    p_cons = sub.add_parser(
        "consolidate",
        help="One-shot WS8 episodic → semantic promotion worker",
    )
    p_cons.add_argument("--apply", action="store_true",
                        help="Actually write promotions (default: dry run)")
    p_cons.add_argument("--max", type=int, default=None,
                        help="Upper bound on promotions this run (default: no cap)")
    p_cons.add_argument("--json", action="store_true",
                        help="Emit the summary as JSON")
    p_cons.set_defaults(func=_cmd_consolidate)

    p_watch = sub.add_parser(
        "watch",
        help="fs-event watcher daemon (sub-second indexing on Linux)",
    )
    p_watch.add_argument("-v", "--verbose", action="store_true",
                         help="Print one line per dispatched event")
    p_watch.add_argument("--install-unit", action="store_true",
                         dest="install_unit",
                         help="Render + enable the systemd --user unit "
                              "(Linux only). No-op on macOS.")
    p_watch.add_argument("--no-enable", action="store_true",
                         help="With --install-unit, write the unit but do "
                              "not `systemctl --user enable --now` it.")
    p_watch.set_defaults(func=_cmd_watch)

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
