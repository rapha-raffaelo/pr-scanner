"""The refresh nobody has to remember: which profiles are due, and what a pass costs.

Two things are load-bearing here and each is a way the feature could quietly ruin
the tool rather than merely fail.

**The refresh never writes.** ``ClientFact.filled_by`` exists because a fact the
consultant knows from a kick-off call and a fact a model read on an about page
must never be confused. A background pass that overwrote the first with the second
would destroy the more valuable of the two, and nobody would be watching when it
did — so the test that matters most here is the one asserting the stored facts are
byte-identical before and after.

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

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from newspulse import config
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
    assert session.scalar(select(func.count()).select_from(ProfileProposal)) == cap


def test_a_run_takes_the_oldest_due_first(session):
    """So a portfolio too large for one run drains evenly instead of the same two
    mandates being refreshed every morning."""
    _client(session, name="Neu", checked=_NOW - dt.timedelta(days=61))
    _client(session, name="Alt", checked=_NOW - dt.timedelta(days=200))
    _client(session, name="Nie", checked=None)

    profile_refresh.run(session, now=_NOW, limit=2, generate=_answer(sitz="Paris"))

    touched = {
        session.get(Client, p.client_id).name
        for p in session.scalars(select(ProfileProposal)).all()
    }
    assert touched == {"Nie", "Alt"}


def test_with_nothing_due_the_run_is_a_no_op(session):
    """Not a cheap pass — no pass at all. Nothing is asked and nothing is written."""
    _client(session, checked=_YESTERDAY)

    def _never(prompt):
        pytest.fail("nothing was due; the model must not be asked")

    assert profile_refresh.run(session, now=_NOW, generate=_never) == 0
    assert session.scalar(select(func.count()).select_from(ProfileProposal)) == 0


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
    with proposals on file and a check date on the client.

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
        lambda c, *, generate=None: [profiles.Proposal(key="sitz", value="Paris")],
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
    assert [p.value for p in profile_refresh.outstanding(session, client.id)] == ["Paris"]
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
