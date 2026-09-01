"""Die schnelle Spur auf Heute (UHR-05): the card, the cap, and the record.

Nothing here reaches a model and nothing reaches the network. The engine —
standing, origin, the window — is pinned in ``test_newsjack.py``; this file is
about what a consultant *sees*: the card above the day with the remaining time
as a number, the two buttons, the deliberate cap of three, and the mandate's
archive where a concluded opportunity stays readable.

Windows are seeded relative to the real clock on purpose: the Today route reads
``dt.datetime.now`` the way the crisis offer does, so "12 hours left" has to be
a distance from now, not a fixed calendar that rots. Everything asserted about
the remaining time uses wide margins (a half-hour past the whole hour), so a
slow test runner cannot flip a floor.

The load-bearing cases are the negative ones: no opportunity leaves Heute
exactly as it was, with no placeholder; a rejection is never a card; and a cut
never happens silently.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, i18n, newsjack
from newspulse.matching import title_hash
from newspulse.models import (
    Angle,
    Article,
    Asset,
    Base,
    Client,
    NewsjackOpportunity,
    Standing,
)
from newspulse.notify import (
    Channel,
    FoundOpportunity,
    NotifyConfig,
    notify_opportunities,
)
from newspulse.web.app import create_app, get_db

_BERLIN = ZoneInfo("Europe/Berlin")

#: A market story the mandate is not in — the only kind the fast lane weighs.
_HEADLINE = "Bundesnetzagentur startet Konsultation zu Netzentgelten im Verteilnetz"

#: The model's one sentence: what the mandate's standing rests on.
_REASON = "Solaris betreibt selbst Einspeisepunkte im Verteilnetz und hat 2025 dazu publiziert."


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


def _origin(
    session, *, title: str = _HEADLINE, source: str = "Handelsblatt", hours_ago: float = 4
) -> Article:
    """The story's origin piece, as UHR-04 stored it."""
    at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)
    article = Article(
        title=title,
        url=f"https://example.de/{abs(hash((title, source)))}",
        source=source,
        published_at=at,
        fetched_at=at,
        summary_text="Eine kurze Zusammenfassung.",
        title_hash=title_hash(title, source),
    )
    session.add(article)
    session.commit()
    return article


def _opportunity(
    session,
    client: Client,
    *,
    title: str = _HEADLINE,
    source: str = "Handelsblatt",
    hours_left: float = 12.5,
    pickup_count: int = 3,
    reason: str = _REASON,
    standing: Standing = Standing.BELEGT,
) -> NewsjackOpportunity:
    """One weighed story, its window ending ``hours_left`` from the real now —
    negative for one that has already run out."""
    row = NewsjackOpportunity(
        client_id=client.id,
        article_id=_origin(session, title=title, source=source).id,
        standing=standing,
        reason=reason,
        pickup_count=pickup_count,
        window_ends_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours_left),
    )
    session.add(row)
    session.commit()
    return row


# --- The card ---------------------------------------------------------------------


def test_an_open_opportunity_stands_on_heute_with_the_time_as_a_number(
    web, session, mandate
):
    """The ten-second decision: the remaining hours as a number, the origin
    piece with its outlet, the pickup count, and the standing's sentence."""
    _opportunity(session, mandate, hours_left=12.5, pickup_count=3)

    page = web.get("/today")

    assert page.status_code == 200
    assert "Gelegenheit" in page.text
    assert "12 Std" in page.text
    assert "verbleibend" in page.text
    assert _HEADLINE in page.text
    assert "Zuerst bei" in page.text and "Handelsblatt" in page.text
    assert "3× aufgegriffen" in page.text
    assert _REASON in page.text


def test_under_an_hour_the_card_counts_minutes(web, session, mandate):
    """Zero is not a number anyone can act on — below an hour the same slot
    carries minutes instead."""
    _opportunity(session, mandate, hours_left=40 / 60)

    page = web.get("/today")

    assert "Min" in page.text and "verbleibend" in page.text
    assert "0 Std" not in page.text


def test_the_cards_stand_above_the_days_coverage(web, session, mandate):
    """Above, not beside and not in a tab: an opening is worthless the moment
    it has to be searched for."""
    _opportunity(session, mandate)

    page = web.get("/today")

    assert page.text.index('class="njacks"') < page.text.index('class="cols"')


def test_without_an_open_opportunity_heute_shows_no_placeholder(web, session, mandate):
    """A quiet fast lane leaves the page exactly as it was."""
    page = web.get("/today")

    assert page.status_code == 200
    assert "njacks" not in page.text
    assert "Gelegenheit" not in page.text


def test_an_expired_opportunity_leaves_heute_without_any_run(web, session, mandate):
    """Expiry is a comparison against the clock, not a job: a row nothing ever
    touched again stops being shown on time."""
    _opportunity(session, mandate, hours_left=-1)

    assert _HEADLINE not in web.get("/today").text


def test_a_rejection_is_never_a_card(web, session, mandate):
    """``duenn`` and ``keins`` are audit rows. The card is only ever the one
    verdict that produced an opportunity."""
    _opportunity(
        session,
        mandate,
        standing=Standing.DUENN,
        reason="Nichts Gespeichertes trägt eine Aussage dazu.",
    )

    assert _HEADLINE not in web.get("/today").text


def test_the_client_filter_hides_the_other_mandates_cards(web, session, mandate):
    other = Client(name="Helio GmbH")
    session.add(other)
    session.commit()
    _opportunity(session, mandate, title=_HEADLINE)
    _opportunity(
        session,
        other,
        title="EU-Kommission legt Entwurf zur Netzentgeltreform vor",
        source="Reuters",
    )

    page = web.get(f"/today?client={other.id}")

    assert "EU-Kommission legt Entwurf" in page.text
    assert _HEADLINE not in page.text


# --- The cap ----------------------------------------------------------------------


def test_never_more_than_three_cards_per_mandate_cut_by_pickup_and_named(
    web, session, mandate
):
    """Three is a selection, ten is the noise this tool was built against —
    and the cut stands on the page instead of happening silently."""
    titles = [
        f"Netzentgelte Studie Nummer {n} sorgt für Debatte in der Energiebranche"
        for n in range(5)
    ]
    for n, title in enumerate(titles):
        _opportunity(session, mandate, title=title, pickup_count=2 + n)

    page = web.get("/today")

    # The three widest waves stand; the two narrowest were cut.
    assert titles[4] in page.text
    assert titles[3] in page.text
    assert titles[2] in page.text
    assert titles[1] not in page.text
    assert titles[0] not in page.text
    assert "2 weitere Gelegenheit(en) bei" in page.text
    assert "Solaris AG" in page.text
    assert "nach Aufgriffszahl gekürzt" in page.text


def test_three_open_opportunities_are_no_cut(web, session, mandate):
    """The cap's name appears only when the cap did something."""
    for n in range(3):
        _opportunity(
            session,
            mandate,
            title=f"Netzentgelte Studie Nummer {n} sorgt für Debatte in der Branche",
            pickup_count=2 + n,
        )

    page = web.get("/today")

    assert "gekürzt" not in page.text


# --- Verwerfen --------------------------------------------------------------------


def test_dismiss_stamps_the_row_and_removes_the_card_without_a_reason(
    web, session, mandate
):
    """No reason asked, none stored — and the stamped row is what keeps the
    same story from coming back for this mandate."""
    row = _opportunity(session, mandate)

    answer = web.post(
        f"/gelegenheit/{row.id}/verwerfen",
        data={"redirect_to": "/today"},
        follow_redirects=False,
    )

    assert answer.status_code == 303
    assert answer.headers["location"] == "/today"
    session.expire_all()
    kept = session.get(NewsjackOpportunity, row.id)
    assert kept is not None and kept.dismissed_at is not None
    assert kept.standing is Standing.BELEGT  # the verdict is untouched
    assert _HEADLINE not in web.get("/today").text
    assert newsjack.open_opportunities(session, mandate) == []


def test_a_double_dismiss_keeps_the_first_stamp(web, session, mandate):
    row = _opportunity(session, mandate)

    web.post(f"/gelegenheit/{row.id}/verwerfen", follow_redirects=False)
    session.expire_all()
    first = session.get(NewsjackOpportunity, row.id).dismissed_at
    web.post(f"/gelegenheit/{row.id}/verwerfen", follow_redirects=False)
    session.expire_all()

    assert session.get(NewsjackOpportunity, row.id).dismissed_at == first


def test_the_dismiss_redirect_cannot_leave_the_site(web, session, mandate):
    row = _opportunity(session, mandate)

    answer = web.post(
        f"/gelegenheit/{row.id}/verwerfen",
        data={"redirect_to": "https://evil.example/phish"},
        follow_redirects=False,
    )

    assert answer.headers["location"] == "/"


# --- Text schreiben ---------------------------------------------------------------


def test_write_opens_the_picker_with_the_opportunity_as_occasion(web, session, mandate):
    row = _opportunity(session, mandate)

    answer = web.post(
        f"/client/{mandate.id}/gelegenheit/{row.id}/text", follow_redirects=False
    )

    assert answer.status_code == 303
    angle = session.scalars(
        select(Angle).where(Angle.newsjack_id == row.id)
    ).one()
    assert f"anlass-{angle.id}" in answer.headers["location"]
    assert angle.client_id == mandate.id
    assert angle.subject == _HEADLINE
    assert angle.message == _REASON
    assert angle.article_ids == [row.article_id]


def test_a_second_click_reuses_the_same_occasion(web, session, mandate):
    """Two presses of the button are one occasion — the texts hang together."""
    row = _opportunity(session, mandate)

    first = web.post(
        f"/client/{mandate.id}/gelegenheit/{row.id}/text", follow_redirects=False
    )
    second = web.post(
        f"/client/{mandate.id}/gelegenheit/{row.id}/text", follow_redirects=False
    )

    assert first.headers["location"] == second.headers["location"]
    angles = session.scalars(select(Angle).where(Angle.newsjack_id == row.id)).all()
    assert len(angles) == 1


def test_a_written_text_is_findable_from_the_card(web, session, mandate):
    """The card says a text came of it and links to where it hangs."""
    row = _opportunity(session, mandate)
    web.post(f"/client/{mandate.id}/gelegenheit/{row.id}/text", follow_redirects=False)
    angle = session.scalars(select(Angle).where(Angle.newsjack_id == row.id)).one()
    session.add(
        Asset(
            client_id=mandate.id,
            angle_id=angle.id,
            kind="statement",
            body="Ein kurzer Text.",
        )
    )
    session.commit()

    page = web.get("/today")

    assert "Text(e) ansehen" in page.text
    assert f"anlass-{angle.id}" in page.text


def test_a_rejection_or_foreign_mandate_cannot_be_opened(web, session, mandate):
    """A ``duenn`` row is an audit record, never an occasion; a URL crossing
    mandates is a mis-aimed or forged POST."""
    thin = _opportunity(session, mandate, standing=Standing.DUENN)
    other = Client(name="Helio GmbH")
    session.add(other)
    session.commit()
    good = _opportunity(
        session,
        mandate,
        title="EU-Kommission legt Entwurf zur Netzentgeltreform vor",
    )

    assert (
        web.post(
            f"/client/{mandate.id}/gelegenheit/{thin.id}/text", follow_redirects=False
        ).status_code
        == 404
    )
    assert (
        web.post(
            f"/client/{other.id}/gelegenheit/{good.id}/text", follow_redirects=False
        ).status_code
        == 404
    )


# --- The mandate's archive --------------------------------------------------------


def test_an_expired_opportunity_stays_readable_in_the_archive(web, session, mandate):
    """Gone from Heute, kept in the record — with the outcome the window
    running out is."""
    _opportunity(session, mandate, hours_left=-2)

    page = web.get(f"/client/{mandate.id}")

    assert "Gelegenheiten aus der schnellen Spur" in page.text
    assert _HEADLINE in page.text
    assert "Abgelaufen am" in page.text
    assert "Kein Text entstanden." in page.text


def test_a_dismissed_opportunity_reads_verworfen_in_the_archive(web, session, mandate):
    row = _opportunity(session, mandate)
    web.post(f"/gelegenheit/{row.id}/verwerfen", follow_redirects=False)

    page = web.get(f"/client/{mandate.id}")

    assert "Verworfen am" in page.text
    assert "Abgelaufen am" not in page.text


def test_an_open_opportunity_is_not_in_the_archive(web, session, mandate):
    """Open ones live on Heute; the archive is the record, not a second inbox.
    And with nothing concluded there is no section at all."""
    _opportunity(session, mandate, hours_left=12)

    assert "Gelegenheiten aus der schnellen Spur" not in web.get(
        f"/client/{mandate.id}"
    ).text


# --- The languages ----------------------------------------------------------------


def test_every_new_string_carries_an_english_side():
    """The card's and the record's chrome, both languages — the stored standing
    sentence itself is data and deliberately not translated."""
    for german, english in [
        ("Gelegenheit", "Opportunity"),
        ("Gelegenheiten", "Opportunities"),
        ("verbleibend", "left"),
        ("Zuerst bei", "First at"),
        ("Stehen", "Standing"),
        ("weitere Gelegenheit(en) bei", "more opportunit(y/ies) for"),
        ("Gelegenheiten aus der schnellen Spur", "Opportunities from the fast lane"),
        ("Verworfen am", "Waved off on"),
        ("Abgelaufen am", "Expired on"),
        ("Kein Text entstanden.", "No text came of it."),
    ]:
        assert i18n.translate(german, "en") == english
        assert i18n.translate(german, "de") == german


def test_the_card_renders_in_english_when_chosen(web, session, mandate):
    _opportunity(session, mandate)
    web.cookies.set(i18n.COOKIE_NAME, "en")

    page = web.get("/today")

    assert "left" in page.text
    assert "First at" in page.text
    # The standing sentence is stored data and stays as written.
    assert _REASON in page.text


# --- The fast lane's own notification ---------------------------------------------


class _Spy:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args) -> None:
        self.calls.append(args)


def _found() -> FoundOpportunity:
    return FoundOpportunity(
        client_name="Solaris AG", headline=_HEADLINE, outlets=3, hours_left=12
    )


def test_a_run_that_found_nothing_sends_nothing():
    """No finding, no noise — the channel stays trusted for the runs that do."""
    desktop, email = _Spy(), _Spy()

    result = notify_opportunities(
        [], NotifyConfig(channel=Channel.DESKTOP), send_desktop=desktop, send_email=email
    )

    assert result.sent is False
    assert result.reason == "no-opportunities"
    assert desktop.calls == []
    assert email.calls == []


def test_a_found_opportunity_reaches_the_desktop_with_the_window(monkeypatch):
    desktop, email = _Spy(), _Spy()

    result = notify_opportunities(
        [_found()],
        NotifyConfig(channel=Channel.DESKTOP),
        send_desktop=desktop,
        send_email=email,
    )

    assert result.sent is True
    assert len(desktop.calls) == 1
    summary = desktop.calls[0][0]
    assert "Solaris AG" in summary.desktop_message
    assert "12h" in summary.desktop_message
    assert _HEADLINE in summary.body


def test_a_broken_channel_never_raises():
    """The rows are already committed; a dead channel costs the tap on the
    shoulder, never the data."""

    def broken(_summary) -> None:
        raise OSError("notifier missing")

    result = notify_opportunities(
        [_found()],
        NotifyConfig(channel=Channel.DESKTOP),
        send_desktop=broken,
        send_email=_Spy(),
    )

    assert result.sent is False
    assert result.reason == "delivery-error"
