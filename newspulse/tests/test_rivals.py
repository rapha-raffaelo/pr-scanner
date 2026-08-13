"""Proposing competitors (newspulse.rivals) and accepting one.

Share of voice needs a comparison group, and the picker only offers companies
already marked as competitors — so the first one had to be created by hand, which
is the step nobody does. The model proposes; a click creates.

The rule under every test here: nothing is created without that click. A wrong
competitor does not merely look odd, it lands in the share-of-voice arithmetic and
changes a number the agency reports.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import rivals
from newspulse.analyzer import ParseError
from newspulse.models import Base, Client
from newspulse.web.app import create_app, get_db


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(factory):
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _client(session, name="Zalando", **over) -> Client:
    obj = Client(
        name=name,
        aliases=[],
        keywords=over.get("keywords", []),
        alert_topics=[],
        industry=over.get("industry", "Fashion"),
        country="DE",
        is_competitor=over.get("is_competitor", False),
    )
    session.add(obj)
    session.commit()
    return obj


def _reply(*names) -> str:
    return json.dumps(
        {"rivals": [{"name": n, "reason": "gleicher Markt"} for n in names]}
    )


def test_proposals_are_returned_and_nothing_is_stored(factory):
    with factory() as session:
        subject = _client(session)

        proposals = rivals.suggest(
            subject, invoke=lambda *a, **k: _reply("About You", "H&M")
        )

        assert [r.name for r in proposals] == ["About You", "H&M"]
        # The whole posture: the model proposed, the database is untouched.
        assert session.scalar(select(func.count()).select_from(Client)) == 1


def test_an_unknown_company_yields_an_empty_list_not_a_guess(factory):
    """The expected answer for a small or very young mandate. A competitor
    invented to fill the list would end up in a share-of-voice calculation as if
    it were real."""
    with factory() as session:
        subject = _client(session, name="IB-7 Beauty Tech GmbH", industry="Beauty Tech")

        assert rivals.suggest(subject, invoke=lambda *a, **k: '{"rivals": []}') == []


def test_the_client_itself_is_never_proposed(factory):
    with factory() as session:
        subject = _client(session)

        proposals = rivals.suggest(
            subject, invoke=lambda *a, **k: _reply("Zalando", "About You")
        )

        assert [r.name for r in proposals] == ["About You"]


def test_an_existing_competitor_is_not_proposed_again(factory):
    with factory() as session:
        subject = _client(session)
        rival = _client(session, name="About You", is_competitor=True)
        subject.competitors.append(rival)
        session.commit()

        proposals = rivals.suggest(
            subject, invoke=lambda *a, **k: _reply("About You", "H&M")
        )

        assert [r.name for r in proposals] == ["H&M"]


def test_the_prompt_carries_what_identifies_the_market(factory):
    with factory() as session:
        subject = _client(session, industry="Fashion")
        seen: dict[str, str] = {}

        def _invoke(prompt, **_):
            seen["prompt"] = prompt
            return _reply("About You")

        rivals.suggest(subject, invoke=_invoke)

        assert "Zalando" in seen["prompt"]
        assert "Fashion" in seen["prompt"]
        assert "DE" in seen["prompt"]


def test_a_non_json_reply_is_a_parse_error(factory):
    with factory() as session:
        subject = _client(session)

        with pytest.raises(ParseError):
            rivals.suggest(subject, invoke=lambda *a, **k: "Gerne! Hier die Liste:")


# --- Accepting one --------------------------------------------------------------


def test_accepting_creates_the_company_as_a_competitor_and_links_it(factory, client):
    with factory() as session:
        subject_id = _client(session).id

    client.post(
        f"/client/{subject_id}/competitors/accept",
        data={"name": "About You"},
        follow_redirects=False,
    )

    with factory() as session:
        created = session.scalars(select(Client).where(Client.name == "About You")).one()
        assert created.is_competitor is True
        subject = session.get(Client, subject_id)
        assert created in subject.competitors


def test_accepting_a_company_that_already_exists_reuses_it(factory, client):
    """A name proposed for two mandates must end up as one row watched by both,
    not two rows with the same name splitting its coverage."""
    with factory() as session:
        subject_id = _client(session).id
        existing_id = _client(session, name="About You", is_competitor=True).id

    client.post(
        f"/client/{subject_id}/competitors/accept",
        data={"name": "About You"},
        follow_redirects=False,
    )

    with factory() as session:
        rows = session.scalars(select(Client).where(Client.name == "About You")).all()
        assert len(rows) == 1
        assert rows[0].id == existing_id
        assert rows[0] in session.get(Client, subject_id).competitors


def test_the_proposal_lives_in_the_clients_own_edit_row(factory, client):
    """Competitors are configuration, so the proposal sits beside the aliases,
    search terms and alert topics — everything else about a mandate is set in
    that row, and a panel floating above the table was a second place to look."""
    with factory() as session:
        subject_id = _client(session).id

    closed = client.get("/settings").text
    open_row = client.get(f"/settings?edit={subject_id}").text

    assert f'action="/settings/clients/{subject_id}/rivals"' not in closed
    assert f'action="/settings/clients/{subject_id}/rivals"' in open_row


def test_an_accepted_competitor_is_visible_in_the_table(factory, client):
    """It was saved all along and nothing showed it: the panel closed, the table
    had no column for it, and "gespeichert" was indistinguishable from
    "verworfen"."""
    with factory() as session:
        subject_id = _client(session).id

    client.post(
        f"/client/{subject_id}/competitors/accept",
        data={"name": "About You", "redirect_to": "/settings"},
    )
    body = client.get("/settings").text

    assert "About You" in body
    assert "tag--rival" in body


def test_the_proposals_render_as_buttons_that_create_nothing_by_themselves(
    factory, client
):
    """The proposal now runs on a worker thread — synchronous, it was a model
    call inside the request, "lädt extrem schwer und langsam" — so the page is
    rendered from the finished result rather than from the POST."""
    from newspulse.web import themework

    with factory() as session:
        subject_id = _client(session).id

    themework.rivals_job.state[subject_id] = {
        "state": "fertig",
        "client": "Zalando",
        "rivals": rivals._parse(_reply("About You")).rivals,
    }
    try:
        body = client.get(f"/settings?edit={subject_id}").text
    finally:
        themework.rivals_job.state.clear()

    assert "About You" in body
    assert f'action="/client/{subject_id}/competitors/accept"' in body
    with factory() as session:
        # Rendered, not created.
        assert session.scalar(select(func.count()).select_from(Client)) == 1


def test_a_running_proposal_says_so_and_fetches_its_own_result(factory, client):
    """A model call takes tens of seconds. Holding the request open for it is
    what made this read as broken."""
    from newspulse.web import themework

    with factory() as session:
        subject_id = _client(session).id

    themework.rivals_job.state[subject_id] = {"state": "läuft", "client": "Zalando"}
    try:
        body = client.get(f"/settings?edit={subject_id}").text
    finally:
        themework.rivals_job.state.clear()

    assert "Wettbewerber werden vorgeschlagen" in body
    assert f'hx-target="#client-edit-{subject_id}"' in body


def test_an_empty_proposal_says_so_rather_than_looking_broken(
    factory, client, monkeypatch
):
    """The expected answer for a young mandate, and the page must not leave the
    reader wondering whether the button worked."""
    with factory() as session:
        subject_id = _client(session, name="IB-7 Beauty Tech GmbH").id

    from newspulse.web import themework

    themework.rivals_job.state[subject_id] = {
        "state": "fertig",
        "client": "IB-7 Beauty Tech GmbH",
        "rivals": [],
    }
    try:
        body = client.get(f"/settings?edit={subject_id}").text
    finally:
        themework.rivals_job.state.clear()

    assert "Keine Wettbewerber vorgeschlagen" in body


def test_accepting_from_settings_returns_to_settings(factory, client):
    """The suggestion was made while configuring; the reader belongs back there,
    not on a page they never asked for."""
    with factory() as session:
        subject_id = _client(session).id

    resp = client.post(
        f"/client/{subject_id}/competitors/accept",
        data={"name": "About You", "redirect_to": "/settings"},
        follow_redirects=False,
    )

    assert resp.headers["location"] == "/settings"


def test_an_offsite_redirect_is_refused(factory, client):
    """Same posture as the run trigger: only a same-site path is honoured."""
    with factory() as session:
        subject_id = _client(session).id

    resp = client.post(
        f"/client/{subject_id}/competitors/accept",
        data={"name": "About You", "redirect_to": "https://evil.example/"},
        follow_redirects=False,
    )

    assert resp.headers["location"] == f"/client/{subject_id}"


def test_a_competitor_from_another_industry_is_grouped_apart(factory, client):
    """Every monitored competitor used to be offered to every mandate, so a
    finance platform was invited to benchmark itself against ASOS and H&M —
    fashion brands that exist in the portfolio only because a fashion mandate
    needed them. Share of voice is a statement about *a market*, and a number
    computed across two of them is not a fact about anything.

    Grouped rather than hidden: an operator may know a cross-industry rival the
    labels cannot see.
    """
    with factory() as session:
        broker = Client(
            name="Freedom24", aliases=[], industry="Neobroker",
            keywords=[], alert_topics=[],
        )
        peer = Client(
            name="Trade Republic", aliases=[], industry="Neobroker",
            keywords=[], alert_topics=[], is_competitor=True,
        )
        unrelated = Client(
            name="H&M", aliases=[], industry="Modehandel",
            keywords=[], alert_topics=[], is_competitor=True,
        )
        session.add_all([broker, peer, unrelated])
        session.commit()
        broker_id = broker.id

    body = client.get(f"/client/{broker_id}").text
    offered = body.split('id="competitor_id"', 1)[1].split("</select>", 1)[0]

    # The picker itself offers the peer and nothing else. Grouping the fashion
    # brand under a heading was not enough: it was still on the list, and the
    # report came back twice.
    assert "Trade Republic" in offered
    assert "H&amp;M" not in offered
    # Reachable, but only for someone who opens the expander and means it.
    assert "anderen Branchen" in body


def test_without_an_industry_every_competitor_is_still_offered(factory, client):
    """No field, no grouping — and no silent narrowing of the reader's options."""
    with factory() as session:
        subject = Client(name="Ohne Feld", aliases=[], industry=None,
                         keywords=[], alert_topics=[])
        rival = Client(name="H&M", aliases=[], industry="Modehandel",
                       keywords=[], alert_topics=[], is_competitor=True)
        session.add_all([subject, rival])
        session.commit()
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}").text

    # No field, so the picker cannot judge comparability. It says that instead of
    # offering a flat list of everything — the state a finance platform was in
    # when it was shown fashion brands.
    assert "keine Branche hinterlegt" in body
    assert 'id="competitor_id"' not in body
    # Escaped in the rendered page, so match what the browser actually receives.
    assert "H&amp;M" in body  # still reachable behind the expander
