"""Das Issue-Register (RIS-02): the engine's four disciplines, one by one.

Nothing here reaches a model and nothing reaches the network — the one model
call in the module (:func:`newspulse.issues.link_signals`) is exercised with an
injected ``invoke`` returning canned JSON. The clock is injected everywhere: a
proposal is a statement about days, and a test that let ``datetime.now`` decide
would pass at 14:00 and fail at 00:30.

The four disciplines, each with its own section:

* **DEC-3 A**: the tool proposes, a person opens. A proposal writes nothing; a
  dismissal costs one click and the same repetition stops being offered.
* **DEC-4 B**: the model decides membership and writes the sentence why — and
  an assignment nobody can justify is not stored. That rule is the most
  expensive one here to lose, so it gets its own tests for the empty-reason
  and the said-no cases.
* Wahrscheinlichkeit and Wirkung are *suggested* by arithmetic and *set* by a
  person, with the person on the row.
* Escalation is a handover: the crisis begins where the issue began.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import crisis, issues, reputation
from newspulse.matching import title_hash
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    Issue,
    IssueDismissal,
    IssueSignal,
    IssueStatus,
    MarketSignal,
    ReputationState,
    SignalKind,
    Tonality,
)

#: Fixed, mid-morning Berlin time: the proposal counts *local* days, so a
#: reference near midnight UTC would file seeded coverage on either side of the
#: boundary depending on nothing the test controls.
_NOW = dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.UTC)

#: The repeated matter. Five significant tokens after the stopwords come out;
#: each seeded wording adds one filler token, so any two share five of seven —
#: 0.71 against the clusterer's 0.6 bar. Identical headlines would be collapsed
#: by dedup rather than grouped, which is why each outlet gets its own word.
_MATTER = "Verbraucherschützer rügen Vertragsklauseln bei Solaranbieter Solaris"


# --- Fixtures ---------------------------------------------------------------------


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as open_session:
        yield open_session


@pytest.fixture
def mandate(session) -> Client:
    client = Client(name="Solaris AG", aliases=["Solaris"], industry="Solarenergie")
    session.add(client)
    session.commit()
    return client


def _slug(title: str, source: str) -> str:
    return hashlib.sha1(f"{title}|{source}".encode()).hexdigest()[:12]


def _cover(
    session,
    client: Client,
    *,
    source: str,
    word: str,
    days_ago: float,
    tonality: Tonality = Tonality.NEGATIV,
    importance: int = 6,
) -> Article:
    """One wording of the matter, ``days_ago`` before the injected clock."""
    title = f"{_MATTER} {word}"
    at = _NOW - dt.timedelta(days=days_ago)
    article = Article(
        title=title,
        url=f"https://example.de/{_slug(title, source)}",
        source=source,
        published_at=at,
        fetched_at=at,
        summary_text="Eine kurze Zusammenfassung.",
        title_hash=title_hash(title, source),
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            is_relevant=True,
            summary="Zusammenfassung.",
            category=Category.SONSTIGES,
            relevance_score=7,
            importance_score=importance,
            tonality=tonality,
            analyzed_at=at,
        )
    )
    session.commit()
    return article


def _market(
    session,
    client: Client,
    *,
    word: str,
    published_days_ago: float | None,
) -> MarketSignal:
    """One market signal of the same matter; ``None`` leaves it undated."""
    signal = MarketSignal(
        client_id=client.id,
        kind=SignalKind.REGULIERUNG,
        title=f"{_MATTER} {word}",
        publisher="Bundesnetzagentur",
        url=f"https://behoerde.example.de/{word}",
        found_at=_NOW - dt.timedelta(days=1),
        published_at=(
            _NOW - dt.timedelta(days=published_days_ago)
            if published_days_ago is not None
            else None
        ),
    )
    session.add(signal)
    session.commit()
    return signal


def _two_day_repetition(session, mandate) -> tuple[Article, Article]:
    """The canonical repetition: the same story on two different local days."""
    monday = _cover(session, mandate, source="FAZ", word="offiziell", days_ago=3)
    friday = _cover(session, mandate, source="WDR", word="erneut", days_ago=1)
    return monday, friday


def _opened(session, mandate) -> Issue:
    """An issue a person opened off the canonical repetition."""
    _monday, friday = _two_day_repetition(session, mandate)
    opened = issues.accept(session, mandate, friday, by="lucas", now=_NOW)
    assert opened is not None
    return opened


# --- DEC-3 A: the tool proposes -----------------------------------------------------


def test_a_story_on_two_days_proposes_and_names_the_repetition(session, mandate):
    """Two wordings of one matter on two local days: the offer says so — kind,
    day count, outlet count — because "the tool thinks something is up" is not
    a sentence anyone can accept or refuse."""
    monday, friday = _two_day_repetition(session, mandate)
    offer = issues.propose(session, mandate, now=_NOW)
    assert offer is not None
    assert offer.kind is issues.Repetition.ZWEITER_TAG
    assert offer.days == 2
    assert offer.outlets == 2
    assert set(offer.article_ids) == {monday.id, friday.id}


def test_one_day_is_not_a_repetition(session, mandate):
    """Two outlets on the same day are a wave, not a matter being carried."""
    _cover(session, mandate, source="FAZ", word="offiziell", days_ago=1)
    _cover(session, mandate, source="WDR", word="erneut", days_ago=1)
    assert issues.propose(session, mandate, now=_NOW) is None


def test_a_single_negative_piece_proposes_nothing(session, mandate):
    _cover(session, mandate, source="FAZ", word="offiziell", days_ago=1)
    assert issues.propose(session, mandate, now=_NOW) is None


def test_a_story_plus_a_dated_market_signal_is_a_repetition(session, mandate):
    """The other half of the acceptance: press and the regulatory calendar
    arriving at the same matter, and the offer names the signal."""
    _cover(session, mandate, source="FAZ", word="offiziell", days_ago=1)
    signal = _market(session, mandate, word="Konsultation", published_days_ago=10)
    offer = issues.propose(session, mandate, now=_NOW)
    assert offer is not None
    assert offer.kind is issues.Repetition.MARKTSIGNAL
    assert offer.signal_id == signal.id
    assert "Konsultation" in offer.signal_title


def test_an_undated_market_signal_is_not_a_repetition(session, mandate):
    """"Datiertes Marktsignal" means the matter has a date. When the sweep
    noticed it is a log line, not a date, so it must not stand in for one."""
    _cover(session, mandate, source="FAZ", word="offiziell", days_ago=1)
    _market(session, mandate, word="Konsultation", published_days_ago=None)
    assert issues.propose(session, mandate, now=_NOW) is None


def test_a_proposal_writes_nothing_at_all(session, mandate):
    """DEC-3's core: nothing anywhere changes until a person presses a button."""
    _two_day_repetition(session, mandate)
    assert issues.propose(session, mandate, now=_NOW) is not None
    for table in (Issue, IssueSignal, IssueDismissal):
        assert session.scalar(select(func.count()).select_from(table)) == 0


def test_dismissing_stops_the_same_repetition_being_offered(session, mandate):
    """The one-click false alarm, and it stays costing one click."""
    _monday, friday = _two_day_repetition(session, mandate)
    issues.dismiss(session, mandate, friday, by="lucas", now=_NOW)
    assert issues.propose(session, mandate, now=_NOW) is None
    assert session.scalar(select(func.count()).select_from(Issue)) == 0


def test_dismissing_twice_is_one_dismissal(session, mandate):
    _monday, friday = _two_day_repetition(session, mandate)
    first = issues.dismiss(session, mandate, friday, by="lucas", now=_NOW)
    second = issues.dismiss(session, mandate, friday, by="lucas", now=_NOW)
    assert first.id == second.id
    assert session.scalar(select(func.count()).select_from(IssueDismissal)) == 1


# --- Accepting: the person's click -------------------------------------------------


def test_accept_opens_the_issue_with_every_founding_signal(session, mandate):
    """Accepting attaches the whole repetition, each signal with a stored
    reason: an issue opened with one signal out of two would start life
    understating its own matter."""
    monday, friday = _two_day_repetition(session, mandate)
    opened = issues.accept(session, mandate, friday, by="lucas", now=_NOW)
    assert opened is not None
    assert opened.opened_by == "lucas"
    assert opened.status is IssueStatus.OFFEN
    assert {row.article_id for row in opened.signals} == {monday.id, friday.id}
    assert all(row.reason for row in opened.signals)


def test_the_age_begins_with_the_first_signal_not_the_click(session, mandate):
    """``opened_at`` is the day the matter began — what the register's age and
    an escalated crisis's chronology are both statements about."""
    monday, _friday = _two_day_repetition(session, mandate)
    opened = issues.accept(session, mandate, _friday, by="lucas", now=_NOW)
    assert opened.opened_at == monday.published_at
    assert opened.last_moved_at == _friday.published_at


def test_a_stale_click_opens_nothing(session, mandate):
    """An article that is not part of any standing proposal accepts to None —
    the second tab's click after the first one won, or a dissolved offer."""
    lone = _cover(session, mandate, source="FAZ", word="offiziell", days_ago=1)
    assert issues.accept(session, mandate, lone, by="lucas", now=_NOW) is None
    assert session.scalar(select(func.count()).select_from(Issue)) == 0


def test_an_accepted_repetition_is_not_offered_again(session, mandate):
    """The attached articles are spoken for: the signal belongs on the open
    row, and a second proposal for it would be the duplication this register
    exists to end."""
    _opened(session, mandate)
    assert issues.propose(session, mandate, now=_NOW) is None


# --- DEC-4 B: the reasoned attach ---------------------------------------------------


def test_attach_refuses_an_empty_reason(session, mandate):
    """The acceptance verbatim: eine unbegründbare Zuordnung wird nicht
    gespeichert. Held at the door, not only at the schema."""
    opened = _opened(session, mandate)
    extra = _cover(session, mandate, source="taz", word="scharf", days_ago=0.5)
    before = session.scalar(select(func.count()).select_from(IssueSignal))
    with pytest.raises(ValueError):
        issues.attach(
            session, opened, article=extra, reason="   ", by="lucas", now=_NOW
        )
    assert session.scalar(select(func.count()).select_from(IssueSignal)) == before


def test_attach_stores_the_reason_and_who(session, mandate):
    opened = _opened(session, mandate)
    extra = _cover(session, mandate, source="taz", word="scharf", days_ago=0.5)
    row = issues.attach(
        session,
        opened,
        article=extra,
        reason="Derselbe Vorwurf in neuer Formulierung.",
        by="lucas",
        now=_NOW,
    )
    assert row.reason == "Derselbe Vorwurf in neuer Formulierung."
    assert row.attached_by == "lucas"
    assert opened.last_moved_at == extra.published_at


def _verdict(gehoert_dazu: bool, begruendung: str):
    """An injected model that answers every pair the same way."""

    def _invoke(prompt: str, timeout=None) -> str:
        return json.dumps({"gehoert_dazu": gehoert_dazu, "begruendung": begruendung})

    return _invoke


def test_link_signals_attaches_with_the_models_reason(session, mandate):
    """The model says yes with a sentence: attached, and the row says the
    model hung it there — its reason reads differently from a consultant's."""
    opened = _opened(session, mandate)
    extra = _cover(session, mandate, source="taz", word="scharf", days_ago=0.5)
    count = issues.link_signals(
        session,
        mandate,
        invoke=_verdict(True, "Derselbe Vorwurf, neu formuliert."),
        now=_NOW,
    )
    assert count == 1
    session.refresh(opened)
    row = next(row for row in opened.signals if row.article_id == extra.id)
    assert row.reason == "Derselbe Vorwurf, neu formuliert."
    assert row.attached_by == issues.ATTACHED_BY_MODEL


def test_a_yes_without_a_reason_is_not_stored(session, mandate):
    """The rule DEC-4 came with, at the model boundary: an assignment nobody
    can justify is not evidence of anything."""
    opened = _opened(session, mandate)
    _cover(session, mandate, source="taz", word="scharf", days_ago=0.5)
    before = len(opened.signals)
    assert (
        issues.link_signals(session, mandate, invoke=_verdict(True, "  "), now=_NOW)
        == 0
    )
    session.refresh(opened)
    assert len(opened.signals) == before


def test_a_no_from_the_model_attaches_nothing(session, mandate):
    opened = _opened(session, mandate)
    _cover(session, mandate, source="taz", word="scharf", days_ago=0.5)
    before = len(opened.signals)
    assert (
        issues.link_signals(
            session, mandate, invoke=_verdict(False, "Anderes Thema."), now=_NOW
        )
        == 0
    )
    session.refresh(opened)
    assert len(opened.signals) == before


def test_an_unreadable_verdict_attaches_nothing(session, mandate):
    """A broken answer stores nothing rather than something unexplained."""
    opened = _opened(session, mandate)
    _cover(session, mandate, source="taz", word="scharf", days_ago=0.5)
    before = len(opened.signals)
    assert (
        issues.link_signals(
            session, mandate, invoke=lambda prompt, timeout=None: "kaputt", now=_NOW
        )
        == 0
    )
    session.refresh(opened)
    assert len(opened.signals) == before


def test_an_unrelated_piece_never_reaches_the_model(session, mandate):
    """The mechanics collect the candidates; a piece clustering with nothing of
    the issue's is not a question, so no call is spent on it."""
    _opened(session, mandate)
    other = "Solaris eröffnet neues Werk für Wechselrichter in Freiburg heute"
    at = _NOW - dt.timedelta(hours=6)
    article = Article(
        title=other,
        url="https://example.de/werk",
        source="Badische Zeitung",
        published_at=at,
        fetched_at=at,
        summary_text="",
        title_hash=title_hash(other, "Badische Zeitung"),
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=mandate.id,
            is_relevant=True,
            summary="",
            category=Category.SONSTIGES,
            relevance_score=7,
            importance_score=5,
            tonality=Tonality.NEUTRAL,
            analyzed_at=at,
        )
    )
    session.commit()

    def _explodes(prompt: str, timeout=None) -> str:
        raise AssertionError("the model was asked about an unrelated piece")

    assert issues.link_signals(session, mandate, invoke=_explodes, now=_NOW) == 0


# --- Grading: suggested by arithmetic, set by a person ------------------------------


def test_the_suggestion_is_counted_and_never_stored(session, mandate):
    """Two days of the matter suggest probability 2 — and the row's own values
    stay NULL until a person sets them."""
    opened = _opened(session, mandate)
    suggestion = issues.suggest(opened)
    assert suggestion.days == 2
    assert suggestion.probability == 2
    assert opened.probability is None
    assert opened.impact is None


def test_grade_records_the_value_and_the_person(session, mandate):
    opened = _opened(session, mandate)
    issues.grade(session, opened, by="lucas", probability=4)
    assert opened.probability == 4
    assert opened.probability_set_by == "lucas"
    assert opened.impact is None
    assert opened.impact_set_by == ""


def test_grade_refuses_a_value_off_the_scale(session, mandate):
    """Refused, not clamped: a clamped grade would store a number the person
    did not choose, under their name."""
    opened = _opened(session, mandate)
    with pytest.raises(ValueError):
        issues.grade(session, opened, by="lucas", probability=6)
    assert opened.probability is None


# --- Escalating and closing ---------------------------------------------------------


def test_escalation_hands_the_crisis_the_prehistory(session, mandate):
    """The crisis's chronology begins on the day the first signal arrived, not
    on the day somebody pressed the button — the reason the register comes
    before everything else."""
    opened = _opened(session, mandate)
    declared = issues.escalate(session, opened, by="lucas", now=_NOW)
    assert opened.status is IssueStatus.ESKALIERT
    assert opened.crisis_id == declared.id
    assert crisis.prehistory(session, declared) is opened
    assert crisis.began_at(session, declared) == opened.opened_at
    assert opened.opened_at < declared.declared_at


def test_escalating_twice_is_one_crisis(session, mandate):
    opened = _opened(session, mandate)
    first = issues.escalate(session, opened, by="lucas", now=_NOW)
    second = issues.escalate(session, opened, by="lucas", now=_NOW)
    assert first.id == second.id


def test_an_issue_does_not_escalate_into_an_unrelated_open_crisis(session, mandate):
    """At most one crisis per mandate is open, so declaring beside a standing
    one could only merge two matters — the issue's signals would hang on an
    unrelated chronology as fact. Refused, and the issue stays open."""
    opened = _opened(session, mandate)
    other = _cover(session, mandate, source="dpa", word="anders", days_ago=0.2)
    foreign = crisis.declare(session, mandate, other, by="lucas", now=_NOW)
    with pytest.raises(ValueError):
        issues.escalate(session, opened, by="lucas", now=_NOW)
    assert opened.status is IssueStatus.OFFEN
    assert opened.crisis_id is None
    assert crisis.prehistory(session, foreign) is None


def test_a_closed_issue_does_not_escalate(session, mandate):
    opened = _opened(session, mandate)
    issues.close(session, opened, reason="Erledigt.", by="lucas", now=_NOW)
    with pytest.raises(ValueError):
        issues.escalate(session, opened, by="lucas", now=_NOW)


def test_an_escalated_issue_is_closed_through_its_crisis_not_directly(session, mandate):
    """Closing an escalated row directly would swap the ``eskaliert`` record
    for a ``geschlossen`` pill while the crisis still runs — refused."""
    opened = _opened(session, mandate)
    issues.escalate(session, opened, by="lucas", now=_NOW)
    with pytest.raises(ValueError):
        issues.close(session, opened, reason="Erledigt.", by="lucas", now=_NOW)
    assert opened.status is IssueStatus.ESKALIERT
    assert opened.closed_at is None


def test_closing_requires_a_reason(session, mandate):
    opened = _opened(session, mandate)
    with pytest.raises(ValueError):
        issues.close(session, opened, reason="  ", by="lucas", now=_NOW)
    assert opened.closed_at is None


def test_a_closed_issue_stays_readable_with_its_signals(session, mandate):
    """The register is the memory this feature exists to be: closing keeps the
    row, its reason, and every attached signal."""
    opened = _opened(session, mandate)
    issues.close(session, opened, reason="Thema abgeebbt.", by="lucas", now=_NOW)
    assert opened.status is IssueStatus.GESCHLOSSEN
    assert opened.close_reason == "Thema abgeebbt."
    assert opened.closed_by == "lucas"
    kept = issues.history(session, mandate)
    assert [row.id for row in kept] == [opened.id]
    assert len(kept[0].signals) == 2


# --- The band's floor ---------------------------------------------------------------


def test_an_open_issue_floors_the_reading_at_the_issue_rung(session, mandate):
    """The acceptance verbatim: an open issue lifts the mandate's band state to
    at least Issue — a register row open in the next tab beside a band saying
    ruhig would be the tool disagreeing with itself."""
    session.add(
        Issue(
            client_id=mandate.id,
            title="Vertragsklauseln",
            opened_by="lucas",
            opened_at=_NOW - dt.timedelta(days=5),
            last_moved_at=_NOW - dt.timedelta(days=1),
        )
    )
    session.commit()
    reading = reputation.measure(session, mandate, now=_NOW)
    assert reputation.rank(reading.state) >= reputation.rank(ReputationState.ISSUE)


def test_without_an_open_issue_a_quiet_mandate_stays_quiet(session, mandate):
    """The floor is the register's and only the register's: no coverage and no
    issue is ruhig, exactly as RIS-01 left it."""
    reading = reputation.measure(session, mandate, now=_NOW)
    assert reading.state is ReputationState.RUHIG
