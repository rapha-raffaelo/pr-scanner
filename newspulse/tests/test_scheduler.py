"""Something has to run the daily sweep.

Nothing on the deployed host did. The compose file's cron container and the
launchd/Windows entries under ``schedule/`` are all host-side, and the platform's
start command is ``newspulse-web`` — one process, serving HTTP. So the tool only
ever fetched, analysed, drafted or mailed anything when a person pressed
"Aktualisieren", and every unattended promise it makes was quietly unkept.

The symptom that surfaced it was the mildest one: "irgendwie sind hier immer noch
keine default Impulse angezeigt". The drafting worked. Nobody was asking for it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import sessionmaker

from newspulse import config
from newspulse.db import make_engine
from newspulse.models import Base, Run, RunStatus
from newspulse.web import scheduler

# 06:10 Berlin on a summer morning is 04:10 UTC.
_BERLIN = config.resolve_local_zone()


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _local(hour: int, minute: int = 0, day: int = 3) -> dt.datetime:
    return dt.datetime(2026, 8, day, hour, minute, tzinfo=_BERLIN).astimezone(dt.UTC)


def _run(session, at: dt.datetime, status: RunStatus = RunStatus.OK) -> Run:
    row = Run(
        started_at=at,
        finished_at=at + dt.timedelta(minutes=4),
        status=status,
        articles_found=3,
        errors=[],
    )
    session.add(row)
    session.commit()
    return row


def test_before_the_scheduled_time_nothing_is_due(session):
    assert scheduler._due(session, _local(5, 0)) is False


def test_after_the_scheduled_time_with_no_run_today_it_is_due(session):
    assert scheduler._due(session, _local(7, 0)) is True


def test_a_sweep_that_already_ran_today_is_not_repeated(session):
    """The runs table is the state, not this thread's memory — so a restart, a
    redeploy or a second replica cannot make the day's sweep run twice."""
    _run(session, _local(6, 12))

    assert scheduler._due(session, _local(9, 0)) is False


def test_yesterdays_sweep_does_not_satisfy_today(session):
    _run(session, _local(6, 12, day=2))

    assert scheduler._due(session, _local(7, 0)) is True


def test_a_failed_sweep_is_not_retried_in_a_loop(session):
    """Retrying every minute for the rest of the day would spend the whole
    subscription on a backend that is down. It is retried tomorrow, and the
    failure is visible in the header meanwhile."""
    _run(session, _local(6, 12), status=RunStatus.FAILED)

    assert scheduler._due(session, _local(9, 0)) is False


def test_a_manual_run_before_the_scheduled_time_still_leaves_it_due(session):
    """Somebody clicking "Aktualisieren" at five in the morning is not the daily
    sweep: the point of the schedule is that the day is ready when it starts."""
    _run(session, _local(4, 30))

    assert scheduler._due(session, _local(7, 0)) is True


def test_the_schedule_is_read_in_the_readers_zone_not_the_hosts(monkeypatch):
    """A container runs on UTC. "06:10" means ten past six where the person
    reading the digest is, or the digest arrives at 08:10 in summer."""
    monkeypatch.setenv(config.ENV_DAILY_AT, "06:10")
    # 05:00 UTC is already 07:00 in Berlin, so today's 06:10 has passed.
    at = scheduler._scheduled_today(dt.datetime(2026, 8, 3, 5, 0, tzinfo=dt.UTC))

    assert (at.hour, at.minute) == (6, 10)
    assert at.utcoffset() == dt.timedelta(hours=2)


@pytest.mark.parametrize(
    "value, expected",
    [("06:10", (6, 10)), ("7", (7, 0)), ("23:59", (23, 59))],
)
def test_the_time_can_be_configured(monkeypatch, value, expected):
    monkeypatch.setenv(config.ENV_DAILY_AT, value)
    assert config.daily_run_at() == expected


@pytest.mark.parametrize("value", ["", "abc", "25:00", "6:99", "-1:00"])
def test_a_malformed_time_falls_back_rather_than_stopping_the_sweep(monkeypatch, value):
    """A typo in a platform variable must not be the reason nothing ever runs —
    that is the exact failure this module was added to end."""
    monkeypatch.setenv(config.ENV_DAILY_AT, value)
    assert config.daily_run_at() == (6, 10)


def test_the_scheduler_is_on_unless_switched_off(monkeypatch):
    """On by default: the deployment that needs it is the one where nobody
    thought about it, and a scheduler that must be switched on is off."""
    monkeypatch.delenv(config.ENV_SCHEDULER, raising=False)
    assert config.scheduler_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "OFF"])
def test_an_external_cron_can_switch_it_off(monkeypatch, value):
    monkeypatch.setenv(config.ENV_SCHEDULER, value)
    assert config.scheduler_enabled() is False


def test_start_returns_nothing_when_disabled(monkeypatch):
    monkeypatch.setenv(config.ENV_SCHEDULER, "0")
    assert scheduler.start() is None


def test_the_loop_runs_the_sweep_when_it_is_due(monkeypatch):
    """The thread, not just the arithmetic.

    A scheduler with a correct ``_due`` and a loop that never calls anything is
    the same as no scheduler — and indistinguishable from one that works, which
    is precisely the failure being fixed.
    """
    import threading

    calls: list[str] = []
    monkeypatch.setattr(scheduler, "_TICK_SECONDS", 0.01)
    monkeypatch.setattr(scheduler, "_due", lambda session, now: not calls)
    monkeypatch.setattr(scheduler, "_run_once", lambda: calls.append("ran"))

    stop = threading.Event()
    thread = threading.Thread(target=scheduler._loop, args=(stop,), daemon=True)
    thread.start()
    for _ in range(200):
        if calls:
            break
        thread.join(0.01)
    stop.set()
    thread.join(1)

    assert calls == ["ran"]


def test_the_loop_survives_a_failing_sweep(monkeypatch):
    """The one thing this thread may never do is stop. A scheduler that died
    quietly in week three is the failure mode the whole job module is written
    against."""
    import threading

    attempts: list[int] = []

    def _boom() -> None:
        attempts.append(1)
        raise RuntimeError("Feed-Anbieter weg")

    monkeypatch.setattr(scheduler, "_TICK_SECONDS", 0.01)
    monkeypatch.setattr(scheduler, "_due", lambda session, now: True)
    monkeypatch.setattr(scheduler, "_run_once", _boom)

    stop = threading.Event()
    thread = threading.Thread(target=scheduler._loop, args=(stop,), daemon=True)
    thread.start()
    for _ in range(300):
        if len(attempts) >= 2:
            break
        thread.join(0.01)
    still_running = thread.is_alive()
    stop.set()
    thread.join(1)

    assert len(attempts) >= 2, "it tried again after the failure"
    assert still_running, "the thread was alive until we asked it to stop"


def test_the_newsjack_pass_runs_on_its_own_cadence(monkeypatch):
    """DEC-6 A: something in the deployed process actually runs the fast lane —
    and on the interval, not on every tick. Without this wiring no opportunity
    row is ever written and the whole UHR-05 surface is unreachable."""
    from newspulse.job import NewsjackRun

    ran: list[str] = []

    def _pass(session) -> NewsjackRun:
        ran.append("pass")
        return NewsjackRun(mandates=1, opportunities=0, rejected=0, errors=[])

    monkeypatch.setattr(scheduler.job, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.job, "run_newsjack", _pass)
    monkeypatch.setattr(scheduler, "_newsjack_last", None)

    first = dt.datetime(2026, 8, 3, 12, 0, tzinfo=dt.UTC)
    scheduler._newsjack_tick(first)
    scheduler._newsjack_tick(first + dt.timedelta(hours=1))  # not due yet
    scheduler._newsjack_tick(first + dt.timedelta(hours=3))  # due again

    assert ran == ["pass", "pass"]


def test_the_newsjack_pass_gives_way_to_a_running_sweep(monkeypatch):
    """Non-blocking on the guard, and the pass stays due: the next tick is a
    minute out, and queueing behind a portfolio sweep would fetch the same
    radar feeds twice in a row."""
    ran: list[str] = []
    monkeypatch.setattr(scheduler.job, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(
        scheduler.job, "run_newsjack", lambda session: ran.append("pass")
    )
    monkeypatch.setattr(scheduler, "_newsjack_last", None)

    assert scheduler.runlock.guard.acquire(blocking=False)
    try:
        scheduler._newsjack_tick(dt.datetime(2026, 8, 3, 12, 0, tzinfo=dt.UTC))
    finally:
        scheduler.runlock.guard.release()

    assert ran == []
    assert scheduler._newsjack_due(
        dt.datetime(2026, 8, 3, 12, 1, tzinfo=dt.UTC)
    ), "the refused pass was not stamped as run"


def test_a_sweep_started_by_hand_does_not_earn_a_second_one(session, monkeypatch):
    """The window between "is one running?" and taking the guard.

    The check and the acquire were not atomic and the acquire *blocked*, so a
    click landing between them made the scheduler wait out the whole manual
    sweep and then run a complete second one behind it: forty feed fetches, a
    model call per batch, and a second ``send_digest`` — a duplicate morning mail
    to the client.
    """
    ran: list[str] = []
    monkeypatch.setattr(scheduler.job, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.job, "run", lambda *a, **k: ran.append("sweep"))

    # Someone pressed the button and it is still going.
    assert scheduler.runlock.guard.acquire(blocking=False)
    try:
        scheduler._run_once()
    finally:
        scheduler.runlock.guard.release()

    assert ran == [], "it did not queue behind the manual sweep"


def test_a_sweep_that_finished_while_we_waited_settles_the_day(
    session, monkeypatch
):
    """Due-ness is read again inside the guard. The first read happened before a
    manual sweep may have finished, and that sweep writes the very row it
    reads."""
    ran: list[str] = []
    monkeypatch.setattr(scheduler.job, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.job, "run", lambda *a, **k: ran.append("sweep"))
    monkeypatch.setattr(scheduler, "_due", lambda *a, **k: False)

    scheduler._run_once()

    assert ran == []
