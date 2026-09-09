"""The three paid news APIs, against captured shapes and never the network.

Every response body below is the shape the live API actually returned during the
probe that preceded this module — including the two failure shapes that cost the
most to discover, because neither looks like a failure:

* Event Registry and mediastack answer **HTTP 200 with an ``error`` key**. An
  exhausted allowance and a quiet news day are the same status code, and a
  reader who only checks the status stores nothing and reports success.
* mediastack answered **HTTP 403 "error code: 1010"** to urllib's default agent
  — Cloudflare blocking the client, which reads exactly like a rejected key.

The rule the whole file protects is in the last section: nothing these APIs
return is trusted to be relevant because it was paid for.
"""

from __future__ import annotations

import datetime as dt

import pytest

from newspulse import config, newsapis
from newspulse.models import Client

_NOW = dt.datetime(2026, 9, 9, 6, 0, tzinfo=dt.UTC)
_SINCE = _NOW - dt.timedelta(days=30)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    """All three configured, so a test states which provider it is about."""
    monkeypatch.setattr(config, "PERIGON_API_KEY", "perigon-key")
    monkeypatch.setattr(config, "EVENT_REGISTRY_API_KEY", "er-key")
    monkeypatch.setattr(config, "MEDIASTACK_API_KEY", "ms-key")
    monkeypatch.setattr(config, "NEWS_API_QUERIES_PER_RUN", 12)


def _mandate(name: str = "Remexian Pharma GmbH", **kw) -> Client:
    return Client(name=name, aliases=[], keywords=[], alert_topics=[], **kw)


# --- Perigon ---------------------------------------------------------------------

_PERIGON_BODY = {
    "numResults": 13,
    "articles": [
        {
            "title": "High Tide setzt seine Expansion in Kanada fort",
            "url": "https://finanznachrichten.de/high-tide-expansion",
            "source": {"domain": "finanznachrichten.de"},
            "pubDate": "2026-08-31T08:14:00Z",
            "description": "Der Händler nennt Remexian als Partner für Europa.",
            "authorsByline": ["Jana Wolf"],
        }
    ],
}


def test_perigon_credits_the_publisher_not_the_provider():
    """A story filed under "Perigon" would put a false masthead on every article
    and make the coverage map read as though one outlet wrote the week."""
    items = newsapis.perigon(
        "Remexian", _SINCE, fetched_at=_NOW, get=lambda *a, **k: _PERIGON_BODY
    )

    assert [i.source for i in items] == ["finanznachrichten.de"]
    assert items[0].author == "Jana Wolf"
    assert items[0].published_at == dt.datetime(2026, 8, 31, 8, 14, tzinfo=dt.UTC)


def test_perigon_sends_the_key_in_the_header_and_never_in_the_query():
    """A key in a query string reaches every proxy log between here and Perigon.
    The live API rejects the query-parameter form anyway (HTTP 401), so this is
    both the safe way and the only working one."""
    seen: dict = {}

    def _get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers") or {}
        return _PERIGON_BODY

    newsapis.perigon("Remexian", _SINCE, fetched_at=_NOW, get=_get)

    assert seen["headers"].get("x-api-key") == "perigon-key"
    assert "perigon-key" not in seen["url"]


# --- Event Registry ---------------------------------------------------------------

_ER_BODY = {
    "articles": {
        "totalResults": 16,
        "results": [
            {
                "title": "High Tide meldet Quartalszahlen",
                "url": "https://boerse-online.de/high-tide-q3",
                "source": {"title": "Börse Online", "uri": "boerse-online.de"},
                "dateTimePub": "2026-08-25T11:02:00Z",
                "body": "Der Großhändler Remexian beliefert den europäischen Markt.",
                "authors": [{"name": "Tim Beck"}],
            }
        ],
    }
}


def test_event_registry_reads_the_body_a_feed_never_carries():
    """The reason this provider is here. Asked for "Remexian" it returns articles
    whose *headline* is about somebody else — the mandate is named in the text.
    No feed-shaped source can find those, because the sentence is not in the feed."""
    items = newsapis.event_registry(
        "Remexian", _SINCE, fetched_at=_NOW, get=lambda *a, **k: _ER_BODY
    )

    assert "Remexian" not in items[0].title
    assert "Remexian" in items[0].summary
    assert items[0].source == "Börse Online"


def test_an_error_inside_a_200_is_a_failure_not_a_quiet_day():
    """The shape that costs the most to discover. Event Registry reports an
    exhausted allowance as HTTP 200 with an ``error`` key, so a reader that only
    checks the status stores nothing and reports success — a portfolio silently
    unmonitored for the rest of the month."""
    with pytest.raises(newsapis.ProviderError, match="Quota"):
        newsapis.event_registry(
            "Remexian",
            _SINCE,
            fetched_at=_NOW,
            get=lambda *a, **k: {"error": "Quota exceeded for this month"},
        )


# --- mediastack --------------------------------------------------------------------

_MS_BODY = {
    "pagination": {"total": 10000},
    "data": [
        {
            "title": "Software aus Münster soll Stromnetze schützen",
            "url": "https://ahlener-zeitung.de/stromnetze",
            "source": "ahlener-zeitung",
            "published_at": "2026-09-09T04:30:00+00:00",
            "description": "Ein Start-up aus dem Münsterland.",
            "author": None,
        }
    ],
}


def test_mediastack_is_asked_once_for_the_whole_portfolio(monkeypatch):
    """Measured: the *second* call of a probe returned HTTP 429. A per-client
    design would spend the allowance on the first two mandates and return nothing
    for the rest — silently, because a rate-limited provider looks exactly like a
    quiet news day."""
    calls: list[str] = []

    def _get(url, *, payload=None, **kwargs):
        calls.append(url)
        if "mediastack" in url:
            return _MS_BODY
        # Two providers, two shapes for the same field name: Event Registry
        # wraps its rows in an object, Perigon returns the list itself.
        return {"articles": {"results": []}} if payload else {"articles": []}

    newsapis.harvest(
        [_mandate("Qonto"), _mandate("Zalando"), _mandate("Freedom24")],
        _SINCE,
        fetched_at=_NOW,
        get=_get,
    )

    assert sum(1 for url in calls if "mediastack" in url) == 1


def test_mediastack_carries_the_regional_press_under_its_own_masthead():
    items = newsapis.mediastack(_SINCE, fetched_at=_NOW, get=lambda *a, **k: _MS_BODY)

    assert items[0].source == "ahlener-zeitung"
    assert items[0].link == "https://ahlener-zeitung.de/stromnetze"


# --- What a sweep spends ------------------------------------------------------------


def test_the_legal_form_is_not_part_of_the_search():
    """"Remexian Pharma GmbH" appears in no headline and in few bodies. Searching
    the registered name verbatim turns sixteen articles into none."""
    assert newsapis.search_terms(_mandate("Remexian Pharma GmbH")) == "Remexian Pharma"


def test_mandates_are_asked_before_competitors_when_the_budget_is_short(monkeypatch):
    """A metered call spent on the thirteenth yardstick while a mandate goes
    unasked is the wrong way round."""
    monkeypatch.setattr(config, "NEWS_API_QUERIES_PER_RUN", 1)
    asked: list[str] = []

    def _get(url, *, payload=None, **kwargs):
        if payload:
            asked.append(payload["keyword"])
            return {"articles": {"results": []}}
        return {"articles": [], "data": []}

    newsapis.harvest(
        [_mandate("Revolut", is_competitor=True), _mandate("Qonto")],
        _SINCE,
        fetched_at=_NOW,
        get=_get,
    )

    assert asked == ["Qonto"]


def test_one_refusal_ends_that_provider_for_the_sweep():
    """A 401 does not become a 200 on the next client, and an exhausted allowance
    least of all. Carrying on would turn one wrong key into twelve identical
    error lines every morning — and spend twelve metered calls to produce them."""
    tries: list[str] = []

    def _get(url, *, payload=None, **kwargs):
        if payload:
            tries.append(payload["keyword"])
            return {"error": "Invalid API key"}
        raise newsapis.ProviderError("HTTP 429: rate_limit_reached")


    found = newsapis.harvest(
        [_mandate("Qonto"), _mandate("Zalando"), _mandate("Freedom24")],
        _SINCE,
        fetched_at=_NOW,
        get=_get,
    )

    assert tries == ["Qonto"]
    assert found.items == []
    assert any("Invalid API key" in e for e in found.errors)


def test_a_provider_without_a_key_is_silent_rather_than_failing(monkeypatch):
    """On a deployment that was never given one, that is the correct silence —
    not an error every morning."""
    monkeypatch.setattr(config, "PERIGON_API_KEY", "")

    def _boom(*args, **kwargs):
        raise AssertionError("a provider with no key was called")

    assert newsapis.perigon("Qonto", _SINCE, fetched_at=_NOW, get=_boom) == []


def test_a_row_without_a_link_is_not_an_article():
    """It cannot be stored, cited or opened, and a placeholder for it is an
    archive entry the reader cannot follow."""
    body = {"data": [{"title": "Ohne Adresse", "source": "x", "published_at": "2026-09-01"}]}

    assert newsapis.mediastack(_SINCE, fetched_at=_NOW, get=lambda *a, **k: body) == []


def test_a_dateless_row_keeps_the_fetch_time_rather_than_being_dropped():
    """The posture ``ingest`` takes with a dateless RSS item: an article with no
    readable date is still an article, and dropping it would lose exactly the
    wire copy that carries the least metadata."""
    body = {"data": [{"title": "Ohne Datum", "url": "https://x.test/1", "source": "x"}]}

    items = newsapis.mediastack(_SINCE, fetched_at=_NOW, get=lambda *a, **k: body)

    assert items[0].published_at == _NOW


# --- Republishers --------------------------------------------------------------
#
# One live harvest of the portfolio returned 800 articles, and 62 of them came
# from a single site that scrapes German outlets wholesale. A republisher's copy
# is the same article under the wrong masthead: stored, it puts a false outlet on
# the coverage map, and a letter citing it sends a journalist to a scrape of
# their own newspaper.


def test_a_scraped_copy_is_not_stored_as_coverage():
    body = {
        "data": [
            {
                "title": "PRESSESPIEGEL/Unternehmen: PORSCHE SE, LEONARDO",
                "url": "https://de.headtopics.com/de/pressespiegel-123",
                "source": "de.headtopics.com",
                "published_at": "2026-09-09T05:00:00+00:00",
            }
        ]
    }

    assert newsapis.mediastack(_SINCE, fetched_at=_NOW, get=lambda *a, **k: body) == []


def test_the_same_site_under_either_spelling_is_the_same_site():
    """One provider returns "de.headtopics.com" and another "headtopics.com". A
    rule that catches one spelling catches nothing."""
    assert newsapis._is_republisher("headtopics.com")
    assert newsapis._is_republisher("de.headtopics.com")
    assert newsapis._is_republisher("www.europesays.com")


def test_a_republisher_is_caught_by_its_link_when_the_row_names_no_source():
    """mediastack rows sometimes carry a display name rather than a host, and
    Event Registry rows carry a title like "Head Topics". The address is the
    fact that does not vary."""
    body = {
        "data": [
            {
                "title": "Irgendeine Meldung",
                "url": "https://europesays.com/de/12345/",
                "source": "Europe Says",
                "published_at": "2026-09-09T05:00:00+00:00",
            }
        ]
    }

    assert newsapis.mediastack(_SINCE, fetched_at=_NOW, get=lambda *a, **k: body) == []


def test_a_financial_portal_carrying_a_press_release_is_kept():
    """The line this rule must not cross. finanznachrichten.de and onvista carry
    IRW releases *and* their own reporting, and the outlet-tier module's own
    measurement is the argument against treating them as filler: demoting the
    financial wires dropped stories a PR manager must not miss."""
    body = {
        "data": [
            {
                "title": "IRW-News: High Tide Inc.",
                "url": "https://finanznachrichten.de/nachrichten-2026-08/high-tide",
                "source": "FinanzNachrichten.de",
                "published_at": "2026-08-31T08:00:00+00:00",
            }
        ]
    }

    items = newsapis.mediastack(_SINCE, fetched_at=_NOW, get=lambda *a, **k: body)

    assert [i.source for i in items] == ["FinanzNachrichten.de"]
