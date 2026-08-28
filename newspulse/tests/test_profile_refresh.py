"""The refresh nobody has to remember: which profiles are due, and what a pass costs.

Two things are load-bearing here and each is a way the feature could quietly ruin
the tool rather than merely fail.

**A person's fact is never overwritten.** ``ClientFact.filled_by`` exists because
a fact the consultant knows from a kick-off call and a fact a model read on an
about page must never be confused. A background pass that overwrote the first
with the second would destroy the more valuable of the two, and nobody would be
watching when it did — so the test that matters most here is the one asserting
the stored facts are byte-identical before and after.

The line between the two is ``may_replace``, not the caller. ``refresh`` still
writes nothing into ``client_facts``: it reads and proposes, and the button on
the profile page is built on exactly that. ``run`` — the unattended daily pass —
then adopts what nobody has to be asked about, which is every field that is empty
or that a previous read filled. A field a person answered keeps its proposal on
the pile as a visible contradiction, and the person decides.

**The due check is a pure function of stored state and an injected clock.** A bug
there means either nothing refreshes or everything does, and both are invisible
until the bill or the silence arrives. Every clock in this file is a value passed
in; nothing patches ``datetime.now``, and nothing needs to.

No test here reaches a model or the network: ``generate`` is injected everywhere,
so the real parsing runs over a canned answer.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config
from newspulse import i18n
from newspulse import profile as profiles
from newspulse import profile_refresh
from newspulse.db import make_engine
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    ClientFact,
    ProfileProposal,
)
from newspulse.web.app import create_app, get_db

# A fixed instant, deliberately years off any wall clock a test run could have, so
# a rule that secretly read ``datetime.now`` would answer differently here.
_NOW = dt.datetime(2031, 3, 5, 6, 10, tzinfo=dt.UTC)
_YESTERDAY = _NOW - dt.timedelta(days=1)


@pytest.fixture
def factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as sess:
        yield sess


@pytest.fixture
def web_factory():
    """One in-memory database the app and the test both see.

    ``StaticPool`` rather than the engine helper above: the app opens its session
    on the request thread, and SQLite's default pooling would hand it a second,
    empty ``:memory:`` database — the page would then render an empty profile and
    every assertion here would pass for the wrong reason.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def web(web_factory):
    app = create_app()

    def _override():
        session = web_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _client(session, name="Qonto", *, checked=None, **over) -> Client:
    client = Client(
        name=name,
        aliases=[],
        industry="Neobank",
        country="DE",
        keywords=[],
        alert_topics=[],
        is_competitor=over.get("is_competitor", False),
        profile_checked_at=checked,
    )
    session.add(client)
    session.commit()
    return client


def _answer(**fields):
    """A ``generate`` that returns what the grounded provider would, with a source."""
    payload = json.dumps({"felder": fields})
    return lambda prompt: (payload, [("https://qonto.com/ueber-uns", "Qonto")])


def _boom(prompt):
    raise RuntimeError("die Suche ist nicht erreichbar")


def _coverage(
    session,
    client,
    *,
    category=Category.PERSONALIE,
    analyzed_at=_YESTERDAY,
    is_alert=False,
    dismissed_at=None,
) -> Analysis:
    """One analysed story about this mandate, filed at a fixed instant."""
    article = Article(
        title=f"Etwas über {client.name} {analyzed_at.isoformat()}",
        url=f"https://ex.de/{client.id}-{analyzed_at.timestamp()}-{category.value}",
        source="Handelsblatt",
        published_at=analyzed_at,
        fetched_at=analyzed_at,
        summary_text=None,
        language="de",
        title_hash=f"h{client.id}{int(analyzed_at.timestamp())}{category.value}",
    )
    session.add(article)
    session.flush()
    analysis = Analysis(
        article_id=article.id,
        client_id=client.id,
        summary="s",
        category=category,
        relevance_score=7,
        importance_score=6,
        is_alert=is_alert,
        analyzed_at=analyzed_at,
        dismissed_at=dismissed_at,
    )
    session.add(analysis)
    session.commit()
    return analysis


# --- Which profiles are due (DEC-1: event-driven, with an age floor) ------------


def test_a_profile_never_checked_is_due(session):
    """The mandate filled at kick-off and never looked at since is the whole
    reason this exists."""
    client = _client(session, checked=None)

    assert profile_refresh.due(session, [client], now=_NOW) == [client]


def test_a_profile_checked_yesterday_with_quiet_coverage_is_not_due(session):
    """Spend the daily budget where the news says something happened."""
    client = _client(session, checked=_YESTERDAY)

    assert profile_refresh.due(session, [client], now=_NOW) == []


def test_sixty_quiet_days_are_enough_on_their_own(session):
    """The age floor under the event triggers: a mandate nobody writes about is
    still looked at twice a year rather than never."""
    client = _client(session, checked=_NOW - profile_refresh.DUE_AFTER)

    assert profile_refresh.due(session, [client], now=_NOW) == [client]


def test_a_day_short_of_the_age_floor_is_not_yet_due(session):
    client = _client(session, checked=_NOW - profile_refresh.DUE_AFTER + dt.timedelta(days=1))

    assert profile_refresh.due(session, [client], now=_NOW) == []


def test_a_personnel_item_since_the_last_check_makes_it_due(session):
    """A departed CEO is the failure this feature is named after."""
    client = _client(session, checked=_NOW - dt.timedelta(days=3))
    _coverage(session, client, category=Category.PERSONALIE, analyzed_at=_YESTERDAY)

    assert profile_refresh.due(session, [client], now=_NOW) == [client]


def test_an_alert_makes_it_due_whatever_its_category(session):
    """An alert is by definition the day something happened."""
    client = _client(session, checked=_NOW - dt.timedelta(days=3))
    _coverage(
        session, client, category=Category.SONSTIGES, analyzed_at=_YESTERDAY, is_alert=True
    )

    assert profile_refresh.due(session, [client], now=_NOW) == [client]


def test_an_ordinary_product_item_does_not_make_it_due(session):
    """Coverage of a company whose profile is still true. Re-researching on it
    would spend the daily budget on nothing."""
    client = _client(session, checked=_NOW - dt.timedelta(days=3))
    _coverage(session, client, category=Category.PRODUKT, analyzed_at=_YESTERDAY)

    assert profile_refresh.due(session, [client], now=_NOW) == []


def test_an_item_from_before_the_last_check_does_not_make_it_due_again(session):
    """Otherwise the same executive change re-triggers a refresh every morning
    for the rest of the mandate."""
    client = _client(session, checked=_NOW - dt.timedelta(days=3))
    _coverage(
        session, client, category=Category.PERSONALIE, analyzed_at=_NOW - dt.timedelta(days=9)
    )

    assert profile_refresh.due(session, [client], now=_NOW) == []


def test_a_dismissed_item_does_not_make_it_due(session):
    """A human said this story is not about the mandate. It is not evidence that
    the mandate's profile moved."""
    client = _client(session, checked=_NOW - dt.timedelta(days=3))
    _coverage(
        session,
        client,
        category=Category.PERSONALIE,
        analyzed_at=_YESTERDAY,
        dismissed_at=_YESTERDAY,
    )

    assert profile_refresh.due(session, [client], now=_NOW) == []


def test_a_competitor_is_never_due(session):
    """A yardstick is monitored to compare its share of the conversation; nothing
    downstream reads its profile."""
    rival = _client(session, name="Trade Republic", checked=None, is_competitor=True)

    assert profile_refresh.due(session, [rival], now=_NOW) == []


def test_the_rule_answers_from_the_injected_clock_and_not_the_wall_clock(session):
    """The same stored state, two clocks, two answers.

    Both instants are years ahead of any wall clock this suite can run under, so a
    rule that quietly read ``datetime.now`` would compute a negative age and call
    the profile fresh in both cases.
    """
    client = _client(session, checked=_NOW - dt.timedelta(days=61))

    assert profile_refresh.due(session, [client], now=_NOW) == [client]
    assert profile_refresh.due(session, [client], now=_NOW - dt.timedelta(days=59)) == []


def test_the_due_list_comes_back_oldest_first(session):
    """A large portfolio drains over days, in the order it aged."""
    middle = _client(session, name="B", checked=_NOW - dt.timedelta(days=70))
    oldest = _client(session, name="C", checked=None)
    newest = _client(session, name="A", checked=_NOW - dt.timedelta(days=61))

    order = profile_refresh.due(session, [middle, oldest, newest], now=_NOW)

    assert [c.name for c in order] == ["C", "B", "A"]


# --- One refresh: it proposes, and it writes nothing ---------------------------


def test_a_refresh_writes_nothing_to_client_facts(session):
    """The rule the whole feature rests on. An automatic pass that overwrote a
    fact the consultant entered by hand would destroy the most valuable data in
    the tool, quietly."""
    client = _client(session)
    profiles.save(session, client, "ceo", "Weiß ich vom Kick-off")
    profiles.save(session, client, "sitz", "Berlin", source_url="https://x.de",
                  filled_by="gemini-2.5-flash")
    before = _facts_snapshot(session, client.id)

    profile_refresh.refresh(
        session, client, now=_NOW,
        generate=_answer(ceo="Jemand ganz anderes", sitz="Paris"),
    )

    assert _facts_snapshot(session, client.id) == before


def _facts_snapshot(session, client_id: int) -> list[tuple]:
    """Every stored fact, field by field, so a changed byte anywhere shows up."""
    return sorted(
        (f.key, f.value, f.source_url, f.source_title, f.filled_by, f.updated_at)
        for f in session.scalars(
            select(ClientFact).where(ClientFact.client_id == client_id)
        ).all()
    )


def test_the_proposals_outlive_the_session_that_made_them(factory):
    """The dict this replaced did not: the 06:10 sweep would find a pile of
    changes and a restart before nine would drop them, silently."""
    with factory() as session:
        client = _client(session)
        client_id = client.id
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(ceo="Alexandre Prot")
        )

    with factory() as fresh:
        stored = profile_refresh.outstanding(fresh, client_id)

    assert [(p.key, p.value) for p in stored] == [("ceo", "Alexandre Prot")]
    assert stored[0].source_url == "https://qonto.com/ueber-uns"


def test_a_second_refresh_replaces_rather_than_stacks(session):
    """Twelve small changes found twice must be twelve proposals, not twenty-four."""
    client = _client(session)
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(ceo="Erster Name"))

    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=1),
        generate=_answer(ceo="Zweiter Name", sitz="Paris"),
    )

    stored = profile_refresh.outstanding(session, client.id)
    assert sorted((p.key, p.value) for p in stored) == [
        ("ceo", "Zweiter Name"),
        ("sitz", "Paris"),
    ]


def test_a_proposal_carries_the_value_it_would_replace(session):
    """"Umsatz: 84 Mio." is not a decision anyone can make without the number it
    is arguing against beside it."""
    client = _client(session)
    profiles.save(session, client, "sitz", "Berlin", filled_by="gemini-2.5-flash")

    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(sitz="Paris"))

    proposal = profile_refresh.outstanding(session, client.id)[0]
    assert proposal.previous_value == "Berlin"
    assert proposal.value == "Paris"


def test_a_value_identical_to_what_is_on_file_is_not_proposed(session):
    """Asking the consultant to confirm that nothing changed is how a review pile
    becomes something he stops opening."""
    client = _client(session)
    profiles.save(session, client, "sitz", "Paris", filled_by="gemini-2.5-flash")

    proposed = profile_refresh.refresh(
        session, client, now=_NOW, generate=_answer(sitz="Paris")
    )

    assert proposed == 0
    assert profile_refresh.outstanding(session, client.id) == []


def test_a_refresh_that_changed_nothing_still_records_the_check(session):
    """"Checked, nothing changed" and "never checked" are different states, and a
    page that cannot tell them apart reports a stale profile as fresh."""
    client = _client(session, checked=None)

    profile_refresh.refresh(session, client, now=_NOW, generate=_answer())

    assert session.get(Client, client.id).profile_checked_at == _NOW


def test_a_failed_research_still_records_the_attempt(session):
    """The same posture ``impulse_checked_at`` takes: the attempt happened."""
    client = _client(session, checked=None)

    with pytest.raises(RuntimeError):
        profile_refresh.refresh(session, client, now=_NOW, generate=_boom)

    assert session.get(Client, client.id).profile_checked_at == _NOW


def test_a_failed_research_records_why_beside_the_check_date(session):
    """A stamp on its own makes a mandate whose research died read as "checked
    today" and quiets its age trigger for sixty days with nothing to show for it
    — the hole ``impulse_note`` exists to close."""
    client = _client(session, checked=None)

    with pytest.raises(RuntimeError):
        profile_refresh.refresh(session, client, now=_NOW, generate=_boom)

    assert "die Suche ist nicht erreichbar" in session.get(Client, client.id).profile_note


def test_a_good_check_clears_the_note_the_broken_one_left(session):
    """A reason that outlives the failure it describes is worse than none: the
    page would keep explaining a problem that is over."""
    client = _client(session, checked=None)
    with pytest.raises(RuntimeError):
        profile_refresh.refresh(session, client, now=_NOW, generate=_boom)

    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=1), generate=_answer(sitz="Paris")
    )

    assert session.get(Client, client.id).profile_note == ""


def test_a_failed_refresh_does_not_consume_the_event_trigger(session):
    """A rate-limited morning must not quiet a CEO change for sixty days.

    The stamp a failed attempt leaves is honest — it happened — but it is not an
    answer, and using it as the watermark buried the personnel item that made the
    mandate due behind a read that never took place. The note the failure leaves
    keeps the mandate due until a read actually succeeds.
    """
    client = _client(session, checked=_NOW - dt.timedelta(days=10))
    _coverage(session, client, category=Category.PERSONALIE, analyzed_at=_YESTERDAY)
    assert profile_refresh.due(session, [client], now=_NOW) == [client]

    assert profile_refresh.run(session, now=_NOW, generate=_boom) == 0

    tomorrow = _NOW + dt.timedelta(days=1)
    assert profile_refresh.due(session, [client], now=tomorrow) == [client], (
        "the CEO change was never read; a 429 must not quiet it for 60 days"
    )


def test_an_empty_answer_does_not_wipe_an_undecided_proposal(session):
    """A thin ``felder`` object is a read that covered nothing, not a finding that
    everything is settled — and an undecided proposal is the one thing a store
    like this must never drop on its own."""
    client = _client(session)
    profile_refresh.refresh(
        session, client, now=_NOW, generate=_answer(ceo="Alexandre Prot")
    )
    assert len(profile_refresh.outstanding(session, client.id)) == 1

    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=61), generate=_answer()
    )

    assert [p.key for p in profile_refresh.outstanding(session, client.id)] == ["ceo"]


def test_a_failed_research_leaves_the_existing_proposals_alone(session):
    """A broken search must not be read as "nothing to propose any more" — that
    would throw away findings nobody has seen yet."""
    client = _client(session)
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(ceo="Alexandre Prot"))

    with pytest.raises(RuntimeError):
        profile_refresh.refresh(
            session, client, now=_NOW + dt.timedelta(days=1), generate=_boom
        )

    assert [p.value for p in profile_refresh.outstanding(session, client.id)] == [
        "Alexandre Prot"
    ]


# --- A pass over the portfolio -------------------------------------------------


def test_a_run_refreshes_at_most_the_configured_cap(session):
    """One refresh is a live search plus a model call. Sixty of them in one
    morning is both a bill and a good way to be rate-limited."""
    cap = config.PROFILE_REFRESH_PER_RUN
    for index in range(cap + 3):
        _client(session, name=f"Mandat {index:02d}", checked=None)

    refreshed = profile_refresh.run(session, now=_NOW, generate=_answer(sitz="Paris"))

    assert refreshed == cap
    # Read off the facts, not the proposal pile: the unattended pass adopts what
    # nobody has to be asked about, so a filled field is what a refreshed mandate
    # now looks like. The pile is where the contradictions stay.
    assert session.scalar(select(func.count()).select_from(ClientFact)) == cap


def test_a_run_takes_the_oldest_due_first(session):
    """So a portfolio too large for one run drains evenly instead of the same two
    mandates being refreshed every morning."""
    _client(session, name="Neu", checked=_NOW - dt.timedelta(days=61))
    _client(session, name="Alt", checked=_NOW - dt.timedelta(days=200))
    _client(session, name="Nie", checked=None)

    profile_refresh.run(session, now=_NOW, limit=2, generate=_answer(sitz="Paris"))

    touched = {
        session.get(Client, f.client_id).name
        for f in session.scalars(select(ClientFact)).all()
    }
    assert touched == {"Nie", "Alt"}


def test_with_nothing_due_the_run_is_a_no_op(session):
    """Not a cheap pass — no pass at all. Nothing is asked and nothing is written."""
    _client(session, checked=_YESTERDAY)

    def _never(prompt):
        pytest.fail("nothing was due; the model must not be asked")

    assert profile_refresh.run(session, now=_NOW, generate=_never) == 0
    assert session.scalar(select(func.count()).select_from(ProfileProposal)) == 0


def test_a_run_with_nothing_due_still_says_so_on_the_run_line(session, caplog):
    """"Nothing was due" and "the pass never ran" producing the same empty log is
    the confusion the count exists to prevent."""
    _client(session, checked=_YESTERDAY)

    with caplog.at_level(logging.INFO, logger="newspulse.profile_refresh"):
        profile_refresh.run(session, now=_NOW, generate=_answer())

    assert "0 of 0 due mandate(s) refreshed" in caplog.text


def test_one_broken_client_does_not_stop_the_rest_of_the_run(session, caplog):
    """A portfolio where one dead website stops the other four from being looked
    at is worse than the one broken profile."""
    _client(session, name="Kaputt", checked=None)
    _client(session, name="Heil", checked=None)
    calls: list[str] = []

    def _one_bad_apple(prompt):
        calls.append(prompt)
        if "Kaputt" in prompt:
            raise RuntimeError("die Suche ist nicht erreichbar")
        return _answer(sitz="Paris")(prompt)

    with caplog.at_level(logging.ERROR, logger="newspulse.profile_refresh"):
        refreshed = profile_refresh.run(session, now=_NOW, generate=_one_bad_apple)

    assert refreshed == 1, "the healthy mandate was still refreshed"
    assert len(calls) == 2, "the broken one did not abort the pass"
    assert "Kaputt" in caplog.text
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_the_facts_of_a_broken_client_are_untouched(session):
    """A failure writes nothing at all, which is the same promise a success makes."""
    client = _client(session, name="Kaputt", checked=None)
    profiles.save(session, client, "ceo", "Weiß ich vom Kick-off")
    before = _facts_snapshot(session, client.id)

    profile_refresh.run(session, now=_NOW, generate=_boom)

    assert _facts_snapshot(session, client.id) == before


# --- The wiring: it has to actually run every morning --------------------------


def test_the_daily_sweep_reaches_the_refresh(session, monkeypatch, no_sweep_profile_refresh):
    """The gap that lets a feature be "built" and still never happen.

    A mandate whose profile was never checked must come out of an ordinary sweep
    with the field filled and a check date on the client. Filled, not merely
    proposed: the unattended pass adopts what nobody has to be asked about, and a
    sweep that only piled up proposals is what left the profile as empty as the
    day the mandate was created.

    Puts back the real helper the suite-wide fixture stubs out — otherwise this
    test would pass against a sweep that had been silently disconnected, which is
    the exact failure it exists to catch.
    """
    from newspulse import job

    monkeypatch.setattr(job, "_refresh_profiles", no_sweep_profile_refresh)
    client = _client(session, checked=None)
    monkeypatch.setattr(
        profiles,
        "research",
        # With a source, because a proposal without one is not stored and not
        # shown: it is a machine asserting something it cannot back up.
        lambda c, *, generate=None: [
            profiles.Proposal(
                key="sitz", value="Paris", source_url="https://qonto.com/ueber-uns",
                source_title="Qonto",
            )
        ],
    )

    class _Analyzer:
        def analyze(self, client, articles):
            return []

    report = job.run(
        session,
        feeds=[],  # no fetching: the point is what the sweep does with the archive
        fetch=lambda *a, **k: [],
        analyzer=_Analyzer(),
        now=lambda: _NOW,
    )

    assert report.status.value != "failed"
    stored = profiles.stored(session, client.id)
    assert stored["sitz"].value == "Paris"
    # Under the model's name, never "mensch": the page must not imply somebody
    # vouched for it, and a hand-filled value would forbid the next correction.
    assert stored["sitz"].filled_by != profiles.BY_HAND
    assert profile_refresh.outstanding(session, client.id) == []
    assert session.get(Client, client.id).profile_checked_at == _NOW


def test_a_broken_refresh_never_fails_the_sweep(
    session, monkeypatch, caplog, no_sweep_profile_refresh
):
    """A stale profile is one mandate's problem. A failed sweep is the whole
    portfolio's, and the second must never be caused by the first.

    The pass isolates its own per-client failures, so what is tested here is the
    outer guard: something the pass itself could not survive — a database error,
    a bug — reaching the sweep.
    """
    from newspulse import job

    _client(session, checked=None)

    def _explode(*args, **kwargs):
        raise RuntimeError("die Datenbank ist weg")

    monkeypatch.setattr(job.profile_refresh, "run", _explode)

    with caplog.at_level(logging.ERROR, logger="newspulse.job"):
        assert no_sweep_profile_refresh(session, _NOW) == 0

    assert "profile refresh failed" in caplog.text
    # And the session is usable afterwards, so the notification and everything
    # else the sweep does after this point still works.
    assert session.scalar(select(func.count()).select_from(Client)) == 1


# --- The review surface: what changed, and who decides it ----------------------
#
# Driven through the real page and the real routes with a FastAPI ``TestClient``,
# because every rule below is about what a *reader* can see and press. The
# research is still injected, so nothing here reaches a model or the network.


def _pids(body: str) -> list[int]:
    """The proposal ids the page drew its buttons with.

    Read out of the markup on purpose: the promise this feature makes is that a
    button acts on the rows that were on the screen, so a test that knows the ids
    some other way is not testing the promise.
    """
    return [int(found) for found in re.findall(r'name="pid" value="(\d+)"', body)]


def _field_row(body: str, key: str) -> str:
    """The one row this field owns, cut out of the profile list.

    The whole body is the wrong haystack: a value can appear in a heading, in a
    contradiction further down, or in another field entirely, so ``"Berlin" in
    body`` passes on a page whose ``sitz`` row prints nothing at all. This used
    to cut out the offers block, which sat above the form as a list of its own;
    offers are now rendered *into* the field each one is an offer for, so the row
    is the unit — from its opening div to the start of the next one.
    """
    for chunk in body.split('<div class="prof__row')[1:]:
        if f'id="f-{key}"' in chunk:
            return chunk
    raise AssertionError(f"the page draws no row for {key!r}")


def _file_proposal(session, client_id: int, key: str, value: str) -> ProfileProposal:
    """One finding, filed the way the 06:10 sweep files it."""
    row = ProfileProposal(
        client_id=client_id,
        key=key,
        value=value,
        source_url="https://qonto.com/presse",
        source_title="Qonto Presse",
        previous_value="",
        proposed_at=_NOW,
        proposed_by="gemini-2.5-flash",
    )
    session.add(row)
    session.commit()
    return row


def test_a_proposal_shows_the_old_value_the_new_one_and_its_source(web_factory, web):
    """"Sitz: Paris" is not a decision anyone can make. "Berlin, and this page
    says Paris" is."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profiles.save(session, client, "sitz", "Berlin", filled_by="gemini-2.5-flash")
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(sitz="Paris")
        )

    row = _field_row(web.get(f"/client/{client_id}/profil").text, "sitz")

    assert "Berlin" in row, "the value it argues against"
    assert "Paris" in row, "the value it proposes"
    assert "https://qonto.com/ueber-uns" in row, "and the page it was read on"


def test_a_row_argues_against_the_value_the_profile_holds_now(web_factory, web):
    """The row remembers what it was filed against; the page has to show what is
    on file when it is read. A field cleared in between must not leave the review
    block claiming a current value the profile no longer has."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profiles.save(session, client, "sitz", "Berlin", filled_by="gemini-2.5-flash")
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(sitz="Paris")
        )

    # The consultant clears the field through the form, then reviews the pile.
    web.post(f"/client/{client_id}/profil", data={"sitz": ""}, follow_redirects=False)
    row = _field_row(web.get(f"/client/{client_id}/profil").text, "sitz")

    assert "Berlin" not in row, "there is nothing on file to argue against"
    assert "props__was" not in row, "and so nothing to strike through"
    assert "Paris" in row, "the offer itself still stands in the field"


def test_a_proposal_the_profile_caught_up_with_is_not_drawn(web_factory, web):
    """The consultant reads what the web says and types it in himself. The row
    is then a contradiction between Paris and Paris, and the Verwerfen button
    under it would record a "no" against the value he has just entered."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profiles.save(session, client, "sitz", "Berlin", filled_by="gemini-2.5-flash")
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(sitz="Paris")
        )
        profiles.save(session, client, "sitz", "Paris")

    body = web.get(f"/client/{client_id}/profil").text

    assert "Das Netz sagt" not in body, "the web agrees; this is no contradiction"
    assert _pids(body) == [], "and there is nothing left to decide"


def test_an_empty_field_is_not_struck_through(web_factory, web):
    """Line-through on "bisher nicht gefüllt" reads as a claim being withdrawn.
    Nothing is being replaced here — the field is empty."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(sitz="Paris")
        )

    row = _field_row(web.get(f"/client/{client_id}/profil").text, "sitz")

    assert "Paris" in row, "the offer is in the field"
    assert "props__was" not in row, "with nothing struck through above it"


def test_a_proposal_without_a_source_is_not_shown(web_factory, web):
    """A machine asserting something it cannot back up is not a decision anyone
    should be asked to make."""
    with web_factory() as session:
        client_id = _client(session).id
        unsourced = _file_proposal(session, client_id, "ceo", "Jemand Unbelegtes")
        unsourced.source_url = ""
        unsourced.source_title = ""
        session.commit()

    body = web.get(f"/client/{client_id}/profil").text

    assert "Jemand Unbelegtes" not in body


def test_a_sourceless_row_cannot_be_accepted_by_a_posted_id(web_factory, web):
    """The page draws no button for one, but the form body is not the page. A
    value nobody can check must not become a fact because an id said so."""
    with web_factory() as session:
        client_id = _client(session).id
        unsourced = _file_proposal(session, client_id, "ceo", "Jemand Unbelegtes")
        unsourced.source_url = ""
        unsourced.source_title = ""
        session.commit()
        pid = unsourced.id

    web.post(f"/client/{client_id}/profil/accept", data={"pid": [pid]},
             follow_redirects=False)

    with web_factory() as session:
        assert profiles.stored(session, client_id) == {}


def test_a_value_the_model_cannot_back_up_is_never_stored(session):
    """The same rule one layer down, so an unsourced finding cannot pile up
    invisibly in a table nothing renders."""
    client = _client(session)

    def _ungrounded(prompt):
        """A search that answered without citing anything."""
        return json.dumps({"felder": {"sitz": "Paris"}}), []

    assert profile_refresh.refresh(session, client, now=_NOW, generate=_ungrounded) == 0
    assert profile_refresh.outstanding(session, client.id) == []


def test_a_read_that_cites_nothing_says_so_and_stays_due(session):
    """Dropping every unsourced value is right; dropping the whole read in
    silence is not. Without a note the mandate reads as "Heute geprüft" with an
    empty pile, and the age floor keeps it out of the sweep for sixty days on the
    strength of a read that filed nothing."""
    client = _client(session, checked=None)

    def _ungrounded(prompt):
        return json.dumps({"felder": {"sitz": "Paris", "ceo": "A. Prot"}}), []

    assert profile_refresh.refresh(session, client, now=_NOW, generate=_ungrounded) == 0

    assert profiles.stored(session, client.id) == {}, "still writes nothing"
    assert client.profile_note, "the read produced nothing usable and says so"
    assert profile_refresh.due(
        session, [client], now=_NOW + dt.timedelta(days=1)
    ) == [client], "and the question is still open"


def test_accepting_writes_the_value_under_the_humans_name(web_factory, web):
    """The model proposed; the person decided. It is the decision that is worth
    recording, and it is what stops the next refresh proposing over it."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(ceo="Alexandre Prot")
        )

    body = web.get(f"/client/{client_id}/profil").text
    web.post(f"/client/{client_id}/profil/accept", data={"pid": _pids(body)},
             follow_redirects=False)

    with web_factory() as session:
        fact = profiles.stored(session, client_id)["ceo"]
    assert fact.value == "Alexandre Prot"
    assert fact.filled_by == profiles.BY_HAND
    assert fact.source_url == "https://qonto.com/ueber-uns", "the source travels"


def test_an_accepted_value_is_not_proposed_over_by_the_next_refresh(web_factory, web):
    """The DEC-2 consequence of stamping the human: a value he has vouched for is
    contradicted rather than replaced, exactly like one he typed himself."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(ceo="Alexandre Prot")
        )

    body = web.get(f"/client/{client_id}/profil").text
    web.post(f"/client/{client_id}/profil/accept", data={"pid": _pids(body)},
             follow_redirects=False)

    with web_factory() as session:
        client = session.get(Client, client_id)
        profile_refresh.refresh(
            session, client, now=_NOW + dt.timedelta(days=61),
            generate=_answer(ceo="Jemand ganz anderes"),
        )
        facts = profiles.stored(session, client_id)
    assert facts["ceo"].value == "Alexandre Prot", "his answer stands"
    assert not profile_refresh.may_replace(facts, "ceo")
    # And the disagreement is on the page as a contradiction, under the rule.
    body = web.get(f"/client/{client_id}/profil").text
    assert "Jemand ganz anderes" in body
    assert "wird nie überschrieben" in body


def test_the_page_states_the_rule_that_protects_a_hand_filled_field(web_factory, web):
    """A reader who sees no accept button should not have to guess whether that is
    a policy or an oversight."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profiles.save(session, client, "ceo", "Weiß ich vom Kick-off")
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(ceo="Jemand anderes")
        )

    body = web.get(f"/client/{client_id}/profil").text

    assert "Weiß ich vom Kick-off" in body, "his answer is the one that stands"
    assert "Jemand anderes" in body, "and the contradiction is visible, not silent"
    assert "Regel: Eine Angabe von Hand wird nie überschrieben." in body


def test_the_contradiction_block_says_what_discarding_it_costs(web_factory, web):
    """Its only button is permanent: the discarded claim is never reported again.
    A consultant tidying the block away is opting out of a warning about a field
    he may have got wrong, and the page has to say so before he clicks."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profiles.save(session, client, "ceo", "Weiß ich vom Kick-off")
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(ceo="Jemand anderes")
        )

    body = web.get(f"/client/{client_id}/profil").text

    assert "nicht erneut gemeldet" in body

    # And it really is permanent: the same claim, read again, stays off the page.
    web.post(f"/client/{client_id}/profil/discard", data={"pid": _pids(body)},
             follow_redirects=False)
    with web_factory() as session:
        client = session.get(Client, client_id)
        profile_refresh.refresh(
            session, client, now=_NOW + dt.timedelta(days=61),
            generate=_answer(ceo="Jemand anderes"),
        )
        assert profile_refresh.outstanding(session, client_id) == []


def test_accept_all_takes_only_the_rows_that_were_on_the_page(web_factory, web):
    """The sweep runs at 06:10 and the tab has been open since yesterday. A
    finding that arrived after the page was drawn was never read by anyone, and
    accept-all must not write it."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(ceo="Alexandre Prot")
        )

    drawn = _pids(web.get(f"/client/{client_id}/profil").text)
    with web_factory() as session:
        _file_proposal(session, client_id, "umsatz", "84 Mio. (2031)")

    web.post(f"/client/{client_id}/profil/accept", data={"pid": drawn},
             follow_redirects=False)

    with web_factory() as session:
        facts = profiles.stored(session, client_id)
        left = profile_refresh.outstanding(session, client_id)
    assert set(facts) == {"ceo"}, "only what was on the page was written"
    assert [p.key for p in left] == ["umsatz"], "the newcomer is still waiting"


def test_discard_all_leaves_a_proposal_that_arrived_after_the_page(web_factory, web):
    """The same promise in the other direction: a silent sweep is how a finding
    nobody ever saw gets thrown away."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(ceo="Alexandre Prot")
        )

    drawn = _pids(web.get(f"/client/{client_id}/profil").text)
    with web_factory() as session:
        _file_proposal(session, client_id, "umsatz", "84 Mio. (2031)")

    web.post(f"/client/{client_id}/profil/discard", data={"pid": drawn},
             follow_redirects=False)

    with web_factory() as session:
        left = profile_refresh.outstanding(session, client_id)
    assert [p.key for p in left] == ["umsatz"]


def test_a_form_that_names_nothing_discards_nothing(web_factory, web):
    """The old discard cleared the client's whole pile when no field was named,
    which is the sweep this rule exists to prevent."""
    with web_factory() as session:
        client = _client(session)
        client_id = client.id
        profile_refresh.refresh(
            session, client, now=_NOW, generate=_answer(ceo="Alexandre Prot")
        )

    web.post(f"/client/{client_id}/profil/discard", follow_redirects=False)

    with web_factory() as session:
        assert [p.key for p in profile_refresh.outstanding(session, client_id)] == ["ceo"]


# --- A discard is an answer, and the refresh has to remember it ----------------


def test_a_discarded_value_is_not_proposed_again_by_the_next_refresh(session):
    """The web does not change its mind between Tuesday and Wednesday. Without a
    memory of the "no", the same rejected sentence is back every morning."""
    client = _client(session)
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(ceo="Der Falsche"))
    refused = profile_refresh.outstanding(session, client.id)
    profile_refresh.discard(session, client.id, [p.id for p in refused], now=_NOW)

    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=61),
        generate=_answer(ceo="Der Falsche"),
    )

    assert profile_refresh.outstanding(session, client.id) == []


def test_a_different_value_for_a_discarded_field_is_proposed_again(session):
    """The refusal was of a sentence, not of the field. A CEO who really does
    change next month must still reach the page."""
    client = _client(session)
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(ceo="Der Falsche"))
    refused = profile_refresh.outstanding(session, client.id)
    profile_refresh.discard(session, client.id, [p.id for p in refused], now=_NOW)

    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=61),
        generate=_answer(ceo="Die Neue"),
    )

    assert [
        (p.key, p.value) for p in profile_refresh.outstanding(session, client.id)
    ] == [("ceo", "Die Neue")]


def test_a_refusal_outlives_a_different_finding_for_the_same_field(session):
    """The refusals are a list and not a slot. A second wrong CEO must not erase
    the memory of the first one, or the first is back on the page the next time
    a website repeats it."""
    client = _client(session)
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(ceo="Der Falsche"))
    profile_refresh.discard(
        session, client.id,
        [p.id for p in profile_refresh.outstanding(session, client.id)], now=_NOW,
    )
    # A different name arrives, is filed beside the refusal, and is refused too.
    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=61), generate=_answer(ceo="Die Neue")
    )
    profile_refresh.discard(
        session, client.id,
        [p.id for p in profile_refresh.outstanding(session, client.id)], now=_NOW,
    )

    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=122),
        generate=_answer(ceo="Der Falsche"),
    )

    assert profile_refresh.outstanding(session, client.id) == [], (
        "the first no still stands"
    )


def test_refusing_a_row_the_profile_agrees_with_records_no_refusal(session):
    """A "no" against a value the profile itself holds would suppress that
    field's next real correction for good. Verwerfen on such a row still clears
    it; it simply leaves nothing behind to poison the field with."""
    client = _client(session)
    profiles.save(session, client, "sitz", "Berlin", filled_by="gemini-2.5-flash")
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(sitz="Paris"))
    row = profile_refresh.outstanding(session, client.id)[0]
    profiles.save(session, client, "sitz", "Paris")  # he types it in himself

    assert profile_refresh.discard(session, client.id, [row.id], now=_NOW) == 1
    assert session.scalar(select(func.count()).select_from(ProfileProposal)) == 0

    # He corrects himself, and the web repeats what it always said.
    profiles.save(session, client, "sitz", "Berlin")
    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=61), generate=_answer(sitz="Paris")
    )

    assert [p.value for p in profile_refresh.outstanding(session, client.id)] == [
        "Paris"
    ]


def test_clearing_the_fact_a_refusal_argued_against_reopens_the_question(session):
    """A refusal is always said against something: not Bob, the CEO is Anna. The
    moment Anna turns out to be wrong and is cleared, the "no" has nothing left
    to stand on, and a refusal that never expires would lock the field out of the
    web for the life of the mandate."""
    client = _client(session)
    profiles.save(session, client, "ceo", "Anna")
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(ceo="Bob"))
    profile_refresh.discard(
        session, client.id,
        [p.id for p in profile_refresh.outstanding(session, client.id)], now=_NOW,
    )

    profiles.save(session, client, "ceo", "")  # he was wrong about Anna
    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=61), generate=_answer(ceo="Bob")
    )

    assert [p.value for p in profile_refresh.outstanding(session, client.id)] == ["Bob"]


def test_a_proposal_nothing_changed_about_keeps_its_id(session):
    """The review page's buttons carry row ids. A refresh that re-reads the same
    sentence and re-files it under a new id turns every open tab into a page
    whose buttons silently do nothing."""
    client = _client(session)
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(sitz="Paris"))
    drawn = profile_refresh.outstanding(session, client.id)[0].id

    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=61), generate=_answer(sitz="Paris")
    )

    assert [p.id for p in profile_refresh.outstanding(session, client.id)] == [drawn]


def test_discarding_names_the_rows_and_not_the_field(session):
    """A field name means "whatever is proposed for the CEO right now". Between
    the page being drawn and the button being pressed that can be a different
    finding, and sweeping it up under the old one's name discards something
    nobody has read."""
    client = _client(session)
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(ceo="Der Falsche"))
    stale = profile_refresh.outstanding(session, client.id)[0].id
    profile_refresh.refresh(
        session, client, now=_NOW + dt.timedelta(days=61), generate=_answer(ceo="Die Neue")
    )

    assert profile_refresh.discard(session, client.id, [stale], now=_NOW) == 0
    assert [p.value for p in profile_refresh.outstanding(session, client.id)] == [
        "Die Neue"
    ]


def test_an_accepted_row_leaves_no_refusal_behind(session):
    """A "no" recorded against a value the profile now holds would suppress a
    real correction to it later, so an accepted row is deleted and not stamped."""
    client = _client(session)
    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(sitz="Paris"))
    taken = profile_refresh.outstanding(session, client.id)

    profile_refresh.clear(session, client.id, [p.id for p in taken])

    assert session.scalar(select(func.count()).select_from(ProfileProposal)) == 0


# --- How old is this profile ---------------------------------------------------


def test_checked_tells_never_from_today_from_old():
    """Three states, one clock, no wall-clock read anywhere in the answer."""
    never = profiles.checked(None, now=_NOW)
    today = profiles.checked(_NOW - dt.timedelta(hours=3), now=_NOW)
    old = profiles.checked(_NOW - dt.timedelta(days=84), now=_NOW)

    assert never.never and never.days is None
    assert not today.never and today.days == 0 and not today.as_age
    assert old.days == 84 and old.as_age


def test_a_stamp_from_the_future_is_not_a_negative_age():
    """A restored backup or a skewed clock must not print "vor -3 Tagen"."""
    assert profiles.checked(_NOW + dt.timedelta(days=3), now=_NOW).days == 0


def test_the_profile_page_says_a_profile_was_never_checked(web_factory, web):
    """A blank reads as "fine", and a profile nobody has ever re-read is the
    opposite of fine."""
    with web_factory() as session:
        client_id = _client(session, checked=None).id

    body = web.get(f"/client/{client_id}/profil").text

    assert "Noch nie geprüft" in body


def test_the_profile_page_prints_an_old_check_as_an_age(web_factory, web):
    """"12.05.2026" is a number nobody subtracts today's date from."""
    with web_factory() as session:
        client_id = _client(
            session, checked=dt.datetime.now(dt.UTC) - dt.timedelta(days=84)
        ).id

    body = web.get(f"/client/{client_id}/profil").text

    assert "Zuletzt geprüft vor 84 Tagen" in body


def test_the_client_list_carries_the_check_age_too(web_factory, web):
    """The consultant picks the mandate to work on from this screen, so this is
    where a stale profile has to be visible."""
    with web_factory() as session:
        _client(session, name="Alt", checked=dt.datetime.now(dt.UTC) - dt.timedelta(days=84))
        _client(session, name="Nie", checked=None)

    body = web.get("/clients").text

    assert "Zuletzt geprüft vor 84 Tagen" in body
    assert "Noch nie geprüft" in body


def test_every_german_string_on_the_review_pages_is_translated():
    """A half-switched interface reads as broken. The pages this story touches
    are checked by rule, so the next string added to one of them cannot ship
    German-only."""
    from pathlib import Path

    templates = Path(profiles.__file__).parent / "web" / "templates"
    missing = [
        text
        for name in ("client_profile.html", "clients.html", "partials/profile_checked.html")
        for text in re.findall(r't\("([^"]+)"\)', (templates / name).read_text())
        if text not in i18n._EN
    ]

    assert missing == []


def test_every_field_label_a_review_row_prints_is_translated():
    """The regex above only sees literal ``t("...")``. Every review row labels
    itself with ``t(field.label)``, and those labels live in ``profile.FIELDS``
    rather than in the markup, so the sweep walked straight past twelve German
    field names on the English page."""
    assert [f.label for f in profiles.FIELDS if f.label not in i18n._EN] == []


# --- Adopting without being asked -----------------------------------------------
#
# "bitte im profil immer alles automatisch mit KI recherchieren und dann
# einpflegen."
#
# The research already ran every morning. What it produced sat in a pile waiting
# for a click that mostly never came, so the profile every drafting prompt reads
# stayed as empty as the day the mandate was created. ``may_replace`` is the
# whole safety of closing that half, and the second test here is the one that
# matters most in this file.


def test_the_unattended_pass_fills_an_empty_field_without_being_asked(session):
    client = _client(session, checked=None)

    profile_refresh.run(session, now=_NOW, generate=_answer(sitz="Paris"))

    stored = profiles.stored(session, client.id)
    assert stored["sitz"].value == "Paris"
    assert profile_refresh.outstanding(session, client.id) == []


def test_the_unattended_pass_never_writes_over_what_a_person_answered(session):
    """The invariant this whole file is built around, now that a pass writes.

    A fact the consultant typed is never overwritten. It may be contradicted,
    visibly, and he decides — so the proposal stays on the pile rather than
    being adopted or dropped.
    """
    client = _client(session, checked=None)
    profiles.save(session, client, "sitz", "Berlin", filled_by=profiles.BY_HAND)

    profile_refresh.run(session, now=_NOW, generate=_answer(sitz="Paris"))

    stored = profiles.stored(session, client.id)
    assert stored["sitz"].value == "Berlin"
    assert stored["sitz"].filled_by == profiles.BY_HAND
    # Not silently dropped either: the disagreement is the consultant's to settle.
    assert [p.value for p in profile_refresh.outstanding(session, client.id)] == ["Paris"]


def test_an_adopted_value_carries_the_source_it_was_read_from(session):
    """Adopted without a click is not adopted without provenance: the page has to
    be able to say where a value the consultant never saw came from."""
    client = _client(session, checked=None)

    profile_refresh.run(session, now=_NOW, generate=_answer(sitz="Paris"))

    fact = profiles.stored(session, client.id)["sitz"]
    assert fact.source_url == "https://qonto.com/ueber-uns"
    assert fact.source_title == "Qonto"


def test_an_adopted_value_is_written_under_the_model_not_the_person(session):
    """Two things follow, and both are the point: the page does not imply
    somebody vouched for it, and the next read may still correct it. Writing
    these as BY_HAND would freeze the first automatic answer forever."""
    client = _client(session, checked=None)

    profile_refresh.run(session, now=_NOW, generate=_answer(sitz="Paris"))
    fact = profiles.stored(session, client.id)["sitz"]
    assert fact.filled_by != profiles.BY_HAND

    # And the correction lands, rather than piling up behind the first answer.
    session.get(Client, client.id).profile_checked_at = _NOW - dt.timedelta(days=200)
    session.commit()
    profile_refresh.run(
        session, now=_NOW, generate=_answer(sitz="Paris, Frankreich")
    )
    assert profiles.stored(session, client.id)["sitz"].value == "Paris, Frankreich"


def test_the_button_still_only_proposes(session):
    """``refresh`` keeps its contract. The profile page's "mit KI ausfüllen" is
    built on it: a click reads and proposes, and the consultant decides."""
    client = _client(session, checked=None)

    profile_refresh.refresh(session, client, now=_NOW, generate=_answer(sitz="Paris"))

    assert profiles.stored(session, client.id) == {}
    assert [p.value for p in profile_refresh.outstanding(session, client.id)] == ["Paris"]


def test_a_backlog_is_adopted_even_when_nothing_is_due_for_a_read(session):
    """Adopting costs no model call, so it must not wait out the due window.

    Measured in production the day this shipped: 76 outstanding proposals, every
    one of them adoptable, six of seven mandates with 0 of 18 fields filled, and
    every one of them read that same morning — so a pass that only adopted what
    it had just read would have left the whole backlog sitting for sixty days.
    """
    client = _client(session, checked=_YESTERDAY)
    profile_refresh.refresh(
        session, client, now=_YESTERDAY, generate=_answer(sitz="Paris")
    )
    assert profile_refresh.outstanding(session, client.id)

    def _never(prompt):
        pytest.fail("nothing was due; the model must not be asked")

    assert profile_refresh.run(session, now=_NOW, generate=_never) == 0

    assert profiles.stored(session, client.id)["sitz"].value == "Paris"
    assert profile_refresh.outstanding(session, client.id) == []
