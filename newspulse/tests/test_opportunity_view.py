"""Das Dossier zur Gelegenheit (GEL-01): the page, and the five things it must
never do.

The engine — standing, origin, the window — is pinned in ``test_newsjack.py``
and the card on Heute in ``test_newsjack_view.py``. This file is about the page
behind the card, and its load-bearing tests are the negative ones:

* a ``GET`` writes no row, and the row counts before and after say so;
* a ``GET`` calls no model, and an analysis backend that raises on use proves it
  by never being reached;
* a rejection, a stranger's mandate and an unknown id are all 404;
* an ended opportunity keeps rendering and loses its buttons;
* a tab that leads nowhere is not in the strip, and every one that is resolves.

The urgency number is checked against a fixture calculated by hand in the test's
own docstring, never against a second call of the code that produced it. The
clock is injected everywhere it can be, and where it cannot — the route reads
``dt.datetime.now`` the way every other page does — the seeded window is a
distance from the real now with a wide margin, so a slow runner cannot flip a
floor.

Nothing here reaches a model and nothing reaches the network.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, opportunity
from newspulse.matching import title_hash
from newspulse.models import (
    Angle,
    Article,
    Asset,
    Base,
    CheckState,
    Client,
    Contact,
    NewsjackOpportunity,
    Outreach,
    Stakeholder,
    StakeholderLevel,
    Standing,
    TopicHit,
)
from newspulse.web.app import create_app, get_db
from newspulse.web.routes import opportunity_view

_BERLIN = ZoneInfo("Europe/Berlin")

_HEADLINE = "Bundesnetzagentur startet Konsultation zu Netzentgelten im Verteilnetz"
_REASON = "Solaris betreibt selbst Einspeisepunkte und hat 2025 dazu publiziert."


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


# --- Builders ---------------------------------------------------------------------


def _article(
    session,
    *,
    title: str,
    source: str,
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
    session,
    client: Client,
    *,
    hours_left: float = 12.0,
    window_hours: float = 48.0,
    pickup_count: int = 3,
    standing: Standing = Standing.BELEGT,
    reason: str = _REASON,
    dismissed_at: dt.datetime | None = None,
    outlook: list[dict] | None = None,
    outlook_at: dt.datetime | None = None,
    brain_version: int | None = 7,
) -> NewsjackOpportunity:
    """One weighed story whose window ends ``hours_left`` from the real now.

    ``window_hours`` is the window's full width, so the origin's timestamp
    follows from the two — which is what the deadline share is computed over.
    """
    now = dt.datetime.now(dt.UTC)
    ends = now + dt.timedelta(hours=hours_left)
    origin = _article(
        session,
        title=_HEADLINE,
        source="Handelsblatt",
        at=ends - dt.timedelta(hours=window_hours),
        author="Michael Bröcker",
        on_radar_for=client,
    )
    row = NewsjackOpportunity(
        client_id=client.id,
        article_id=origin.id,
        standing=standing,
        reason=reason,
        pickup_count=pickup_count,
        window_ends_at=ends,
        created_at=origin.published_at + dt.timedelta(minutes=15),
        dismissed_at=dismissed_at,
        brain_version=brain_version,
        outlook=outlook or [],
        outlook_at=outlook_at,
    )
    session.add(row)
    session.commit()
    return row


def _url(row: NewsjackOpportunity) -> str:
    return f"/client/{row.client_id}/gelegenheit/{row.id}"


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
    session,
    angle: Angle,
    *,
    kind: str = "pressemitteilung",
    state: CheckState = CheckState.UNGEPRUEFT,
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


# --- Who gets the page, and who gets a 404 -----------------------------------------


def test_a_belegt_opportunity_renders_its_dossier(web, session, mandate):
    row = _opportunity(session, mandate)

    page = web.get(_url(row))

    assert page.status_code == 200
    assert _HEADLINE in page.text
    assert _REASON in page.text
    assert "Entscheidungsspur" in page.text


def test_an_unknown_id_is_a_404(web, session, mandate):
    assert web.get(f"/client/{mandate.id}/gelegenheit/9999").status_code == 404


def test_another_mandates_opportunity_is_a_404(web, session, mandate):
    """The id is real and the mandate is real; the pairing is not."""
    other = Client(name="Helio GmbH")
    session.add(other)
    session.commit()
    row = _opportunity(session, mandate)

    assert web.get(f"/client/{other.id}/gelegenheit/{row.id}").status_code == 404


@pytest.mark.parametrize("standing", [Standing.DUENN, Standing.KEINS])
def test_a_rejection_has_no_dossier(web, session, mandate, standing):
    """``duenn`` and ``keins`` are the stored record of a refusal. Rendering a
    dossier for one would present a rejection as a case."""
    row = _opportunity(
        session, mandate, standing=standing, reason="Nichts trägt eine Aussage dazu."
    )

    assert web.get(_url(row)).status_code == 404


# --- The page writes nothing and calls nothing -------------------------------------


def _counts(session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(Angle)),
        session.scalar(select(func.count()).select_from(Asset)),
        session.scalar(select(func.count()).select_from(NewsjackOpportunity)),
    )


def test_opening_the_dossier_writes_no_row(web, session, mandate):
    """The whole reason this page exists as a read: the path from card to text
    writes an occasion on the click, and a page doing the same on *view* would
    leave one behind for every opportunity anybody ever looked at."""
    row = _opportunity(session, mandate)
    before = _counts(session)

    assert web.get(_url(row)).status_code == 200
    session.expire_all()

    assert _counts(session) == before


def test_opening_the_dossier_calls_no_model(web, session, mandate, monkeypatch):
    """An analysis backend that raises the moment it is used. It is never used,
    so the page renders — a page that costs money to look at is not one anybody
    opens four times on a closing morning."""
    from newspulse import analyzer

    def _explode(*args, **kwargs):
        raise AssertionError("the dossier called a model")

    monkeypatch.setattr(analyzer, "invoke_with_fallback", _explode)
    monkeypatch.setattr(analyzer, "invoke_claude_cli", _explode)
    row = _opportunity(session, mandate)

    assert web.get(_url(row)).status_code == 200


# --- The urgency badge (DEC-2) -----------------------------------------------------


def test_the_urgency_matches_a_hand_calculated_fixture(session, mandate):
    """Four outlets, 27 of 48 hours left, standing belegt.

    Reach:    min(4, 8) / 8 * 40  = 20
    Deadline: (48 - 27) / 48 * 40 = 17.5 -> 18   (rounded half up)
    Standing: belegt              = 20
    Total                         = 58

    The same arithmetic the mock's badge shows, which is why those are the
    numbers: if this ever disagrees, one of the two is wrong and it is visible.
    """
    row = _opportunity(session, mandate, hours_left=27, window_hours=48, pickup_count=4)
    # Read at exactly the moment the stored window implies, so the fixture is
    # arithmetic and not a race with the wall clock for a floor.
    exactly_27_hours_before_the_end = row.window_ends_at - dt.timedelta(hours=27)

    score = opportunity.urgency(row, now=exactly_27_hours_before_the_end)

    assert (score.reach, score.deadline, score.standing) == (20, 18, 20)
    assert score.total == 58
    assert (score.media, score.hours_left, score.hours_total) == (4, 27, 48)


def test_the_reach_stops_at_the_media_cap(session, mandate):
    """A story on twenty mastheads is not five times a story on four."""
    row = _opportunity(session, mandate, hours_left=48, window_hours=48, pickup_count=20)

    assert opportunity.urgency(row, now=dt.datetime.now(dt.UTC)).reach == 40


def test_a_closed_window_scores_the_full_deadline_and_never_more(session, mandate):
    """Past the end the share is capped at one, so the number stays inside 0..100."""
    row = _opportunity(session, mandate, hours_left=-10, window_hours=48)

    score = opportunity.urgency(row, now=dt.datetime.now(dt.UTC))

    assert score.deadline == 40
    assert score.hours_left == 0
    assert score.total <= 100


def test_the_badge_and_the_provenance_bar_carry_the_three_addends(web, session, mandate):
    """The badge shows the number alone; the calculation stands beside it and
    again in the provenance bar, so it can be checked without leaving the page."""
    # The route reads the real clock, so the seed sits in the middle of both the
    # hour floor (24) and the rounding bucket (Frist 20 holds from 23.4 to 24.6
    # hours left) — half an hour of margin either way, so a slow runner cannot
    # flip a digit.
    row = _opportunity(
        session, mandate, hours_left=24.4, window_hours=48, pickup_count=4
    )

    page = web.get(_url(row)).text

    assert "60" in page
    assert "Verbreitung 20" in page
    assert "Frist 20" in page
    assert "Stehen 20" in page
    assert "Dringlichkeit gerechnet aus" in page


def test_the_outlet_count_and_the_readable_pieces_are_never_conflated(
    web, session, mandate
):
    """Two numbers that legitimately differ, and the bar names both.

    ``pickup_count`` is the distinct outlets counted when the verdict was
    written and frozen on the row — the score's own input. The sources list is
    what is still stored today, which is fewer once radar rows age past the
    lookback. A page printing one under the other's name would be lying about
    both, so this pins that they are labelled apart.
    """
    row = _opportunity(session, mandate, pickup_count=4)

    page = web.get(_url(row)).text

    assert "4 Medien trugen die Story bei der Prüfung" in page
    assert "1 Beitrag/Beiträge davon sind gespeichert" in page


# --- The sources list ---------------------------------------------------------------


def test_the_sources_list_names_every_piece_oldest_first_with_the_origin_marked(
    web, session, mandate
):
    """The row anchors the origin only; the pickups are re-derived by clustering
    the stored radar the way the scan did."""
    row = _opportunity(session, mandate, hours_left=12, window_hours=48)
    _article(
        session,
        title=_HEADLINE + " — Verbände reagieren",
        source="DVZ",
        at=row.article.published_at + dt.timedelta(hours=2),
        author="Jana Wolf",
        on_radar_for=mandate,
    )

    listed = opportunity.sources(session, row)

    assert [s.outlet for s in listed] == ["Handelsblatt", "DVZ"]
    assert listed[0].is_origin and not listed[1].is_origin
    assert [s.author for s in listed] == ["Michael Bröcker", "Jana Wolf"]
    page = web.get(_url(row)).text
    assert "Ursprung" in page and "Jana Wolf" in page


def test_a_piece_without_a_stored_byline_gets_a_named_gap_not_a_guess(
    web, session, mandate
):
    """Most German feeds carry no byline; a plausible name would be used, and it
    would reach the wrong person or nobody."""
    row = _opportunity(session, mandate)
    row.article.author = None
    session.commit()

    assert opportunity.sources(session, row)[0].author == ""
    assert "kein Autor im Beitrag" in web.get(_url(row)).text


# --- The status in the head --------------------------------------------------------


def test_the_status_follows_the_rows_that_exist(session, mandate):
    """Offen until an occasion hangs on it, aktiv once one does — and no column
    anywhere claims either."""
    row = _opportunity(session, mandate)
    now = dt.datetime.now(dt.UTC)

    assert opportunity.status(row, None, now=now) is opportunity.Status.OFFEN
    assert (
        opportunity.status(row, _occasion(session, row), now=now)
        is opportunity.Status.AKTIV
    )


def test_an_ending_beats_a_beginning_in_the_status(session, mandate):
    """A waved-off opportunity that had an occasion is verworfen, not aktiv: the
    last act on it is the one that describes it."""
    row = _opportunity(session, mandate, dismissed_at=dt.datetime.now(dt.UTC))
    angle = _occasion(session, row)

    assert (
        opportunity.status(row, angle, now=dt.datetime.now(dt.UTC))
        is opportunity.Status.VERWORFEN
    )


def test_activating_posts_at_the_same_endpoint_as_the_card(web, session, mandate):
    """"Aktivieren" is not a second way to open an occasion — it is the one the
    card already uses, which is what keeps the race settled in one place."""
    row = _opportunity(session, mandate)

    page = web.get(_url(row)).text

    assert f'action="/client/{mandate.id}/gelegenheit/{row.id}/text"' in page


# --- An opportunity that has ended --------------------------------------------------


def test_an_expired_opportunity_stays_readable_without_buttons(web, session, mandate):
    row = _opportunity(session, mandate, hours_left=-3, window_hours=48)

    page = web.get(_url(row))

    assert page.status_code == 200
    assert _HEADLINE in page.text
    assert "Abgelaufen am" in page.text
    assert f"/gelegenheit/{row.id}/text" not in page.text
    assert f"/gelegenheit/{row.id}/verwerfen" not in page.text


def test_a_dismissed_opportunity_names_when_and_on_what_it_ended(web, session, mandate):
    ended = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    row = _opportunity(session, mandate, dismissed_at=ended)

    page = web.get(_url(row))

    assert page.status_code == 200
    assert "Verworfen am" in page.text
    assert "von Hand abgeräumt" in page.text
    assert f"/gelegenheit/{row.id}/text" not in page.text


# --- The next step, read off the state ----------------------------------------------


def test_without_an_occasion_the_next_step_is_to_write(session, mandate):
    row = _opportunity(session, mandate)

    steps = opportunity.steps(None, [], [])

    assert steps[0].kind is opportunity.StepKind.TEXT
    assert steps[0].weight == "hoch"
    assert [s.label for s in steps[1:]] == ["Format wählen", "Prüfung starten", "Senden"]


def test_an_occasion_without_a_text_asks_for_a_format(session, mandate):
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)

    steps = opportunity.steps(angle, opportunity.texts(session, angle), [])

    assert steps[0].kind is opportunity.StepKind.FORMAT
    assert steps[0].weight == "hoch"


def test_an_unchecked_text_asks_for_the_check(session, mandate):
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, state=CheckState.UNGEPRUEFT)

    steps = opportunity.steps(angle, opportunity.texts(session, angle), [])

    assert steps[0].kind is opportunity.StepKind.PRUEFUNG
    assert steps[0].weight == "hoch"
    assert "ungeprüft" in steps[0].why


def test_a_checked_text_without_a_letter_asks_to_send_and_is_the_last_step(
    session, mandate
):
    """Mittel rather than hoch, and for one reason only: nothing waits behind
    it. The weight is a reading of the chain, not an opinion about importance."""
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, state=CheckState.GEPRUEFT)

    steps = opportunity.steps(angle, opportunity.texts(session, angle), [])

    assert steps[0].kind is opportunity.StepKind.SENDEN
    assert steps[0].weight == "mittel"
    assert len(steps) == 1


def test_a_released_letter_leaves_no_step_to_recommend(session, mandate):
    """A tile inventing a next move once there is nothing left would be exactly
    the guess this whole page refuses."""
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, state=CheckState.GEPRUEFT)
    letter = Outreach(
        angle_id=angle.id,
        client_id=mandate.id,
        journalist="Michael Bröcker",
        outlet="Handelsblatt",
        message="Ein Brief.",
        released_at=dt.datetime.now(dt.UTC),
        released_by="mensch",
    )
    session.add(letter)
    session.commit()

    assert opportunity.steps(angle, opportunity.texts(session, angle), [letter]) == []


def test_the_step_tile_renders_the_next_move_and_its_weight(web, session, mandate):
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, state=CheckState.UNGEPRUEFT)

    page = web.get(_url(row)).text

    assert "Empfohlene nächste Schritte" in page
    assert "Prüfung starten" in page
    assert "hoch" in page


# --- The texts on the occasion, with version and check state ------------------------


def test_a_second_draft_of_one_format_is_its_second_version(session, mandate):
    """The version is the stored order, because that is what the order *is*."""
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    first = _asset(session, angle, kind="pressemitteilung")
    _asset(session, angle, kind="statement")
    # ``ux_assets_angle_kind_unreleased`` allows exactly one unreleased draft per
    # format, so a second version exists only once the first has gone out.
    first.released_at = dt.datetime.now(dt.UTC)
    first.released_by = "mensch"
    session.commit()
    second = _asset(session, angle, kind="pressemitteilung")
    second.generated_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
    session.commit()

    stored = opportunity.texts(session, angle)

    versions = {(t.kind, t.version) for t in stored}
    assert ("pressemitteilung", 1) in versions
    assert ("pressemitteilung", 2) in versions
    assert ("statement", 1) in versions


def test_an_objection_never_renders_like_a_clean_check(web, session, mandate):
    """Three states and not two: "nothing objected" and "nothing looked" must
    not read alike, and neither may look like a pass."""
    row = _opportunity(session, mandate)
    angle = _occasion(session, row)
    _asset(session, angle, kind="statement", state=CheckState.EINWAND)

    page = web.get(_url(row)).text

    assert "Einwand" in page
    assert "geprüft</span>" not in page.replace("ungeprüft</span>", "")


# --- The trail ----------------------------------------------------------------------


def test_the_trail_merges_the_market_and_our_own_steps_in_time_order(
    session, mandate
):
    """One row, sorted by the stored timestamps — and no second table recording
    the same morning a second time."""
    row = _opportunity(session, mandate, hours_left=12, window_hours=48)
    _article(
        session,
        title=_HEADLINE + " — Verbände reagieren",
        source="DVZ",
        at=row.article.published_at + dt.timedelta(hours=2),
        on_radar_for=mandate,
    )
    angle = _occasion(session, row)
    _asset(session, angle, state=CheckState.UNGEPRUEFT)

    story = opportunity.sources(session, row)
    events = opportunity.trail(
        session, row, story, angle, opportunity.texts(session, angle), []
    )

    assert [e.at for e in events] == sorted(e.at for e in events)
    what = [e.what for e in events]
    assert "Gelegenheit erkannt" in what
    assert "Stehen geprüft" in what
    assert "Anlass geöffnet" in what
    assert "pressemitteilung v1" in what
    assert all(e.who for e in events), "every entry names who it came from"


def test_the_trail_names_the_standards_the_standing_was_checked_against(
    session, mandate
):
    row = _opportunity(session, mandate, brain_version=7)

    events = opportunity.trail(
        session, row, opportunity.sources(session, row), None, [], []
    )

    checked = next(e for e in events if e.what == "Stehen geprüft")
    assert checked.who == "Standards v7"


def test_an_unstamped_standing_says_unknown_rather_than_claiming_a_version(
    session, mandate
):
    """NULL means "unknown", which is not the same answer as version 0."""
    row = _opportunity(session, mandate, brain_version=None)

    events = opportunity.trail(
        session, row, opportunity.sources(session, row), None, [], []
    )

    assert next(e for e in events if e.what == "Stehen geprüft").who == (
        "Standards unbekannt"
    )


# --- The audience chips (DEC-6 A) ---------------------------------------------------


def test_the_chips_stand_on_the_mandates_stored_stakeholder_rows(
    web, session, mandate
):
    session.add_all(
        [
            Stakeholder(
                client_id=mandate.id,
                group_name="Netzbetreiber",
                einfluss=StakeholderLevel.HOCH,
                set_by="mensch",
            ),
            Stakeholder(
                client_id=mandate.id,
                group_name="Kommunen",
                einfluss=StakeholderLevel.NIEDRIG,
                set_by="mensch",
            ),
        ]
    )
    session.commit()
    row = _opportunity(session, mandate)

    page = web.get(_url(row)).text

    assert "Netzbetreiber" in page and "Kommunen" in page
    # The order is the map's, not the template's.
    assert page.index("Netzbetreiber") < page.index("Kommunen")


def test_without_a_stakeholder_map_the_chips_are_a_named_gap(web, session, mandate):
    """Never a list written into the template — that is what DEC-6 rules out in
    both of its options."""
    row = _opportunity(session, mandate)

    page = web.get(_url(row)).text

    assert "Für dieses Mandat sind keine Anspruchsgruppen hinterlegt." in page


# --- The media list (DEC-5) ---------------------------------------------------------


def test_the_byline_on_this_story_with_a_contact_outranks_a_stranger(
    session, mandate
):
    """Wrote it (50) + one field piece (5) + contact on file (20) = 75, against
    a byline with neither, and the arithmetic is on the row so it can be checked."""
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
    angle = _occasion(session, row)

    people = opportunity.recipients(
        session, mandate, angle, {"michael bröcker"}, now=dt.datetime.now(dt.UTC)
    )

    top = people[0]
    assert top.name == "Michael Bröcker"
    assert top.wrote_the_story and top.has_contact
    assert top.fit == 75
    assert [p.fit for p in people] == sorted((p.fit for p in people), reverse=True)


def test_the_media_list_never_shows_a_derived_address(web, session, mandate):
    """The tool holds no address it was not given, and it will not invent one:
    the row says whether a contact exists, and nothing more."""
    row = _opportunity(session, mandate)
    session.add(
        Contact(
            name="Michael Bröcker", outlet="Handelsblatt", email="m.broecker@example.de"
        )
    )
    session.commit()
    _occasion(session, row)

    page = web.get(_url(row)).text

    assert "Kontakt da" in page
    assert "m.broecker@example.de" not in page
    assert "@" not in page.split("Wen ansprechen")[1].split("Texte zu dieser")[0]


def test_without_a_stored_byline_the_media_tile_says_so(web, session, mandate):
    row = _opportunity(session, mandate)
    row.article.author = None
    session.commit()

    page = web.get(_url(row)).text

    assert "ist kein Autor gespeichert" in page


# --- The forward look (DEC-3) --------------------------------------------------------


def test_a_stored_forward_look_is_shown_as_an_estimate_with_its_date(
    web, session, mandate
):
    """It carries visibly that it is an estimate and when it was made, so it is
    never read like the evidenced lines beside it."""
    made = dt.datetime.now(dt.UTC) - dt.timedelta(hours=6)
    row = _opportunity(
        session,
        mandate,
        outlook=[{"when": "2 bis 7 Tage", "what": "Verbände positionieren sich"}],
        outlook_at=made,
    )

    page = web.get(_url(row)).text

    assert "2 bis 7 Tage" in page
    assert "Verbände positionieren sich" in page
    assert "Einschätzung bei der Erkennung am" in page
    assert "Keine belegte Zeile." in page


def test_without_a_stored_forward_look_the_tile_says_none_was_made(
    web, session, mandate
):
    """Never computed on the way past: an estimate made at view time would
    charge a model call for looking."""
    row = _opportunity(session, mandate, outlook=[], outlook_at=None)

    page = web.get(_url(row)).text

    assert "keine Vorschau geschrieben" in page
    assert opportunity.outlook(row).exists is False


def test_a_half_written_forward_look_entry_is_dropped(session, mandate):
    """Half a pair renders as "2 bis 7 Tage: " under a heading claiming an
    estimate, which is the one thing an estimate must not be — unreadable."""
    row = _opportunity(
        session,
        mandate,
        outlook=[{"when": "2 bis 7 Tage", "what": ""}, {"when": "", "what": "etwas"}],
        outlook_at=dt.datetime.now(dt.UTC),
    )

    assert opportunity.outlook(row).entries == ()


# --- The tab strip -------------------------------------------------------------------


def test_every_tab_on_the_strip_resolves_to_a_route_that_exists(web, session, mandate):
    """A tab that leads nowhere is worse than one that is missing, because it is
    a promise the reader spends a click discovering was empty."""
    row = _opportunity(session, mandate)

    for tab in opportunity_view.page_tabs(mandate.id, row.id):
        answer = web.get(tab.href, follow_redirects=False)
        assert answer.status_code != 404, f"{tab.label} -> {tab.href}"


def test_situation_is_this_page_and_the_absent_tabs_are_absent(session, mandate):
    """Performance has no per-opportunity measurement behind it and Historie is
    the trail at the bottom of this very page."""
    row = _opportunity(session, mandate)

    tabs = opportunity_view.page_tabs(mandate.id, row.id)

    here = [tab for tab in tabs if tab.here]
    assert [tab.label for tab in here] == ["Situation"]
    assert here[0].href == _url(row)
    labels = {tab.label for tab in tabs}
    assert "Performance" not in labels and "Historie" not in labels


# --- Everything empty, and it still renders -------------------------------------------


def test_an_opportunity_with_nothing_on_it_renders_in_full(web, session, mandate):
    """No texts, no stored bylines, no contacts, no stakeholder map. Every gap is
    a sentence naming what is missing — never a zero and never a placeholder."""
    row = _opportunity(session, mandate)
    row.article.author = None
    session.commit()

    page = web.get(_url(row))

    assert page.status_code == 200
    assert "Zu dieser Gelegenheit ist noch kein Anlass angelegt" in page.text
    assert "kein Autor im Beitrag" in page.text
    assert "Für dieses Mandat sind keine Anspruchsgruppen hinterlegt." in page.text
    assert "keine Vorschau geschrieben" in page.text
    # The next step is still readable, because it is the one thing on the page
    # that never needs anything to have happened yet.
    assert "Text schreiben" in page.text


# --- The way in ------------------------------------------------------------------------


def test_the_card_on_heute_links_to_the_dossier(web, session, mandate):
    row = _opportunity(session, mandate, hours_left=12)

    page = web.get("/today")

    assert _url(row) in page.text


def test_the_mandates_concluded_list_links_to_the_dossier(web, session, mandate):
    """An ended opportunity leaves Heute and stays readable there — and the link
    has to reach a page that still renders."""
    row = _opportunity(session, mandate, hours_left=-4, window_hours=48)

    page = web.get(f"/client/{mandate.id}")

    assert _url(row) in page.text
    assert web.get(_url(row)).status_code == 200
