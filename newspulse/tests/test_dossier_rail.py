"""RAUTE Intelligence, the rail beside the dossier (GEL-02).

The rail's whole reason for existing is a negative one, so most of this file is
negative too. The submitted design had six engines confirming one another with
green ticks, and a tick behind which nothing ran is the most expensive kind of
trust there is: it reads exactly like a checked box and costs nothing to make.

So the load-bearing tests here are:

* a green tick is unreachable from every empty state, checked over all six tiles
  and over the enum rather than over one hand-picked case;
* a zero that means "nie gemessen" and a zero that means "gemessen und nichts
  gefunden" produce different marks and different sentences — the same reason a
  text's check state has three values and not two;
* rendering the rail calls no model and makes no request, proved by an analysis
  backend that raises the moment it is touched;
* every one of the six links resolves to a route this app serves.

The clock is injected wherever the code takes one. Nothing here reaches a model
and nothing here reaches the network.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, dossier_rail, opportunity
from newspulse.dossier_rail import Signal, TileKey
from newspulse.matching import title_hash
from newspulse.models import (
    Angle,
    Article,
    Asset,
    Base,
    CheckState,
    Client,
    ClientFact,
    Contact,
    NewsjackOpportunity,
    ReputationReading,
    ReputationState,
    Standing,
    TopicHit,
    VisibilityAnswer,
    VisibilityBand,
    VisibilityQuestion,
    VisibilityRun,
)
from newspulse.web.app import create_app, get_db

_BERLIN = ZoneInfo("Europe/Berlin")
_HEADLINE = "Bundesnetzagentur startet Konsultation zu Netzentgelten im Verteilnetz"


# --- Fixtures ---------------------------------------------------------------------


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as open_session:
        yield open_session


@pytest.fixture
def web(factory):
    app = create_app()

    def _override():
        open_session = factory()
        try:
            yield open_session
        finally:
            open_session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    """Pin the display zone, so a test states the calendar it renders in."""
    monkeypatch.setattr(config, "LOCAL_ZONE", _BERLIN)


@pytest.fixture
def mandate(session) -> Client:
    client = Client(name="Solaris AG", aliases=["Solaris"], industry="Solarenergie")
    session.add(client)
    session.commit()
    return client


@pytest.fixture
def now() -> dt.datetime:
    """One fixed instant for every call that takes a clock."""
    return dt.datetime(2026, 6, 4, 9, 0, tzinfo=dt.UTC)


# --- Builders ---------------------------------------------------------------------


def _article(
    session,
    *,
    title: str = _HEADLINE,
    source: str = "Handelsblatt",
    at: dt.datetime,
    author: str | None = None,
    on_radar_for: Client | None = None,
) -> Article:
    article = Article(
        title=title,
        url=f"https://example.de/{abs(hash((title, source)))}",
        source=source,
        published_at=at,
        fetched_at=at,
        summary_text="Die Bundesnetzagentur prüft die Entgeltsystematik.",
        title_hash=title_hash(title, source),
        author=author,
    )
    session.add(article)
    session.commit()
    if on_radar_for is not None:
        session.add(TopicHit(client_id=on_radar_for.id, article_id=article.id))
        session.commit()
    return article


def _opportunity(
    session, client: Client, *, author: str | None = "Michael Bröcker"
) -> NewsjackOpportunity:
    """One weighed story whose window is open a long way past the real clock."""
    ends = dt.datetime.now(dt.UTC) + dt.timedelta(hours=12)
    origin = _article(
        session,
        at=ends - dt.timedelta(hours=48),
        author=author,
        on_radar_for=client,
    )
    row = NewsjackOpportunity(
        client_id=client.id,
        article_id=origin.id,
        standing=Standing.BELEGT,
        reason="Solaris betreibt selbst Einspeisepunkte.",
        pickup_count=3,
        window_ends_at=ends,
        created_at=origin.published_at + dt.timedelta(minutes=15),
        brain_version=7,
    )
    session.add(row)
    session.commit()
    return row


def _occasion(session, row: NewsjackOpportunity) -> Angle:
    angle = Angle(
        client_id=row.client_id,
        newsjack_id=row.id,
        subject=row.article.title,
        message=row.reason,
        context="Zuerst bei Handelsblatt.",
        article_ids=[row.article_id],
        generated_at=dt.datetime.now(dt.UTC),
    )
    session.add(angle)
    session.commit()
    return angle


def _asset(
    session, angle: Angle, *, kind: str = "pressemitteilung", state: CheckState
) -> Asset:
    reviewed = state is not CheckState.UNGEPRUEFT
    asset = Asset(
        client_id=angle.client_id,
        angle_id=angle.id,
        kind=kind,
        title="Solaris zur Netzentgeltreform",
        body="Ein Text.",
        generated_at=dt.datetime.now(dt.UTC),
        reviewed_by="modell" if reviewed else "",
        review_ok=state is not CheckState.EINWAND,
        guide_reviewed_by="modell" if reviewed else "",
        guide_review_ok=state is not CheckState.EINWAND,
    )
    session.add(asset)
    session.commit()
    return asset


def _measurement(
    session,
    client: Client,
    *,
    named: tuple[bool, ...],
    position: int | None = None,
    bands: tuple[VisibilityBand, ...] | None = None,
) -> VisibilityRun:
    """One finished run, a question and an answer per entry of ``named``."""
    moment = dt.datetime.now(dt.UTC)
    run = VisibilityRun(
        client_id=client.id,
        ran_at=moment,
        providers_asked=["claude"],
        finished_at=moment,
    )
    session.add(run)
    session.commit()
    for index, was_named in enumerate(named):
        band = (bands or ())[index] if bands else VisibilityBand.KATEGORIE
        question = VisibilityQuestion(
            client_id=client.id,
            text=f"Wer baut Solarparks? ({index})",
            band=band,
        )
        session.add(question)
        session.commit()
        session.add(
            VisibilityAnswer(
                run_id=run.id,
                question_id=question.id,
                provider="claude",
                answer="Eine Antwort.",
                named=was_named,
                position=position if was_named else None,
            )
        )
    session.commit()
    return run


def _reading(
    session,
    client: Client,
    *,
    day: dt.date,
    state: ReputationState = ReputationState.RUHIG,
    articles: int = 14,
    negative: int = 2,
) -> ReputationReading:
    row = ReputationReading(
        client_id=client.id,
        day=day,
        state=state,
        outlets=1,
        national=False,
        articles=articles,
        negative=negative,
        named=False,
        points=3,
        computed_at=dt.datetime.now(dt.UTC),
    )
    session.add(row)
    session.commit()
    return row


def _rail(session, client, row, *, now, people=None):
    """The six tiles for one opportunity, keyed for the assertions below."""
    occasion = opportunity.occasion(session, row)
    texts = opportunity.texts(session, occasion)
    tiles = dossier_rail.rail(session, client, people or [], texts, now=now)
    return {tile.key: tile for tile in tiles}


def _url(row: NewsjackOpportunity) -> str:
    return f"/client/{row.client_id}/gelegenheit/{row.id}"


# --- The six, in the template's order ---------------------------------------------


def test_the_rail_is_six_tiles_in_the_templates_order_and_names(session, mandate, now):
    """The order and the words are the design's, and they are pinned here so a
    reshuffle in the module is a failing test rather than a different product."""
    tiles = dossier_rail.rail(session, mandate, [], [], now=now)

    assert [tile.key for tile in tiles] == [
        TileKey.POSITIONING,
        TileKey.MEDIA,
        TileKey.ASSET,
        TileKey.QUALITY,
        TileKey.VISIBILITY,
        TileKey.INTELLIGENCE,
    ]
    assert [tile.title for tile in tiles] == [
        "Positioning",
        "Media & Relationship",
        "Communication Asset",
        "Quality & Verification",
        "AI Visibility & GEO",
        "Communication Intelligence",
    ]


# --- The mark: three cases, and a tick that no absence can reach -------------------


def test_a_missing_run_never_wears_the_green_tick(session, mandate, now):
    """The rule the whole rail was rebuilt for, checked over all six tiles at
    once on a mandate with nothing on file: no profile, no byline, no text, no
    measurement, no reading."""
    _opportunity(session, mandate, author=None)

    tiles = dossier_rail.rail(session, mandate, [], [], now=now)

    assert [tile.signal for tile in tiles] == [Signal.NICHTS] * 6
    assert not any(tile.measured for tile in tiles)
    assert Signal.OHNE_EINWAND not in {tile.signal for tile in tiles}


def test_texts_nobody_has_read_are_nothing_run_and_not_a_clean_check(
    session, mandate, now
):
    """Three unchecked texts and zero objections is the trap a two-valued mark
    walks into: 0 Einwände reads as clean and is the exact opposite."""
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    for kind in ("pressemitteilung", "statement", "qa"):
        _asset(session, angle, kind=kind, state=CheckState.UNGEPRUEFT)

    tiles = _rail(session, mandate, row, now=now)

    assert tiles[TileKey.QUALITY].signal is Signal.NICHTS
    assert tiles[TileKey.QUALITY].figures.objected == 0
    # The Asset tile did run — three texts exist — and nobody objected to them.
    # "Unfertig" is the Quality tile's sentence above, not an objection here:
    # ``EINWAND`` reads out as "Gelaufen, mit Einwand" and would be a claim
    # about a checker who never looked.
    assert tiles[TileKey.ASSET].signal is Signal.OHNE_EINWAND
    assert tiles[TileKey.ASSET].figures.objected == 0


def test_a_tick_never_stands_over_a_text_nobody_read(session, mandate, now):
    """One text checked and two nobody opened is the common closing morning, and
    it used to be enough for the green tick — printed directly above this tile's
    own result line, "Mit Einwand: 0 · ungeprüft: 2", which the CSS paints in the
    colour of the mark. A set that is not through has not passed."""
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, kind="pressemitteilung", state=CheckState.GEPRUEFT)
    _asset(session, angle, kind="statement", state=CheckState.UNGEPRUEFT)
    _asset(session, angle, kind="qa", state=CheckState.UNGEPRUEFT)

    tile = _rail(session, mandate, row, now=now)[TileKey.QUALITY]

    assert (tile.figures.objected, tile.figures.unchecked) == (0, 2)
    assert tile.signal is Signal.EINWAND


def test_the_asset_tile_claims_no_objection_that_nobody_raised(session, mandate, now):
    """``EINWAND`` renders as "Gelaufen, mit Einwand" in a tooltip and in an
    ``aria-label``. An unfinished set reached it, so the tile asserted an
    objection to a screen reader while the Quality tile beside it printed
    "Mit Einwand: 0" about the same text."""
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, kind="pressemitteilung", state=CheckState.UNGEPRUEFT)

    tile = _rail(session, mandate, row, now=now)[TileKey.ASSET]

    assert tile.figures.objected == 0
    assert tile.signal is not Signal.EINWAND


def test_a_measurement_from_years_ago_is_not_a_clean_bill(session, mandate, now):
    """``visibility.latest_run`` and ``reputation.history`` hand back the newest
    row whatever its day, so without a bound a sweep switched off in 2023 keeps
    its green tick forever — the same untrue mark as a tick over a run that
    never happened, wearing a date."""
    row = _opportunity(session, mandate)
    _measurement(session, mandate, named=(True,), position=1)
    session.query(VisibilityRun).update({"ran_at": dt.datetime(2023, 1, 4, tzinfo=dt.UTC)})
    _reading(session, mandate, day=dt.date(2023, 1, 4))
    session.commit()

    tiles = _rail(session, mandate, row, now=now)

    assert tiles[TileKey.VISIBILITY].signal is Signal.EINWAND
    assert tiles[TileKey.INTELLIGENCE].signal is Signal.EINWAND
    # And the tile says how old, rather than only that something is wrong.
    assert tiles[TileKey.VISIBILITY].stale_days == 1247
    assert tiles[TileKey.INTELLIGENCE].stale_days == 1247


def test_one_reachable_byline_out_of_many_is_not_a_clean_media_list(
    session, mandate, now
):
    """The tick asks for the whole list. One address out of seventeen was passing
    it, and the tile then printed "1 von 17" in green beside a check mark."""
    row = _opportunity(session, mandate)
    _occasion(session, row)
    session.add(
        Contact(name="Michael Bröcker", outlet="Handelsblatt", email="m.b@example.de")
    )
    session.commit()
    _article(
        session,
        title="Netzentgelte: Länder fordern Nachbesserung",
        source="DVZ",
        at=row.article.published_at + dt.timedelta(hours=1),
        author="Jana Wolf",
        on_radar_for=mandate,
    )
    people = opportunity.recipients(session, mandate, None, set(), now=now)

    tile = _rail(session, mandate, row, now=now, people=people)[TileKey.MEDIA]

    assert tile.figures.reach.with_contact == 1
    assert tile.figures.reach.named > 1
    assert tile.signal is Signal.EINWAND


def test_every_tile_key_renders_a_sentence_of_its_own(web, session, mandate):
    """The partial dispatches on ``tile.key`` through an if/elif chain with no
    ``else``, so a seventh key would draw a frame, a mark and a link with no
    sentence between them. Counted against the enum, so that fails here."""
    row = _opportunity(session, mandate)

    page = web.get(_url(row)).text

    rail = page.split("Was RauteOS zum Mandat weiß")[1].split("Entscheidungsspur")[0]
    assert rail.count('class="know__res"') == len(TileKey)


def test_a_check_that_objected_is_marked_apart_from_one_that_did_not(
    session, mandate, now
):
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, kind="pressemitteilung", state=CheckState.GEPRUEFT)

    clean = _rail(session, mandate, row, now=now)[TileKey.QUALITY]
    _asset(session, angle, kind="statement", state=CheckState.EINWAND)
    objected = _rail(session, mandate, row, now=now)[TileKey.QUALITY]

    assert clean.signal is Signal.OHNE_EINWAND
    assert objected.signal is Signal.EINWAND
    assert (objected.figures.objected, objected.figures.unchecked) == (1, 0)


def test_every_signal_the_module_can_produce_is_one_of_the_three(
    session, mandate, now
):
    """A fourth state would slip through the template's three branches and draw
    an unstyled tile, so the enum is the contract and this pins it."""
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, state=CheckState.GEPRUEFT)
    _measurement(session, mandate, named=(True,), position=1)
    _reading(session, mandate, day=now.date())

    tiles = dossier_rail.rail(session, mandate, [], [], now=now)

    assert {tile.signal for tile in tiles} <= set(Signal)
    assert len(Signal) == 3


# --- A measured zero and an unmeasured one do not read alike ----------------------


def test_a_measurement_that_named_nobody_differs_from_a_mandate_never_measured(
    session, mandate, now
):
    """Both tiles show a zero. One is a finding about the mandate, the other is a
    gap in the tool's own housekeeping, and they may not read the same."""
    row = _opportunity(session, mandate)
    never = _rail(session, mandate, row, now=now)[TileKey.VISIBILITY]

    _measurement(session, mandate, named=(False, False))
    measured = _rail(session, mandate, row, now=now)[TileKey.VISIBILITY]

    assert never.signal is Signal.NICHTS
    assert never.figures.run.measured_at is None
    assert measured.signal is Signal.EINWAND
    assert measured.figures.run.measured_at is not None
    assert (measured.figures.run.answers, measured.figures.run.named_in) == (2, 0)
    assert never.signal is not measured.signal


def test_the_two_zeros_render_as_two_different_sentences(web, session, mandate):
    """The distinction has to survive the template, not only the dataclass."""
    row = _opportunity(session, mandate)

    unmeasured = web.get(_url(row)).text
    _measurement(session, mandate, named=(False, False))
    measured = web.get(_url(row)).text

    assert "Dieses Mandat wurde noch nie gemessen." in unmeasured
    assert "Dieses Mandat wurde noch nie gemessen." not in measured
    assert "0 von 2" in measured
    assert "Antworten nennen das Mandat, gemessen am" in measured


def test_a_mandate_never_read_is_never_rendered_as_quiet(session, mandate, now):
    """``ruhig`` is a reading. Printing the reassuring word for an absent one is
    the worst sentence this rail could carry."""
    row = _opportunity(session, mandate)

    tile = _rail(session, mandate, row, now=now)[TileKey.INTELLIGENCE]

    assert tile.signal is Signal.NICHTS
    assert tile.figures.state is None


# --- What each tile stands on -----------------------------------------------------


def test_positioning_names_the_stored_line_and_when_it_was_confirmed(
    session, mandate, now
):
    row = _opportunity(session, mandate)
    session.add(
        ClientFact(
            client_id=mandate.id,
            key="positionierung",
            value="Führender Anbieter regenerativer Klimaprozesse.",
            filled_by="mensch",
        )
    )
    mandate.profile_checked_at = now - dt.timedelta(days=3)
    session.commit()

    tile = _rail(session, mandate, row, now=now)[TileKey.POSITIONING]

    assert tile.figures.value == "Führender Anbieter regenerativer Klimaprozesse."
    assert tile.figures.checked.at == now - dt.timedelta(days=3)
    assert tile.figures.checked.days == 3
    assert tile.signal is Signal.OHNE_EINWAND


def test_a_positioning_nobody_has_confirmed_objects_rather_than_passing(
    session, mandate, now
):
    """A line on file with no check stamp is something that ran and needs a
    person — not a green tick, and not an empty tile either."""
    row = _opportunity(session, mandate)
    session.add(
        ClientFact(client_id=mandate.id, key="positionierung", value="Etwas.")
    )
    session.commit()

    tile = _rail(session, mandate, row, now=now)[TileKey.POSITIONING]

    assert tile.figures.checked.never
    assert tile.signal is Signal.EINWAND


def test_media_names_the_count_and_the_best_placed_byline_with_its_origin(
    web, session, mandate
):
    """DEC-5 orders the list, so the top of it is the answer — and the origin is
    ``pitch``'s own words rather than a sentence this tile invents."""
    row = _opportunity(session, mandate)
    session.add(
        Contact(
            name="Michael Bröcker", outlet="Handelsblatt", email="m.broecker@example.de"
        )
    )
    session.commit()
    _article(
        session,
        title="Netzentgelte: Länder fordern Nachbesserung",
        source="DVZ",
        at=row.article.published_at + dt.timedelta(hours=1),
        author="Jana Wolf",
        on_radar_for=mandate,
    )
    _occasion(session, row)

    page = web.get(_url(row)).text

    assert "1 von 2" in page
    assert "vorgeschlagenen Bylines haben einen hinterlegten Kontakt." in page
    assert "Bestplatziert" in page
    assert "Michael Bröcker" in page


def test_media_objects_when_the_list_has_names_and_no_way_to_reach_them(
    session, mandate, now
):
    """Seventeen names and no address is a finding, not an empty list."""
    row = _opportunity(session, mandate)
    _occasion(session, row)
    people = opportunity.recipients(session, mandate, None, set(), now=now)
    assert people, "the story's byline should produce at least one recipient"

    tile = _rail(session, mandate, row, now=now, people=people)[TileKey.MEDIA]

    assert tile.figures.reach.named == len(people)
    assert tile.figures.reach.with_contact == 0
    assert tile.figures.lead is people[0]
    assert tile.signal is Signal.EINWAND


def test_the_asset_tile_counts_the_texts_and_the_quality_tile_counts_the_checks(
    session, mandate, now
):
    """One occasion, three texts, one of each state — and the two tiles read the
    same stored check state for two different questions."""
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, kind="pressemitteilung", state=CheckState.GEPRUEFT)
    _asset(session, angle, kind="statement", state=CheckState.EINWAND)
    _asset(session, angle, kind="qa", state=CheckState.UNGEPRUEFT)

    tiles = _rail(session, mandate, row, now=now)

    asset = tiles[TileKey.ASSET].figures
    assert (asset.texts, asset.checked) == (3, 1)
    quality = tiles[TileKey.QUALITY].figures
    assert (quality.objected, quality.unchecked) == (1, 1)
    assert tiles[TileKey.QUALITY].signal is Signal.EINWAND


def test_visibility_names_the_band_and_the_date_of_the_newest_measurement(
    session, mandate, now
):
    """The band furthest from the brand the mandate reached: a mention on a
    ``problem`` question is the finding, a ``marke`` one is table stakes."""
    row = _opportunity(session, mandate)
    _measurement(
        session,
        mandate,
        named=(True, True),
        position=2,
        bands=(VisibilityBand.MARKE, VisibilityBand.PROBLEM),
    )

    tile = _rail(session, mandate, row, now=now)[TileKey.VISIBILITY]

    assert tile.figures.band is VisibilityBand.PROBLEM
    assert tile.figures.run.measured_at is not None
    assert (tile.figures.run.answers, tile.figures.run.named_in) == (2, 2)
    assert tile.figures.run.best_position == 2
    assert tile.signal is Signal.OHNE_EINWAND


def test_intelligence_names_the_band_and_the_pieces_it_stands_on(
    session, mandate, now
):
    row = _opportunity(session, mandate)
    _reading(
        session,
        mandate,
        day=now.date(),
        state=ReputationState.BEOBACHTUNG,
        articles=14,
        negative=2,
    )

    tile = _rail(session, mandate, row, now=now)[TileKey.INTELLIGENCE]

    assert tile.figures.state is ReputationState.BEOBACHTUNG
    assert (tile.figures.articles, tile.figures.negative) == (14, 2)
    assert tile.figures.day == now.date()
    # Above ``ruhig`` is a state that wants a person, not a green tick.
    assert tile.signal is Signal.EINWAND


def test_a_quiet_reading_is_the_one_reputation_state_that_passes(
    session, mandate, now
):
    row = _opportunity(session, mandate)
    _reading(session, mandate, day=now.date(), state=ReputationState.RUHIG)

    tile = _rail(session, mandate, row, now=now)[TileKey.INTELLIGENCE]

    assert tile.signal is Signal.OHNE_EINWAND


# --- The links --------------------------------------------------------------------


def test_every_tile_carries_a_link_and_all_six_resolve_to_a_route(
    web, session, mandate, now
):
    """Half of what makes a line useful is the page it came from — and a link
    that 404s is worse than none, because it costs a click to discover."""
    tiles = dossier_rail.rail(session, mandate, [], [], now=now)

    assert len(tiles) == 6
    for tile in tiles:
        assert tile.href, tile.key
        assert web.get(tile.href, follow_redirects=False).status_code != 404, tile.href
    # Resolving is half of it. ``/today`` builds its band over the roster it is
    # showing, so the unfiltered page answers with the portfolio's standing —
    # a different measurement from the one this tile just printed.
    by_key = {tile.key: tile for tile in tiles}
    assert by_key[TileKey.INTELLIGENCE].href == f"/today?client={mandate.id}"


def test_an_empty_tile_links_to_the_page_its_row_would_come_from(
    session, mandate, now
):
    """A tile that says what is missing and does not say where it comes from
    leaves the reader to go looking, which is the work this page exists to end.

    So the href is the same whether the row exists or not: the page it came from
    and the page it would come from are one address.
    """
    _opportunity(session, mandate, author=None)
    empty = {tile.key: tile for tile in dossier_rail.rail(session, mandate, [], [], now=now)}

    session.add(
        ClientFact(client_id=mandate.id, key="positionierung", value="Etwas.")
    )
    session.commit()
    filled = {tile.key: tile for tile in dossier_rail.rail(session, mandate, [], [], now=now)}

    assert not empty[TileKey.POSITIONING].measured
    assert filled[TileKey.POSITIONING].measured
    assert empty[TileKey.POSITIONING].href == filled[TileKey.POSITIONING].href
    assert all(tile.href for tile in empty.values())


def test_the_rendered_rail_carries_six_links_that_all_resolve(web, session, mandate):
    row = _opportunity(session, mandate)

    page = web.get(_url(row)).text

    rail = page.split("Was RauteOS zum Mandat weiß")[1].split("Entscheidungsspur")[0]
    hrefs = re.findall(r'class="know__l" href="([^"]+)"', rail)
    assert len(hrefs) == 6
    for href in hrefs:
        assert web.get(href, follow_redirects=False).status_code != 404, href


# --- The rail costs nothing to look at --------------------------------------------


def test_building_the_rail_calls_no_model(session, mandate, now, monkeypatch):
    """An analysis backend that raises the moment it is used. It is never used."""
    from newspulse import analyzer

    def _explode(*args, **kwargs):
        raise AssertionError("the rail called a model")

    monkeypatch.setattr(analyzer, "invoke_with_fallback", _explode)
    monkeypatch.setattr(analyzer, "invoke_claude_cli", _explode)
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, state=CheckState.GEPRUEFT)
    _measurement(session, mandate, named=(True,), position=1)
    _reading(session, mandate, day=now.date())

    tiles = _rail(session, mandate, row, now=now)

    assert len(tiles) == 6


def test_rendering_the_rail_opens_no_socket(web, session, mandate, monkeypatch):
    """Below every HTTP client in this process is one call, and it raises here.

    Patched at the socket rather than at ``httpx``: the test client *is* an httpx
    client speaking ASGI in-process, so replacing its request method would break
    the request under test instead of proving anything about it. Nothing in the
    page's path opens a connection — the database is in memory and the rail only
    reads it.
    """
    import socket

    def _explode(*args, **kwargs):
        raise AssertionError("the rail went to the network")

    monkeypatch.setattr(socket.socket, "connect", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    row = _opportunity(session, mandate)
    _measurement(session, mandate, named=(True,), position=1)
    _reading(session, mandate, day=dt.datetime.now(dt.UTC).date())

    assert web.get(_url(row)).status_code == 200
