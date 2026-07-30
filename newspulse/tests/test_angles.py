"""Tests for the positioning drafts (newspulse.angles).

Offline throughout: ``suggest`` takes an injectable ``invoke``, so the whole path —
prompt assembly, schema validation, evidence cleaning, storage — runs without a
subprocess. The cases that matter are the ones that keep the column trustworthy:
silence when there is no opening, a hard failure when the model contradicts itself,
and no citation that points at nothing.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import angles
from newspulse.analyzer import ParseError
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Base,
    Category,
    Client,
)

_WHEN = dt.datetime(2026, 7, 30, 6, 0, tzinfo=dt.UTC)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _client(session, **over) -> Client:
    client = Client(
        name=over.get("name", "Arrakis"),
        aliases=[],
        industry=over.get("industry", "Onchain-Liquidität"),
        website=over.get("website", "https://arrakis.finance"),
        country="DE",
        keywords=over.get("keywords", ["Onchain-Liquidität", "Token-Emittenten"]),
        alert_topics=over.get("alert_topics", ["Börsenschließung"]),
    )
    session.add(client)
    session.commit()
    return client


def _article(session, title: str, url: str, *, source="cash.at", summary=None) -> Article:
    article = Article(
        title=title,
        url=url,
        source=source,
        published_at=_WHEN,
        fetched_at=_WHEN,
        summary_text=summary,
        language="de",
        title_hash=url[-10:],
    )
    session.add(article)
    session.commit()
    return article


def _material(session, *titles: str) -> list[tuple[Article, str]]:
    """Radar material: stored articles paired with the feed that surfaced them."""
    return [
        (_article(session, title, f"https://ex.de/{i}"), "Themen-Radar: Arrakis")
        for i, title in enumerate(titles)
    ]


def _reply(**over) -> str:
    payload = {
        "worth_sending": True,
        "subject": "Börsenschließungen: Liquidität als Infrastruktur",
        "message": "Die aktuellen Schließungen zeigen nicht das Ende des Marktes.\n\n"
        "Projekte müssen Liquidität als eigene Infrastruktur betrachten.",
        "context": "Mehrere Handelsplätze haben ihre Abwicklung angekündigt (cash.at).",
        "credibility": "Berührt den Kern: der Mandant baut Liquidität onchain.",
        "thesis": "Der Markt konsolidiert und macht Plattformabhängigkeit sichtbar.",
        "overclaim": "Zentrale Börsen verschwinden, dezentrale gewinnen.",
        "statements": ["Liquidität ist Infrastruktur.", "Emittenten brauchen Kontrolle."],
        "evidence": [0],
    }
    payload.update(over)
    return json.dumps(payload)


# --- The gate ------------------------------------------------------------------


def test_no_material_means_no_call_at_all(session):
    """A mandate whose radar found nothing must not cost a model call."""
    client = _client(session)
    calls: list[str] = []

    def _invoke(prompt, **_):
        calls.append(prompt)
        return _reply()

    assert angles.suggest(session, client, [], invoke=_invoke) is None
    assert calls == []


def test_worth_sending_false_yields_nothing(session):
    """The normal answer on a normal day: no opening, no card, no error."""
    client = _client(session)
    material = _material(session, "BitMEX stellt den Betrieb ein")

    result = angles.suggest(
        session,
        client,
        material,
        invoke=lambda prompt, **_: _reply(
            worth_sending=False, subject="Kein Bezug zum Kern", message=""
        ),
    )

    assert result is None


def test_worth_sending_without_a_message_is_a_parse_error(session):
    """A draft that claims an opening but carries no text is a failed call.

    Storing it would put an empty card in the column, which reads as a broken tool
    rather than as the model having nothing to say — and the model *does* have a way
    to say that.
    """
    client = _client(session)
    material = _material(session, "BitMEX stellt den Betrieb ein")

    with pytest.raises(ParseError):
        angles.suggest(
            session, client, material, invoke=lambda prompt, **_: _reply(message="   ")
        )


def test_a_non_json_reply_is_a_parse_error(session):
    client = _client(session)
    material = _material(session, "BitMEX stellt den Betrieb ein")

    with pytest.raises(ParseError):
        angles.suggest(
            session, client, material, invoke=lambda prompt, **_: "Gerne! Hier der Text:"
        )


def test_a_fenced_reply_is_still_read(session):
    """Wrapping JSON in ```json is a habit, not an error.

    There is no retry behind this call — one call per mandate per day — so a fence
    that was allowed to fail would cost the day's draft outright.
    """
    client = _client(session)
    material = _material(session, "BitMEX stellt den Betrieb ein")
    fenced = f"```json\n{_reply()}\n```"

    draft, _numbered = angles.suggest(
        session, client, material, invoke=lambda prompt, **_: fenced
    )

    assert draft.worth_sending is True
    assert "Liquidität" in draft.message


# --- The prompt ----------------------------------------------------------------


def test_prompt_carries_the_profile_the_developments_and_the_warning(session):
    """The model must know who it writes for, off what — and that the coverage is
    not about them, which is the one thing it could otherwise get wrong."""
    client = _client(session)
    material = _material(session, "BitMEX stellt den Betrieb ein")
    seen: dict[str, str] = {}

    def _invoke(prompt, **_):
        seen["prompt"] = prompt
        return _reply()

    angles.suggest(session, client, material, invoke=_invoke)
    prompt = seen["prompt"]

    assert "Arrakis" in prompt
    assert "https://arrakis.finance" in prompt
    assert "Onchain-Liquidität" in prompt
    assert "Börsenschließung" in prompt  # the alert topics go in as themes too
    assert "BitMEX stellt den Betrieb ein" in prompt
    assert "NICHT vom Mandanten" in prompt
    assert "[0]" in prompt  # developments are numbered for citation


def test_own_coverage_is_offered_as_background_when_it_exists(session):
    """So the draft does not repeat a point the client already made this week."""
    client = _client(session)
    covered = _article(session, "Arrakis erweitert Vaults", "https://ex.de/own")
    session.add(
        Analysis(
            article_id=covered.id,
            client_id=client.id,
            summary="s",
            category=Category.PRODUKT,
            relevance_score=6,
            importance_score=6,
            is_alert=False,
        )
    )
    session.commit()
    material = _material(session, "BitMEX stellt den Betrieb ein")
    seen: dict[str, str] = {}

    def _invoke(prompt, **_):
        seen["prompt"] = prompt
        return _reply()

    angles.suggest(session, client, material, invoke=_invoke)

    assert "BERICHTERSTATTUNG ÜBER DEN MANDANTEN" in seen["prompt"]
    assert "Arrakis erweitert Vaults" in seen["prompt"]


def test_the_own_coverage_section_is_absent_when_there_is_none(session):
    """No dangling empty section on the common case."""
    client = _client(session)
    material = _material(session, "BitMEX stellt den Betrieb ein")
    seen: dict[str, str] = {}

    def _invoke(prompt, **_):
        seen["prompt"] = prompt
        return _reply()

    angles.suggest(session, client, material, invoke=_invoke)

    assert "BERICHTERSTATTUNG ÜBER DEN MANDANTEN" not in seen["prompt"]


def test_developments_are_newest_first(session):
    """An opening is a moment: the freshest development leads."""
    older = _article(session, "Alte Meldung", "https://ex.de/old")
    older.published_at = _WHEN - dt.timedelta(days=3)
    newer = _article(session, "Neue Meldung", "https://ex.de/new")
    session.commit()

    numbered = angles.developments([(older, "radar"), (newer, "radar")])

    assert [d.headline for d in numbered] == ["Neue Meldung", "Alte Meldung"]
    assert [d.index for d in numbered] == [0, 1]


# --- Evidence ------------------------------------------------------------------


def test_invented_evidence_ids_are_dropped(session):
    """A citation pointing at nothing discredits the whole draft."""
    client = _client(session)
    material = _material(session, "BitMEX stellt den Betrieb ein")

    draft, _numbered = angles.suggest(
        session, client, material, invoke=lambda prompt, **_: _reply(evidence=[0, 7, 99])
    )

    assert draft.evidence == [0]


def test_store_resolves_evidence_to_stored_article_ids(session):
    client = _client(session)
    material = _material(session, "Erste Meldung", "Zweite Meldung")
    draft, numbered = angles.suggest(
        session, client, material, invoke=lambda prompt, **_: _reply(evidence=[1])
    )

    stored = angles.store(session, client, draft, numbered)

    assert stored.article_ids == [numbered[1].article_id]
    assert stored.statements == ["Liquidität ist Infrastruktur.", "Emittenten brauchen Kontrolle."]
    assert stored.overclaim == "Zentrale Börsen verschwinden, dezentrale gewinnen."


def test_store_falls_back_to_every_development_when_none_was_cited(session):
    """A draft is never shown without the coverage behind it."""
    client = _client(session)
    material = _material(session, "Erste Meldung", "Zweite Meldung")
    draft, numbered = angles.suggest(
        session, client, material, invoke=lambda prompt, **_: _reply(evidence=[])
    )

    stored = angles.store(session, client, draft, numbered)

    assert sorted(stored.article_ids) == sorted(d.article_id for d in numbered)


# --- Reading them back ---------------------------------------------------------


def _angle(client_id: int, subject: str, when: dt.datetime) -> Angle:
    return Angle(
        client_id=client_id, generated_at=when, subject=subject, message="m", context="c"
    )


def test_recent_keeps_a_draft_standing_for_a_week(session):
    """An opening does not expire at midnight.

    The market development a draft rests on is still current days later, and a
    column that empties itself every night hides work that is still usable.
    """
    client = _client(session)
    session.add_all(
        [
            _angle(client.id, "vorgestern", dt.datetime(2026, 7, 28, 6, 0, tzinfo=dt.UTC)),
        ]
    )
    session.commit()

    found = angles.recent(session, dt.datetime(2026, 7, 30, 22, 0, tzinfo=dt.UTC))

    assert [a.subject for a in found] == ["vorgestern"]


def test_recent_drops_a_draft_older_than_the_window(session):
    """Past a week the news has moved and the draft is no longer current."""
    client = _client(session)
    session.add(_angle(client.id, "alt", dt.datetime(2026, 7, 20, 6, 0, tzinfo=dt.UTC)))
    session.commit()

    assert angles.recent(session, dt.datetime(2026, 7, 30, 22, 0, tzinfo=dt.UTC)) == []


def test_recent_keeps_only_the_newest_draft_per_client(session):
    """Two drafts for one mandate in a week are two attempts at the same moment,
    not two things to send — stacking them turns the column into a backlog."""
    client = _client(session)
    session.add_all(
        [
            _angle(client.id, "aelter", dt.datetime(2026, 7, 27, 6, 0, tzinfo=dt.UTC)),
            _angle(client.id, "neuer", dt.datetime(2026, 7, 29, 6, 0, tzinfo=dt.UTC)),
        ]
    )
    session.commit()

    found = angles.recent(session, dt.datetime(2026, 7, 30, 22, 0, tzinfo=dt.UTC))

    assert [a.subject for a in found] == ["neuer"]


def test_recent_ignores_drafts_generated_after_the_viewed_day(session):
    """Looking at a past day must show what was current then, not what is now —
    the page is a day, and its right-hand column has to agree with the rest."""
    client = _client(session)
    session.add(_angle(client.id, "heute", dt.datetime(2026, 7, 30, 6, 0, tzinfo=dt.UTC)))
    session.commit()

    found = angles.recent(session, dt.datetime(2026, 7, 29, 22, 0, tzinfo=dt.UTC))

    assert found == []


def test_recent_lists_one_draft_per_client_newest_first(session):
    first = _client(session)
    second = _client(session, name="Zalando")
    session.add_all(
        [
            _angle(first.id, "arrakis", dt.datetime(2026, 7, 28, 6, 0, tzinfo=dt.UTC)),
            _angle(second.id, "zalando", dt.datetime(2026, 7, 29, 6, 0, tzinfo=dt.UTC)),
        ]
    )
    session.commit()

    found = angles.recent(session, dt.datetime(2026, 7, 30, 22, 0, tzinfo=dt.UTC))

    assert [a.subject for a in found] == ["zalando", "arrakis"]


def test_latest_returns_the_newest_draft_for_one_client(session):
    client = _client(session)
    other = _client(session, name="Zalando")
    session.add_all(
        [
            Angle(
                client_id=client.id,
                generated_at=dt.datetime(2026, 7, 28, 6, 0, tzinfo=dt.UTC),
                subject="alt",
                message="m",
                context="c",
            ),
            Angle(
                client_id=client.id,
                generated_at=dt.datetime(2026, 7, 30, 6, 0, tzinfo=dt.UTC),
                subject="neu",
                message="m",
                context="c",
            ),
            Angle(
                client_id=other.id,
                generated_at=dt.datetime(2026, 7, 31, 6, 0, tzinfo=dt.UTC),
                subject="fremd",
                message="m",
                context="c",
            ),
        ]
    )
    session.commit()

    assert angles.latest(session, client.id).subject == "neu"
    assert session.scalar(select(Angle).where(Angle.subject == "fremd")) is not None
