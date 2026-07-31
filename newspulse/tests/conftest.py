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

    monkeypatch.setattr(advisory, "_run_impulse", lambda *args, **kwargs: None)
