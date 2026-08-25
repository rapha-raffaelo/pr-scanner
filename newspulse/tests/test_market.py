"""The Marktumfeld view: what the radar saw, what is coming, and who reported it.

Radar articles were stored with nothing linking them to the client whose themes
found them, so the material was in the database and attached to nobody —
unbrowsable, and unusable for ranking outlets. ``topic_hits`` carries that pairing,
and this view is the first thing that reads it.

The load-bearing distinction in the first half: an article that never names the
client is market material, not coverage of the client. It must appear here and
nowhere that counts a mandate's own press.

The second half (SRC-02) is about the three classes a news feed cannot carry, and
its load-bearing rule is DEC-2 C: a market signal does not age out on a timer. A
study stays citable for months; regulation and events leave the page when nothing
is coming, not when a window closes behind them. No test here performs a network
call — the one place the view would make one is the industry probe, and the tests
that reach it inject an answer.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import clients, config, i18n, industry, job, market_sources
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    MarketSignal,
    SignalKind,
    SignalOrigin,
    TopicHit,
)
from newspulse.web.app import create_app, get_db
from newspulse.web.routes import client as client_routes

_NOW = dt.datetime(2026, 7, 30, 9, 0, tzinfo=dt.UTC)

_TEMPLATES = Path(client_routes.__file__).resolve().parents[1] / "templates"


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(factory):
    app = create_app()

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _client(session, name="Arrakis Finance", **over) -> Client:
    obj = Client(
        name=name,
        aliases=[],
        keywords=over.get("keywords", ["Onchain-Liquidität"]),
        alert_topics=over.get("alert_topics", []),
        country="DE",
    )
    session.add(obj)
    session.commit()
    return obj


def _article(session, title, *, source="yellow.com", author=None, age_days=1) -> Article:
    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 100000}",
        source=source,
        author=author,
        published_at=_NOW - dt.timedelta(days=age_days),
        fetched_at=_NOW,
        summary_text="Ein Satz.",
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.commit()
    return article


def _market(session, client_obj, article) -> None:
    session.add(
        TopicHit(article_id=article.id, client_id=client_obj.id, found_at=_NOW)
    )
    session.commit()


def _coverage(session, client_obj, article) -> None:
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client_obj.id,
            summary="s",
            category=Category.PRODUKT,
            relevance_score=6,
            importance_score=6,
            is_alert=False,
        )
    )
    session.commit()


def test_the_market_view_lists_what_the_radar_found(factory, client):
    with factory() as session:
        subject = _client(session)
        _market(session, subject, _article(session, "BitMart schliesst nach neun Jahren"))
        _market(session, subject, _article(session, "BitMEX stellt Betrieb ein"))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "BitMart schliesst nach neun Jahren" in body
    assert "BitMEX stellt Betrieb ein" in body


def test_market_material_stays_out_of_the_clients_own_archive(factory, client):
    """The distinction the whole second table exists for.

    A story that never names the mandate must not inflate the number the agency is
    judged on, so it appears in the market view and nowhere else.
    """
    with factory() as session:
        subject = _client(session)
        _market(session, subject, _article(session, "BitMEX stellt Betrieb ein"))
        subject_id = subject.id

    archive = client.get(f"/client/{subject_id}").text
    market = client.get(f"/client/{subject_id}/market").text

    assert "BitMEX stellt Betrieb ein" not in archive
    assert "BitMEX stellt Betrieb ein" in market


def test_outlets_on_the_subject_are_ranked_by_how_much_they_cover_it(factory, client):
    """The answer to "who should we pitch": whoever writes about the subject."""
    with factory() as session:
        subject = _client(session)
        for i in range(3):
            _market(session, subject, _article(session, f"Krypto {i}", source="CoinDesk"))
        _market(session, subject, _article(session, "Einzeln", source="yellow.com"))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text
    ranking = body.split("Medien im Themenfeld", 1)[1]

    assert ranking.index("CoinDesk") < ranking.index("yellow.com")


def test_outlets_on_the_client_are_a_separate_list(factory, client):
    """Existing relationships and targets mean different things to a consultant,
    so the two counts are never added together."""
    with factory() as session:
        subject = _client(session)
        _coverage(session, subject, _article(session, "Arrakis meldet Zahlen", source="Handelsblatt"))
        _market(session, subject, _article(session, "Markt bewegt sich", source="CoinDesk"))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text
    subject_block = body.split("Medien im Themenfeld", 1)[1].split("Medien über den Mandanten", 1)[0]
    client_block = body.split("Medien über den Mandanten", 1)[1]

    assert "CoinDesk" in subject_block
    assert "Handelsblatt" not in subject_block
    assert "Handelsblatt" in client_block


def test_the_journalist_list_says_when_the_feeds_carried_no_author(factory, client):
    """Measured on the live archive: 22 of 291 articles carry an author, and Google
    News — every radar hit — carries none. A padded list would be worse than an
    empty one, because pitching someone who does not cover the beat costs a
    relationship."""
    with factory() as session:
        subject = _client(session)
        _market(session, subject, _article(session, "Ohne Autor", author=None))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Kein Feed in diesem Zeitraum hat einen Autor mitgeliefert." in body


def test_a_journalist_is_listed_when_the_feed_did_supply_one(factory, client):
    with factory() as session:
        subject = _client(session)
        _market(session, subject, _article(session, "Mit Autor", source="heise online",
                                           author="Frank Schräer"))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Frank Schräer" in body


def test_a_client_without_themes_is_told_so_rather_than_shown_a_quiet_market(factory, client):
    """Without themes there is no radar, so an empty page here is a configuration
    fact — blaming the market for it would send the reader looking in the wrong
    place."""
    with factory() as session:
        subject = _client(session, keywords=[], alert_topics=[])
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Für diesen Mandanten ist kein Themen-Radar eingerichtet." in body
    assert f'href="/settings?edit={subject_id}"' in body


def test_themes_are_shown_so_the_reader_knows_what_was_searched(factory, client):
    with factory() as session:
        subject = _client(session, keywords=["Onchain-Liquidität"], alert_topics=["Börsenschließung"])
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Onchain-Liquidität" in body
    assert "Börsenschließung" in body


def test_the_tab_is_reachable_from_the_other_client_views(factory, client):
    with factory() as session:
        subject = _client(session)
        subject_id = subject.id

    for path in (f"/client/{subject_id}", f"/client/{subject_id}/map"):
        assert f"/client/{subject_id}/market" in client.get(path).text, path


def test_an_unknown_client_is_a_404(client):
    assert client.get("/client/9999/market").status_code == 404


# --- The three market classes: four sections, and a calendar (SRC-02) ----------


def _signal(session, client_obj, kind, title, **over) -> MarketSignal:
    signal = MarketSignal(
        client_id=client_obj.id,
        kind=kind,
        title=title,
        publisher=over.get("publisher", "Beispiel-Institut"),
        url=over.get("url", f"https://quelle.de/{abs(hash(title)) % 100000}"),
        found_at=over.get("found_at", _NOW),
        published_at=over.get("published_at"),
        effective_at=over.get("effective_at"),
        deadline_at=over.get("deadline_at"),
        summary=over.get("summary", ""),
        origin=over.get("origin", SignalOrigin.KURATIERT),
    )
    session.add(signal)
    session.commit()
    return signal


def _in(days: int) -> dt.datetime:
    """The instant at midday, local, ``days`` calendar days from today.

    Anchored on the *local date* rather than on ``now + timedelta`` so the weeks
    the page counts are a property of the data. An offset built from an instant
    lands on the previous local day whenever the clock crosses a DST boundary or
    the suite runs near midnight, which turns "in 5 Wochen" into "in 4 Wochen"
    twice a year — the kind of failure nobody can reproduce.
    """
    tz = config.local_zone()
    day = dt.datetime.now(tz).date() + dt.timedelta(days=days)
    return dt.datetime.combine(day, dt.time(12, 0), tzinfo=tz).astimezone(dt.UTC)


def _de(days: int) -> str:
    """The same day as :func:`_in`, as the page prints it."""
    return (dt.datetime.now(config.local_zone()).date() + dt.timedelta(days=days)).strftime(
        "%d.%m.%Y"
    )


def _markup(body: str) -> str:
    """The page without its inline ``<style>``.

    Every class this page marks a row with is also *declared* in that block, so a
    bare ``"sig__deadline--soon" in body`` is true whether or not a single row
    carries it — an assertion that can never fail is worse than none.
    """
    return re.sub(r"<style>.*?</style>", "", body, flags=re.S)


# --- Four sections, each with its own empty state ------------------------------


def test_a_class_with_no_signals_shows_its_own_empty_line(factory, client):
    """Not an empty box and not a collapsed section: a reader has to be able to
    tell "nothing is coming" from "this section is not on the page"."""
    with factory() as session:
        subject_id = _client(session).id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Noch keine Studie aus dem Feld dieses Mandanten gefunden." in body
    assert "Für dieses Feld steht derzeit nichts im Regulierungskalender." in body
    assert "Keine Veranstaltung im Feld dieses Mandanten gefunden." in body


def test_the_four_sections_render_independently(factory, client):
    """One class having something says nothing about the other three."""
    with factory() as session:
        subject = _client(session)
        _market(session, subject, _article(session, "BitMEX stellt Betrieb ein"))
        _signal(
            session,
            subject,
            SignalKind.STUDIE,
            "Zahlungsverhalten im Onlinehandel 2026",
            published_at=_NOW - dt.timedelta(days=20),
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "BitMEX stellt Betrieb ein" in body
    assert "Zahlungsverhalten im Onlinehandel 2026" in body
    # The two that have nothing still say so, in their own words.
    assert "Für dieses Feld steht derzeit nichts im Regulierungskalender." in body
    assert "Keine Veranstaltung im Feld dieses Mandanten gefunden." in body


# --- The regulatory calendar ---------------------------------------------------


def test_regulation_is_ordered_by_when_it_lands_soonest_first(factory, client):
    """The whole reason it is a calendar and not a list: what is next comes first,
    regardless of which item the sweep happened to find this morning."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.REGULIERUNG,
            "Meldepflicht tritt in Kraft", effective_at=_in(300),
        )
        _signal(
            session, subject, SignalKind.REGULIERUNG,
            "Konsultation zur Verordnung", effective_at=_in(35),
        )
        subject_id = subject.id

    calendar = client.get(f"/client/{subject_id}/market").text.split(
        "Regulierungskalender", 1
    )[1]

    assert calendar.index("Konsultation zur Verordnung") < calendar.index(
        "Meldepflicht tritt in Kraft"
    )


def test_a_regulatory_row_states_the_weeks_that_are_left(factory, client):
    """"In 5 Wochen" is a different instruction to a consultant than a date in a
    column, which is the entire point of the class."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.REGULIERUNG,
            "Konsultation zur Verordnung", effective_at=_in(35),
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "in 5 Wochen" in body
    assert _de(35) in body


def test_a_regulatory_date_that_has_passed_leaves_the_calendar(factory, client):
    """DEC-2 C: a row goes because nothing is coming, not because a timer ran out."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.REGULIERUNG,
            "Verordnung ist längst in Kraft", effective_at=_in(-3),
        )
        _signal(
            session, subject, SignalKind.REGULIERUNG,
            "Konsultation läuft noch", effective_at=_in(35),
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Verordnung ist längst in Kraft" not in body
    assert "Konsultation läuft noch" in body


def test_an_item_landing_today_is_still_on_the_calendar(factory, client):
    """The boundary the "has passed" rule sits on. A rule taking effect this
    morning is the most actionable row on the page, not the first one to go."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.REGULIERUNG,
            "Meldepflicht gilt ab heute", effective_at=_in(0),
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Meldepflicht gilt ab heute" in body
    assert "heute" in body


def test_a_regulatory_item_whose_date_was_never_read_stays_at_the_end(factory, client):
    """A parser that found no date has not established that nothing is coming.

    Dropping such a row would be the one failure a forward calendar must never
    have — a silent one — so it is kept and labelled instead.
    """
    with factory() as session:
        subject = _client(session)
        _signal(session, subject, SignalKind.REGULIERUNG, "Entwurf ohne Datumsangabe")
        _signal(
            session, subject, SignalKind.REGULIERUNG,
            "Konsultation zur Verordnung", effective_at=_in(35),
        )
        subject_id = subject.id

    calendar = client.get(f"/client/{subject_id}/market").text.split(
        "Regulierungskalender", 1
    )[1]

    assert "kein Datum erkannt" in calendar
    assert calendar.index("Konsultation zur Verordnung") < calendar.index(
        "Entwurf ohne Datumsangabe"
    )


def test_a_consultation_still_open_survives_its_effective_date(factory, client):
    """The two dates mean opposite things — "it now applies to you" and "you may
    still speak" — so a rule already in force whose consultation is still open has
    something coming, and the page counts down to the door rather than dropping it.
    """
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.REGULIERUNG, "Verordnung mit offener Frist",
            effective_at=_in(-10), deadline_at=_in(21),
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Verordnung mit offener Frist" in body
    assert "in 3 Wochen" in body


# --- Events --------------------------------------------------------------------


def test_an_event_shows_its_date_and_its_speaker_deadline(factory, client):
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.VERANSTALTUNG, "Fachtagung Zahlungsverkehr",
            effective_at=_in(120), deadline_at=_in(40),
        )
        subject_id = subject.id

    events = client.get(f"/client/{subject_id}/market").text.split(
        "Veranstaltungen", 1
    )[1]

    assert "Einreichfrist" in events
    assert _de(120) in events, "the event's own date"
    assert _de(40) in events, "the speaker deadline"


def test_a_speaker_deadline_inside_two_weeks_is_marked(factory, client):
    """Two weeks is the shortest lead time in which a statement can still be
    agreed, drafted and sent — inside it the row is an instruction, not a date."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.VERANSTALTUNG, "Kongress mit knapper Frist",
            effective_at=_in(60), deadline_at=_in(9),
        )
        subject_id = subject.id

    body = _markup(client.get(f"/client/{subject_id}/market").text)

    assert "sig__deadline--soon" in body
    # The mark says what it is marked by. "Läuft ab" beside a date nine days out
    # is not something a reader can check against anything.
    assert "läuft in unter 2 Wochen ab" in body


def test_the_countdown_says_which_of_an_events_two_dates_it_counts_to(factory, client):
    """"In 5 Wochen" beside a conference means two entirely different things: five
    weeks to submit a talk, or five weeks until the doors open. A calendar that
    does not say which is not one."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.VERANSTALTUNG, "Fachtagung mit frühem Aufruf",
            effective_at=_in(120), deadline_at=_in(35),
        )
        subject_id = subject.id

    events = _markup(client.get(f"/client/{subject_id}/market").text).split(
        "Veranstaltungen", 1
    )[1]
    countdown = events.split('class="sig__when', 1)[1].split("</div>", 1)[0]

    assert "in 5 Wochen" in countdown, "the nearer of the two dates is what is next"
    assert "Einreichfrist" in countdown, "and it says that is the deadline"
    assert _de(35) in countdown
    assert _de(120) not in countdown


def test_a_deadline_further_out_than_two_weeks_is_not_marked(factory, client):
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.VERANSTALTUNG, "Kongress mit Vorlauf",
            effective_at=_in(200), deadline_at=_in(60),
        )
        subject_id = subject.id

    body = _markup(client.get(f"/client/{subject_id}/market").text)

    assert "Einreichfrist" in body
    assert "sig__deadline--soon" not in body


def test_an_event_without_a_call_for_speakers_shows_no_deadline(factory, client):
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.VERANSTALTUNG, "Messe ohne Programmaufruf",
            effective_at=_in(50),
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Messe ohne Programmaufruf" in body
    assert "Einreichfrist" not in body


# --- Studies -------------------------------------------------------------------


def test_a_study_shows_its_publisher_and_links_out_to_the_source(factory, client):
    """What a study is worth to a consultant is that it can be cited, and a
    citation needs both halves: who published it and where it stands."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.STUDIE, "Zahlungsverhalten im Onlinehandel",
            publisher="Statistisches Bundesamt",
            url="https://destatis.example.de/studie-1",
            summary="Erhoben wurden 4.000 Kaufabbrüche.",
            published_at=_NOW - dt.timedelta(days=30),
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Statistisches Bundesamt" in body
    assert "Erhoben wurden 4.000 Kaufabbrüche." in body
    assert 'href="https://destatis.example.de/studie-1"' in body


def test_a_study_does_not_age_off_the_page(factory, client):
    """DEC-2 C, the half a news feed gets wrong: a study six months old is roughly
    when a consultant wants it, not when it should disappear."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.STUDIE, "Alte, aber zitierfähige Studie",
            published_at=_NOW - dt.timedelta(days=400),
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Alte, aber zitierfähige Studie" in body


def test_studies_are_ordered_newest_publication_first(factory, client):
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.STUDIE, "Ältere Erhebung",
            published_at=_NOW - dt.timedelta(days=200),
        )
        _signal(
            session, subject, SignalKind.STUDIE, "Frische Erhebung",
            published_at=_NOW - dt.timedelta(days=5),
        )
        subject_id = subject.id

    studies = client.get(f"/client/{subject_id}/market").text.split("Studien", 1)[1]

    assert studies.index("Frische Erhebung") < studies.index("Ältere Erhebung")


def test_two_studies_published_the_same_day_do_not_swap_places_between_renders(
    factory, client
):
    """Both orderings on this page are stable sorts, so whatever the database
    happens to return decides every tie. Unordered, two studies published the same
    morning could trade places between two loads of the same page — which reads as
    the list having changed when nothing did."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.STUDIE, "Zuerst gefunden",
            published_at=_NOW - dt.timedelta(days=3),
            found_at=_NOW - dt.timedelta(days=3),
        )
        _signal(
            session, subject, SignalKind.STUDIE, "Heute gefunden",
            published_at=_NOW - dt.timedelta(days=3),
            found_at=_NOW,
        )
        subject_id = subject.id

    renders = [
        client.get(f"/client/{subject_id}/market").text.split("Studien", 1)[1]
        for _ in range(2)
    ]

    for studies in renders:
        assert studies.index("Heute gefunden") < studies.index("Zuerst gefunden")


# --- Provenance (DEC-1 B) ------------------------------------------------------


def test_a_search_found_row_is_marked_as_one_and_a_curated_row_is_not(factory, client):
    """The search half returns things that are not really studies. A reader has to
    be able to judge such a row as one rather than guess from the publisher."""
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.STUDIE, "Aus der Feldsuche",
            origin=SignalOrigin.SUCHE, published_at=_NOW,
        )
        _signal(
            session, subject, SignalKind.STUDIE, "Vom Institut",
            origin=SignalOrigin.KURATIERT, published_at=_NOW - dt.timedelta(days=1),
        )
        subject_id = subject.id

    body = _markup(client.get(f"/client/{subject_id}/market").text)
    searched = body.split("Aus der Feldsuche", 1)[1].split("Vom Institut", 1)[0]
    curated = body.split("Vom Institut", 1)[1].split("</article>", 1)[0]

    assert "sig__origin--suche" in searched
    assert "sig__origin--suche" not in curated
    assert "Kuratiert" in curated


# --- The per-class mute --------------------------------------------------------


def test_a_muted_class_disappears_from_the_page_for_that_client(factory, client):
    with factory() as session:
        subject = _client(session)
        _signal(
            session, subject, SignalKind.VERANSTALTUNG, "Fachtagung Zahlungsverkehr",
            effective_at=_in(30),
        )
        subject_id = subject.id

    client.post(f"/client/{subject_id}/market/veranstaltung/mute", follow_redirects=False)
    body = client.get(f"/client/{subject_id}/market").text

    assert "Fachtagung Zahlungsverkehr" not in body
    # Gone, not collapsed into an empty box that explains itself.
    assert "Keine Veranstaltung im Feld dieses Mandanten gefunden." not in body
    # The other two are untouched.
    assert "Noch keine Studie aus dem Feld dieses Mandanten gefunden." in body


def test_a_muted_class_is_not_fetched_for_that_client_on_the_next_sweep(
    factory, no_market_sweep
):
    """Unlike a muted category, which still arrives and is merely hidden. There is
    no count and no report to stay honest to here, so asking a dozen sources every
    morning for a page this mandate switched off would buy nothing."""
    asked: list[str] = []

    def _fetch(url, since, **kwargs):
        asked.append(url)
        return []

    with factory() as session:
        subject = _client(session)
        subject.muted_signal_kinds = ["regulierung"]
        session.commit()

        no_market_sweep(
            session, [subject], _NOW - dt.timedelta(days=14), _fetch, _NOW
        )

    regulation = {
        source.url
        for source in market_sources.load_sources()
        if source.kind is SignalKind.REGULIERUNG
    }
    assert regulation, "the curated list must still carry a regulatory source"
    assert not (regulation & set(asked)), "a muted class was still fetched"
    assert asked, "the classes that were not muted must still be fetched"


def test_a_muted_class_can_be_brought_back_from_the_same_page(factory, client):
    """A class that is gone from the page and from the sweep has exactly one way
    back, and it has to be where the reader is looking."""
    with factory() as session:
        subject = _client(session)
        subject.muted_signal_kinds = ["studie"]
        session.commit()
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text
    assert f"/client/{subject_id}/market/studie/unmute" in body

    client.post(f"/client/{subject_id}/market/studie/unmute", follow_redirects=False)

    assert "Noch keine Studie aus dem Feld dieses Mandanten gefunden." in client.get(
        f"/client/{subject_id}/market"
    ).text


def test_muting_an_unknown_class_is_a_404_rather_than_a_stored_typo(factory, client):
    with factory() as session:
        subject_id = _client(session).id

    resp = client.post(
        f"/client/{subject_id}/market/erfunden/mute", follow_redirects=False
    )

    assert resp.status_code == 404
    with factory() as session:
        assert session.get(Client, subject_id).muted_signal_kinds == []


def test_one_mandates_mute_does_not_touch_another(factory, client):
    with factory() as session:
        quiet = _client(session, "Arrakis Finance")
        loud = _client(session, "Harkonnen AG")
        _signal(
            session, loud, SignalKind.STUDIE, "Studie für Harkonnen", published_at=_NOW
        )
        quiet_id, loud_id = quiet.id, loud.id

    client.post(f"/client/{quiet_id}/market/studie/mute", follow_redirects=False)

    assert "Studie für Harkonnen" in client.get(f"/client/{loud_id}/market").text


# --- An industry term the press does not write ---------------------------------


def test_an_unusable_field_is_explained_rather_than_left_as_a_quiet_market(
    factory, client
):
    """The measured example is "Beauty Tech": accurate, and almost absent from
    German press text, so the search half of the radar returns nothing at all. The
    curated half still arrives, and without a word the page reads as a quiet market
    rather than as a broken filter."""
    with factory() as session:
        subject = _client(session)
        subject.industry = "Beauty Tech"
        subject.field_usable = False
        subject.field_checked_at = _NOW
        session.commit()
        _signal(
            session, subject, SignalKind.STUDIE, "Aus kuratierter Quelle",
            published_at=_NOW,
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Aus kuratierter Quelle" in body, "the curated half still arrives"
    assert "der Branchenbegriff dieses Mandanten kommt in der deutschen Presse" in body
    assert "keine Branche hinterlegt" not in body, "it has one; it just does not work"


def test_a_working_field_is_not_accused_of_being_unusable(factory, client):
    with factory() as session:
        subject = _client(session)
        subject.industry = "Onlinehandel"
        subject.field_usable = True
        subject.field_checked_at = _NOW
        session.commit()
        _signal(
            session, subject, SignalKind.STUDIE, "Aus kuratierter Quelle",
            published_at=_NOW,
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert (
        "der Branchenbegriff dieses Mandanten kommt in der deutschen Presse" not in body
    )


def test_a_field_that_could_not_be_measured_is_not_accused_of_anything(factory, client):
    """The failure this section exists to prevent, one turn further on.

    ``industry.measure`` records an unreachable search as zero hits, so a probe
    that never completed and a term nobody writes look identical from the number
    alone. Telling an operator to change a word over a rate-limited morning is
    confidently wrong and actionable — worse than the silence it replaces — so an
    unanswered question says nothing.
    """
    with factory() as session:
        subject = _client(session)
        subject.industry = "Onlinehandel"
        subject.field_usable = None  # the probe could not reach the search
        subject.field_checked_at = _NOW
        session.commit()
        _signal(
            session, subject, SignalKind.STUDIE, "Aus kuratierter Quelle",
            published_at=_NOW,
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Aus kuratierter Quelle" in body
    assert "kommt in der deutschen Presse zu selten vor" not in body
    assert "keine Branche hinterlegt" not in body


def test_rendering_the_market_page_never_measures_the_industry(factory, client, monkeypatch):
    """``field_is_usable`` issues one live search per industry term, each with a
    twenty-second feed timeout. A page render is the one place that cost cannot be
    paid — and the mandate it would be paid for on every single load is exactly the
    one this section exists for, whose search half is empty every morning. The
    answer is produced by the sweep and read here."""

    def _never(*args, **kwargs):
        raise AssertionError("the market page measured the industry term")

    monkeypatch.setattr(industry, "field_is_usable", _never)
    monkeypatch.setattr(industry, "measure", _never)
    with factory() as session:
        subject = _client(session)
        subject.industry = "Beauty Tech"
        session.commit()
        _signal(
            session, subject, SignalKind.STUDIE, "Aus kuratierter Quelle",
            published_at=_NOW,
        )
        subject_id = subject.id

    assert client.get(f"/client/{subject_id}/market").status_code == 200


def test_a_search_found_signal_answers_the_question_even_when_it_is_not_rendered(
    factory, client
):
    """The evidence is the stored table, not the rendered list.

    The rows the page shows have been narrowed twice — a muted class is not among
    them, and the calendar has already dropped everything whose dates are behind
    us. A mandate whose only search-found signal is a regulation that has landed
    would otherwise look exactly like one whose search returns nothing, and be
    told its industry term is the reason for a field that demonstrably works.
    """
    with factory() as session:
        subject = _client(session)
        subject.industry = "Beauty Tech"
        subject.field_usable = False
        subject.field_checked_at = _NOW
        session.commit()
        _signal(
            session, subject, SignalKind.REGULIERUNG, "Gilt seit vorletzter Woche",
            origin=SignalOrigin.SUCHE,
            effective_at=_in(-14),
        )
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Gilt seit vorletzter Woche" not in body, "it has landed; it left the calendar"
    assert "kommt in der deutschen Presse zu selten vor" not in body


def test_a_mandate_with_no_industry_at_all_is_told_that_and_not_something_else(
    factory, client
):
    """A different fact, and a different message.

    A mandate that never had an industry and one whose accurate industry the press
    does not write are not the same problem and do not send the reader to the same
    work, so one message covering both would be wrong about whichever case it was
    not written for.
    """
    with factory() as session:
        subject = _client(session)
        subject_id = subject.id

        assert (
            client_routes._field_gap(subject, searched=False)
            is client_routes.FieldGap.UNSET
        )

    body = client.get(f"/client/{subject_id}/market").text

    assert "für diesen Mandanten ist keine Branche hinterlegt" in body
    assert "kommt in der deutschen Presse zu selten vor" not in body


# --- The measurement, on the sweep rather than in the page ----------------------


def _probe_urls(asked: list[tuple[str, str | None]]) -> list[str]:
    """The industry probes among everything a sweep fetched.

    ``industry.measure`` labels its own fetch ``Branchen-Probe``, which is what
    tells a probe apart from the dozen curated market feeds beside it.
    """
    return [url for url, source in asked if source == "Branchen-Probe"]


def _recording_fetch(asked: list[tuple[str, str | None]], *, probe_items: list | None = None):
    def _fetch(url, since, **kwargs):
        asked.append((url, kwargs.get("source")))
        if kwargs.get("source") == "Branchen-Probe":
            return list(probe_items or [])
        return []

    return _fetch


def test_the_sweep_measures_the_industry_term_and_stores_the_answer(
    factory, no_market_sweep
):
    """The page reads this; it never asks the question itself. One live search per
    industry term, each with a twenty-second feed timeout, is a cost a GET cannot
    carry — least of all on the mandates the answer exists for, whose search half
    is empty every morning."""
    asked: list[tuple[str, str | None]] = []
    with factory() as session:
        subject = _client(session)
        subject.industry = "Onlinehandel"
        session.commit()

        no_market_sweep(
            session,
            [subject],
            _NOW - dt.timedelta(days=14),
            _recording_fetch(asked, probe_items=["ein Treffer", "noch einer"]),
            _NOW,
        )
        subject = session.get(Client, subject.id)

        assert _probe_urls(asked), "the term was never measured"
        assert subject.field_usable is True
        assert subject.field_checked_at == _NOW


def test_a_probe_that_could_not_reach_the_search_claims_nothing_about_the_term(
    factory, no_market_sweep
):
    """``industry.measure`` records an unreachable search as zero hits, so on the
    number alone an outage and a word nobody writes are the same thing. Stored as
    ``False`` it would put "Ihr Branchenbegriff kommt zu selten vor" on the page
    over a rate-limited morning, and send an operator off to change a term that
    works. The stamp is still written, so one bad night does not make every later
    sweep re-probe."""
    asked: list[tuple[str, str | None]] = []

    def _fetch(url, since, **kwargs):
        asked.append((url, kwargs.get("source")))
        if kwargs.get("source") == "Branchen-Probe":
            raise RuntimeError("Netzwerk weg")
        return []

    with factory() as session:
        subject = _client(session)
        subject.industry = "Onlinehandel"
        session.commit()

        no_market_sweep(session, [subject], _NOW - dt.timedelta(days=14), _fetch, _NOW)
        subject = session.get(Client, subject.id)

        assert _probe_urls(asked), "the probe was issued"
        assert subject.field_usable is None, "an outage is not a verdict about a word"
        assert subject.field_checked_at == _NOW


def test_a_measured_term_is_not_re_measured_every_morning(factory, no_market_sweep):
    """What the probe measures — whether the German press writes a word at all —
    moves over months. Asking every sweep would spend a live search a day to
    re-learn the same fact."""
    asked: list[tuple[str, str | None]] = []
    with factory() as session:
        subject = _client(session)
        subject.industry = "Onlinehandel"
        subject.field_usable = False
        subject.field_checked_at = _NOW - dt.timedelta(days=3)
        session.commit()

        no_market_sweep(
            session, [subject], _NOW - dt.timedelta(days=14), _recording_fetch(asked), _NOW
        )
        subject = session.get(Client, subject.id)

        assert not _probe_urls(asked), "the term was measured again three days on"
        assert subject.field_usable is False, "and the stored answer is untouched"


def test_a_term_measured_long_enough_ago_is_measured_again(factory, no_market_sweep):
    with factory() as session:
        subject = _client(session)
        subject.industry = "Onlinehandel"
        subject.field_usable = False
        subject.field_checked_at = _NOW - job._FIELD_RECHECK - dt.timedelta(days=1)
        session.commit()
        asked: list[tuple[str, str | None]] = []

        no_market_sweep(
            session,
            [subject],
            _NOW - dt.timedelta(days=14),
            _recording_fetch(asked, probe_items=["a", "b"]),
            _NOW,
        )

        assert _probe_urls(asked)
        assert session.get(Client, subject.id).field_usable is True


def test_a_mandate_with_no_industry_is_not_probed(factory, no_market_sweep):
    """Nothing to measure, and the page has a different message for that case."""
    asked: list[tuple[str, str | None]] = []
    with factory() as session:
        subject = _client(session)

        no_market_sweep(
            session, [subject], _NOW - dt.timedelta(days=14), _recording_fetch(asked), _NOW
        )

        assert not _probe_urls(asked)
        assert session.get(Client, subject.id).field_checked_at is None


def test_changing_the_industry_term_clears_the_verdict_about_the_old_one(factory):
    """The verdict is about a word. Carried over to a new one it would tell an
    operator that the term they just fixed is still the problem."""
    with factory() as session:
        subject = _client(session)
        subject.industry = "Beauty Tech"
        subject.field_usable = False
        subject.field_checked_at = _NOW
        session.commit()

        clients.update_client(session, subject.id, industry="Kosmetik")

        subject = session.get(Client, subject.id)
        assert subject.field_usable is None
        assert subject.field_checked_at is None


def test_a_mandate_that_muted_every_class_costs_the_sweep_nothing(
    factory, no_market_sweep, monkeypatch
):
    """"Not fetched, not merely hidden" has to be true of the work done for a
    class as well as of its feeds. The dedup set is built per mandate before the
    per-class check, so a mandate that has switched all three off was still paying
    that query every morning to hand it to nobody."""
    asked: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        market_sources,
        "already_seen",
        lambda *a, **k: pytest.fail("built a dedup set for a mandate that wants no class"),
    )
    with factory() as session:
        subject = _client(session)
        # An industry term as well, so the industry probe is in scope for the
        # claim: its answer is only ever read above the three sections this
        # mandate does not render.
        subject.industry = "Onlinehandel"
        subject.muted_signal_kinds = [kind.value for kind in SignalKind]
        session.commit()

        written, errors = no_market_sweep(
            session, [subject], _NOW - dt.timedelta(days=14), _recording_fetch(asked), _NOW
        )

        assert asked == [], "a mandate that wants no class was still fetched for"
        assert (written, errors) == (0, [])


# --- Every new German string has an English one --------------------------------


def test_every_german_string_on_the_market_page_is_translated():
    """A page that switches its nav and keeps its section heads German reads as
    broken, which is worse than a page that is wholly in one language."""
    known = set(i18n.known_keys())
    called: set[str] = set()
    for template in (
        _TEMPLATES / "client_market.html",
        _TEMPLATES / "partials" / "market_remaining.html",
    ):
        called |= {
            match[1]
            for match in re.findall(
                r"""t\(\s*("|')(.+?)\1\s*\)""", template.read_text(), re.S
            )
        }

    assert called, "the market templates call t() — the scan must not silently find none"
    assert not sorted(called - known), (
        "German strings on the market page with no English entry in i18n._EN: "
        f"{sorted(called - known)}"
    )
