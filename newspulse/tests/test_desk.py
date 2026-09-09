"""The Desk: what the agency owes, what it delivered, what needs deciding.

"Wenn wir morgens ins System gehen, brauchen wir keinen Newsfeed oder eine
Artikelliste, sondern ein echtes Arbeitsdashboard."

The figures on this page get quoted in client meetings, which is the whole
reason these tests are specific about three things that would otherwise be
plausible-looking and wrong: a draft counted as a delivery, a mandate with no
contract figure drawn as on target, and an action logged in the quarter somebody
remembered it rather than the quarter it happened in.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import desk
from newspulse.models import (
    Angle,
    Article,
    Asset,
    AssetKind,
    Base,
    Client,
    ClientAction,
    Crisis,
    Outreach,
    OutreachState,
    Report,
    ReportState,
)
from newspulse.web.app import create_app, get_db

# Mid-quarter and mid-month, so a test can move a row either side of a boundary
# without also crossing the other one.
_NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)


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


def _mandate(session, name="Qonto", **over) -> Client:
    client = Client(
        name=name,
        aliases=[],
        keywords=[],
        alert_topics=[],
        **over,
    )
    session.add(client)
    session.commit()
    return client


def _angle(session, client) -> Angle:
    row = Angle(
        client_id=client.id, generated_at=_NOW, subject="Impuls", message="m", context="k"
    )
    session.add(row)
    session.commit()
    return row


def _letter(session, client, angle, *, released_at=None, state=OutreachState.ENTWURF, **over):
    row = Outreach(
        angle_id=angle.id,
        client_id=client.id,
        subject=over.pop("subject", "Ein Anschreiben"),
        message="x",
        generated_at=_NOW - dt.timedelta(days=1),
        state=state,
        released_at=released_at,
        **over,
    )
    session.add(row)
    session.commit()
    return row


# --- What counts as delivered -------------------------------------------------------


def test_a_released_artefact_is_an_action_and_a_draft_is_not(session):
    """``released_at``, never ``generated_at``. The tool drafts far more than the
    agency sends, and counting drafts would report work that never happened — in
    the number a client conversation starts from."""
    client = _mandate(session)
    angle = _angle(session, client)
    _letter(session, client, angle, released_at=_NOW - dt.timedelta(days=2), state=OutreachState.RAUS)
    _letter(session, client, angle)  # drafted, never released

    board = desk.build(session, now=_NOW)

    assert board.rows[0].actions_month.released == 1


def test_all_three_kinds_of_delivery_count(session):
    """A letter, a text and a report are three different things the agency
    delivers, and the contract does not distinguish them."""
    client = _mandate(session)
    angle = _angle(session, client)
    _letter(session, client, angle, released_at=_NOW - dt.timedelta(days=2), state=OutreachState.RAUS)
    session.add(
        Asset(
            client_id=client.id,
            angle_id=angle.id,
            kind=AssetKind.PRESSEMITTEILUNG,
            title="Release",
            body="x",
            generated_at=_NOW - dt.timedelta(days=3),
            released_at=_NOW - dt.timedelta(days=3),
        )
    )
    session.add(
        Report(
            client_id=client.id,
            period_start=_NOW - dt.timedelta(days=30),
            period_end=_NOW,
            state=ReportState.FREIGEGEBEN,
            generated_at=_NOW - dt.timedelta(days=4),
            released_at=_NOW - dt.timedelta(days=4),
        )
    )
    session.commit()

    board = desk.build(session, now=_NOW)

    assert board.rows[0].actions_month.released == 3


def test_work_done_outside_the_tool_is_counted_too(session):
    """The reason the ledger exists. A background call never touches RauteOS, and
    a quarter counted from released artefacts alone shows the agency doing less
    than it did."""
    client = _mandate(session)
    session.add(
        ClientAction(
            client_id=client.id,
            title="Hintergrundgespräch Handelsblatt",
            happened_at=_NOW - dt.timedelta(days=5),
            logged_at=_NOW,
        )
    )
    session.commit()

    row = desk.build(session, now=_NOW).rows[0]

    assert (row.actions_month.released, row.actions_month.logged) == (0, 1)
    assert row.actions_month.total == 1


def test_an_action_lands_in_the_quarter_it_happened_in(session):
    """``happened_at``, not ``logged_at``. An action is written down after the
    fact — often in the following week, sometimes in the following quarter — and
    filing it by the day somebody remembered would move deliveries across the
    boundary the contract is measured on."""
    client = _mandate(session)
    session.add(
        ClientAction(
            client_id=client.id,
            title="Briefing im Juni",
            happened_at=dt.datetime(2026, 6, 30, 10, 0, tzinfo=dt.UTC),  # Q2
            logged_at=_NOW,  # written down in Q3
        )
    )
    session.commit()

    row = desk.build(session, now=_NOW).rows[0]

    assert row.actions_quarter.logged == 0, "a Q2 action is not Q3 delivery"


def test_the_quarter_is_the_calendar_quarter_not_ninety_days(session):
    """The promise is contractual, and a contract's quarter is January, April,
    July, October. A rolling window would show a mandate slipping out of
    compliance on a Tuesday because a delivery from ninety-one days ago fell off
    the back."""
    assert desk.quarter_start(_NOW) == dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    assert desk.quarter_start(dt.datetime(2026, 1, 4, tzinfo=dt.UTC)) == dt.datetime(
        2026, 1, 1, tzinfo=dt.UTC
    )


# --- The contract figure --------------------------------------------------------


def test_a_mandate_with_no_figure_is_unmeasured_not_on_target(session):
    """A green tick against a promise that does not exist is the sort of number
    that gets quoted in a client meeting. ``None``, not ``True``."""
    _mandate(session, actions_per_quarter=None)

    assert desk.build(session, now=_NOW).rows[0].on_track is None


def test_zero_promised_is_a_promise_and_is_kept(session):
    """The other half of the same distinction: zero is a mandate that owes
    nothing, and it is on target the moment the quarter starts."""
    _mandate(session, actions_per_quarter=0)

    assert desk.build(session, now=_NOW).rows[0].on_track is True


def test_delivering_more_than_promised_is_on_track(session):
    """"Vertraglich zugesichert pro Quartal: 10, durchgeführt: 12" — the example
    in the brief, and it is the good case, not an anomaly to flag."""
    client = _mandate(session, actions_per_quarter=10)
    for index in range(12):
        session.add(
            ClientAction(
                client_id=client.id,
                title=f"Aktion {index}",
                happened_at=_NOW - dt.timedelta(days=3),
                logged_at=_NOW,
            )
        )
    session.commit()

    row = desk.build(session, now=_NOW).rows[0]

    assert (row.actions_quarter.total, row.on_track) == (12, True)


# --- Who appears -----------------------------------------------------------------


def test_a_competitor_is_not_a_mandate_and_is_not_on_the_desk(session):
    """A yardstick is tracked so a mandate can be compared against it. Nobody
    owes it actions, and counting it would inflate every total on the page."""
    _mandate(session, "Qonto")
    _mandate(session, "Revolut", is_competitor=True)

    board = desk.build(session, now=_NOW)

    assert [row.client.name for row in board.rows] == ["Qonto"]
    assert board.clients_active == 1


def test_an_archived_mandate_is_not_on_the_desk(session):
    _mandate(session, "Qonto")
    _mandate(session, "Alt AG", active=False)

    assert [row.client.name for row in desk.build(session, now=_NOW).rows] == ["Qonto"]


# --- Status ------------------------------------------------------------------------


def test_an_open_crisis_outranks_everything_in_the_status_column(session):
    """The one state where "Aktuell nichts Relevantes" would be actively wrong."""
    client = _mandate(session)
    article = Article(
        url="https://test.example/rueckruf",
        title="Rückruf",
        source="Test",
        published_at=_NOW - dt.timedelta(days=1),
        fetched_at=_NOW,
        title_hash="h-rueckruf",
    )
    session.add(article)
    session.commit()
    session.add(
        Crisis(
            client_id=client.id,
            article_id=article.id,
            declared_by="Lucas",
            declared_at=_NOW - dt.timedelta(days=1),
        )
    )
    session.commit()

    assert desk.build(session, now=_NOW).rows[0].status == "krise"


def test_a_mandate_with_nothing_this_week_reads_as_quiet(session):
    _mandate(session)

    assert desk.build(session, now=_NOW).rows[0].status == "ruhig"


# --- The Twin ----------------------------------------------------------------------


def test_the_twin_names_which_part_is_thin_rather_than_only_a_percentage(session):
    """"Kickoff unvollständig" tells a consultant what to do next; "72 %" does
    not."""
    client = _mandate(session)

    twin = desk.build(session, now=_NOW).rows[0].twin

    assert twin.percent == 0
    assert "Kein Kommunikationsguide" in twin.gaps
    assert "Kickoff unvollständig" in twin.gaps


def test_a_guide_moves_the_twin_and_clears_its_own_gap(session):
    _mandate(session, comms_guide="Nie ohne Zahl.")

    twin = desk.build(session, now=_NOW).rows[0].twin

    assert twin.has_guide
    assert "Kein Kommunikationsguide" not in twin.gaps
    assert twin.percent == round(100 * desk.TWIN_WEIGHTS["guide"])


# --- Decisions ---------------------------------------------------------------------


def test_a_letter_waiting_on_the_client_and_one_waiting_on_us_are_different(session):
    """OUT-05 put the client's sign-off in front of every journalist. A Desk that
    lumps both into "offen" cannot tell the consultant which phone to pick up."""
    client = _mandate(session)
    angle = _angle(session, client)
    _letter(session, client, angle, subject="Bei der Agentur",
            client_ok_at=_NOW - dt.timedelta(days=1), client_ok_by="Lucas")
    _letter(session, client, angle, subject="Beim Kunden")

    kinds = {d.title: d.kind for d in desk.build(session, now=_NOW).decisions}

    assert kinds["Bei der Agentur"] == "freigabe"
    assert kinds["Beim Kunden"] == "kundenfreigabe"


def test_the_oldest_decision_is_first(session):
    """A decision that has waited three weeks is more urgent than one raised this
    morning, and a list sorted the other way buries it."""
    client = _mandate(session)
    angle = _angle(session, client)
    fresh = _letter(session, client, angle, subject="Heute")
    fresh.generated_at = _NOW
    old = _letter(session, client, angle, subject="Vor drei Wochen")
    old.generated_at = _NOW - dt.timedelta(days=21)
    session.commit()

    titles = [d.title for d in desk.build(session, now=_NOW).decisions if d.kind != "impuls"]

    assert titles[0] == "Vor drei Wochen"


# --- The page and the ledger form ----------------------------------------------------


def test_the_desk_states_the_contract_beside_the_delivery(web, factory):
    with factory() as session:
        client = _mandate(session, actions_per_quarter=10)
        angle = _angle(session, client)
        _letter(session, client, angle, released_at=dt.datetime.now(dt.UTC),
                state=OutreachState.RAUS)

    body = web.get("/desk").text

    assert "Mandantenüberblick" in body
    assert "10 / 1" in body


def test_an_action_can_be_logged_from_the_desk(web, factory):
    with factory() as session:
        client = _mandate(session)
        client_id = client.id

    response = web.post(
        "/desk/action",
        data={
            "client_id": client_id,
            "title": "Redaktionsbesuch NZZ",
            "happened_on": "2026-08-14",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with factory() as session:
        row = session.scalars(select(ClientAction)).one()
        assert row.title == "Redaktionsbesuch NZZ"
        assert row.happened_at.date() == dt.date(2026, 8, 14)


def test_a_logged_action_is_stamped_at_midday_so_its_date_survives_any_timezone(
    web, factory
):
    """Midday UTC, not midnight, and the reason is display rather than
    arithmetic: the quarter comparison happens in UTC where midnight would do,
    but every timestamp here is rendered in the local zone, and midnight UTC
    renders as the previous evening anywhere west of Greenwich. Then the date on
    screen is not the date that was typed."""
    with factory() as session:
        client_id = _mandate(session).id

    web.post(
        "/desk/action",
        data={"client_id": client_id, "title": "Briefing", "happened_on": "2026-03-31"},
        follow_redirects=False,
    )

    with factory() as session:
        stored = session.scalars(select(ClientAction)).one()
        assert stored.happened_at.date() == dt.date(2026, 3, 31)
        assert stored.happened_at.hour == 12, "midnight would shift the day westward"
        # And it is still filed in the quarter it happened in.
        assert desk.quarter_start(stored.happened_at) == dt.datetime(
            2026, 1, 1, tzinfo=dt.UTC
        )


def test_a_nameless_action_is_not_logged(web, factory):
    with factory() as session:
        client_id = _mandate(session).id

    web.post(
        "/desk/action",
        data={"client_id": client_id, "title": "   "},
        follow_redirects=False,
    )

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ClientAction)) == 0


def test_the_ledger_form_cannot_be_used_to_leave_the_site(web, factory):
    with factory() as session:
        client_id = _mandate(session).id

    response = web.post(
        "/desk/action",
        data={
            "client_id": client_id,
            "title": "Briefing",
            "redirect_to": "https://example.com/",
        },
        follow_redirects=False,
    )

    assert response.headers["location"] == "/desk"


def test_an_empty_portfolio_says_so_rather_than_rendering_an_empty_grid(web):
    body = web.get("/desk").text

    assert "Noch kein Mandant angelegt" in body
    assert "/mandant/neu" in body
