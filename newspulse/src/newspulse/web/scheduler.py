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
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy import select

from .. import config, digest, job
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
        _log.info("a sweep is already running; leaving the scheduled one for tomorrow")
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
                "scheduled sweep finished: status=%s, %d new article(s), %d draft(s)",
                report.status.value,
                report.new_articles,
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


def start() -> threading.Event | None:
    """Start the daily scheduler. Returns its stop event, or ``None`` if disabled.

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
    return stop


__all__ = ["start"]
