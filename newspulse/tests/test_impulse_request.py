"""Drafting an impulse on request (newspulse.job.draft_impulse).

The sweep drafts only from material that arrived that morning — right for a daily
rhythm, wrong for a person asking the question directly. A mandate whose field was
quiet today may still have plenty worth saying from the past fortnight, and before
this the answer to "give me something to send" was an empty column.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from newspulse import angles, job
from newspulse.db import make_engine
from newspulse.ingest import FeedItem
from newspulse.models import Angle, Article, Base, Client, TopicHit
from newspulse.schemas import AngleDraft

_NOW = dt.datetime(2026, 7, 31, 10, 0, tzinfo=dt.UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _client(session, **over) -> Client:
    client = Client(
        name=over.get("name", "IB-7 Beauty Tech GmbH"),
        aliases=[],
        keywords=over.get("keywords", []),
        alert_topics=over.get("alert_topics", ["KI in der Kosmetik"]),
        country="DE",
    )
    session.add(client)
    session.commit()
    return client


def _stored_market(session, client, title, *, days_ago=10) -> Article:
    """Market material from before today — the case the sweep cannot use."""
    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 100000}",
        source="Cosmetics Business",
        published_at=_NOW - dt.timedelta(days=days_ago),
        fetched_at=_NOW - dt.timedelta(days=days_ago),
        summary_text=None,
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.flush()
    session.add(
        TopicHit(article_id=article.id, client_id=client.id, found_at=_NOW - dt.timedelta(days=days_ago))
    )
    session.commit()
    return article


def _draft(**over):
    def _suggest(sess, cli, material, **_):
        numbered = angles.developments(material)
        draft = AngleDraft(
            worth_sending=over.get("worth_sending", True),
            subject="KI-Rezepturen: die Prüfbarkeit entscheidet",
            message="Zwei Absätze Text." if over.get("worth_sending", True) else "",
            context="c",
            thesis="t",
            evidence=[0] if numbered else [],
        )
        return (draft, numbered) if draft.worth_sending else None

    return _suggest


def _no_fetch(url, since, **_):
    """The radar returns nothing new — the quiet morning this feature is for."""
    return []


def test_a_quiet_morning_still_yields_an_impulse(session, monkeypatch):
    """The whole point: stored material from the past weeks is enough."""
    client = _client(session)
    _stored_market(session, client, "Kosmetikbranche diskutiert KI-Rezepturen")
    monkeypatch.setattr(angles, "suggest", _draft())

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is True
    assert session.scalar(select(func.count()).select_from(Angle)) == 1


def test_the_radar_is_refreshed_before_drafting(session, monkeypatch):
    """A click should pick up what appeared since the last sweep, not only what
    was already stored."""
    client = _client(session)
    seen: list[str] = []

    def _fetch(url, since, *, source=None, **_):
        seen.append(source or "")
        return [
            FeedItem(
                title="Neue Studie zu KI-Wirksamkeit",
                link="https://ex.de/neu",
                source="Global Cosmetics News",
                published_at=_NOW - dt.timedelta(hours=2),
                summary="Ein Satz.",
                language="de",
            )
        ]

    monkeypatch.setattr(angles, "suggest", _draft())

    assert job.draft_impulse(session, client, fetch=_fetch, now=lambda: _NOW) is True
    assert any("Themen-Radar" in s for s in seen)
    # The fresh item is stored and linked, so the market view shows it too.
    assert session.scalar(select(func.count()).select_from(TopicHit)) == 1


def test_a_story_the_archive_already_holds_still_links_to_this_client(session, monkeypatch):
    """The case that made the button useless for a real mandate.

    A search feed lists the same items for days, and a story can already be in the
    archive because another mandate's radar found it first. Dedup therefore drops
    it as "not new" — and the link between story and client used to be written
    only for what dedup kept. So the client's radar stayed empty no matter how
    often the button was pressed, and the page kept answering "the radar has
    collected nothing" while the material sat in the archive.

    The link is an association, not a copy: it must be recorded whether or not
    the article arrived today.
    """
    client = _client(session)
    # Already stored, and attached to nobody — exactly what an earlier sweep for
    # a different mandate leaves behind.
    known = Article(
        title="Kosmetikbranche diskutiert KI-Rezepturen",
        url="https://ex.de/bekannt",
        source="Cosmetics Business",
        published_at=_NOW - dt.timedelta(days=3),
        fetched_at=_NOW - dt.timedelta(days=3),
        summary_text="Ein Satz.",
        language="de",
        title_hash="already-stored",
    )
    session.add(known)
    session.commit()
    assert session.scalar(select(func.count()).select_from(TopicHit)) == 0

    def _fetch(url, since, *, source=None, **_):
        return [
            FeedItem(
                title=known.title,
                link=known.url,
                source=known.source,
                published_at=known.published_at,
                summary="Ein Satz.",
                language="de",
            )
        ]

    monkeypatch.setattr(angles, "suggest", _draft())

    assert job.draft_impulse(session, client, fetch=_fetch, now=lambda: _NOW) is True
    # Linked to this client, and no second copy of the article.
    assert session.scalar(select(func.count()).select_from(TopicHit)) == 1
    assert session.scalar(select(func.count()).select_from(Article)) == 1


def test_an_empty_field_scoped_radar_widens_once_rather_than_going_dark(session, monkeypatch):
    """Scoping the radar to the client's field is what keeps "Wachstum" from
    returning Canada's GDP — but for a mandate whose themes are already narrow
    phrases the AND can intersect to nothing. Measured live: "KI in der Kosmetik"
    AND "Beauty Tech" returns zero. So an empty field-scoped result widens once.
    """
    client = _client(session, keywords=["KI in der Kosmetik"])
    client.industry = "Beauty Tech"
    session.commit()
    asked: list[str] = []

    def _fetch(url, since, *, source=None, **_):
        asked.append(url)
        if "AND" in urllib.parse.unquote_plus(url):
            return []  # the field-scoped query finds nothing
        return [
            FeedItem(
                title="L'Oréal startet Kampagne für nachfüllbare Kosmetik",
                link="https://ex.de/loreal",
                source="Horizont",
                published_at=_NOW - dt.timedelta(days=1),
                summary="Ein Satz.",
                language="de",
            )
        ]

    monkeypatch.setattr(angles, "suggest", _draft())

    assert job.draft_impulse(session, client, fetch=_fetch, now=lambda: _NOW) is True
    assert len(asked) == 2, "the scoped query is tried first, then widened"
    assert "AND" in urllib.parse.unquote_plus(asked[0])
    assert "AND" not in urllib.parse.unquote_plus(asked[1])


def test_a_radar_that_finds_something_in_its_field_is_not_widened(session, monkeypatch):
    """The widening is a fallback, not a second query on every run: doubling
    every radar fetch would double the cost to remove the precision."""
    client = _client(session, keywords=["Kosmetik"])
    client.industry = "Beauty Tech"
    session.commit()
    asked: list[str] = []

    def _fetch(url, since, *, source=None, **_):
        asked.append(url)
        return [
            FeedItem(
                title="Kosmetikbranche diskutiert KI-Rezepturen",
                link="https://ex.de/treffer",
                source="Cosmetics Business",
                published_at=_NOW - dt.timedelta(days=1),
                summary="Ein Satz.",
                language="de",
            )
        ]

    monkeypatch.setattr(angles, "suggest", _draft())

    assert job.draft_impulse(session, client, fetch=_fetch, now=lambda: _NOW) is True
    assert len(asked) == 1


def test_the_clients_own_coverage_is_not_offered_as_market_material(session, monkeypatch):
    """A theme search finds the mandate's own press too.

    Handing that back as "a development to position against" asks for a statement
    about itself. The model saw through it and refused — "both items report on the
    mandate itself; a text about that would be self-promotion, not analysis" — but
    only after a full call had been spent, and the reader was left with an empty
    section. The radar is coverage the client can speak *to*, never coverage *of*
    the client.
    """
    from newspulse.models import Analysis, Category

    client = _client(session)
    own = _stored_market(session, client, "IB-7 eröffnet neues Werk")
    session.add(
        Analysis(
            article_id=own.id,
            client_id=client.id,
            summary="Berichterstattung über den Mandanten.",
            category=Category.SONSTIGES,
            relevance_score=8,
            importance_score=6,
            is_alert=False,
        )
    )
    session.commit()
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask about its own press")
    )
    said: list[str] = []

    assert (
        job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW, note=said.append)
        is False
    )
    # And the reason names the fixable cause rather than shrugging.
    assert "Themen" in said[0]


def test_market_material_survives_beside_the_clients_own_coverage(session, monkeypatch):
    """The filter must remove the mandate's own press, not the radar's findings."""
    from newspulse.models import Analysis, Category

    client = _client(session)
    own = _stored_market(session, client, "IB-7 eröffnet neues Werk")
    session.add(
        Analysis(
            article_id=own.id,
            client_id=client.id,
            summary="Über den Mandanten.",
            category=Category.SONSTIGES,
            relevance_score=8,
            importance_score=6,
            is_alert=False,
        )
    )
    _stored_market(session, client, "Kosmetikbranche diskutiert KI-Rezepturen")
    session.commit()

    seen: list[int] = []

    def _capture(sess, cli, material, **kw):
        seen.append(len(material))
        return _draft()(sess, cli, material, **kw)

    monkeypatch.setattr(angles, "suggest", _capture)

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is True
    assert seen == [1]


def test_a_client_without_themes_yields_nothing_and_costs_nothing(session, monkeypatch):
    """No themes, no radar, no question to ask — and no model call to waste."""
    client = _client(session, keywords=[], alert_topics=[])
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
    )

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is False


def test_no_market_material_is_an_honest_no(session, monkeypatch):
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
    )
    client = _client(session)

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is False


def test_the_model_declining_stores_nothing(session, monkeypatch):
    """Asking on demand does not force an opening into existence: manufactured
    urgency is what the whole design refuses."""
    client = _client(session)
    _stored_market(session, client, "Irgendeine Marktmeldung")
    monkeypatch.setattr(angles, "suggest", _draft(worth_sending=False))

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is False
    assert session.scalar(select(func.count()).select_from(Angle)) == 0


def test_a_refusal_comes_back_with_its_reason(session, monkeypatch):
    """The complaint behind this: "creating an impulse still doesn't work".

    It ran every time. The model simply judged the material too thin — a fair
    call, since the archive stores headlines only and never article bodies — and
    said so in a sentence that went to the log and nowhere else. From the page it
    was indistinguishable from a button that did nothing.
    """
    client = _client(session)
    _stored_market(session, client, "Vereinzelte Wettbewerbermeldung")

    def _refuse(sess, cli, material, **kw):
        kw["note"]("Kein tragfähiger Anlass: vereinzelte Wettbewerber-Entscheidungen.")
        return None

    monkeypatch.setattr(angles, "suggest", _refuse)
    said: list[str] = []

    assert (
        job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW, note=said.append)
        is False
    )
    assert said == ["Kein tragfähiger Anlass: vereinzelte Wettbewerber-Entscheidungen."]


def test_the_reason_names_the_missing_piece_when_there_is_no_material(session, monkeypatch):
    """"Nothing found" and "found nothing worth saying" need different answers:
    the first is a configuration problem the reader can fix."""
    client = _client(session)
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
    )
    said: list[str] = []

    assert (
        job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW, note=said.append)
        is False
    )
    assert len(said) == 1
    assert "Themen-Radar" in said[0]


def test_a_client_without_themes_says_so_rather_than_going_quiet(session, monkeypatch):
    client = _client(session, keywords=[], alert_topics=[])
    said: list[str] = []

    assert (
        job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW, note=said.append)
        is False
    )
    assert "keine Themen hinterlegt" in said[0]


def test_fresh_material_wins_over_old(session, monkeypatch):
    """The window is a preference, not a wall: what is inside it is what the draft
    reads, and the old item stays out of the way."""
    client = _client(session)
    _stored_market(session, client, "Uralte Meldung", days_ago=200)
    _stored_market(session, client, "Meldung von gestern", days_ago=1)
    seen: list[str] = []
    monkeypatch.setattr(
        angles, "suggest",
        lambda s_, c_, material, **k: seen.extend(a.title for a, _ in material) or None,
    )

    job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW)

    assert seen == ["Meldung von gestern"]


def test_material_outside_the_window_is_used_rather_than_nothing(session, monkeypatch):
    """"Vielleicht lösen wir einfach die 90-Tage-Restriktion und schauen immer auf
    die letzten 3 Artikel."

    A mandate in a field that moves twice a year had an empty column for months
    because of a boundary it could not see. A four-month-old development it never
    spoke to beats nothing at all — and the model still refuses stale material, so
    the bar stays with the judgement rather than with the SQL.
    """
    client = _client(session)
    _stored_market(session, client, "Uralte Meldung", days_ago=200)
    seen: list[str] = []
    monkeypatch.setattr(
        angles, "suggest",
        lambda s_, c_, material, **k: seen.extend(a.title for a, _ in material) or None,
    )

    job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW)

    assert seen == ["Uralte Meldung"]


def test_a_radar_that_finds_only_the_client_itself_widens(session, monkeypatch):
    """The gap that kept a young mandate empty for months.

    The field-scoped query returned three items and two were the mandate's own
    launch coverage — non-empty, so the old fallback never fired, and after the
    own-coverage filter there was nothing to draft from. A radar that only finds
    the client is a radar that found nothing, and has to widen the same way an
    empty one does.
    """
    client = _client(session, keywords=["Hautpflege"])
    client.industry = "Beauty Tech"
    session.commit()
    asked: list[str] = []

    def _fetch(url, since, *, source=None, **_):
        asked.append(url)
        if "AND" in urllib.parse.unquote_plus(url):
            # Everything the field-scoped query finds is about the mandate.
            return [
                FeedItem(
                    title="IB-7 Beauty Tech GmbH launcht KI-Hautpflege",
                    link="https://ex.de/eigen",
                    source="cash.at",
                    published_at=_NOW - dt.timedelta(days=2),
                    summary="Ein Satz.",
                    language="de",
                )
            ]
        return [
            FeedItem(
                title="L'Oréal und OpenAI kooperieren bei Hautanalyse",
                link="https://ex.de/loreal",
                source="Horizont",
                published_at=_NOW - dt.timedelta(days=1),
                summary="Ein Satz.",
                language="de",
            )
        ]

    monkeypatch.setattr(angles, "suggest", _draft())

    assert job.draft_impulse(session, client, fetch=_fetch, now=lambda: _NOW) is True
    assert len(asked) == 2, "the scoped query found only the client, so it widened"
    assert "AND" not in urllib.parse.unquote_plus(asked[1])


def test_a_radar_with_real_market_news_is_not_widened(session, monkeypatch):
    """The widening is a fallback, not a second query on every run."""
    client = _client(session, keywords=["Hautpflege"])
    client.industry = "Beauty Tech"
    session.commit()
    asked: list[str] = []

    def _fetch(url, since, *, source=None, **_):
        asked.append(url)
        return [
            FeedItem(
                title="Kosmetikbranche diskutiert KI-Rezepturen",
                link="https://ex.de/markt",
                source="Cosmetics Business",
                published_at=_NOW - dt.timedelta(days=1),
                summary="Ein Satz.",
                language="de",
            )
        ]

    monkeypatch.setattr(angles, "suggest", _draft())

    assert job.draft_impulse(session, client, fetch=_fetch, now=lambda: _NOW) is True
    assert len(asked) == 1
