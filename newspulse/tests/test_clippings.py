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
        relevance_score=relevance,
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
    assert 'href="/' not in export
    # The content itself is identical: the difference is chrome outside the document.
    doc = re.compile(r'<article class="doc">.*</article>', re.DOTALL)
    assert doc.search(screen).group() == doc.search(export).group()


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
