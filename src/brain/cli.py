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
    """`brain consolidate` — one-shot WS8 worker.

    Default mode = Part A (episodic → semantic promotion). With
    ``--aliases``, runs Part B (LLM-judged alias canonicalisation
    over unresolved ``object_text`` phrases). Default dry-run prints
    a summary; ``--apply`` writes to DB + vault-root
    ``disambiguations.jsonl``.

    Resource-guard gate (scheduler-friendly): when ``--respect-guard``
    is set (scheduler invocation default) the worker refuses to run
    unless ``resource_guard.clearance_level() >= args.min_level``.
    This keeps the 30-min systemd timer from stealing CPU while the
    user is harvesting/extracting.
    """
    if getattr(args, "respect_guard", False):
        from brain import resource_guard
        level = resource_guard.clearance_level()
        min_level = int(getattr(args, "min_level", 2) or 2)
        if level < min_level:
            msg = {
                "skipped": True,
                "reason": "resource_guard",
                "clearance_level": level,
                "min_level": min_level,
            }
            if args.json:
                print(json.dumps(msg, ensure_ascii=False, indent=2))
            else:
                print(
                    f"[SKIP] consolidate: clearance={level} < "
                    f"min={min_level} (harvest/extract active or system busy)"
                )
            return 0

    from brain import consolidation
    if getattr(args, "aliases", False):
        summary = consolidation.consolidate_aliases(
            apply=args.apply,
            max_pairs=args.max,
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        mode = "APPLY" if args.apply else "DRY-RUN"
        status_note = summary.get("status")
        print(
            f"[{mode}] consolidate --aliases: "
            f"checked={summary['checked']} "
            f"judged={summary['judged']} "
            f"merged={summary['merged']} "
            f"kept_distinct={summary['kept_distinct']} "
            f"needs_user={summary['needs_user']} "
            f"judge_failed={summary['judge_failed']} "
            f"skipped(disambig={summary['skipped_disambig']}, "
            f"correction={summary['skipped_correction']}, "
            f"budget={summary['skipped_budget']}) "
            f"rewritten_rows={summary['rewritten_rows']} "
            f"tokens={summary['tokens_spent']} "
            f"budget={summary['budget_remaining']}"
            + (f" status={status_note}" if status_note != "ok" else "")
        )
        return 0

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


def _cmd_consolidation_list(args: argparse.Namespace) -> int:
    """`brain consolidation list` — print audit rows, newest first."""
    from brain import consolidation
    rows = consolidation.list_actions(
        since=args.since,
        action_id=args.id,
        action=args.action,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no matching actions)")
        return 0
    for r in rows:
        if r.get("action") == "promote":
            print(
                f"{r.get('ts')}  [promote]  id={r.get('promoted_id')}  "
                f"subject={r.get('subject_slug')!r}  "
                f"predicate={r.get('predicate')!r}  "
                f"n={r.get('n_contributors')}  "
                f"salience={r.get('aggregate_salience')}"
            )
        elif r.get("action") == "rollback":
            print(
                f"{r.get('ts')}  [rollback] id={r.get('promoted_id')}  "
                f"subject={r.get('subject_slug')!r}  "
                f"restored={r.get('restored')}  "
                f"reason={r.get('reason')!r}"
            )
        else:
            print(f"{r.get('ts')}  [?] {r}")
    return 0


def _cmd_consolidation_install(args: argparse.Namespace) -> int:
    """`brain consolidation install-scheduler` — render + enable the
    30-min consolidation timer.

    Linux: systemd user service+timer.  macOS: launchd LaunchAgent.
    Other platforms: reports `unsupported` and exits 0.
    """
    from brain import consolidation
    result = consolidation.install_scheduler(enable=not args.no_enable)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if result.get("error"):
        print(
            f"[ERROR] {result['error']}: "
            f"{result.get('expected') or result.get('searched')}"
        )
        return 2
    if result.get("platform") == "unsupported":
        print(f"[SKIP] unsupported platform: {result.get('system')}")
        return 0
    if result.get("platform") == "linux":
        print(
            f"wrote {result['service']}\n"
            f"wrote {result['timer']}\n"
            f"enabled={result['enabled']}"
        )
        if result.get("note"):
            print(result["note"])
        return 0
    if result.get("platform") == "darwin":
        print(
            f"wrote {result['plist']}\n"
            f"enabled={result['enabled']}"
        )
        if result.get("note"):
            print(result["note"])
        return 0
    return 0


def _cmd_consolidation_rollback(args: argparse.Namespace) -> int:
    """`brain consolidation rollback --id=<N>` — undo one promotion.

    Idempotent — calling twice on the same id reports
    ``already_rolled_back``.
    """
    from brain import consolidation
    result = consolidation.rollback(args.id, reason=args.reason or "manual")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if result.get("already_rolled_back"):
        print(f"[NO-OP] id={args.id} — no contributors and no semantic row "
              f"(already rolled back or never promoted)")
        return 0
    print(
        f"[ROLLBACK] id={args.id} restored={result['restored']} "
        f"semantic_deleted={result['semantic_deleted']}"
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
        help="One-shot WS8 worker (promotion by default, aliases with --aliases)",
    )
    p_cons.add_argument("--apply", action="store_true",
                        help="Actually write changes (default: dry run)")
    p_cons.add_argument("--max", type=int, default=None,
                        help="Upper bound on promotions/pairs this run (default: no cap)")
    p_cons.add_argument("--aliases", action="store_true",
                        help="Run Part B: alias canonicalisation with LLM pair-judge "
                             "instead of promotion")
    p_cons.add_argument("--json", action="store_true",
                        help="Emit the summary as JSON")
    # Scheduler invocation: --respect-guard turns on the
    # resource_guard check. Manual use (bench, debugging) keeps the
    # old unconditional behaviour so a human poking at the worker
    # isn't silently no-op'd by a busy system.
    p_cons.add_argument("--respect-guard", action="store_true",
                        dest="respect_guard",
                        help="Skip when resource_guard.clearance_level() "
                             "< --min-level. Used by the systemd/launchd "
                             "scheduler to stand down during harvest/extract.")
    p_cons.add_argument("--min-level", type=int, default=2,
                        dest="min_level",
                        help="Minimum clearance level required when "
                             "--respect-guard is set (default 2, per spec).")
    p_cons.set_defaults(func=_cmd_consolidate)

    # `brain consolidation {list,rollback}` — audit walk + reversal.
    p_cons_audit = sub.add_parser(
        "consolidation",
        help="WS8 audit trail — list past promotions + rollback",
    )
    cons_sub = p_cons_audit.add_subparsers(dest="consolidation_cmd")
    p_cons_audit.set_defaults(
        func=lambda _a: (p_cons_audit.print_help() or 0)
    )

    p_cl = cons_sub.add_parser("list", help="Print audit rows (newest first)")
    p_cl.add_argument("--since", default=None,
                      help="Filter: ts >= YYYY-MM-DD or full ISO-8601")
    p_cl.add_argument("--id", type=int, default=None,
                      help="Filter: only actions touching this promoted_id")
    p_cl.add_argument("--action", choices=("promote", "rollback"),
                      default=None, help="Filter by action type")
    p_cl.add_argument("-n", "--limit", type=int, default=50,
                      help="Max rows to return (default 50)")
    p_cl.add_argument("--json", action="store_true",
                      help="Emit JSON instead of text")
    p_cl.set_defaults(func=_cmd_consolidation_list)

    p_cr = cons_sub.add_parser("rollback", help="Undo a promotion by id")
    p_cr.add_argument("--id", type=int, required=True,
                      help="promoted_id from `brain consolidation list`")
    p_cr.add_argument("--reason", default=None,
                      help="Free-text reason recorded in the audit trail")
    p_cr.add_argument("--json", action="store_true",
                      help="Emit JSON instead of text")
    p_cr.set_defaults(func=_cmd_consolidation_rollback)

    p_ci = cons_sub.add_parser(
        "install-scheduler",
        help="Install the 30-min consolidation timer "
             "(Linux: systemd user, macOS: launchd LaunchAgent)",
    )
    p_ci.add_argument("--no-enable", action="store_true",
                      help="Write unit file(s) but don't enable/load")
    p_ci.add_argument("--json", action="store_true",
                      help="Emit JSON instead of text")
    p_ci.set_defaults(func=_cmd_consolidation_install)

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
