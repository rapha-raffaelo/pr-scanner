"""The industry field is a filter, so it has to be a word the press writes.

It used to be a label. Since the radar started scoping its query with it, and the
archive linking started refusing to run without it, a wrong word is expensive in
a way nothing on the form suggests: a beauty-tech mandate carried "Beauty Tech" —
accurate, and almost absent from German press text — so every query intersected
to nothing and the mandate sat for months with no market material.

Measured live while building this: "Beauty Tech" is nearly unwritten, while
"Kosmetikindustrie" returns 8 items in ninety days and "Kosmetik" 62. The
difference is invisible from the form and decisive for everything downstream.
"""

from __future__ import annotations

import datetime as dt
import urllib.error

import pytest
from sqlalchemy.orm import sessionmaker

from newspulse import industry, ingest
from newspulse.db import make_engine
from newspulse.ingest import FeedItem
from newspulse.models import Base, Client
from newspulse.web.routes import settings as settings_routes

_NOW = dt.datetime(2026, 8, 4, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _client(**over) -> Client:
    return Client(
        name=over.get("name", "IB-7 Beauty Tech GmbH"),
        aliases=["IB-7"],
        industry=over.get("industry"),
        country="AT",
        website="https://ib-7.com",
        keywords=over.get("keywords", ["KI in der Kosmetik"]),
        alert_topics=[],
    )


def _reply(*terms: str) -> str:
    inner = ", ".join(f'"{t}"' for t in terms)
    return '{"terms": [' + inner + "]}"


def _hits(counts: dict[str, int]):
    """A fetch that returns as many items as the term's measured press volume."""

    def _fetch(url, since, **_):
        import urllib.parse

        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
        term = query.strip('"')
        return [
            FeedItem(
                title=f"{term} Meldung {i}",
                link=f"https://ex.de/{term}-{i}",
                source="Fachpresse",
                published_at=_NOW - dt.timedelta(days=i + 1),
                summary=None,
                language="de",
            )
            for i in range(counts.get(term, 0))
        ]

    return _fetch


def test_the_nearest_term_the_press_writes_wins():
    """Ordered specific-to-broad, and the first that works is taken: a broader
    term is a weaker filter, so the narrowest one that exists in print is best."""
    best = industry.classify(
        _client(),
        invoke=lambda *a, **k: _reply("Beauty Tech", "Kosmetikindustrie", "Konsumgüter"),
        fetch=_hits({"Beauty Tech": 0, "Kosmetikindustrie": 8, "Konsumgüter": 31}),
        now=lambda: _NOW,
    )

    assert best is not None
    assert best.term == "Kosmetikindustrie"
    assert best.hits == 8


def test_a_term_the_press_does_not_write_is_refused():
    """The exact failure this exists to prevent: an accurate label that filters
    every query to nothing."""
    measured = industry.measure(
        _client(), ["Beauty Tech"], fetch=_hits({"Beauty Tech": 1}), now=lambda: _NOW
    )

    assert measured[0].usable is False


def test_nothing_usable_yields_nothing_rather_than_the_least_bad(session):
    """A field that filters everything away is worse than no field: without one
    the radar at least searches, unscoped."""
    best = industry.classify(
        _client(),
        invoke=lambda *a, **k: _reply("Beauty Tech", "KI-Hautpflege"),
        fetch=_hits({}),
        now=lambda: _NOW,
    )

    assert best is None


def test_a_failing_probe_does_not_promote_the_term(session):
    """"The search errored" must not read as "nobody writes this" — but it must
    not read as "this works" either."""

    def _boom(*a, **k):
        raise RuntimeError("Netzwerk weg")

    measured = industry.measure(_client(), ["Kosmetik"], fetch=_boom, now=lambda: _NOW)

    assert measured == [industry.Candidate(term="Kosmetik", hits=0, measured=False)]
    assert measured[0].usable is False


def test_a_field_nobody_could_measure_is_not_a_field_nobody_writes(session):
    """The number alone cannot tell them apart: a probe that never reached the
    search is recorded as zero hits, exactly like a word the press does not use.
    Read as "nobody writes this", one rate-limited morning sends an operator off
    to change an industry term that works — so the two answers are kept apart.
    """

    def _boom(*a, **k):
        raise RuntimeError("Netzwerk weg")

    client = _client(industry="Kosmetik")

    assert industry.field_is_usable(client, fetch=_boom, now=lambda: _NOW) is None
    assert industry.field_is_usable(client, fetch=_hits({}), now=lambda: _NOW) is False
    assert (
        industry.field_is_usable(
            client, fetch=_hits({"Kosmetik": 9}), now=lambda: _NOW
        )
        is True
    )


def test_a_real_outage_is_not_a_verdict_about_the_term(monkeypatch):
    """The version of the above that the production default actually takes.

    Every other test here injects a ``fetch`` that *raises*; the real
    ``fetch_feed`` never does. It answers an unreachable feed with an empty list,
    which arrives at the probe as "nobody writes this word" — so the whole
    distinction above was dead code on the one path that matters. Mocked at the
    socket, not at the fetch: ``fetch_feed`` itself stays real.
    """

    def _down(url, timeout):
        raise urllib.error.URLError("Netzwerk weg")

    monkeypatch.setattr(ingest, "_fetch_raw", _down)
    client = _client(industry="Onlinehandel")

    assert industry.field_is_usable(client, now=lambda: _NOW) is None


def test_a_search_that_answers_with_nothing_is_a_verdict(monkeypatch):
    """The other half: a well-formed feed with no items is the press being asked
    and having written nothing, which is the answer the market page explains."""
    empty_feed = b'<?xml version="1.0"?><rss version="2.0"><channel>'
    empty_feed += b"<title>Google News</title></channel></rss>"
    monkeypatch.setattr(ingest, "_fetch_raw", lambda url, timeout: empty_feed)

    assert industry.field_is_usable(_client(industry="Onlinehandel"), now=lambda: _NOW) is False


def test_the_probe_uses_the_clients_own_news_edition():
    """A word can be common in one edition and absent in another; an Austrian
    mandate is measured against the Austrian one."""
    seen: list[str] = []

    def _fetch(url, since, **_):
        seen.append(url)
        return []

    industry.measure(_client(), ["Kosmetik"], fetch=_fetch, now=lambda: _NOW)

    assert "gl=AT" in seen[0]


# --- Onboarding -----------------------------------------------------------------


def test_a_new_mandate_without_an_industry_gets_one(session, monkeypatch, no_industry_settling):
    """Everything downstream depends on the field: the radar scopes with it and
    the archive linking refuses to run without it. A mandate onboarded with it
    blank goes straight into the state that produces no impulses."""
    monkeypatch.setattr(industry, "settle", no_industry_settling)
    client = _client(industry=None)
    session.add(client)
    session.commit()
    monkeypatch.setattr(
        industry, "classify", lambda c, **_: industry.Candidate(term="Kosmetik", hits=62)
    )

    settings_routes._settle_industry(session, client)

    assert session.get(Client, client.id).industry == "Kosmetik"


def test_an_industry_somebody_typed_is_left_alone(session, monkeypatch, no_industry_settling):
    """Overwriting what an operator wrote to make a search work is a trade they
    never agreed to."""
    monkeypatch.setattr(industry, "settle", no_industry_settling)
    client = _client(industry="Beauty Tech")
    session.add(client)
    session.commit()
    monkeypatch.setattr(
        industry, "classify", lambda c, **_: pytest.fail("must not reclassify")
    )

    settings_routes._settle_industry(session, client)

    assert session.get(Client, client.id).industry == "Beauty Tech"


def test_a_failing_classifier_never_blocks_onboarding(session, monkeypatch):
    """The mandate still has to arrive with its coverage."""
    client = _client(industry=None)
    session.add(client)
    session.commit()

    def _boom(c):
        raise RuntimeError("claude ist weg")

    monkeypatch.setattr(industry, "classify", _boom)

    settings_routes._settle_industry(session, client)  # must not raise

    assert session.get(Client, client.id).industry is None


# --- Adopting a proposal by hand -------------------------------------------------


def test_adopting_a_term_adds_it_rather_than_replacing(session):
    """The operator's word is usually the accurate one and the measured one is
    merely the searchable one. The field takes both — it is split on semicolons
    and OR-joined — so "Beauty Tech" keeps describing the mandate while
    "Kosmetikindustrie" makes the search work."""
    client = _client(industry="Beauty Tech")
    session.add(client)
    session.commit()

    settings_routes.accept_industry_route(
        client.id, term="Kosmetikindustrie", session=session
    )

    stored = session.get(Client, client.id).industry
    assert "Beauty Tech" in stored and "Kosmetikindustrie" in stored


def test_adopting_the_same_term_twice_does_not_repeat_it(session):
    client = _client(industry="Kosmetik")
    session.add(client)
    session.commit()

    for _ in range(2):
        settings_routes.accept_industry_route(
            client.id, term="kosmetik", session=session
        )

    assert session.get(Client, client.id).industry == "Kosmetik"


# --- The whole portfolio, every morning ------------------------------------------
#
# The step lived in the onboarding route, so it ran once down one path. Anything
# created any other way kept an empty field forever — and the analyzer decides
# relevance from name, industry, aliases and alert topics, so a company with an
# empty industry is judged on its name alone.


def test_settle_leaves_a_term_that_is_already_there(session, monkeypatch, no_industry_settling):
    monkeypatch.setattr(industry, "settle", no_industry_settling)
    client = _client(industry="Beauty Tech")
    session.add(client)
    session.commit()
    monkeypatch.setattr(
        industry, "classify", lambda c, **_: pytest.fail("must not reclassify")
    )

    assert industry.settle(session, client) is False


def test_settle_writes_nothing_when_no_term_is_usable(session, monkeypatch, no_industry_settling):
    """A term the press never writes filters everything away, and is worse than
    the empty field it replaced."""
    monkeypatch.setattr(industry, "settle", no_industry_settling)
    client = _client(industry=None)
    session.add(client)
    session.commit()
    monkeypatch.setattr(industry, "classify", lambda c, **_: None)

    assert industry.settle(session, client) is False
    assert session.get(Client, client.id).industry is None


def test_the_sweep_settles_a_competitor_that_arrived_as_a_bare_name(
    session, monkeypatch, no_industry_settling
):
    """The G-20 case, and the reason competitors are included here while
    ``_settle_themes`` skips them: a yardstick needs no radar and no pitch, but it
    does need to be the right company."""
    from newspulse import job

    monkeypatch.setattr(industry, "settle", no_industry_settling)
    rival = _client(name="G-20", industry=None, keywords=[])
    rival.is_competitor = True
    session.add(rival)
    session.commit()
    monkeypatch.setattr(
        industry,
        "classify",
        lambda c, **_: industry.Candidate(term="Krypto-Market-Making", hits=48),
    )

    settled = job._settle_industries(session, [rival], lambda *a, **k: [])

    assert settled == 1
    assert session.get(Client, rival.id).industry == "Krypto-Market-Making"


def test_a_failed_classification_does_not_stop_the_sweep(
    session, monkeypatch, no_industry_settling
):
    """One dead classifier must not cost the portfolio its sweep, and the session
    has to be usable for every stage after it."""
    from newspulse import job

    monkeypatch.setattr(industry, "settle", no_industry_settling)
    first = _client(name="Kaputt", industry=None)
    second = _client(name="Heil", industry=None)
    session.add_all([first, second])
    session.commit()

    def _classify(c, **_):
        if c.name == "Kaputt":
            raise RuntimeError("der Klassifizierer ist weg")
        return industry.Candidate(term="Logistik", hits=31)

    monkeypatch.setattr(industry, "classify", _classify)

    settled = job._settle_industries(session, [first, second], lambda *a, **k: [])

    assert settled == 1
    assert session.get(Client, second.id).industry == "Logistik"
    assert session.get(Client, first.id).industry is None
