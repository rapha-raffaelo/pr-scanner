"""Command-line entry point: ``newspulse run [--dry-run]``.

This is the one place in the package allowed to write to stdout: it prints the
run summary a human reads at the terminal. Every durable diagnostic goes through
the rotating log file that :func:`newspulse.job.setup_logging` installs — stdout
is only the at-a-glance result of *this* invocation, so a piece of the sweep that
failed is still recoverable from the log even after the terminal scrolls away.

``newspulse run`` executes a full sweep; ``newspulse run --dry-run`` fetches,
matches, and deduplicates and reports the counts without calling the analyzer or
writing anything.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import job
from .analyzer import get_analyzer
from .db import get_session
from .models import RunStatus

_PROG = "newspulse"


def _build_parser() -> argparse.ArgumentParser:
    """The ``newspulse`` argument parser with the ``run`` subcommand."""
    parser = argparse.ArgumentParser(
        prog=_PROG, description="NewsPulse — local German media monitor"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run", help="Run one daily sweep")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Fetch, match, and deduplicate and report counts without calling the "
            "analyzer or writing articles/analyses"
        ),
    )
    return parser


def _print_report(report: job.RunReport) -> None:
    """Print the run summary to stdout (the CLI's one permitted output channel)."""
    label = "DRY RUN" if report.dry_run else "RUN"
    print(f"[{label}] status={report.status.value}")
    print(f"  feeds fetched:     {report.feeds_ok}/{report.feeds_total}")
    print(f"  items fetched:     {report.items_fetched}")
    print(f"  candidate matches: {report.candidates}")
    print(f"  new articles:      {report.new_articles}")
    if report.dry_run:
        print("  analyses:          skipped (dry run)")
    else:
        print(f"  analyses written:  {report.analyses_written}")
    if report.errors:
        print(f"  errors ({len(report.errors)}):")
        for message in report.errors:
            print(f"    - {message}")


def _run_command(dry_run: bool) -> int:
    """Wire up logging + session and execute one sweep; return a process exit code."""
    job.setup_logging()
    # A dry run never calls the analyzer, so don't even construct it — the real
    # analyzer expects the `claude` CLI to be available, which a preview shouldn't need.
    analyzer = None if dry_run else get_analyzer()
    with get_session() as session:
        report = job.run(session, analyzer=analyzer, dry_run=dry_run)
    _print_report(report)
    # A crashed sweep (`failed`) is a non-zero exit so a scheduler/cron notices;
    # `partial` still did useful work, so it exits 0.
    return 1 if report.status is RunStatus.FAILED else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point (``newspulse``). Returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return _run_command(dry_run=args.dry_run)
    return 2  # unreachable: the subcommand is required by the parser


if __name__ == "__main__":
    raise SystemExit(main())
