"""Shared test guard rails.

The suite's standing rule is that nothing here touches the network, spawns a
`claude` subprocess, or leaves a thread running past the test that started it.
One route breaks that rule by design: creating a client kicks off a background
fetch of its recent coverage (``settings._start_onboarding``). That is correct in
production and poison in a test run — it would hit Google News from every test
that happens to add a client, and while it holds the run guard every *other*
test's page renders the "a sweep is running" header.

So it is neutralised globally here, and exercised explicitly in
``test_onboarding.py``, which patches the pieces it wants to observe.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_background_onboarding(monkeypatch):
    """Stop client creation from starting a real onboarding fetch."""
    from newspulse.web.routes import settings

    monkeypatch.setattr(settings, "_start_onboarding", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def run_guard_is_free():
    """Fail loudly if a test leaves the run guard held.

    A leaked guard is invisible in the test that caused it and breaks a later one
    that merely renders a page — the exact failure this file exists to prevent, so
    it is worth catching at the source rather than debugging downstream.
    """
    from newspulse.web import runlock

    yield
    assert not runlock.is_running(), "a test left the run guard held"


@pytest.fixture(autouse=True)
def no_background_impulse(monkeypatch):
    """Stop the impulse button from starting a real draft in a test run.

    Same reason as the onboarding fetch: it reaches Google News and shells out to
    `claude`, and it holds the run guard while it does. Exercised explicitly in
    ``test_impulse_request.py``.
    """
    from newspulse.web.routes import advisory

    monkeypatch.setattr(advisory, "_run_impulse", _stub(advisory, "_drafting"))


@pytest.fixture(autouse=True)
def no_background_message(monkeypatch):
    """Same for the "write me the message" button, for the same two reasons: it
    shells out to `claude` and it holds the run guard while it does.

    Yields the real worker, so the one test that is *about* the worker
    (``test_runstatus``) can call it deliberately rather than reaching around the
    patch this fixture just installed.
    """
    from newspulse.web.routes import advisory

    original = advisory._run_outreach
    monkeypatch.setattr(advisory, "_run_outreach", _stub(advisory, "_writing"))
    return original


def _stub(module, lock_name: str):
    """A stand-in worker that releases its lock the way the real one does.

    Both routes acquire before starting the thread and rely on the worker's
    ``finally`` to let go. A stub that only returns leaves the lock held for the
    rest of the process — which is invisible until some later test tries to take
    it and the whole run hangs there instead of failing. Ask how long that took to
    find once.
    """

    def _release(*args, **kwargs):
        try:
            getattr(module, lock_name).release()
        except RuntimeError:  # called without the route having acquired it
            pass

    return _release


@pytest.fixture(autouse=True)
def background_locks_are_free():
    """Fail the test that leaked one, rather than the innocent test that waits."""
    from newspulse.web.routes import advisory

    yield
    for name in ("_drafting", "_writing"):
        assert not getattr(advisory, name).locked(), f"a test left {name} held"


@pytest.fixture(autouse=True)
def no_theme_settling(monkeypatch):
    """Stop the sweep from proposing themes in a test run.

    ``job.run`` now gives a themeless mandate a radar, which means a model call and
    a live search per proposal. Most fixtures here create clients with no themes at
    all, so without this the suite shells out to `claude` and to Google News from
    every test that drives a sweep — measured the hard way: the run stopped
    responding and had to be killed.

    Yields the real function for the tests that are about it.
    """
    from newspulse import themes

    original = themes.settle
    monkeypatch.setattr(themes, "settle", lambda *args, **kwargs: [])
    return original
