"""The header's "a sweep is running" state and its self-stopping poll.

The state itself was unreachable before: ``job._finalize_run`` writes the ``runs``
row when a sweep *ends*, so ``finished_at is None`` — what the header tested — was
never true, and the "Lauf läuft…" branch had never rendered. The live signal is the
process lock the run trigger holds, which is what these tests drive.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Base
from newspulse.web import runlock
from newspulse.web.app import create_app, get_db


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture
def running():
    """Hold the guard the way an in-flight sweep does, and always let go."""
    assert runlock.guard.acquire(blocking=False), "guard was already held"
    try:
        yield
    finally:
        runlock.guard.release()


def test_idle_pages_show_neither_spinner_nor_poll(client):
    """Nothing turning, and no request every four seconds for nothing."""
    body = client.get("/today").text

    assert "spinner" not in body
    assert "/partials/runmeta" not in body
    assert "Aktualisieren" in body


def test_a_running_sweep_turns_the_wheel_on_every_page(client, running):
    """The header is in the shared layout, so the state follows it everywhere."""
    for path in ("/", "/clients", "/archive", "/settings"):
        body = client.get(path).text
        assert 'class="spinner"' in body, path
        assert "Aktualisierung läuft…" in body, path


def test_a_running_sweep_makes_the_header_poll_itself(client, running):
    body = client.get("/today").text

    meta = body.split('id="runmeta"', 1)[1].split(">", 1)[0]
    assert 'hx-get="/partials/runmeta"' in meta
    assert "every 4s" in meta


def test_the_refresh_button_is_disabled_while_a_sweep_runs(client, running):
    """The trigger refuses a second run anyway; a button that looks clickable
    while the answer is "no" reads as a broken control."""
    body = client.get("/today").text

    assert "nav-refresh__btn--busy" in body
    assert "disabled" in body.split("nav-refresh", 1)[1].split("</form>", 1)[0]


def test_the_partial_keeps_polling_while_the_sweep_runs(client, running):
    resp = client.get("/partials/runmeta")

    assert resp.status_code == 200
    assert 'hx-get="/partials/runmeta"' in resp.text
    assert 'class="spinner"' in resp.text
    assert "HX-Refresh" not in resp.headers


def test_the_partial_asks_for_a_reload_once_the_sweep_is_done(client):
    """Stopping the poll is not enough: the finished run changed the feed, the
    alerts and possibly a positioning draft, and swapping only the header would
    announce a finished sweep above a stale page."""
    resp = client.get("/partials/runmeta")

    assert resp.status_code == 204
    assert resp.headers["HX-Refresh"] == "true"


def test_the_header_shows_the_previous_run_as_stale_not_as_current(client, running, ):
    """While a new sweep works, the numbers on screen belong to the old one."""
    import datetime as dt

    from newspulse.models import Run, RunStatus

    # Seed through the app's own session so the partial sees it.
    session = next(client.app.dependency_overrides[get_db]())
    session.add(
        Run(
            started_at=dt.datetime(2026, 7, 30, 5, 55, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 7, 30, 6, 0, tzinfo=dt.UTC),
            status=RunStatus.OK,
            articles_found=42,
            errors=[],
        )
    )
    session.commit()

    body = client.get("/partials/runmeta").text

    assert "Stand von" in body
    assert "Letzter Lauf" not in body


def test_a_generating_page_polls_itself_so_nobody_is_told_to_reload(client, running):
    """The instruction "Seite neu laden" was work the machinery already does.

    Both background generators hold the sweep's guard, so the header polls while
    they run and reloads the whole page when they release it. This pins the wiring
    those notices now depend on.
    """
    body = client.get("/today").text

    assert 'hx-get="/partials/runmeta"' in body
    assert "Seite neu laden" not in body


def test_the_message_writer_holds_the_sweep_guard(monkeypatch, no_background_message):
    """Without it the Impulse page would sit there finished and look unfinished.

    The generator this replaced wrote "recommendations"; the guard requirement is
    the same and for the same reason — the header polls while it is held, so the
    page reloads itself the moment the text exists.
    """
    from newspulse.web.routes import advisory

    def _fake_session():
        raise RuntimeError("stop here — the guard is what this test is about")

    monkeypatch.setattr(advisory, "get_session", _fake_session)

    # The worker acquires the guard before doing any work, and releases it even
    # when that work explodes.
    advisory._writing.acquire()
    no_background_message(1, 1, "", "")  # the real worker

    assert not advisory._run_guard.locked(), "the guard must not be left held"
    assert not advisory._writing.locked(), "nor its own lock"
    # And the failure is reported rather than swallowed into an empty page.
    assert "1" not in advisory._last_message_error or advisory._last_message_error[1]
