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
def no_monthly_report_draft(monkeypatch):
    """Stop the sweep from drafting monthly reports in a test run.

    Same reason as theme settling: ``job.run`` now reads last month for every
    mandate that has no report yet, and that is one `claude` call per mandate. It
    also fires or does not fire depending on the *real* calendar day, which is the
    worst kind of test flake — a suite that is green on the eighth and shells out
    to a model on the first.

    Yields the real function for the tests that are about it.
    """
    from newspulse import job

    original = job._draft_reports

    def _stub(session, clients, *, now, generate=None) -> int:
        """The real signature, so a change to it breaks the suite rather than
        production."""
        return 0

    monkeypatch.setattr(job, "_draft_reports", _stub)
    return original


@pytest.fixture(autouse=True)
def brain_composes_the_shipped_blocks(monkeypatch):
    """Compose prompts against the repository's blocks, not against a database.

    ``brain.current()`` resolves the stored overrides in front of the shipped
    text, and it does that by opening its own session — a prompt render has no
    session to be handed one. In a test run that means every generator test would
    create a SQLite file in whatever directory pytest was started from and read a
    table that fixture databases build separately anyway.

    So the override source is pinned to "nothing overridden" here, which is the
    state every test that is not about the brain assumes. The tests that *are*
    about it install a source over the top of this one (``test_brain.py``).

    That leaves ``brain._stored_overrides`` — the only source a running
    installation ever uses — pinned away everywhere, so ``test_brain.py`` has a
    ``live_override_source`` fixture that opts back out of this one, narrowly,
    for the handful of tests that exercise it against a fixture database.
    """
    from newspulse import brain

    monkeypatch.setattr(brain, "_override_source", dict)


@pytest.fixture(autouse=True)
def no_real_mailbox(tmp_path, monkeypatch):
    """Keep the daily sweep away from a mailbox somebody actually connected.

    ``job.run`` reads the replies to released letters, and whether a mailbox is
    connected is answered by a token file beside ``config.DATABASE_PATH`` — which
    defaults to the working directory. On a machine where the app has been run
    for real, every test that drives a sweep would then reach Google. Pointing
    the database at a tmp directory means the sync finds no connection and does
    nothing, which is also the state it has to work in.

    The tests that *are* about the mailbox set the same attribute themselves and
    win, because pytest sets up autouse fixtures of a scope before the ones the
    test asked for by name, and the later ``monkeypatch.setattr`` is the one that
    stands. Both point inside the same per-test ``tmp_path``, so the token file
    is the same file either way — the ordering decides which database name sits
    beside it, not whether the two fixtures agree.
    """
    from newspulse import config

    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "newspulse.db")


@pytest.fixture(autouse=True)
def no_live_profile_research(monkeypatch):
    """Stop the sweep's profile refresh from reaching the web in a test run.

    ``job.run`` now re-researches the mandates whose profile has aged, which is a
    live search plus a model call per mandate. Whether that happens in a test
    would otherwise depend on whether the developer running it happens to have a
    ``GEMINI_API_KEY`` in the shell — the suite would be silent and free on CI and
    would quietly spend money on someone's laptop.

    So the boundary is closed rather than the feature switched off: with an
    injected ``generate`` this is the real function, parsing a canned answer with
    no network anywhere near it, and without one it refuses instead of picking up
    an ambient key.
    """
    from newspulse import profile

    original = profile.research

    def _research(client, *, generate=None):
        if generate is None:
            raise RuntimeError("no research provider configured in the test suite")
        return original(client, generate=generate)

    monkeypatch.setattr(profile, "research", _research)
    return original


@pytest.fixture(autouse=True)
def no_sweep_profile_refresh(monkeypatch):
    """Keep the sweep's profile pass out of the tests that are not about it.

    ``no_live_profile_research`` above closes the network boundary, which is the
    part that must never depend on whose laptop the suite runs on. It leaves the
    pass *running*, though, and that is its own problem: every test that drives
    ``job.run`` walks up to ``config.PROFILE_REFRESH_PER_RUN`` never-checked
    mandates, has each
    one refuse, and logs an ERROR with a traceback per mandate. Dozens of tests
    with nothing to do with profiles then print a wall of stack traces, which is
    how a real failure stops being visible in the output.

    So the boundary stays closed *and* the feature is switched off here. Yields
    the real helper, so the two tests that are about the wiring can put it back
    and assert the sweep genuinely reaches it.
    """
    from newspulse import job

    original = job._refresh_profiles
    monkeypatch.setattr(job, "_refresh_profiles", lambda session, now: 0)
    return original


@pytest.fixture(autouse=True)
def no_market_sweep(monkeypatch):
    """Keep the sweep's market classes out of the tests that are not about them.

    Same reason as the two fixtures below, and the same shape. ``job.run`` now
    fetches studies, regulation and events per mandate — a dozen curated sources
    each — so without this every test that drives a sweep reaches a dozen external
    feeds, and a test that merely pinned which *news* feeds were fetched would be
    asserting against the market list as well.

    Yields the real function, so the tests that are about the wiring can put it
    back and prove the sweep genuinely reaches it.
    """
    from newspulse import job

    original = job._sweep_market

    def _stub(session, clients, since, fetch, now) -> tuple[int, list[str]]:
        """The real signature, so a change to it breaks the suite rather than
        production: ``lambda *a, **k`` would have accepted anything."""
        return 0, []

    monkeypatch.setattr(job, "_sweep_market", _stub)
    return original


@pytest.fixture(autouse=True)
def no_plan_recompute(monkeypatch):
    """Keep the sweep's plan recompute out of the tests that are not about it.

    ``job.run`` now recomputes the editorial plan for every mandate whose weekly
    window is open, and a mandate with any evidenced candidate — a stored future
    signal, a resonant theme, last year's coverage — costs a model call. Whether
    a given fixture crosses that line depends on dates the test never thought
    about, which is the worst kind of flake: a suite that shells out to `claude`
    only when the seeded archive happens to be a year old.

    Yields the real function, so the tests that are about the wiring can put it
    back and prove the sweep genuinely reaches it.
    """
    from newspulse import job

    original = job._recompute_plans

    def _stub(session, clients, *, now) -> int:
        """The real signature, so a change to it breaks the suite rather than
        production: ``lambda *a, **k`` would have accepted anything."""
        return 0

    monkeypatch.setattr(job, "_recompute_plans", _stub)
    return original


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

    def _stub(session, client, *, limit=themes.SETTLE_LIMIT, fetch=None,
              invoke=None, now=None) -> list[str]:
        """The real signature, so a change to it breaks the suite rather than
        production: ``lambda *a, **k`` would have accepted anything."""
        return []

    monkeypatch.setattr(themes, "settle", _stub)
    return original


@pytest.fixture(autouse=True)
def no_industry_settling(monkeypatch):
    """Stop the sweep from classifying industries in a test run.

    The same reason ``no_theme_settling`` exists, found the same way: ``job.run``
    now gives a company without an industry one, and that is a model call plus a
    live search per candidate. Most fixtures here create clients with no industry
    at all, so without this the suite shells out to `claude` and to Google News
    from every test that drives a sweep — measured, again the hard way: the run
    went from 130 seconds to 680.

    Yields the real function for the tests that are about it.
    """
    from newspulse import industry

    original = industry.settle

    def _stub(session, client, *, invoke=None, fetch=None, now=None) -> bool:
        """The real signature, so a change to it breaks the suite rather than
        production: ``lambda *a, **k`` would have accepted anything."""
        return False

    monkeypatch.setattr(industry, "settle", _stub)
    return original
