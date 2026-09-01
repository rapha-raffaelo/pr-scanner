"""The daily sweep, run by the web process itself.

Everything this tool produces on its own — the morning's coverage, the digest,
and the positioning drafts a mandate is supposed to have waiting — is produced by
``job.run()``. Nothing on the deployed host was calling it. ``docker-compose.yml``
carries a cron container and ``schedule/`` carries a launchd plist and a Windows
task, and none of the three exists on the platform this runs on: the start command
there is ``newspulse-web``, one process, serving HTTP.

So the tool only ever did anything when somebody pressed "Aktualisieren", and the
symptom that surfaced it was the mildest one — "irgendwie sind hier immer noch
keine default Impulse angezeigt". The drafts were never missing. Nothing had asked
for them since the last time a human clicked.

An in-process thread rather than a second service, because one process is what the
platform starts. It is deliberately dumb: wake up every minute, ask the database
whether today's sweep has already happened, and if not, run it. The runs table is
the state — a restart, a redeploy or a second replica cannot make it run twice,
because "has a successful run started since today's scheduled time" is a question
about stored rows, not about this thread's memory.

The second clock: a declared crisis
-----------------------------------
A crisis is the one condition in this tool under which that rhythm changes. While
one is open, the affected mandate's own sources are re-read every
``NEWSPULSE_CRISIS_SWEEP_MINUTES`` minutes — and nothing else happens on that
tick (see ``job.run_crisis``).

It runs on a thread of its own rather than as a branch of the daily one, because
the two answer to different clocks: the daily sweep is due once, at a wall-clock
time, and a crisis reading is due repeatedly, at an interval since its own last
one. Sharing a thread would have meant a crisis reading that could only start on
a tick the daily sweep had nothing to say about. They share the run guard
instead, so the two can never fetch at the same moment, and the crisis reading
gives way — its next tick is a minute out, the daily sweep's is a day.

Its state is read from ``crises.last_swept_at`` for exactly the same reason the
daily one reads ``runs``: a restart or a crash halfway through a reading must
leave neither a hung crisis nor a second reading racing the first.

The third clock: the fast lane (UHR-05, DEC-6 A)
------------------------------------------------
Every ``NEWSPULSE_NEWSJACK_EVERY_HOURS`` hours a light run
(:func:`newspulse.job.run_newsjack`) refreshes the active mandates' topic radar
and weighs what it holds — nothing else. Its state is this process's memory
rather than a table, deliberately: the light run writes no ``runs`` row (it must
not move the daily sweep's watermark), and repeating it is nearly free — every
model call is gated behind stored verdicts — so the worst a restart costs is one
extra radar read, run promptly, which is what DEC-6's ninety minutes want anyway.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable

from sqlalchemy import select

from .. import config, crisis, digest, job
from ..db import get_session
from ..models import Run, RunStatus
from . import runlock

_log = logging.getLogger(__name__)

# How often to look at the clock. A minute is far finer than needed for a daily
# job, and it is what makes the first run after a deploy happen promptly rather
# than at the next full hour.
_TICK_SECONDS = 60


def _scheduled_today(now: dt.datetime) -> dt.datetime:
    """Today's sweep time, in the reader's zone rather than the host's.

    A container runs on UTC; "06:10" means ten past six where the person reading
    the digest is, and on a UTC host the naive reading would fire at 08:10 local
    in summer and 07:10 in winter.
    """
    hour, minute = config.daily_run_at()
    return now.astimezone(config.local_zone()).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def _already_ran(session, scheduled: dt.datetime) -> bool:
    """Has a sweep already started on or after today's scheduled time?

    Any run counts, not only a successful one: a sweep that failed at 06:10 must
    not be retried in a loop every minute for the rest of the day. It is retried
    tomorrow, and the failure is visible in the header meanwhile.
    """
    last = session.scalars(
        select(Run).order_by(Run.started_at.desc()).limit(1)
    ).first()
    return last is not None and last.started_at >= scheduled.astimezone(dt.UTC)


def _due(session, now: dt.datetime) -> bool:
    scheduled = _scheduled_today(now)
    if now.astimezone(config.local_zone()) < scheduled:
        return False
    return not _already_ran(session, scheduled)


def _run_once() -> None:
    """One sweep plus the digest, exactly what the cron container used to do."""
    # The web process installs no file handler of its own, so without this the
    # only unattended thing the tool does would leave no trace anywhere — the
    # failure mode the logging exists to prevent. Idempotent, so calling it on
    # every sweep costs nothing.
    job.setup_logging()
    # Non-blocking, like every other caller of this guard. Blocking made the
    # scheduler wait out a sweep somebody had just started by hand and then run
    # a second complete one behind it — forty feed fetches, a model call per
    # batch, and a second digest, which is a duplicate morning mail to the
    # client. The dashboard's own trigger and the asset writer both take it this
    # way; this was the one that queued.
    if not runlock.guard.acquire(blocking=False):
        # The holder decides what happens next, and this thread need not know
        # which it is: a manual sweep writes the runs row that makes tomorrow the
        # next chance, a crisis reading writes none and the next tick retries.
        _log.info("another fetch holds the run guard; the sweep stays due")
        return
    try:
        with get_session() as session:
            # Asked again, now that nothing else can be running. The check
            # outside the guard was made before a manual sweep may have finished,
            # and that sweep wrote the very row this reads: without the re-read a
            # click at 06:09 gives the client two of everything, digest included.
            if not _due(session, dt.datetime.now(dt.UTC)):
                _log.info("a sweep landed while we waited; nothing due any more")
                return
            report = job.run(session)
            _log.info(
                "scheduled sweep finished: status=%s, %d new article(s), "
                "%d market signal(s), %d draft(s)",
                report.status.value,
                report.new_articles,
                report.signals_written,
                report.angles_written,
            )
            if report.status is not RunStatus.FAILED:
                # Unconfigured SMTP logs a warning and returns None; the digest is
                # a convenience and must never fail the sweep that produced it.
                digest.send_digest(session)
    finally:
        runlock.guard.release()


def _loop(stop: threading.Event) -> None:
    while not stop.wait(_TICK_SECONDS):
        try:
            with get_session() as session:
                if not _due(session, dt.datetime.now(dt.UTC)):
                    continue
            if runlock.is_running():
                # Someone pressed the button a moment ago; that run writes the row
                # this checks, so tomorrow's decision stays correct either way.
                continue
            _log.info("daily sweep is due; starting it")
            _run_once()
        except Exception:  # noqa: BLE001 — this thread must outlive every failure
            # The one thing it may never do is stop: a scheduler that dies quietly
            # is indistinguishable from one that is working, which is the failure
            # this whole module exists to end.
            _log.exception("scheduled sweep failed; will try again tomorrow")


def _crisis_tick(
    now: dt.datetime, *, clock: Callable[[], dt.datetime] | None = None
) -> None:
    """Read the sources of every mandate whose crisis reading is due.

    Due-ness is asked twice and the second time inside the guard, exactly as
    :func:`_run_once` does: the first read happens before a sweep somebody else
    started may have finished, and that sweep can have moved the very rows this
    reads.

    ``now`` is the tick, and it decides *what is due*. It is deliberately not the
    timestamp the readings are stamped with: each one takes its own start off
    ``clock`` (the wall clock, unless a test hands one in), because a slow first
    reading would otherwise stamp every crisis behind it with a
    ``last_swept_at`` that was already minutes old — and those crises would fall
    due again that much sooner, for ever.

    Non-blocking on the guard. A crisis reading that finds a sweep in progress
    simply does not happen this minute — queueing behind a full portfolio sweep
    would mean fetching the same feeds twice in a row, and the next tick is sixty
    seconds away.

    It takes the guard because it fetches, and two fetchers racing on the same
    URLs is what that lock exists to prevent — but it also raises
    :data:`newspulse.web.runlock.crisis_reading` while it holds it, so the
    dashboard header does not announce a single-mandate reading as a portfolio
    sweep.

    A reading that raises takes the rest of this tick with it, and that costs at
    most a minute: ``run_crisis`` stamps the row before it reads, so the crisis
    that failed is no longer due and the next tick reaches the ones behind it.

    The errors a reading *isolated* rather than raised are logged here at
    WARNING, because a crisis reading writes no ``runs`` row: a dead feed or an
    expired ``claude`` login has no other place to surface, and a crisis is the
    worst moment for a degraded reading to look exactly like a healthy one.
    """
    with get_session() as session:
        if not crisis.due(session, now=now):
            return
    # The web process installs no file handler of its own; without this the
    # crisis cadence would be the one unattended thing that leaves no trace.
    # Idempotent, so calling it per reading costs nothing.
    job.setup_logging()
    if not runlock.guard.acquire(blocking=False):
        _log.info("a sweep is running; the crisis reading waits for the next tick")
        return
    runlock.crisis_reading.set()
    try:
        with get_session() as session:
            for declared in crisis.due(session, now=now):
                sweep = job.run_crisis(session, declared, now=clock)
                _log.log(
                    logging.WARNING if sweep.errors else logging.INFO,
                    "crisis %d: %d new article(s), %d analysis(es), level %d%s",
                    declared.id,
                    sweep.articles,
                    sweep.analyses,
                    sweep.level,
                    _degraded(sweep),
                )
    finally:
        # Cleared before the lock, so no render can ever see the guard held with
        # the flag already down and call a crisis reading a sweep.
        runlock.crisis_reading.clear()
        runlock.guard.release()


def _degraded(sweep: job.CrisisSweep) -> str:
    """The tail of the log line when a reading was degraded, else empty.

    Two things count as degraded and only one of them raises. An isolated error
    is reported with its first line and a count — an operator grepping the log
    needs to see *that* a reading was degraded and roughly why, and the rest of a
    forty-feed failure would push the line itself off the screen. A feed that
    self-isolated to an empty list raises nothing at all, so without the feed
    tally a dead source and a quiet hour write the identical line.
    """
    parts: list[str] = []
    if sweep.feeds_failed:
        parts.append(f"{sweep.feeds_failed}/{sweep.feeds} feed(s) returned nothing")
    if sweep.errors:
        parts.append(f"{len(sweep.errors)} error(s), first: {sweep.errors[0]}")
    return f" — degraded: {'; '.join(parts)}" if parts else ""


def _crisis_loop(stop: threading.Event) -> None:
    """The crisis cadence's own tick. Same posture as :func:`_loop`: it may fail,
    and it may never stop."""
    while not stop.wait(_TICK_SECONDS):
        try:
            _crisis_tick(dt.datetime.now(dt.UTC))
        except Exception:  # noqa: BLE001 — this thread must outlive every failure
            # A crisis is the worst moment for the tool to go quiet, so the same
            # rule as the daily loop, harder: log it and be back in a minute.
            _log.exception("a crisis reading failed; the cadence stands")


# --- The third clock: the fast lane's light run (UHR-05, DEC-6 A) ---------------

#: When this process last started a light run. In-memory on purpose — see the
#: module docstring: no ``runs`` row may be written, and an extra pass after a
#: restart costs one radar read.
_newsjack_last: dt.datetime | None = None


def _newsjack_due(now: dt.datetime) -> bool:
    """Whether the fast lane's interval has elapsed since this process's last
    pass. The first tick after a start is always due — promptness is the point
    of the lane, and the pass is cheap by construction."""
    if _newsjack_last is None:
        return True
    return now - _newsjack_last >= dt.timedelta(hours=config.newsjack_every_hours())


def _newsjack_tick(now: dt.datetime) -> None:
    """One light run, if it is due: :func:`newspulse.job.run_newsjack` and
    nothing else.

    Non-blocking on the guard, like the crisis reading and for the same reason:
    the pass fetches radar feeds, and queueing behind a portfolio sweep would
    fetch the same URLs twice in a row — the next tick is a minute out. While it
    holds the guard it raises :data:`newspulse.web.runlock.crisis_reading`,
    whose real meaning is "the guard's holder is not a portfolio sweep":
    without it the header would announce a radar-only pass as a full sweep
    every few hours.

    The stamp lands *before* the run, the way ``run_crisis`` stamps its row: a
    pass that fails must fall due again next interval, not next minute — a dead
    radar feed retried every sixty seconds is the loop the daily scheduler's
    ``_already_ran`` exists to prevent, and this pass has no table to prevent
    it with.
    """
    global _newsjack_last
    if not _newsjack_due(now):
        return
    # The web process installs no file handler of its own; idempotent, so
    # calling it per pass costs nothing — same as the other two clocks.
    job.setup_logging()
    if not runlock.guard.acquire(blocking=False):
        _log.info("another fetch holds the run guard; the newsjack pass stays due")
        return
    runlock.crisis_reading.set()
    try:
        _newsjack_last = now
        with get_session() as session:
            report = job.run_newsjack(session)
        _log.log(
            logging.WARNING if report.errors else logging.INFO,
            "newsjack pass: %d mandate(s), %d opportunit(y/ies), "
            "%d rejection(s), %d error(s)",
            report.mandates,
            report.opportunities,
            report.rejected,
            len(report.errors),
        )
    finally:
        # Cleared before the lock, so no render can ever see the guard held
        # with the flag already down and call a radar pass a sweep.
        runlock.crisis_reading.clear()
        runlock.guard.release()


def _newsjack_loop(stop: threading.Event) -> None:
    """The fast lane's own tick. Same posture as the other two loops: it may
    fail, and it may never stop."""
    while not stop.wait(_TICK_SECONDS):
        try:
            _newsjack_tick(dt.datetime.now(dt.UTC))
        except Exception:  # noqa: BLE001 — this thread must outlive every failure
            # The stamp already landed inside the tick, so the failed pass is
            # not retried in a loop; it falls due again next interval.
            _log.exception("a newsjack pass failed; the cadence stands")


def start() -> threading.Event | None:
    """Start all three clocks. Returns the stop event they share, or ``None``
    if off.

    The daily sweep, the crisis cadence and the fast lane's light run get a
    thread each and one stop event, because ``NEWSPULSE_SCHEDULER=0`` means "an
    external cron does the unattended work" and that has to be true of all of
    them.

    Called from ``web.app.main`` rather than ``create_app`` on purpose: the tests
    build the app hundreds of times, and none of them wants a thread that fetches
    feeds and shells out to a model.
    """
    if not config.scheduler_enabled():
        _log.info("daily scheduler is switched off (%s)", config.ENV_SCHEDULER)
        return None
    hour, minute = config.daily_run_at()
    stop = threading.Event()
    threading.Thread(
        target=_loop, args=(stop,), daemon=True, name="newspulse-scheduler"
    ).start()
    _log.info("daily scheduler armed for %02d:%02d %s", hour, minute, config.local_zone())
    # One stop event for both: the switch that turns the scheduler off turns off
    # the crisis cadence with it, because both mean "an external cron does this".
    threading.Thread(
        target=_crisis_loop, args=(stop,), daemon=True, name="newspulse-crisis"
    ).start()
    _log.info(
        "crisis cadence armed at every %d minute(s) while a crisis is open",
        config.crisis_sweep_minutes(),
    )
    # The same stop event again: the switch means "an external cron does the
    # unattended work", and the fast lane is unattended work (DEC-6 A).
    threading.Thread(
        target=_newsjack_loop, args=(stop,), daemon=True, name="newspulse-newsjack"
    ).start()
    _log.info(
        "newsjack cadence armed: a light run every %d hour(s) (DEC-6)",
        config.newsjack_every_hours(),
    )
    return stop


__all__ = ["start"]
