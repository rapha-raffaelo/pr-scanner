"""The paid news APIs: Perigon, Event Registry (newsapi.ai) and mediastack.

The registry in ``feeds_default.toml`` reads 44 German publisher feeds, and
:mod:`newspulse.gnews` searches Google News on top of it. Both see only what a
feed carries: a headline, sometimes a summary, never the body. This module adds
three metered APIs that see more, and each is here for a different reason —
measured on the live portfolio before any of this was written:

* **Event Registry** searches the *body*. Asked for "Remexian" it returned 16
  German articles in thirty days, none of which name the mandate in the
  headline: they are High Tide press releases that mention the wholesaler in
  their text. No feed-shaped source can find those, because the sentence is not
  in the feed.
* **mediastack** carries the regional press. One unfiltered German query came
  back with 10.000 results from *ahlener-zeitung*, *hr-online* and the like —
  the local papers where a plant closure or a dispute surfaces first, and which
  the 44-feed registry does not contain.
* **Perigon** is asked the same question as Event Registry, and is here for the
  day its key works. The deployment's ``PERIGON`` variable holds a URL rather
  than a key, so every call 401s; the provider stays silent until a real key
  arrives rather than filling the run's error list every morning.

Three properties shape the code, and all three were measured rather than assumed:

* **mediastack rate-limits hard.** The *second* call in a probe returned HTTP
  429 ``rate_limit_reached``. So it is never asked per client — it gets exactly
  one broad German query per sweep, and the ordinary matcher decides which
  mandate each story concerns. That is the registry's own model, with ten
  thousand sources instead of forty-four.
* **Everything returns to the normal pipeline.** These are :class:`FeedItem` s
  like any other. Nothing here is trusted to be relevant because an API charged
  for it: the matcher pre-filters, the analyzer judges, and a mention of a
  company that shares a word with a mandate is discarded exactly as it is when
  Google News returns one.
* **A provider failing is not the sweep failing.** Each returns an empty list
  and logs; a metered API that has run out of allowance mid-month must cost the
  morning nothing.

No key is ever logged. The failures quoted here carry the provider's own status
line and nothing else.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import company_names, config
from .ingest import FeedItem
from .models import Client

_log = logging.getLogger(__name__)

_TIMEOUT = 25.0

# Sent on every request. mediastack sits behind Cloudflare and answered HTTP 403
# ``error code: 1010`` to urllib's default agent — a blocked client, not a bad
# key, and indistinguishable from one in the response body.
_USER_AGENT = "RauteOS/1.0 (+https://pr-scanner-production.up.railway.app)"

#: Read over plain HTTP because that is what the subscription serves: the same
#: request to ``https://api.mediastack.com`` is refused. Nothing secret travels
#: outward except the key itself, which is why this is stated here rather than
#: buried — an operator upgrading the plan should switch this to https.
_MEDIASTACK_URL = "http://api.mediastack.com/v1/news"
_EVENT_REGISTRY_URL = "https://eventregistry.org/api/v1/article/getArticles"
_PERIGON_URL = "https://api.perigon.io/v1/all"

#: How many articles one query may bring back. Not a page count: these are read
#: once per sweep and the sweep runs every morning, so the second page of a
#: thirty-day window is yesterday's news that was already stored yesterday.
_PAGE = 50

#: The language every query is scoped to, in each provider's own spelling.
_LANG_PERIGON = "de"
_LANG_EVENT_REGISTRY = "deu"
_LANG_MEDIASTACK = "de"


class ProviderError(Exception):
    """One provider refused or failed. Caught per provider, never propagated."""


#: Sources that republish and originate nothing, refused at the door.
#:
#: Not a quality judgement — :mod:`newspulse.outlets` deliberately makes none,
#: and measuring one killed it: financial wires run genuine corporate news
#: alongside ticker filler, so demoting the outlet dropped stories a PR manager
#: must not miss. This is a different rule. A republisher's copy is the *same
#: article* under the wrong masthead: stored, it puts a false outlet on the
#: coverage map, and a letter citing it sends a journalist to a scrape of their
#: own newspaper.
#:
#: Every entry was observed in one live harvest of the portfolio, not guessed.
#: The financial portals that carry IRW press releases — finanznachrichten.de,
#: boerse-online.de, onvista.de, aktiencheck.de — are deliberately *not* here:
#: they also publish their own reporting, and the tier module's measurement is
#: the argument against treating them as filler.
_REPUBLISHERS = frozenset(
    {
        "de.headtopics.com",   # scrapes German outlets wholesale, 62 copies in one run
        "headtopics.com",
        "europesays.com",      # machine-translated reposts, three copies of one story
        "vietnam.vn",          # ditto, and not a German source at all
        "it-boltwise.de",      # generated summaries of other people's articles
        "handelsmeldungen.de", # a ticker whose every item is titled the same
    }
)


def _is_republisher(source: str | None) -> bool:
    """Whether this source only carries other people's articles.

    Matched on the host as the providers spell it, plus the bare domain: one
    provider returns ``de.headtopics.com`` and another ``headtopics.com`` for the
    same site, and a rule that catches only one spelling catches nothing.
    """
    host = (source or "").strip().casefold().removeprefix("www.")
    if not host:
        return False
    if host in _REPUBLISHERS:
        return True
    # A subdomain of a listed site is the same site.
    return any(host.endswith(f".{known}") for known in _REPUBLISHERS)


def _rows(value: object) -> list[dict]:
    """The dict rows in a provider's result list, and nothing else.

    Three providers, three shapes for the same field: Perigon's ``articles`` is a
    list of rows, Event Registry's is an object *containing* the list. A shape
    that is neither is not a crash worth having — it would take the whole
    harvest down through one provider's bad afternoon, so it becomes no rows and
    the count beside that provider's name says so.
    """
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


#: A callable shaped like :func:`_request`, injected so tests never touch the
#: network and so a rate-limited provider can be driven deliberately.
Get = Callable[..., dict]


@dataclass(frozen=True, slots=True)
class Harvest:
    """What one sweep's worth of paid queries returned, per provider.

    Counted rather than merely logged because the run report states it: a
    provider that silently returns nothing for a week is the failure mode these
    APIs actually have, and a zero beside its name is the only place that shows.
    """

    items: list[FeedItem]
    by_provider: dict[str, int]
    errors: list[str]


def _request(
    url: str,
    *,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = _TIMEOUT,
) -> dict:
    """One JSON call. Raises :class:`ProviderError` for anything but a JSON body.

    The error text carries the status and the first of the provider's own
    message, never the request — a URL with the key in its query string must not
    reach a log line.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=body, headers={"User-Agent": _USER_AGENT, **(headers or {})}
    )
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace").replace("\n", " ")
        raise ProviderError(f"HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"nicht erreichbar: {exc.reason}") from None
    except json.JSONDecodeError:
        raise ProviderError("Antwort war kein JSON") from None
    except Exception as exc:  # noqa: BLE001 — one provider must not end a sweep
        raise ProviderError(f"{type(exc).__name__}: {exc}") from None


def _parsed_date(raw: str | None, fallback: dt.datetime) -> dt.datetime:
    """A provider's date as tz-aware UTC, or ``fallback`` when it is unusable.

    The same posture :mod:`newspulse.ingest` takes with a dateless RSS item: an
    article with no readable date is still an article, and dropping it would
    lose exactly the wire copy that carries the least metadata.
    """
    text = (raw or "").strip()
    if not text:
        return fallback
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    for parse in (dt.datetime.fromisoformat, lambda v: dt.datetime.strptime(v, "%Y-%m-%d")):
        try:
            value = parse(text)
        except (ValueError, TypeError):
            continue
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return fallback


def _item(
    *,
    title: str | None,
    link: str | None,
    source: str | None,
    published: str | None,
    summary: str | None,
    author: str | None,
    provider: str,
    fetched_at: dt.datetime,
) -> FeedItem | None:
    """One provider row as a :class:`FeedItem`, or ``None`` if it is not one.

    A row with no title or no link cannot be stored, cited or opened, and a
    placeholder for it would be an archive entry the reader cannot follow.
    """
    headline = (title or "").strip()
    url = (link or "").strip()
    if not headline or not url:
        return None
    house = (source or "").strip()
    if _is_republisher(house) or _is_republisher(urllib.parse.urlsplit(url).netloc):
        return None
    text = (summary or "").strip()
    return FeedItem(
        title=headline,
        link=url,
        # The publisher, not the provider. Crediting these to "mediastack" would
        # put a false masthead on every story and make the coverage map read as
        # though one outlet wrote the whole week.
        source=house or provider,
        published_at=_parsed_date(published, fetched_at),
        summary=text or None,
        language=_LANG_MEDIASTACK,
        author=(author or "").strip() or None,
    )


# --- The three providers ---------------------------------------------------------


def perigon(
    term: str, since: dt.datetime, *, fetched_at: dt.datetime, get: Get = _request
) -> list[FeedItem]:
    """Perigon's German coverage for one search term."""
    key = config.PERIGON_API_KEY
    if not key:
        return []
    query = urllib.parse.urlencode(
        {
            "q": term,
            "language": _LANG_PERIGON,
            "from": since.date().isoformat(),
            "size": _PAGE,
            "sortBy": "date",
        }
    )
    body = get(f"{_PERIGON_URL}?{query}", headers={"x-api-key": key})
    rows = _rows(body.get("articles"))
    items = [
        _item(
            title=row.get("title"),
            link=row.get("url"),
            source=(row.get("source") or {}).get("domain"),
            published=row.get("pubDate"),
            summary=row.get("description"),
            # Perigon returns a list of authors; the first is the byline the
            # contact book would file the letter under.
            author=next(iter(row.get("authorsByline") or []), None)
            if isinstance(row.get("authorsByline"), list)
            else row.get("authorsByline"),
            provider="Perigon",
            fetched_at=fetched_at,
        )
        for row in rows
    ]
    return [item for item in items if item is not None]


def event_registry(
    term: str, since: dt.datetime, *, fetched_at: dt.datetime, get: Get = _request
) -> list[FeedItem]:
    """Event Registry's German coverage for one term, headline *and* body.

    ``keywordLoc`` is left at its default, which searches both: restricted to
    the body it misses the ordinary coverage that names the mandate in the
    headline, and restricted to the title it becomes another feed reader. The
    body is the reason this provider is here.
    """
    key = config.EVENT_REGISTRY_API_KEY
    if not key:
        return []
    body = get(
        _EVENT_REGISTRY_URL,
        payload={
            "action": "getArticles",
            "keyword": term,
            "lang": _LANG_EVENT_REGISTRY,
            "dateStart": since.date().isoformat(),
            "articlesPage": 1,
            "articlesCount": _PAGE,
            "articlesSortBy": "date",
            "resultType": "articles",
            "apiKey": key,
        },
    )
    # Event Registry answers HTTP 200 with an ``error`` key: an exhausted
    # allowance and a good empty day are the same status code, and only this
    # tells them apart.
    if body.get("error"):
        raise ProviderError(str(body["error"])[:200])
    articles = body.get("articles")
    rows = _rows(articles.get("results") if isinstance(articles, dict) else None)
    items = [
        _item(
            title=row.get("title"),
            link=row.get("url"),
            source=(row.get("source") or {}).get("title")
            or (row.get("source") or {}).get("uri"),
            published=row.get("dateTimePub") or row.get("date"),
            summary=row.get("body"),
            author=next(
                (a.get("name") for a in (row.get("authors") or []) if a.get("name")),
                None,
            ),
            provider="Event Registry",
            fetched_at=fetched_at,
        )
        for row in rows
    ]
    return [item for item in items if item is not None]


def mediastack(
    since: dt.datetime, *, fetched_at: dt.datetime, get: Get = _request
) -> list[FeedItem]:
    """One broad German query, not one per client.

    The subscription answered HTTP 429 on the second call of a probe, so a
    per-client design would spend the whole allowance on the first two mandates
    and return nothing for the rest — silently, since a rate-limited provider
    looks exactly like a quiet news day. One query, and the matcher sorts the
    result the way it sorts the registry's 44 feeds.
    """
    key = config.MEDIASTACK_API_KEY
    if not key:
        return []
    query = urllib.parse.urlencode(
        {
            "access_key": key,
            "languages": _LANG_MEDIASTACK,
            "sort": "published_desc",
            "limit": _PAGE * 2,
            "date": f"{since.date().isoformat()},{fetched_at.date().isoformat()}",
        }
    )
    body = get(f"{_MEDIASTACK_URL}?{query}")
    # Same shape as Event Registry: an error inside a 200.
    if body.get("error"):
        raise ProviderError(str(body["error"])[:200])
    items = [
        _item(
            title=row.get("title"),
            link=row.get("url"),
            source=row.get("source"),
            published=row.get("published_at"),
            summary=row.get("description"),
            author=row.get("author"),
            provider="mediastack",
            fetched_at=fetched_at,
        )
        for row in _rows(body.get("data"))
    ]
    return [item for item in items if item is not None]


# --- One sweep's worth ------------------------------------------------------------


def search_terms(client: Client) -> str:
    """The one term this client is searched under.

    The registered name, cleaned the way :mod:`newspulse.gnews` cleans it, so
    "Remexian Pharma GmbH" is asked as "Remexian Pharma" — the legal form is in
    no headline and in few bodies, and including it turns a search that finds
    sixteen articles into one that finds none.

    One term rather than name-plus-aliases: each of these calls is metered, and
    an alias is what the matcher is for once the articles are back.
    """
    return company_names.strip_legal_form((client.name or "").strip())


def harvest(
    clients: Sequence[Client],
    since: dt.datetime,
    *,
    fetched_at: dt.datetime,
    get: Get = _request,
) -> Harvest:
    """Ask every configured provider once per sweep, and never raise.

    Per-client providers are asked for at most
    :data:`newspulse.config.NEWS_API_QUERIES_PER_RUN` clients, mandates first: a
    metered call spent on the thirteenth yardstick while a mandate goes unasked
    is the wrong way round, and the cap exists precisely because the portfolio
    grows without anyone rereading this file.
    """
    items: list[FeedItem] = []
    counts: dict[str, int] = {}
    errors: list[str] = []

    # Mandates before competitors: both are worth searching, only one is worth
    # spending the last query of the allowance on.
    ordered = [c for c in clients if not c.is_competitor] + [
        c for c in clients if c.is_competitor
    ]
    budget = max(0, config.NEWS_API_QUERIES_PER_RUN)

    for name, provider in (("Perigon", perigon), ("Event Registry", event_registry)):
        found = 0
        for client in ordered[:budget]:
            term = search_terms(client)
            if not term:
                continue
            try:
                batch = provider(term, since, fetched_at=fetched_at, get=get)
            except ProviderError as exc:
                # The first refusal ends this provider for the sweep. A 401 does
                # not become a 200 on the next client, and an exhausted
                # allowance least of all — carrying on would turn one wrong key
                # into twelve identical error lines every morning.
                _log.warning("%s failed on %r: %s; skipping it", name, term, exc)
                errors.append(f"{name}: {exc}")
                break
            items.extend(batch)
            found += len(batch)
        counts[name] = found

    try:
        batch = mediastack(since, fetched_at=fetched_at, get=get)
    except ProviderError as exc:
        _log.warning("mediastack failed: %s; skipping it", exc)
        errors.append(f"mediastack: {exc}")
        batch = []
    items.extend(batch)
    counts["mediastack"] = len(batch)

    if items:
        _log.info(
            "paid news APIs returned %d item(s): %s",
            len(items),
            ", ".join(f"{name} {count}" for name, count in sorted(counts.items())),
        )
    return Harvest(items=items, by_provider=counts, errors=errors)
