"""Who to send a draft to — from what the archive knows, and nothing else.

"Ich denke es wäre sehr hilfreich, wenn passende Journalisten für den Inhalt
genannt werden und ggf. auch die Medien, wo sie arbeiten."

The archive can answer that honestly: the byline on the story a draft answers is
the strongest signal there is, and the outlets covering the field without ever
naming the mandate are the pitch list. What it cannot answer is contact details,
and the tests below pin that down as hard as the features — a plausible address
would be used, and would reach the wrong person.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import sessionmaker

from newspulse import pitch
from newspulse.db import make_engine
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Base,
    Category,
    Client,
    TopicHit,
)

_NOW = dt.datetime(2026, 8, 12, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _client(session) -> Client:
    client = Client(
        name="Zalando", aliases=[], industry="Modehandel",
        keywords=["Retouren"], alert_topics=[], country="DE",
    )
    session.add(client)
    session.commit()
    return client


def _article(session, title, *, source="Textilwirtschaft", author=None, days_ago=3):
    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 10**7}",
        source=source,
        author=author,
        published_at=_NOW - dt.timedelta(days=days_ago),
        fetched_at=_NOW - dt.timedelta(days=days_ago),
        summary_text=None,
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.commit()
    return article


def _market(session, client, article):
    session.add(
        TopicHit(article_id=article.id, client_id=client.id, found_at=_NOW)
    )
    session.commit()


def _coverage(session, client, article):
    session.add(
        Analysis(
            article_id=article.id, client_id=client.id, summary="s",
            category=Category.PRODUKT, relevance_score=6,
            importance_score=6, is_alert=False,
        )
    )
    session.commit()


def _angle(session, client, article_ids) -> Angle:
    angle = Angle(
        client_id=client.id, generated_at=_NOW, subject="s", message="m",
        context="c", thesis="t", article_ids=article_ids,
    )
    session.add(angle)
    session.commit()
    return angle


def test_the_byline_on_the_story_the_draft_answers_comes_first(session):
    """Nothing else is close: they are demonstrably on the subject this week."""
    client = _client(session)
    answered = _article(session, "Retouren im Modehandel sinken", author="Maria Berg")
    _market(session, client, answered)
    other = _article(
        session, "Modehandel diskutiert Rücksendungen",
        source="Handelsblatt", author="Jens Kley",
    )
    _market(session, client, other)
    angle = _angle(session, client, [answered.id])

    targets = pitch.targets_for(session, client, angle, now=_NOW)

    assert targets[0].journalist == "Maria Berg"
    assert targets[0].outlet == "Textilwirtschaft"
    assert "bezieht" in targets[0].reason


def test_every_suggestion_carries_the_headline_behind_it(session):
    """Checkable rather than trusted — the rule the drafts themselves follow."""
    client = _client(session)
    answered = _article(session, "Retouren im Modehandel sinken", author="Maria Berg")
    _market(session, client, answered)
    angle = _angle(session, client, [answered.id])

    target = pitch.targets_for(session, client, angle, now=_NOW)[0]

    assert target.evidence == ("Retouren im Modehandel sinken",)


def test_an_outlet_that_never_wrote_about_the_client_is_flagged(session):
    """The point of the exercise: they cover the field and have never named us."""
    client = _client(session)
    for i in range(2):
        item = _article(session, f"Retouren-Analyse {i}", source="InStyle")
        _market(session, client, item)

    targets = pitch.targets_for(session, client, now=_NOW)
    instyle = next(t for t in targets if t.outlet == "InStyle")

    assert instyle.is_new_contact is True
    assert "nie über den Mandanten" in instyle.reason


def test_an_outlet_that_already_covers_the_client_is_not_flagged_as_new(session):
    client = _client(session)
    for i in range(2):
        item = _article(session, f"Retouren-Analyse {i}", source="Handelsblatt")
        _market(session, client, item)
    own = _article(session, "Zalando meldet Zahlen", source="Handelsblatt")
    _coverage(session, client, own)

    targets = pitch.targets_for(session, client, now=_NOW)
    hb = next(t for t in targets if t.outlet == "Handelsblatt")

    assert hb.is_new_contact is False
    assert hb.about_client == 1


def test_a_missing_byline_is_left_empty_rather_than_invented(session):
    """Measured on a real archive: 22 of 291 articles carried an author, and the
    Google News items none. A name filled in to complete the row would be a
    fabrication that gets acted on."""
    client = _client(session)
    for i in range(2):
        item = _article(session, f"Retouren-Analyse {i}", source="InStyle", author=None)
        _market(session, client, item)

    targets = pitch.targets_for(session, client, now=_NOW)

    assert targets
    assert all(t.journalist is None for t in targets)


def test_no_contact_details_are_produced_anywhere(session):
    """The hard line. Feeds carry a byline sometimes and a contact never, and a
    plausible "vorname.nachname@medium.de" is worse than an empty field: it will
    be used, and it will reach the wrong person or nobody."""
    client = _client(session)
    item = _article(session, "Retouren im Modehandel", author="Maria Berg")
    _market(session, client, item)
    angle = _angle(session, client, [item.id])

    rendered = " ".join(
        f"{t.outlet} {t.journalist or ''} {t.reason} {' '.join(t.evidence)}"
        for t in pitch.targets_for(session, client, angle, now=_NOW)
    )

    assert "@" not in rendered


def test_the_same_person_is_not_listed_twice(session):
    """They wrote the answered story and cover the field; that is one suggestion."""
    client = _client(session)
    answered = _article(session, "Retouren sinken", author="Maria Berg")
    _market(session, client, answered)
    again = _article(session, "Retouren zweite Meldung", author="Maria Berg")
    _market(session, client, again)
    angle = _angle(session, client, [answered.id])

    targets = pitch.targets_for(session, client, angle, now=_NOW)
    named = [t for t in targets if t.journalist == "Maria Berg"]

    assert len(named) == 1


def test_a_one_off_outlet_yields_to_a_real_beat(session):
    """A publication that wrote about the field once is not a target while a
    publication that writes about it regularly exists; a pitch aimed at a one-off
    mention wastes the relationship."""
    client = _client(session)
    _market(session, client, _article(session, "Retouren einmal erwähnt", source="Zufall.de"))
    for i in range(2):
        _market(session, client, _article(session, f"Retouren als Dauerthema {i}", source="Fachblatt"))

    outlets = {t.outlet for t in pitch.targets_for(session, client, now=_NOW)}

    assert "Fachblatt" in outlets
    assert "Zufall.de" not in outlets


def test_one_off_outlets_are_offered_when_there_is_nothing_better(session):
    """Measured on a mandate's first day: the radar found 16 market items across
    16 different outlets, one each — Handelsblatt, SZ, ZDFheute, Der Standard
    among them — and the two-article floor removed every one of them. An empty
    pitch list on a day when the answer is obvious to anyone reading the page is
    the rule failing, not the data."""
    client = _client(session)
    for source in ("Zufall.de", "Einzelfall-Magazin"):
        _market(session, client, _article(session, f"Retouren: eine Meldung aus {source}", source=source))

    targets = pitch.targets_for(session, client, now=_NOW)

    assert {t.outlet for t in targets} == {"Zufall.de", "Einzelfall-Magazin"}
    # And it never pretends a single hit is a beat.
    assert all("kein Beat" in t.reason for t in targets)


def test_the_fallback_leads_with_the_better_masthead(session):
    """Alphabetical or arbitrary order would put a games blog above ZDFheute. The
    tier table already knows which is which."""
    client = _client(session)
    for source in ("Caschys Blog", "ZDFheute", "AD HOC NEWS"):
        _market(session, client, _article(session, f"Retouren: eine Meldung aus {source}", source=source))

    outlets = [t.outlet for t in pitch.targets_for(session, client, now=_NOW)]

    assert outlets[0] == "ZDFheute"
    assert outlets[-1] == "AD HOC NEWS"


def test_material_outside_the_window_is_ignored(session):
    client = _client(session)
    for i in range(2):
        old = _article(session, f"Retouren-Rückblick {i}", source="InStyle", days_ago=200)
        _market(session, client, old)

    assert pitch.targets_for(session, client, now=_NOW) == []


def test_the_list_is_short_enough_to_act_on(session):
    """A pitch list of twenty is a research project."""
    client = _client(session)
    for i in range(20):
        item = _article(session, f"Retouren-Meldung {i}", source=f"Medium-{i}", author=f"A{i}")
        _market(session, client, item)

    assert len(pitch.targets_for(session, client, now=_NOW)) <= pitch.MAX_TARGETS


def test_another_mandates_field_is_not_offered_as_this_ones_press(session):
    """"Du musst auch die Journalisten und Medien pro Kunde trennen — aktuell
    sagst du, dass CoinDesk und Cointelegraph über Qonto schreiben."

    A ``topic_hits`` row only says a search for this client surfaced this article,
    and for a while the search could surface anything: an unanchored query put
    Russian crypto legislation into a business-banking mandate's radar, and the
    pitch list then offered CoinDesk, Cointelegraph and Decrypt as the people who
    cover its field. Rows already stored keep that reach, so a recipient has to
    prove the article carries one of *this* mandate's themes.
    """
    client = _client(session)  # themes: Retouren
    on_field = _article(session, "Retouren treiben die Kosten", source="Textilwirtschaft")
    _market(session, client, on_field)
    stray = _article(
        session, "Strategy sells 1,638 Bitcoin to fund dividends",
        source="Cointelegraph", author="Zoltan Vardai",
    )
    _market(session, client, stray)

    outlets = {t.outlet for t in pitch.targets_for(session, client, now=_NOW)}

    assert "Textilwirtschaft" in outlets
    assert "Cointelegraph" not in outlets


def test_the_byline_of_the_answered_story_is_offered_regardless(session):
    """The one exception, and it is deliberate: whoever wrote the very story an
    impulse answers is on the subject this week, whatever their headline happened
    to say. That signal comes from the draft's own citations, not from the radar,
    so the theme filter never sees it."""
    client = _client(session)
    answered = _article(
        session, "BitMEX stellt den Betrieb ein", source="Börsen-Zeitung", author="Jan Roth"
    )
    _market(session, client, answered)
    angle = _angle(session, client, [answered.id])

    named = [t.journalist for t in pitch.targets_for(session, client, angle, now=_NOW)]

    assert "Jan Roth" in named
