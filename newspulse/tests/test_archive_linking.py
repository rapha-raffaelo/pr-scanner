"""Market material was only ever looked for in one place.

A mandate's market material came exclusively from a Google News search built out
of its themes. Meanwhile the registry's 68 subscribed feeds fetched the German
trade press every morning and left it attached to nobody. Measured on a real
archive: 397 articles in the ninety-day window, **six** of them linked as market
material, while 38 carried "Logistik" in the headline and 18 carried "Mode".

So "das Themen-Radar hat keine Marktmeldung gefunden" could be true and useless
at the same time — the answer was in the client's own archive, already fetched
and already paid for. That is why the message kept reappearing after every fix
upstream of it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from newspulse import job
from newspulse.db import make_engine
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    TopicHit,
)

_NOW = dt.datetime(2026, 8, 4, 7, 0, tzinfo=dt.UTC)
_SINCE = _NOW - dt.timedelta(days=90)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _client(session, **over) -> Client:
    client = Client(
        name=over.get("name", "Zalando"),
        aliases=over.get("aliases", []),
        industry=over.get("industry", "Fashion"),
        country="DE",
        keywords=over.get("keywords", ["Retouren", "Wachstum"]),
        alert_topics=[],
        is_competitor=over.get("is_competitor", False),
    )
    session.add(client)
    session.commit()
    return client


def _article(session, title, *, days_ago=5, summary=None) -> Article:
    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 10**7}",
        source="Textilwirtschaft",
        published_at=_NOW - dt.timedelta(days=days_ago),
        fetched_at=_NOW - dt.timedelta(days=days_ago),
        summary_text=summary,
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.commit()
    return article


def _link(session, clients):
    return job.link_archive_to_themes(session, clients, _SINCE, _NOW)


def test_an_archived_article_in_the_clients_field_becomes_market_material(session):
    """The registry fetched it this morning and nothing connected it to the
    mandate whose field it is."""
    client = _client(session)
    _article(session, "Retouren im Fashion-Handel sinken dank KI")

    assert _link(session, [client]) == 1
    assert len(job.market_material(session, client, _SINCE)) == 1


def test_a_theme_hit_outside_the_field_is_not_linked(session):
    """Measured: matching Zalando's themes against the archive unscoped linked
    129 articles, among them "Wirtschaft in Kanada: Wachstum 3,4 %" and "Apple
    Watch: 21 % Wachstum". The field clause is what makes a theme mean
    something."""
    client = _client(session)
    _article(session, "Wirtschaft in Kanada: Überraschend starkes Wachstum")
    _article(session, "Apple Watch: 21% Wachstum dank Edge AI")

    assert _link(session, [client]) == 0


def test_a_mandate_without_an_industry_is_skipped_rather_than_filled_with_noise(session):
    """A theme like "Wachstum" means nothing without a field to read it in, and
    guessing one would change what the tool finds without saying so."""
    client = _client(session, industry=None)
    _article(session, "Retouren im Fashion-Handel sinken dank KI")

    assert _link(session, [client]) == 0


def test_the_clients_own_coverage_is_never_linked_as_market(session):
    """An article about the mandate belongs in its coverage. Offering it back as
    a development to position against asks for a statement about itself."""
    client = _client(session)
    own = _article(session, "Zalando meldet sinkende Retouren im Fashion-Segment")
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
    session.commit()

    assert _link(session, [client]) == 0


def test_an_article_naming_the_client_is_not_market_even_without_an_analysis(session):
    """The analysis may not have run yet, or may have scored it away — the name
    in the headline is evidence enough that this is not the market talking."""
    client = _client(session)
    _article(session, "Zalando baut Fashion-Retouren um")

    assert _link(session, [client]) == 0


def test_running_twice_links_nothing_twice(session):
    client = _client(session)
    _article(session, "Retouren im Fashion-Handel sinken dank KI")

    assert _link(session, [client]) == 1
    assert _link(session, [client]) == 0
    assert session.scalar(select(func.count()).select_from(TopicHit)) == 1


def test_a_competitor_gets_no_market_material(session):
    """A competitor is monitored to compare its share of the conversation.
    Nobody writes it a positioning, so linking material for it is spend with no
    reader."""
    rival = _client(session, name="H&M", is_competitor=True)
    _article(session, "Retouren im Fashion-Handel sinken dank KI")

    assert _link(session, [rival]) == 0


def test_articles_older_than_the_window_stay_out(session):
    client = _client(session)
    _article(session, "Retouren im Fashion-Handel sinken dank KI", days_ago=200)

    assert _link(session, [client]) == 0


def test_the_match_respects_word_boundaries(session):
    """"Mode" must not match "Modernisierung" — the same rule the client matcher
    has carried since the beginning."""
    client = _client(session, keywords=["Mode"], industry="Fashion")
    _article(session, "Modernisierung im Fashion-Lager")

    assert _link(session, [client]) == 0


def test_the_feed_summary_counts_as_well_as_the_headline(session):
    """Only syndicated text is ever searched — never a fetched body."""
    client = _client(session)
    _article(
        session,
        "Was der Handel diesen Herbst plant",
        summary="Fashion-Händler berichten von sinkenden Retouren.",
    )

    assert _link(session, [client]) == 1
