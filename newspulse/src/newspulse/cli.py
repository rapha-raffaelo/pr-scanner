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
    run_parser.add_argument(
        "--since-days",
        type=int,
        metavar="N",
        help=(
            "Backfill: accept items published in the last N days instead of since "
            "the last successful run. Only widens the accepted window — a feed "
            "still returns only what it currently syndicates. Safe to repeat; "
            "already-stored articles are deduplicated away"
        ),
    )
    digest_parser = subcommands.add_parser(
        "digest", help="Email the morning digest for a day (default: today)"
    )
    digest_parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="The day to summarise (default: today, local)",
    )
    digest_parser.add_argument(
        "--print", action="store_true", dest="print_only",
        help="Print the digest instead of sending it (no SMTP needed)",
    )

    check_parser = subcommands.add_parser(
        "check-feeds",
        help="Fetch every registered feed and report which ones are dead",
    )
    check_parser.add_argument(
        "--days",
        type=int,
        default=30,
        metavar="N",
        help="Window to count recent entries over (default: 30)",
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


def _run_command(dry_run: bool, since_days: int | None = None) -> int:
    """Wire up logging + session and execute one sweep; return a process exit code."""
    job.setup_logging()
    since = job.lookback_since(since_days) if since_days is not None else None
    # A dry run never calls the analyzer, so don't even construct it — the real
    # analyzer expects the `claude` CLI to be available, which a preview shouldn't need.
    analyzer = None if dry_run else get_analyzer()
    with get_session() as session:
        report = job.run(session, analyzer=analyzer, dry_run=dry_run, since=since)
    _print_report(report)
    # A crashed sweep (`failed`) is a non-zero exit so a scheduler/cron notices;
    # `partial` still did useful work, so it exits 0.
    return 1 if report.status is RunStatus.FAILED else 0


def _check_feeds_command(days: int) -> int:
    """Fetch every registered feed and report the dead ones.

    Feed URLs rot silently — an outlet moves its RSS path and the sweep simply
    stops seeing that publication, with nothing on the dashboard to say so. This
    is the maintenance check that surfaces it. Returns non-zero when any feed is
    unreachable, so it can be wired into a periodic job.
    """
    import datetime as dt

    from .feeds import load_feeds
    from .ingest import fetch_feed

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    feeds = load_feeds()
    dead: list[tuple[str, str, str]] = []
    empty: list[tuple[str, str]] = []

    print(f"Checking {len(feeds)} feed(s) over the last {days} day(s)…\n")
    for feed in feeds:
        try:
            items = fetch_feed(feed.url, since, source=feed.name)
        except Exception as exc:  # noqa: BLE001 — report every failure mode alike
            dead.append((feed.name, feed.url, f"{type(exc).__name__}: {exc}"))
            print(f"  DEAD   {feed.name:32} {type(exc).__name__}")
            continue
        if not items:
            empty.append((feed.name, feed.url))
            print(f"  EMPTY  {feed.name:32} no entries in window")
        else:
            print(f"  ok     {feed.name:32} {len(items)} item(s)")

    print(f"\n{len(feeds) - len(dead) - len(empty)}/{len(feeds)} feed(s) delivering.")
    if empty:
        print(f"\n{len(empty)} reachable but empty in this window "
              "(may simply be a quiet publication):")
        for name, url in empty:
            print(f"  - {name}: {url}")
    if dead:
        print(f"\n{len(dead)} unreachable — fix or replace the URL:")
        for name, url, why in dead:
            print(f"  - {name}: {url}\n      {why}")
    return 1 if dead else 0


def _digest_command(date: str | None, print_only: bool) -> int:
    """Build the digest and either print it or email it."""
    import datetime as dt

    from .digest import build_digest, send_digest

    day = None
    if date:
        try:
            day = dt.datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            _build_parser().error("--date must be YYYY-MM-DD")

    job.setup_logging()
    with get_session() as session:
        if print_only:
            digest = build_digest(session, day=day)
            print(digest.subject)
            print()
            print(digest.body)
            return 0
        digest = send_digest(session, day=day)
    if digest is None:
        print("Digest not sent (SMTP not configured or delivery failed).")
        return 1
    print(f"Digest sent: {digest.subject}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point (``newspulse``). Returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        if args.since_days is not None and args.since_days < 1:
            _build_parser().error("--since-days must be at least 1")
        return _run_command(dry_run=args.dry_run, since_days=args.since_days)
    if args.command == "digest":
        return _digest_command(date=args.date, print_only=args.print_only)
    if args.command == "check-feeds":
        return _check_feeds_command(days=args.days)
    return 2  # unreachable: the subcommand is required by the parser


if __name__ == "__main__":
    raise SystemExit(main())
