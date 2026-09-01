"""The fast lane's engine: standing, origin and the window (UHR-04).

Nothing here reaches a model and nothing here reaches the network. Every test
that needs a verdict injects it as a string — which is also the only way to
check the two properties DEC-6 turns on: that a scan without a qualifying
story spends no model call at all, and that a story is weighed exactly once.
A test that let a real model answer could not tell "no call was made" from
"the call happened to fail".

The clock is injected rather than patched, for the reason the PRD's test
strategy gives: a window is thirty-six hours counted from *an* origin, and a
suite that reads the wall clock is a suite whose expiry tests mean something
different at night.

The origin rule is checked against hand-built members rather than against the
clusterer's own output, so "earliest wins, ties go to retrieval order" is
asserted against timestamps a person wrote down and not against itself.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, job, newsjack, notify, profile, stories
from newspulse.analyzer import AnalyzerError
from newspulse.ingest import FeedItem
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    ClientFact,
    NewsjackOpportunity,
    Run,
    Standing,
    TopicHit,
)

#: The moment every scan in this file runs at. Fixed, so "the window has 34
#: hours left" is a fact of the fixture and not of the hour the suite ran.
_NOW = dt.datetime(2026, 8, 24, 9, 0, tzinfo=dt.UTC)

#: A wire headline two outlets would carry near-verbatim: enough significant
#: tokens for the clusterer to trust, and no mandate name in it.
_STORY = "Verbraucherzentrale mahnt sechs Solaranbieter wegen Werbeversprechen ab"

#: A second, unrelated market story, so a scan can hold a qualifying story and
#: a non-story side by side.
_OTHER = "Bundesnetzagentur startet Konsultation zu Netzentgelten im Verteilnetz"


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as open_session:
        yield open_session


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    """The suite's standing rule, enforced rather than assumed.

    "Ein Lauf ohne Story über der Medienschwelle verbraucht keinen Modellaufruf"
    is a property under test, not a given, so the real backend is closed off and
    a breach is reported at teardown — where the engine's deliberate catch-all
    around its model call cannot swallow it.
    """
    from newspulse import analyzer

    reached: list[str] = []

    def _forbidden(prompt: str, **kwargs) -> str:
        reached.append(prompt)
        raise AnalyzerError("this suite does not shell out")

    monkeypatch.setattr(analyzer, "invoke_claude_cli", _forbidden)
    yield
    assert not reached, "a test reached the real `claude` CLI"


@pytest.fixture
def mandate(session) -> Client:
    client = Client(
        name="Solarhaus AG",
        aliases=["Solarhaus"],
        industry="Solarenergie",
        keywords=["Photovoltaik"],
        comms_guide="Wir sprechen über Speicher, nie über Preise.",
    )
    session.add(client)
    session.commit()
    return client


# --- Seeding ----------------------------------------------------------------------


def _radar_article(
    session,
    client: Client,
    slug: str,
    *,
    source: str,
    published_at: dt.datetime,
    title: str = _STORY,
) -> Article:
    """One stored radar article for ``client``: an ``articles`` row plus the
    ``topic_hits`` link the radar writes. No analysis — radar material is
    stored unanalysed, exactly as the sweep stores it."""
    article = Article(
        title=title,
        url=f"https://presse.example.de/{slug}",
        source=source,
        published_at=published_at,
        fetched_at=published_at,
        summary_text=None,
        language="de",
        title_hash=slug[:16],
    )
    session.add(article)
    session.commit()
    session.add(
        TopicHit(article_id=article.id, client_id=client.id, found_at=published_at)
    )
    session.commit()
    return article


def _a_story(session, mandate, *, hours_old: float = 4.0) -> tuple[Article, Article]:
    """One qualifying story: the origin at ``hours_old`` and a pickup an hour
    later, on two distinct outlets."""
    origin = _radar_article(
        session,
        mandate,
        "ursprung",
        source="Handelsblatt",
        published_at=_NOW - dt.timedelta(hours=hours_old),
    )
    pickup = _radar_article(
        session,
        mandate,
        "aufgriff",
        source="Börsen-Zeitung",
        published_at=_NOW - dt.timedelta(hours=hours_old - 1),
    )
    return origin, pickup


def _belegt(*_args, **_kwargs) -> str:
    return json.dumps(
        {"standing": "belegt", "reason": "Das Profil nennt eine eigene Fertigung."}
    )


def _recording(reply: str):
    """An ``invoke`` that keeps every prompt it was handed."""
    prompts: list[str] = []

    def invoke(prompt: str, **_kwargs) -> str:
        prompts.append(prompt)
        return reply

    return prompts, invoke


def _rows(session) -> list[NewsjackOpportunity]:
    return list(session.scalars(select(NewsjackOpportunity)).all())


# --- The origin rule --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Member:
    """The minimum a story member needs for the origin question."""

    headline: str
    source: str
    importance: int
    published_at: dt.datetime


def _story_of(*members: _Member) -> stories.Story:
    return stories.Story(
        lead=members[0], members=tuple(members), outlets=("a", "b")
    )


def test_origin_is_the_earliest_member_not_the_first_retrieved():
    late = _Member(_STORY, "Handelsblatt", 0, _NOW - dt.timedelta(hours=1))
    early = _Member(_STORY, "Börsen-Zeitung", 0, _NOW - dt.timedelta(hours=5))
    assert stories.origin(_story_of(late, early)) is early


def test_origin_ties_break_by_retrieval_order_not_chance():
    stamp = _NOW - dt.timedelta(hours=3)
    first_fetched = _Member(_STORY, "Handelsblatt", 0, stamp)
    second_fetched = _Member(_STORY, "Börsen-Zeitung", 0, stamp)
    assert stories.origin(_story_of(first_fetched, second_fetched)) is first_fetched
    # And the same rows in the other retrieval order name the other origin:
    # the tie-break is the order, never the outlet or the hash of anything.
    assert stories.origin(_story_of(second_fetched, first_fetched)) is second_fetched


# --- The gates before the model ---------------------------------------------------


def test_below_the_media_threshold_no_opportunity_is_created(session, mandate):
    _radar_article(
        session, mandate, "eins", source="Handelsblatt",
        published_at=_NOW - dt.timedelta(hours=4),
    )
    _radar_article(
        session, mandate, "zwei", source="Handelsblatt",
        published_at=_NOW - dt.timedelta(hours=3),
    )
    created = newsjack.scan(session, mandate, invoke=_belegt, now=_NOW)
    assert created == []
    assert _rows(session) == []


def test_a_scan_without_a_qualifying_story_costs_no_model_call(session, mandate):
    _radar_article(
        session, mandate, "einzeln", source="Handelsblatt",
        published_at=_NOW - dt.timedelta(hours=4),
    )
    prompts, invoke = _recording(_belegt())
    newsjack.scan(session, mandate, invoke=invoke, now=_NOW)
    assert prompts == []


def test_a_story_naming_the_mandate_is_not_an_opportunity(session, mandate):
    named = "Solarhaus AG von Verbraucherzentrale wegen Werbeversprechen abgemahnt"
    _radar_article(
        session, mandate, "n1", source="Handelsblatt",
        published_at=_NOW - dt.timedelta(hours=4), title=named,
    )
    _radar_article(
        session, mandate, "n2", source="Börsen-Zeitung",
        published_at=_NOW - dt.timedelta(hours=3), title=named,
    )
    prompts, invoke = _recording(_belegt())
    created = newsjack.scan(session, mandate, invoke=invoke, now=_NOW)
    assert created == []
    assert prompts == []


def test_a_story_whose_origin_is_past_the_window_is_skipped_without_a_call(
    session, mandate
):
    """The window runs from the origin piece, so a fresh pickup on a dead story
    changes nothing: the story is through, however lively its tail looks."""
    _radar_article(
        session, mandate, "alt", source="Handelsblatt",
        published_at=_NOW - dt.timedelta(hours=40),
    )
    _radar_article(
        session, mandate, "spät", source="Börsen-Zeitung",
        published_at=_NOW - dt.timedelta(hours=1),
    )
    prompts, invoke = _recording(_belegt())
    created = newsjack.scan(session, mandate, invoke=invoke, now=_NOW)
    assert created == []
    assert prompts == []
    assert _rows(session) == []


# --- The verdicts ------------------------------------------------------------------


def test_belegt_creates_an_opportunity_anchored_at_the_origin(session, mandate):
    origin, _pickup = _a_story(session, mandate)
    created = newsjack.scan(session, mandate, invoke=_belegt, now=_NOW)
    assert len(created) == 1
    row = created[0]
    assert row.standing is Standing.BELEGT
    assert row.article_id == origin.id
    assert row.pickup_count == 2
    assert row.reason == "Das Profil nennt eine eigene Fertigung."
    assert newsjack.open_opportunities(session, mandate, now=_NOW) == [row]


def test_the_window_runs_the_configured_hours_from_the_origin(session, mandate):
    origin, _pickup = _a_story(session, mandate)
    created = newsjack.scan(session, mandate, invoke=_belegt, now=_NOW)
    assert created[0].window_ends_at == origin.published_at + dt.timedelta(hours=36)


def test_the_window_honours_the_environment_variable(session, mandate, monkeypatch):
    monkeypatch.setenv(config.ENV_NEWSJACK_WINDOW_HOURS, "12")
    origin, _pickup = _a_story(session, mandate)
    created = newsjack.scan(session, mandate, invoke=_belegt, now=_NOW)
    assert created[0].window_ends_at == origin.published_at + dt.timedelta(hours=12)


def test_a_window_below_the_floor_is_clamped_not_obeyed(monkeypatch):
    monkeypatch.setenv(config.ENV_NEWSJACK_WINDOW_HOURS, "0")
    assert config.newsjack_window_hours() == 1


@pytest.mark.parametrize("verdict", ["duenn", "keins"])
def test_a_thin_or_absent_standing_is_stored_rejected_with_its_reason(
    session, mandate, verdict
):
    _a_story(session, mandate)
    reply = json.dumps({"standing": verdict, "reason": "Nichts im Profil trägt das."})
    created = newsjack.scan(
        session, mandate, invoke=lambda *a, **k: reply, now=_NOW
    )
    assert len(created) == 1
    assert created[0].standing is Standing(verdict)
    assert created[0].reason == "Nichts im Profil trägt das."
    assert newsjack.open_opportunities(session, mandate, now=_NOW) == []


def test_the_umlaut_spelling_is_folded_onto_the_stored_value(session, mandate):
    _a_story(session, mandate)
    reply = json.dumps({"standing": "dünn", "reason": "Plausibel, aber unbelegt."})
    created = newsjack.scan(session, mandate, invoke=lambda *a, **k: reply, now=_NOW)
    assert created[0].standing is Standing.DUENN


def test_a_fourth_answer_is_refused_rather_than_filed(session, mandate):
    _a_story(session, mandate)
    reply = json.dumps({"standing": "vielleicht", "reason": "?"})
    created = newsjack.scan(session, mandate, invoke=lambda *a, **k: reply, now=_NOW)
    assert created == []
    assert _rows(session) == []


def test_an_unreadable_verdict_stores_nothing_so_the_next_scan_asks_again(
    session, mandate
):
    _a_story(session, mandate)
    first = newsjack.scan(
        session, mandate, invoke=lambda *a, **k: "kein JSON", now=_NOW
    )
    assert first == []
    assert _rows(session) == []
    second = newsjack.scan(session, mandate, invoke=_belegt, now=_NOW)
    assert len(second) == 1


# --- Once per story, and the window's own clock ------------------------------------


def test_a_second_scan_creates_nothing_new_and_pays_no_second_call(session, mandate):
    _a_story(session, mandate)
    prompts, invoke = _recording(_belegt())
    newsjack.scan(session, mandate, invoke=invoke, now=_NOW)
    again = newsjack.scan(
        session, mandate, invoke=invoke, now=_NOW + dt.timedelta(hours=3)
    )
    assert again == []
    assert len(_rows(session)) == 1
    assert len(prompts) == 1


def test_a_late_pickup_does_not_reopen_a_weighed_story(session, mandate):
    """The dedupe is by story, not by origin row alone: a pickup arriving after
    the verdict joins the same cluster and must not buy a second verdict."""
    _a_story(session, mandate)
    prompts, invoke = _recording(_belegt())
    newsjack.scan(session, mandate, invoke=invoke, now=_NOW)
    _radar_article(
        session, mandate, "nachzügler", source="Frankfurter Allgemeine",
        published_at=_NOW + dt.timedelta(hours=1),
    )
    again = newsjack.scan(
        session, mandate, invoke=invoke, now=_NOW + dt.timedelta(hours=2)
    )
    assert again == []
    assert len(prompts) == 1


def test_an_opportunity_expires_without_any_run_having_happened(session, mandate):
    origin, _pickup = _a_story(session, mandate)
    created = newsjack.scan(session, mandate, invoke=_belegt, now=_NOW)
    row = created[0]
    assert newsjack.open_opportunities(session, mandate, now=_NOW) == [row]
    after = origin.published_at + dt.timedelta(hours=36, minutes=1)
    # No scan, no job, no write between the two reads: expiry is a comparison.
    assert newsjack.open_opportunities(session, mandate, now=after) == []
    assert newsjack.is_expired(row, now=after)
    # The row itself stands — expired is not deleted.
    assert len(_rows(session)) == 1


# --- What the standing is checked against ------------------------------------------


def test_the_standing_is_checked_against_profile_guide_and_archive(session, mandate):
    field = profile.FIELDS[0]
    session.add(
        ClientFact(
            client_id=mandate.id,
            key=field.key,
            value="Eigene Speicherfertigung in Sachsen",
        )
    )
    own = Article(
        title="Solarhaus AG erweitert die Speicherfertigung",
        url="https://presse.example.de/eigene",
        source="pv magazine",
        published_at=_NOW - dt.timedelta(days=10),
        fetched_at=_NOW - dt.timedelta(days=10),
        summary_text=None,
        language="de",
        title_hash="eigene",
    )
    session.add(own)
    session.commit()
    session.add(
        Analysis(
            article_id=own.id,
            client_id=mandate.id,
            category=Category.PRODUKT,
            relevance_score=6,
            importance_score=5,
        )
    )
    session.commit()
    _a_story(session, mandate)

    prompts, invoke = _recording(_belegt())
    newsjack.scan(session, mandate, invoke=invoke, now=_NOW)

    assert len(prompts) == 1
    prompt = prompts[0]
    assert "Eigene Speicherfertigung in Sachsen" in prompt  # das Profil
    assert "Wir sprechen über Speicher, nie über Preise." in prompt  # der Guide
    assert "Solarhaus AG erweitert die Speicherfertigung" in prompt  # das Archiv
    assert _STORY in prompt  # und die Geschichte selbst


# --- The light run -----------------------------------------------------------------

#: Every ``run_newsjack`` call here switches the channel off explicitly. Left to
#: default, the run resolves ``NotifyConfig.from_env()`` — and on the machine
#: this tool is built for that means real osascript notifications (or real mail)
#: popping out of a test run. The channel itself is pinned in ``test_notify.py``
#: and ``test_newsjack_view.py`` with injected senders.
_NOTIFY_OFF = notify.NotifyConfig(channel=notify.Channel.OFF)


def _feed_items() -> list[FeedItem]:
    """What the injected radar fetch returns: one story on two outlets, plus an
    unrelated third item so the radar does not look empty and trigger the
    widened re-query.

    The two copies are near-verbatim but not identical, the way real wire copy
    arrives: an *identical* normalized headline is collapsed to one stored
    article by dedup before the clusterer ever sees it, and a story with one
    stored piece is below the media threshold by construction.
    """
    fresh = _NOW - dt.timedelta(hours=4)
    return [
        FeedItem(
            title=_STORY, link="https://a.example.de/1", source="Handelsblatt",
            published_at=fresh, summary=None, language="de",
        ),
        FeedItem(
            title=(
                "Verbraucherzentrale mahnt sechs Solaranbieter wegen "
                "irreführender Werbeversprechen ab"
            ),
            link="https://b.example.de/2", source="Börsen-Zeitung",
            published_at=fresh + dt.timedelta(hours=1), summary=None, language="de",
        ),
        FeedItem(
            title=_OTHER, link="https://c.example.de/3", source="Tagesspiegel",
            published_at=fresh, summary=None, language="de",
        ),
    ]


def test_the_light_run_finds_the_opportunity_off_the_radar_alone(session, mandate):
    report = job.run_newsjack(
        session,
        fetch=lambda *a, **k: _feed_items(),
        invoke=_belegt,
        now=lambda: _NOW,
        notify_config=_NOTIFY_OFF,
    )
    assert report.opportunities == 1
    assert report.rejected == 0
    assert report.errors == []
    assert len(newsjack.open_opportunities(session, mandate, now=_NOW)) == 1


def test_the_light_run_analyses_nothing_and_writes_no_profile_or_run_row(
    session, mandate
):
    job.run_newsjack(
        session,
        fetch=lambda *a, **k: _feed_items(),
        invoke=_belegt,
        now=lambda: _NOW,
        notify_config=_NOTIFY_OFF,
    )
    # No client coverage is analysed: the radar's material is stored unanalysed,
    # exactly as the daily sweep stores it.
    assert session.scalars(select(Analysis)).first() is None
    # No profile data is written, and the impulse bookkeeping is untouched.
    assert session.scalars(select(ClientFact)).first() is None
    assert mandate.impulse_note == ""
    assert mandate.impulse_checked_at is None
    # And no ``runs`` row: the light run must not move the sweep's watermark.
    assert session.scalars(select(Run)).first() is None


def test_the_light_run_skips_a_competitor(session, mandate):
    rival = Client(
        name="Konkurrenz GmbH", keywords=["Photovoltaik"],
        industry="Solarenergie", is_competitor=True,
    )
    session.add(rival)
    session.commit()
    fetched: list[str] = []

    def fetch(url, *args, **kwargs):
        fetched.append(url)
        return []

    report = job.run_newsjack(
        session, fetch=fetch, invoke=_belegt, now=lambda: _NOW,
        notify_config=_NOTIFY_OFF,
    )
    assert report.mandates == 1  # the mandate, never the yardstick
