"""Der Pressespiegel (newspulse.clippings and its two routes).

Two layers, tested at their own level. ``clippings.build`` is a pure read over a
seeded in-memory database: grouping, pickup counts, the tier-based strongest
outlet, ordering, and what a clipping may say. The two routes are driven through
``TestClient``: the screen with its app links and the download without them.

The golden-file test at the bottom is the acceptance criterion in file form: the
rendered document of a seeded, fixed July is compared byte for byte against
``fixtures/clippings/pressespiegel_2026-07.html``, so every wording change shows
up as a reviewable diff. Regenerate deliberately with
``NEWSPULSE_UPDATE_GOLDEN=1 pytest tests/test_clippings.py``.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import branding, clippings, config, i18n
from newspulse.models import Analysis, Article, Base, Category, Client, Tonality
from newspulse.reporting import Period
from newspulse.web.app import create_app, get_db

#: The seeded month, fixed rather than relative to "now" so the assertions stay
#: true next January. Bounded by local midnight, which is what ``Period.month``
#: and therefore both routes compute.
JULY = Period.month(2026, 7)

GOLDEN = Path(__file__).parent / "fixtures" / "clippings" / "pressespiegel_2026-07.html"

#: Every address the rendered document points at, however it is spelled.
_TARGETS = re.compile(r'(?:href|src|action)\s*=\s*"([^"]*)"')


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def http(factory):
    app = create_app()

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _piece(
    session,
    client,
    title,
    *,
    when=dt.datetime(2026, 7, 10, 9, 0, tzinfo=dt.UTC),
    source="Handelsblatt",
    tonality=Tonality.NEUTRAL,
    importance=6,
    summary="Die gespeicherte Zusammenfassung.",
    snippet="Der Feed-Ausriss.",
    relevance=5,
    dismissed=False,
) -> Analysis:
    article = Article(
        title=title,
        url=f"https://presse.example/{re.sub(r'[^a-z0-9]+', '-', title.lower())}",
        source=source,
        published_at=when,
        fetched_at=when,
        summary_text=snippet,
        language="de",
        title_hash=title[:8],
    )
    session.add(article)
    session.flush()
    analysis = Analysis(
        article_id=article.id,
        client_id=client.id,
        summary=summary,
        category=Category.PRODUKT,
        is_relevant=relevance >= 1, relevance_score=relevance,
        importance_score=importance,
        tonality=tonality,
        dismissed_at=dt.datetime(2026, 7, 20, tzinfo=dt.UTC) if dismissed else None,
    )
    session.add(analysis)
    session.flush()
    return analysis


@pytest.fixture
def mandate(factory):
    with factory() as session:
        client = Client(name="Alpha AG")
        session.add(client)
        session.commit()
        return client.id


# Three write-ups of one event, worded the way wire copy actually differs: the
# same significant tokens under different section labels. Distinct enough from
# the fourth headline that the conservative clustering keeps them apart.
_WIRE = (
    "Bafin rügt Alpha AG wegen fehlerhaftem Geschäftsbericht",
    "Online-Händler: Bafin rügt Alpha AG wegen fehlerhaftem Geschäftsbericht",
    "Bafin rügt Alpha AG wegen fehlerhaftem Geschäftsbericht - Wirtschaft",
)
_OTHER = "Alpha AG eröffnet neues Logistikzentrum in Leipzig"


def _seed_july(session, client_id):
    client = session.get(Client, client_id)
    for index, (title, source) in enumerate(
        zip(_WIRE, ("Handelsblatt", "Regionalkurier", "Stadtanzeiger"))
    ):
        _piece(
            session,
            client,
            title,
            source=source,
            when=dt.datetime(2026, 7, 8, 6 + index, 0, tzinfo=dt.UTC),
            tonality=Tonality.NEGATIV,
            importance=8,
        )
    _piece(
        session,
        client,
        _OTHER,
        source="Logistik Heute",
        when=dt.datetime(2026, 7, 21, 9, 0, tzinfo=dt.UTC),
        tonality=Tonality.POSITIV,
        importance=5,
    )
    session.commit()


# --- clippings.build -------------------------------------------------------------


def test_wire_copies_group_into_one_story_with_the_pickup_count(factory, mandate):
    with factory() as session:
        _seed_july(session, mandate)
        document = clippings.build(session, session.get(Client, mandate), JULY)

    assert len(document.stories) == 2
    reprimand = document.stories[0]
    assert reprimand.pickup_count == 3
    assert len(reprimand.items) == 3
    assert document.total == 4


def test_the_heaviest_story_comes_first(factory, mandate):
    with factory() as session:
        _seed_july(session, mandate)
        document = clippings.build(session, session.get(Client, mandate), JULY)

    assert [story.pickup_count for story in document.stories] == [3, 1]
    assert document.stories[1].headline == _OTHER


def test_the_best_tiered_outlet_is_the_reach_strongest(factory, mandate):
    """"Reichweitenstärkstes Medium" is the tier table's answer, never a guess:
    the tier-1 outlet outranks the unlisted ones even though it did not run the
    story first."""
    with factory() as session:
        client = session.get(Client, mandate)
        for title, source, hour in (
            (_WIRE[0], "Regionalkurier", 6),
            (_WIRE[1], "Handelsblatt", 9),
            (_WIRE[2], "Stadtanzeiger", 11),
        ):
            _piece(
                session, client, title, source=source,
                when=dt.datetime(2026, 7, 8, hour, 0, tzinfo=dt.UTC),
            )
        session.commit()
        document = clippings.build(session, client, JULY)

    assert document.stories[0].top_outlet == "Handelsblatt"


def test_equal_tiers_go_to_the_outlet_that_ran_it_first(factory, mandate):
    with factory() as session:
        client = session.get(Client, mandate)
        for title, source, hour in ((_WIRE[0], "Regionalkurier", 6), (_WIRE[1], "Stadtanzeiger", 9)):
            _piece(
                session, client, title, source=source,
                when=dt.datetime(2026, 7, 8, hour, 0, tzinfo=dt.UTC),
            )
        session.commit()
        document = clippings.build(session, client, JULY)

    assert document.stories[0].top_outlet == "Regionalkurier"


def test_the_richest_copy_heads_the_story_and_items_run_chronologically(factory, mandate):
    with factory() as session:
        client = session.get(Client, mandate)
        _piece(
            session, client, _WIRE[1], source="Regionalkurier", importance=4,
            when=dt.datetime(2026, 7, 8, 6, 0, tzinfo=dt.UTC),
        )
        _piece(
            session, client, _WIRE[0], source="Handelsblatt", importance=9,
            when=dt.datetime(2026, 7, 8, 9, 0, tzinfo=dt.UTC),
        )
        session.commit()
        document = clippings.build(session, client, JULY)

    story = document.stories[0]
    assert story.headline == _WIRE[0]
    assert [item.source for item in story.items] == ["Regionalkurier", "Handelsblatt"]


def test_a_clipping_carries_the_stored_summary_or_falls_back_to_the_snippet(
    factory, mandate
):
    with factory() as session:
        client = session.get(Client, mandate)
        _piece(session, client, _WIRE[0], summary="Die Analyse-Zusammenfassung.")
        _piece(
            session, client, _OTHER, summary=None, snippet="Nur der Feed-Ausriss."
        )
        session.commit()
        document = clippings.build(session, client, JULY)

    summaries = {
        item.headline: item.summary
        for story in document.stories
        for item in story.items
    }
    assert summaries[_WIRE[0]] == "Die Analyse-Zusammenfassung."
    assert summaries[_OTHER] == "Nur der Feed-Ausriss."


def test_a_long_feed_snippet_is_cut_so_no_article_body_reaches_the_document(
    factory, mandate
):
    """The one way a full text could enter the Pressespiegel, closed.

    ``summary_text`` is the only body-ish column in the archive, and a feed that
    syndicates a whole article into its ``<description>`` would otherwise put
    that article verbatim into a document a client is sent.
    """
    body = "Wort " * 400
    with factory() as session:
        client = session.get(Client, mandate)
        _piece(session, client, _WIRE[0], summary=None, snippet=body)
        session.commit()
        document = clippings.build(session, client, JULY)

    summary = document.stories[0].items[0].summary
    assert len(summary) <= clippings._MAX_SNIPPET_CHARS + len(clippings._ELLIPSIS)
    assert summary.endswith("\u2026")
    # Cut on a word boundary: the quotation stops, it does not break mid-word.
    assert not summary.removesuffix("\u2026").rstrip().endswith("Wor")


def test_a_short_feed_snippet_is_reproduced_whole(factory, mandate):
    with factory() as session:
        client = session.get(Client, mandate)
        _piece(session, client, _WIRE[0], summary=None, snippet="Zwei kurze Sätze.")
        session.commit()
        document = clippings.build(session, client, JULY)

    assert document.stories[0].items[0].summary == "Zwei kurze Sätze."


def test_the_stored_analysis_summary_is_never_cut(factory, mandate):
    """The cut is on the outlet's copy, not on ours: an agency summary is the
    text the client is owed, and truncating it would be losing our own work."""
    written = "Ausführliche Einordnung. " * 40
    with factory() as session:
        client = session.get(Client, mandate)
        _piece(session, client, _WIRE[0], summary=written)
        session.commit()
        document = clippings.build(session, client, JULY)

    assert document.stories[0].items[0].summary == written.strip()


def test_dismissed_and_irrelevant_coverage_stays_out_of_the_document(factory, mandate):
    """The same visibility gate as every screen: a piece a person dismissed must
    not resurface in a document that leaves the house."""
    with factory() as session:
        client = session.get(Client, mandate)
        _piece(session, client, _WIRE[0])
        _piece(session, client, "Alpha AG Namensvetter ohne Bezug baut Brücken", relevance=0)
        _piece(session, client, "Alpha AG Meldung wurde händisch verworfen extra", dismissed=True)
        session.commit()
        document = clippings.build(session, client, JULY)

    assert document.total == 1


def test_coverage_outside_the_period_stays_out(factory, mandate):
    with factory() as session:
        client = session.get(Client, mandate)
        _piece(session, client, _WIRE[0], when=dt.datetime(2026, 6, 15, 12, 0, tzinfo=dt.UTC))
        session.commit()
        document = clippings.build(session, client, JULY)

    assert document.total == 0
    assert document.stories == ()


def test_the_header_names_the_last_day_the_period_contains_not_the_next_first(
    factory, mandate
):
    """``Period.end`` is exclusive; a header built from it claims a day the
    document does not cover."""
    with factory() as session:
        document = clippings.build(session, session.get(Client, mandate), JULY)

    assert document.period_last == JULY.last
    assert document.period_last.astimezone(config.local_zone()).day == 31
    assert document.period_last < JULY.end


def test_the_header_names_the_last_day_of_a_month_that_loses_an_hour(monkeypatch):
    """A spring-forward month's last local day is 23 hours long, so a header
    corrected by a whole day would name the 30th while the document lists a piece
    from the 31st — a document contradicting itself in front of a client."""
    monkeypatch.setattr(config, "LOCAL_ZONE", ZoneInfo("Europe/Berlin"))
    for year, month, last in ((2024, 3, 31), (2019, 3, 31), (2026, 7, 31), (2026, 2, 28)):
        period = Period.month(year, month)
        named = period.last.astimezone(config.local_zone())
        assert (named.month, named.day) == (month, last), (year, month, named)
        assert period.start <= period.last < period.end


def test_one_outlet_spelled_two_ways_is_one_aufgriff(factory, mandate):
    """The pickup count is the one figure the grouping exists to state, and the
    one a client cannot check — so it counts mastheads, not feed spellings."""
    with factory() as session:
        client = session.get(Client, mandate)
        for title, source, hour in (
            (_WIRE[0], "Handelsblatt", 6),
            (_WIRE[1], "handelsblatt.de", 9),
        ):
            _piece(
                session, client, title, source=source,
                when=dt.datetime(2026, 7, 8, hour, 0, tzinfo=dt.UTC),
            )
        session.commit()
        document = clippings.build(session, client, JULY)

    story = document.stories[0]
    assert story.pickup_count == 1
    assert len(story.items) == 2
    # And the line names the masthead, not whichever spelling arrived first.
    assert story.top_outlet == "Handelsblatt"


def test_a_snippet_with_no_word_boundary_in_reach_is_cut_at_the_ceiling(
    factory, mandate
):
    """Cutting back to the last space is only a kindness while there is a space
    near the ceiling. A run without one would otherwise be published as its first
    word, throwing the outlet's summary away instead of trimming it."""
    with factory() as session:
        client = session.get(Client, mandate)
        _piece(
            session, client, _WIRE[0], summary=None, snippet="Kurz " + "x" * 500
        )
        session.commit()
        document = clippings.build(session, client, JULY)

    summary = document.stories[0].items[0].summary
    assert len(summary) == clippings._MAX_SNIPPET_CHARS + len(clippings._ELLIPSIS)
    assert summary.endswith("…")


def test_an_empty_period_builds_an_empty_document_not_an_error(factory, mandate):
    with factory() as session:
        document = clippings.build(session, session.get(Client, mandate), JULY)
    assert document.stories == ()
    assert document.total == 0
    assert document.period_last.astimezone(config.local_zone()).day == 31


# --- The routes ------------------------------------------------------------------


def test_the_screen_groups_by_story_and_names_the_period_in_the_header(
    http, factory, mandate
):
    with factory() as session:
        _seed_july(session, mandate)

    body = http.get(f"/client/{mandate}/pressespiegel?zeitraum=2026-07").text
    assert "Pressespiegel" in body
    assert "01.07.2026" in body and "31.07.2026" in body
    assert "3 Aufgriffe" in body
    assert "reichweitenstärkstes Medium" in body
    assert "Handelsblatt" in body
    # One heading per story, not one row per article date.
    assert body.count('class="story__h"') == 2


def test_every_clipping_states_its_five_fields_and_no_body_text(http, factory, mandate):
    with factory() as session:
        _seed_july(session, mandate)

    body = http.get(f"/client/{mandate}/pressespiegel?zeitraum=2026-07").text
    assert _OTHER in body
    assert "Logistik Heute" in body
    assert "21.07.2026" in body
    assert "Die gespeicherte Zusammenfassung." in body
    assert "positiv" in body and "negativ" in body


def test_the_download_names_mandate_and_period_like_the_report_does(
    http, factory, mandate
):
    with factory() as session:
        _seed_july(session, mandate)

    resp = http.get(f"/client/{mandate}/pressespiegel.html?zeitraum=2026-07")
    assert resp.status_code == 200
    assert (
        resp.headers["Content-Disposition"]
        == 'attachment; filename="pressespiegel_Alpha_AG_2026-07.html"'
    )


def test_the_download_carries_no_links_back_into_the_application(http, factory, mandate):
    """The screen navigates; the file a client receives must not — a recipient
    has no account behind those links."""
    with factory() as session:
        _seed_july(session, mandate)

    screen = http.get(f"/client/{mandate}/pressespiegel?zeitraum=2026-07").text
    export = http.get(f"/client/{mandate}/pressespiegel.html?zeitraum=2026-07").text
    assert 'href="/' in screen
    # Every target the file carries, not just the root-relative ones: a bare
    # "client/1/berichte" or a protocol-relative "//host/…" is a link back into
    # the application too, and asserting on one spelling would let those through.
    targets = _TARGETS.findall(export)
    assert targets, "the export should still carry the outlets' own links"
    assert all(target.startswith(("http://", "https://")) for target in targets), targets
    # The content itself is identical: the difference is chrome outside the document.
    doc = re.compile(r'<article class="doc">.*</article>', re.DOTALL)
    assert doc.search(screen).group() == doc.search(export).group()


def test_the_review_page_links_to_the_pressespiegel_of_the_same_period(
    http, mandate
):
    """The document has to be reachable from somewhere, and the period picker is
    where a consultant already is when they are thinking in months. Offered even
    with no report drafted: the archive of a month exists either way."""
    body = http.get(f"/client/{mandate}/berichte?zeitraum=2026-07").text
    assert f'href="/client/{mandate}/pressespiegel?zeitraum=2026-07"' in body


def test_an_empty_period_yields_the_explanatory_sentence(http, mandate):
    body = http.get(f"/client/{mandate}/pressespiegel?zeitraum=2026-07").text
    assert "keine Berichterstattung über den Mandanten" in body
    assert 'class="story__h"' not in body


def test_the_document_wears_the_mandates_branding(http, factory, mandate):
    with factory() as session:
        _seed_july(session, mandate)

    body = http.get(f"/client/{mandate}/pressespiegel?zeitraum=2026-07").text
    assert branding.colour("Alpha AG") in body
    assert branding.monogram("Alpha AG") in body


def test_a_stored_logo_replaces_the_monogram(http, factory, mandate):
    with factory() as session:
        client = session.get(Client, mandate)
        client.logo_url = "https://cdn.alpha.example/logo.png"
        session.commit()

    body = http.get(f"/client/{mandate}/pressespiegel?zeitraum=2026-07").text
    assert "https://cdn.alpha.example/logo.png" in body
    # The monogram element (the CSS rule for it legitimately stays in <style>).
    assert '<span class="brandmark brandmark--mono">' not in body


def test_an_unknown_mandate_is_a_404(http):
    assert http.get("/client/999/pressespiegel").status_code == 404


def test_an_unreadable_period_falls_back_instead_of_erroring(http, mandate):
    assert http.get(f"/client/{mandate}/pressespiegel?zeitraum=quatsch").status_code == 200


@pytest.mark.parametrize("zeitraum", ["0001-01", "0000-01", "9999-12", "2026-13"])
def test_a_period_outside_the_calendar_falls_back_instead_of_erroring(
    http, mandate, zeitraum
):
    """A month with the right shape can still leave the calendar, and at both
    ends: converting local midnight of the first year to UTC underflows past it
    (an OverflowError, not a ValueError), and December 9999 ends in a year that
    does not exist. A hand-edited query string is not a 500."""
    assert http.get(f"/client/{mandate}/pressespiegel?zeitraum={zeitraum}").status_code == 200
    assert (
        http.get(f"/client/{mandate}/pressespiegel.html?zeitraum={zeitraum}").status_code
        == 200
    )


# --- Language --------------------------------------------------------------------

_LITERAL_T = re.compile(r"""\bt\(\s*["'](.+?)["']\s*\)""", re.DOTALL)


def test_every_german_string_in_the_template_is_translated():
    """Checked against the template itself rather than a list somebody has to
    remember to extend — the same rule the report templates follow."""
    from newspulse.web import app as web_app

    source = (web_app._TEMPLATES_DIR / "press_clippings.html").read_text("utf-8")
    known = set(i18n.known_keys())
    missing = [text for text in _LITERAL_T.findall(source) if text not in known]
    assert not missing, missing


def test_the_tonality_values_the_template_renders_dynamically_are_translated():
    known = set(i18n.known_keys())
    for tone in Tonality:
        assert tone.value in known, tone


# --- The golden file -------------------------------------------------------------


def test_the_seeded_month_renders_exactly_the_golden_file(
    http, factory, mandate, monkeypatch
):
    """Every wording change in the document becomes a diff a reviewer can read.

    The zone is pinned because the golden bytes contain rendered local dates, and
    a suite that produced different bytes on a differently-configured host would
    be testing the host. Deliberate changes regenerate the fixture with
    ``NEWSPULSE_UPDATE_GOLDEN=1`` — and the diff goes into review with them.
    """
    monkeypatch.setattr(config, "LOCAL_ZONE", ZoneInfo("Europe/Berlin"))
    with factory() as session:
        _seed_july(session, mandate)

    body = http.get(f"/client/{mandate}/pressespiegel.html?zeitraum=2026-07").text
    if os.environ.get("NEWSPULSE_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(body, "utf-8")
    assert body == GOLDEN.read_text("utf-8")
