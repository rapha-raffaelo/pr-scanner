"""Proposing themes, and measuring them before offering them.

A mandate's radar searches the terms an operator typed, and what they type
describes the company: a beauty-tech client carried "KI in der Kosmetik", which
reads perfectly and returns nothing, because no journalist writes that phrase.
Its radar found ten items, nine about the mandate itself, and every drafting
call refused — correctly, since a statement about your own press release is not
a positioning.

Proposing better themes is only half an answer: a proposal reads plausible
whether or not the press covers it. So each one is put through the real radar
query first and offered with what it actually returned.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import themes
from newspulse.ingest import FeedItem
from newspulse.models import Base, Client
from newspulse.schemas import ThemeSuggestion
from newspulse.web.app import create_app, get_db
from newspulse.web import themework
from newspulse.web.routes import settings as settings_routes

_NOW = dt.datetime(2026, 8, 3, 9, 0, tzinfo=dt.UTC)


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

    def _override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_state():
    themework.state.clear()
    yield
    themework.state.clear()


def _client(**over) -> Client:
    return Client(
        name=over.get("name", "IB-7 Beauty Tech"),
        aliases=over.get("aliases", ["IB-7"]),
        industry=over.get("industry", "Beauty Tech"),
        country="AT",
        keywords=over.get("keywords", ["KI in der Kosmetik"]),
        alert_topics=[],
    )


def _item(title: str) -> FeedItem:
    return FeedItem(
        title=title,
        link=f"https://ex.de/{abs(hash(title)) % 10**6}",
        source="Textilwirtschaft",
        published_at=_NOW - dt.timedelta(days=3),
        summary=None,
        language="de",
    )


# --- The measurement ------------------------------------------------------------


def test_a_theme_is_offered_with_what_it_actually_returns():
    subject = _client()
    proposals = [ThemeSuggestion(term="Clean Beauty", reason="passt")]

    def _fetch(url, since, **_):
        return [_item("Clean Beauty wird Pflicht im Handel")]

    probe = themes.probe(subject, proposals, fetch=_fetch, now=lambda: _NOW)[0]

    assert probe.term == "Clean Beauty"
    assert probe.external == 1
    assert probe.usable


def test_hits_about_the_client_itself_are_counted_apart():
    """The failure this replaces: a theme so close to the company that it only
    finds the company. Those hits are real coverage, but no positioning can be
    built on them — a text about your own press release is self-promotion."""
    subject = _client()
    proposals = [ThemeSuggestion(term="KI-Hautpflege", reason="")]

    def _fetch(url, since, **_):
        return [
            _item("IB-7 launcht vollständig KI-entwickelte Hautpflegemarke"),
            _item("IB-7 Beauty Tech: Weltweit erste KI-Hautpflege"),
            _item("L'Oréal und OpenAI kooperieren"),
        ]

    probe = themes.probe(subject, proposals, fetch=_fetch, now=lambda: _NOW)[0]

    assert probe.own == 2
    assert probe.external == 1


def test_a_theme_nobody_writes_about_is_reported_as_empty_not_dropped():
    """Measured live: "KI in der Kosmetik" returns nothing at all. The operator
    may still know something the search does not, so it is offered — but it must
    not look like the ones that work."""
    subject = _client()
    proposals = [ThemeSuggestion(term="KI in der Kosmetik", reason="")]

    probe = themes.probe(
        subject, proposals, fetch=lambda *a, **k: [], now=lambda: _NOW
    )[0]

    assert probe.external == 0
    assert not probe.usable


def test_a_failing_probe_is_not_reported_as_an_empty_field():
    """"The search errored" and "nobody writes about this" are different facts."""
    subject = _client()

    def _boom(*a, **k):
        raise RuntimeError("Netzwerk weg")

    probes = themes.probe(
        subject, [ThemeSuggestion(term="Clean Beauty", reason="")],
        fetch=_boom, now=lambda: _NOW,
    )

    assert len(probes) == 1 and probes[0].external == 0


def test_the_field_scoped_query_widens_before_giving_up():
    """The radar makes the same fallback, so the number shown has to describe the
    query that will actually run — measured, AND ("Beauty Tech") sent every
    proposal to zero because the label is too rare to appear in the press."""
    subject = _client()
    seen: list[str] = []

    def _fetch(url, since, **_):
        seen.append(url)
        if "AND" in url or "%29+AND+%28" in url:
            return []
        return [_item("Nachhaltige Verpackung im Handel")]

    probe = themes.probe(
        subject, [ThemeSuggestion(term="Nachhaltige Verpackung", reason="")],
        fetch=_fetch, now=lambda: _NOW,
    )[0]

    assert len(seen) == 2
    assert probe.widened is True
    assert probe.external == 1


def test_proposals_the_client_already_has_are_not_offered_again():
    subject = _client(keywords=["Clean Beauty"])

    result = themes.suggest(
        subject,
        invoke=lambda *a, **k: (
            '{"themes": [{"term": "Clean Beauty", "reason": "x"},'
            ' {"term": "Mikroplastik-Verbot", "reason": "y"}]}'
        ),
    )

    assert [t.term for t in result] == ["Mikroplastik-Verbot"]


# --- The route ------------------------------------------------------------------


def test_accepting_a_theme_adds_it_to_the_search_terms(factory, client, monkeypatch):
    monkeypatch.setattr(settings_routes, "_run_theme_radar", lambda *a, **k: None)
    with factory() as session:
        subject = _client()
        session.add(subject)
        session.commit()
        client_id = subject.id

    client.post(f"/settings/clients/{client_id}/themes/accept", data={"term": "Clean Beauty"})

    with factory() as session:
        assert session.get(Client, client_id).keywords == [
            "KI in der Kosmetik",
            "Clean Beauty",
        ]


def test_accepting_the_same_theme_twice_does_not_duplicate_it(factory, client, monkeypatch):
    monkeypatch.setattr(settings_routes, "_run_theme_radar", lambda *a, **k: None)
    with factory() as session:
        subject = _client(keywords=["Clean Beauty"])
        session.add(subject)
        session.commit()
        client_id = subject.id

    for _ in range(2):
        client.post(
            f"/settings/clients/{client_id}/themes/accept", data={"term": "clean beauty"}
        )

    with factory() as session:
        assert session.get(Client, client_id).keywords == ["Clean Beauty"]


def test_the_panel_shows_the_measurement_and_marks_the_empty_ones(factory, client):
    with factory() as session:
        subject = _client()
        session.add(subject)
        session.commit()
        client_id = subject.id

    themework.state[client_id] = {
        "state": "fertig",
        "client": "IB-7 Beauty Tech",
        "probes": [
            themes.ThemeProbe(
                term="Nachhaltige Verpackung", reason="r", external=4, own=0,
                samples=("Transgourmet ersetzt Styropor",),
            ),
            themes.ThemeProbe(term="Mikroplastik-Verbot", reason="r", external=0, own=0),
        ],
    }

    body = " ".join(client.get("/settings").text.split())

    assert "Nachhaltige Verpackung" in body
    assert "<strong>4</strong> Marktmeldung(en)" in body
    assert "Transgourmet ersetzt Styropor" in body
    # The one with no hits is offered but must not look like the ones that work.
    assert "keine Treffer" in body
    assert "rival--empty" in body


def test_a_failed_suggestion_says_so_rather_than_showing_an_empty_list(factory, client):
    with factory() as session:
        subject = _client()
        session.add(subject)
        session.commit()
        client_id = subject.id

    themework.state[client_id] = {
        "state": "fehler",
        "error": "claude ist weg",
    }

    body = client.get("/settings").text

    assert "Themenvorschlag fehlgeschlagen" in body
    assert "claude ist weg" in body


# --- The remedy where the problem appears ---------------------------------------


def test_the_impulse_page_offers_themes_when_the_radar_found_nothing(factory, client):
    """The report came back three times — "es funktioniert immer noch nicht" —
    while the fix sat one page away. A message that names a cause has to carry
    its remedy."""
    from newspulse.web.routes import advisory

    with factory() as session:
        subject = _client()
        session.add(subject)
        session.commit()
        client_id = subject.id

    advisory._last_refusal[client_id] = (
        "Das Themen-Radar hat keine Marktmeldung gefunden, die nicht schon "
        "Berichterstattung über den Mandanten selbst ist."
    )
    try:
        body = client.get(f"/client/{client_id}/advice").text
    finally:
        advisory._last_refusal.pop(client_id, None)

    assert "Passende Themen vorschlagen" in body
    assert f'action="/client/{client_id}/themes"' in body


def test_the_measured_proposals_appear_on_the_impulse_page_itself(factory, client):
    from newspulse.web.routes import advisory

    with factory() as session:
        subject = _client()
        session.add(subject)
        session.commit()
        client_id = subject.id

    advisory._last_refusal[client_id] = "Kein Marktmaterial."
    themework.state[client_id] = {
        "state": "fertig",
        "client": "IB-7 Beauty Tech",
        "probes": [
            themes.ThemeProbe(term="Clean Beauty", reason="r", external=5, own=0),
            themes.ThemeProbe(term="KI-Hautpflege", reason="r", external=0, own=9),
        ],
    }
    try:
        body = " ".join(client.get(f"/client/{client_id}/advice").text.split())
    finally:
        advisory._last_refusal.pop(client_id, None)

    assert "Clean Beauty" in body
    assert "<strong>5</strong> Marktmeldung(en)" in body
    assert "rival--empty" in body


def test_no_remedy_is_offered_when_there_was_no_refusal(factory, client):
    """The offer belongs to the failure, not to the page."""
    with factory() as session:
        subject = _client()
        session.add(subject)
        session.commit()
        client_id = subject.id

    body = client.get(f"/client/{client_id}/advice").text

    assert "Passende Themen vorschlagen" not in body
