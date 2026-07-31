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


def test_the_page_offers_the_suggestion_button_when_there_are_none(factory, client):
    with factory() as session:
        subject_id = _client(session).id

    body = client.get(f"/client/{subject_id}").text

    assert "Wettbewerber vorschlagen" in body
    assert f'action="/client/{subject_id}/competitors/suggest"' in body


def test_the_proposals_render_as_buttons_that_create_nothing_by_themselves(
    factory, client, monkeypatch
):
    with factory() as session:
        subject_id = _client(session).id

    monkeypatch.setattr(
        rivals, "suggest", lambda c, **k: rivals._parse(_reply("About You")).rivals
    )

    body = client.post(f"/client/{subject_id}/competitors/suggest").text

    assert "About You" in body
    assert f'action="/client/{subject_id}/competitors/accept"' in body
    with factory() as session:
        # Rendered, not created.
        assert session.scalar(select(func.count()).select_from(Client)) == 1
