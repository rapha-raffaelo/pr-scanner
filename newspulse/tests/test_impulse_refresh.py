"""Every mandate should have something to say, not only the loud ones.

The sweep drafted a positioning only for mandates whose radar moved that
morning. Right for "react to what arrived overnight", wrong as the only source
of drafts: a mandate whose field was quiet for a fortnight showed an empty
Impulse column for a fortnight — and that is precisely the mandate whose
consultant most needs something to send.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from newspulse import angles, job
from newspulse.db import make_engine
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Base,
    Category,
    Client,
    TopicHit,
)
from newspulse.schemas import AngleDraft

_NOW = dt.datetime(2026, 8, 3, 7, 0, tzinfo=dt.UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _client(session, name="IB-7", **over) -> Client:
    client = Client(
        name=name,
        aliases=[],
        keywords=over.get("keywords", ["Clean Beauty"]),
        alert_topics=[],
        country="DE",
        is_competitor=over.get("is_competitor", False),
    )
    session.add(client)
    session.commit()
    return client


def _market(session, client, title, *, days_ago=20) -> Article:
    """A stored radar hit: market coverage, linked to the client, not about it."""
    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 10**6}",
        source="Textilwirtschaft",
        published_at=_NOW - dt.timedelta(days=days_ago),
        fetched_at=_NOW - dt.timedelta(days=days_ago),
        summary_text=None,
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.flush()
    session.add(
        TopicHit(article_id=article.id, client_id=client.id, found_at=_NOW)
    )
    session.commit()
    return article


def _draft(**over):
    def _suggest(sess, cli, material, **_):
        numbered = angles.developments(material)
        draft = AngleDraft(
            worth_sending=over.get("worth_sending", True),
            subject=over.get("subject", "Clean Beauty: die Prüfbarkeit entscheidet"),
            message="Zwei Absätze Text." if over.get("worth_sending", True) else "",
            context="c",
            thesis="t",
            evidence=[0] if numbered else [],
        )
        return (draft, numbered) if draft.worth_sending else None

    return _suggest


def test_a_mandate_with_no_draft_gets_one_from_stored_material(session, monkeypatch):
    """The radar found nothing new today, but the archive is not empty."""
    client = _client(session)
    _market(session, client, "Clean Beauty wird Pflicht im Handel")
    monkeypatch.setattr(angles, "suggest", _draft())

    written = job._refresh_impulses(session, [client], [], now=_NOW)

    assert written == 1
    assert session.scalar(select(func.count()).select_from(Angle)) == 1


def test_a_current_draft_is_left_alone(session, monkeypatch):
    """Re-pitching the same development every morning is the behaviour that
    trains a reader to ignore the column."""
    client = _client(session)
    _market(session, client, "Clean Beauty wird Pflicht im Handel")
    session.add(
        Angle(
            client_id=client.id,
            generated_at=_NOW - dt.timedelta(days=2),
            subject="Schon vorhanden",
            message="Text.",
            context="c",
            thesis="t",
        )
    )
    session.commit()
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
    )

    assert job._refresh_impulses(session, [client], [], now=_NOW) == 0


def test_a_draft_that_has_aged_out_is_replaced(session, monkeypatch):
    """Seven days, to match the window the Today column reads: any longer and the
    column empties while a draft still exists, which reads as a broken feature."""
    client = _client(session)
    _market(session, client, "Clean Beauty wird Pflicht im Handel")
    session.add(
        Angle(
            client_id=client.id,
            generated_at=_NOW - dt.timedelta(days=9),
            subject="Zu alt",
            message="Text.",
            context="c",
            thesis="t",
        )
    )
    session.commit()
    monkeypatch.setattr(angles, "suggest", _draft(subject="Frisch"))

    assert job._refresh_impulses(session, [client], [], now=_NOW) == 1
    assert angles.latest(session, client.id).subject == "Frisch"


def test_a_competitor_is_never_drafted_for(session, monkeypatch):
    """A competitor is monitored, never reported to — and never pitched for."""
    rival = _client(session, name="H&M", is_competitor=True)
    _market(session, rival, "Clean Beauty wird Pflicht im Handel")
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
    )

    assert job._refresh_impulses(session, [rival], [], now=_NOW) == 0


def test_a_mandate_without_market_material_costs_no_call(session, monkeypatch):
    """No material, no question to ask. The refresh is bounded by what is stored,
    not by a cap, so a quiet portfolio must not spend a call per client per day."""
    client = _client(session)
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
    )

    assert job._refresh_impulses(session, [client], [], now=_NOW) == 0


def test_the_clients_own_press_is_not_what_gets_refreshed_from(session, monkeypatch):
    """Same rule as the button: the radar is coverage the client can speak *to*."""
    client = _client(session)
    own = _market(session, client, "IB-7 eröffnet neues Werk")
    session.add(
        Analysis(
            article_id=own.id,
            client_id=client.id,
            summary="Über den Mandanten.",
            category=Category.SONSTIGES,
            relevance_score=8,
            importance_score=6,
            is_alert=False,
        )
    )
    session.commit()
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask about its own press")
    )

    assert job._refresh_impulses(session, [client], [], now=_NOW) == 0


def test_a_model_failure_never_marks_the_sweep_broken(session, monkeypatch):
    """A missing draft is a missed opportunity, not a broken sweep — marking the
    run partial for it would also re-open the "since" window for a reason that has
    nothing to do with coverage."""
    client = _client(session)
    _market(session, client, "Clean Beauty wird Pflicht im Handel")

    def _boom(*a, **k):
        raise RuntimeError("claude ist weg")

    monkeypatch.setattr(angles, "suggest", _boom)
    errors: list[str] = []

    assert job._refresh_impulses(session, [client], errors, now=_NOW) == 0
    assert errors == []


def test_the_daily_run_actually_calls_the_refresh(session, monkeypatch):
    """The wiring, not just the function.

    The refresh had unit tests and no test that it was reached — which is exactly
    the gap that lets a feature be "built" and still never happen. A mandate with
    stored market material and no current draft must come out of an ordinary
    sweep with one.
    """
    client = _client(session)
    _market(session, client, "Clean Beauty wird Pflicht im Handel")
    monkeypatch.setattr(angles, "suggest", _draft())

    class _Analyzer:
        def analyze(self, client, articles):
            return []

    report = job.run(
        session,
        feeds=[],  # no fetching: the point is what the sweep does with the archive
        fetch=lambda *a, **k: [],
        analyzer=_Analyzer(),
        now=lambda: _NOW,
    )

    assert report.angles_written == 1
    assert session.scalar(select(func.count()).select_from(Angle)) == 1


# --- The reason has to outlive the sweep that produced it -----------------------


def test_the_sweep_records_why_it_produced_nothing(session, monkeypatch):
    """Six reports of "es funktioniert immer noch nicht" over work that was
    running correctly, because the explanation lived in a dict in the web process
    and only the button ever wrote to it. The 06:10 sweep left no trace, so the
    page at nine had nothing to say."""
    client = _client(session)
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("no material to ask about")
    )

    job._refresh_impulses(session, [client], [], now=_NOW)

    stored = session.get(Client, client.id)
    assert "Kein Marktmaterial" in stored.impulse_note
    assert stored.impulse_checked_at == _NOW


def test_the_models_own_reason_is_what_gets_recorded(session, monkeypatch):
    client = _client(session)
    _market(session, client, "Clean Beauty wird Pflicht im Handel")

    def _refuse(sess, cli, material, **kw):
        kw["note"]("Vereinzelte Wettbewerbermeldungen, kein Aufhänger.")
        return None

    monkeypatch.setattr(angles, "suggest", _refuse)

    job._refresh_impulses(session, [client], [], now=_NOW)

    assert "kein Aufhänger" in session.get(Client, client.id).impulse_note


def test_a_successful_draft_clears_the_note(session, monkeypatch):
    """Otherwise last week's refusal sits under this week's impulse."""
    client = _client(session)
    client.impulse_note = "Alte Absage."
    session.commit()
    _market(session, client, "Clean Beauty wird Pflicht im Handel")
    monkeypatch.setattr(angles, "suggest", _draft())

    job._refresh_impulses(session, [client], [], now=_NOW)

    assert session.get(Client, client.id).impulse_note == ""


def test_the_page_shows_what_the_sweep_recorded(factory_app):
    """The whole point: opened at nine, it says what happened at 06:10."""
    factory, http = factory_app
    with factory() as session:
        client = Client(name="Still AG", aliases=[], keywords=["X"], alert_topics=[])
        client.impulse_note = "Kein Marktmaterial in 90 Tagen."
        client.impulse_checked_at = _NOW
        session.add(client)
        session.commit()
        client_id = client.id

    body = http.get(f"/client/{client_id}/advice").text

    assert "Kein Marktmaterial in 90 Tagen." in body
    assert "Zuletzt geprüft" in body


@pytest.fixture
def factory_app():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from newspulse.web.app import create_app, get_db

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def _db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return factory, TestClient(app)
