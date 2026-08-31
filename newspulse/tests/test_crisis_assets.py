"""The two crisis formats (UHR-02): refusal before invention, DEC-3's order.

Driven with an injected ``invoke`` and an injected ``generate`` like every other
generator test, so the whole path runs without a subprocess or a network call.

The tests worth reading twice are the refusal ones and the order-of-operations
ones. A holding statement that invents a spokesperson or a number is the most
expensive artefact this tool can produce, in exactly the hour it is quoted most;
and DEC-3 locks the sequence — the draft is on screen before any check has run,
visibly unchecked, and release waits for the guide's answer and for nothing else.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import assets, profile
from newspulse.models import (
    Article,
    Asset,
    AssetKind,
    Base,
    CheckState,
    Client,
    Crisis,
)
from newspulse.schemas import AssetDraft


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    return factory()


_SPRECHER = "Alexandra Prot, Geschäftsführerin"
_KONTAKT = "Jonas Weber, Leiter Kommunikation, erreichbar bis Mitternacht"
_GUIDE = "No-Gos: Keine Heilversprechen. Nie über Preise sprechen."
_HEADLINE = "Verbraucherzentrale mahnt Solaranbieter ab"
_SNIPPET = "Sechs Anbieter erhalten Abmahnungen wegen irreführender Werbung."

_CRISIS_PROFILE = {"sprecher": _SPRECHER, "krisenkontakt": _KONTAKT}


def _crisis_mandate(
    session, *, facts: dict[str, str] | None = None, articles: int = 2
) -> tuple[Client, Crisis]:
    """A mandate in a declared crisis, with exactly the profile the test needs."""
    client = Client(
        name="Alpha AG", industry="Solar", keywords=["Solar"], comms_guide=_GUIDE
    )
    session.add(client)
    session.flush()
    for key, value in (facts if facts is not None else _CRISIS_PROFILE).items():
        profile.save(session, client, key, value)

    first = None
    for i in range(articles):
        article = Article(
            title=f"{_HEADLINE} ({i})",
            url=f"https://ex.de/krise-{i}",
            source="Handelsblatt",
            published_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=3),
            fetched_at=dt.datetime.now(dt.UTC),
            summary_text=_SNIPPET,
            language="de",
            title_hash=f"krise{i:04d}",
        )
        session.add(article)
        session.flush()
        first = first or article

    crisis = Crisis(
        client_id=client.id,
        article_id=first.id,
        declared_by="mensch",
        declared_at=dt.datetime.now(dt.UTC),
        level=3,
    )
    session.add(crisis)
    session.commit()
    return client, crisis


_GOOD_HOLDING_BODY = (
    "Die Alpha AG nimmt die Berichterstattung ernst. Wir prüfen derzeit die "
    "betroffenen Abläufe. Sobald gesicherte Erkenntnisse vorliegen, informieren "
    "wir öffentlich.\n\nErreichbar: Jonas Weber, Leiter Kommunikation."
)


def _reply(body: str = _GOOD_HOLDING_BODY, **over) -> str:
    payload = {"title": "Holding Statement", "body": body, "speaker": _SPRECHER}
    payload.update(over)
    return json.dumps(payload)


#: One payload both checkers can read: MessageReview and GuideVerdict ignore
#: unknown fields, so the crosscheck takes its half and the guide check its own.
_ALL_CLEAR = json.dumps(
    {"send": True, "concerns": [], "fix": "", "ok": True, "breaches": []}
)


def _holding():
    return assets.definition(AssetKind.HOLDING_STATEMENT)


def _qa():
    return assets.definition(AssetKind.KRISEN_QA)


# --- The refusal: no field, no text, and the old text stands ---------------------


def test_a_missing_spokesperson_refuses_and_names_the_field(session):
    client, crisis = _crisis_mandate(session, facts={"krisenkontakt": _KONTAKT})

    with pytest.raises(assets.RequirementsMissing) as excinfo:
        assets.write_crisis(
            session, _holding(), client, crisis, invoke=lambda *a, **k: _reply()
        )

    assert "Sprecher" in str(excinfo.value)
    assert session.scalars(select(Asset)).first() is None


def test_a_missing_crisis_contact_refuses_and_names_the_field(session):
    client, crisis = _crisis_mandate(session, facts={"sprecher": _SPRECHER})

    with pytest.raises(assets.RequirementsMissing) as excinfo:
        assets.write_crisis(
            session, _holding(), client, crisis, invoke=lambda *a, **k: _reply()
        )

    assert "Krisenkontakt" in str(excinfo.value)


def test_a_refusal_leaves_the_previous_text_standing(session):
    """The acceptance's second half: a failed re-write never touches the draft
    that already exists for this format."""
    client, crisis = _crisis_mandate(session)
    standing = assets.produce_crisis(
        session,
        _holding(),
        client,
        crisis,
        invoke=lambda *a, **k: _reply(),
        generate=lambda prompt: _ALL_CLEAR,
    )
    kept_body = standing.body

    # The spokesperson disappears from the profile before the second attempt.
    profile.save(session, client, "sprecher", "")
    with pytest.raises(assets.RequirementsMissing):
        assets.produce_crisis(
            session,
            _holding(),
            client,
            crisis,
            invoke=lambda *a, **k: _reply(body="Ein anderer Text. Wir prüfen."),
            generate=lambda prompt: _ALL_CLEAR,
        )

    rows = list(session.scalars(select(Asset)).all())
    assert len(rows) == 1
    assert rows[0].body == kept_body


# --- The anchor: both texts hang on the crisis -----------------------------------


def test_the_text_hangs_on_the_crisis_and_is_findable(session):
    client, crisis = _crisis_mandate(session)

    row = assets.produce_crisis(
        session,
        _holding(),
        client,
        crisis,
        invoke=lambda *a, **k: _reply(),
        generate=lambda prompt: _ALL_CLEAR,
    )

    assert row.crisis_id == crisis.id
    assert row.angle_id is None
    assert [found.id for found in assets.for_crisis(session, crisis.id)] == [row.id]


def test_a_rewrite_replaces_the_crisis_draft_rather_than_stacking_one(session):
    client, crisis = _crisis_mandate(session)
    kwargs = {"invoke": lambda *a, **k: _reply(), "generate": lambda p: _ALL_CLEAR}

    first = assets.produce_crisis(session, _holding(), client, crisis, **kwargs)
    second = assets.produce_crisis(session, _holding(), client, crisis, **kwargs)

    assert first.id == second.id
    assert len(assets.for_crisis(session, crisis.id)) == 1


# --- The prompt: guide, profile, and the coverage that counts --------------------


def test_the_prompt_carries_contact_speaker_and_the_crisis_coverage(session):
    client, crisis = _crisis_mandate(session)

    prompt = assets.crisis_prompt_for(session, _holding(), client, crisis)

    assert _KONTAKT in prompt
    assert _SPRECHER in prompt
    assert _HEADLINE in prompt.partition("BEITRÄGE, DIE ZUR KRISE ZÄHLEN")[2]
    assert "No-Gos" in prompt


# --- The contract: no unbacked number, the checking sentence, the open answer ----


def test_an_unbacked_number_is_refused_after_the_retry(session):
    """A number no handed-over source carries is the invention the block
    forbids, and the writer gives up on it the way it gives up on a missing
    boilerplate: one retry carrying the complaint, then a refusal."""
    client, crisis = _crisis_mandate(session)
    calls = []

    def invoke(prompt, **kwargs):
        calls.append(prompt)
        return _reply(body="Wir prüfen die Lage. Betroffen sind 250 Kunden.")

    with pytest.raises(assets.Malformed) as excinfo:
        assets.write_crisis(session, _holding(), client, crisis, invoke=invoke)

    assert len(calls) == 2
    assert "250" in str(excinfo.value)
    # A backed number is a different story: the snippet says "Sechs", digits in
    # the sources pass.
    assert "250" not in calls[0]


def test_a_backed_number_passes_the_number_check():
    given = assets.Given(sources="Die Prüfung läuft seit dem 29. August 2026.")
    draft = AssetDraft(
        title="", body="Wir prüfen die Abläufe seit dem 29. August 2026."
    )

    assert assets.validate(_holding(), draft, given) == []


def test_the_clients_own_name_is_not_an_unbacked_number(session):
    """A mandate whose name carries a digit can be named in its own statement.

    The prompt hands the writer the mandate's identity, so the sources the
    number check searches carry it too. Without that, a client called
    "Energie 2050 AG" could never appear in his own holding statement — the
    one text that exists to carry his name."""
    client, crisis = _crisis_mandate(session)
    client.name = "Energie 2050 AG"
    session.commit()
    body = _GOOD_HOLDING_BODY.replace("Alpha AG", "Energie 2050 AG")

    draft = assets.write_crisis(
        session,
        _holding(),
        client,
        crisis,
        invoke=lambda *a, **k: _reply(body=body),
    )

    assert "Energie 2050 AG" in draft.body


def test_the_checking_sentence_is_required():
    draft = AssetDraft(title="", body="Die Alpha AG nimmt die Lage ernst.")

    faults = assets.validate(_holding(), draft, assets.Given())

    assert any("geprüft wird" in fault for fault in faults)


def _qa_body(questions: int, *, open_answer: str | None = None) -> str:
    lines = []
    for i in range(questions):
        lines.append(f"Frage Nummer {'abcdefghijklm'[i]}?")
        if open_answer is not None and i == questions - 1:
            lines.append(open_answer)
        else:
            lines.append("Die Antwort stützt sich auf die Meldungen oben.")
    return "\n".join(lines)


def test_the_qa_needs_six_to_twelve_questions():
    fmt = _qa()

    too_few = assets.validate(fmt, AssetDraft(title="", body=_qa_body(3)), assets.Given())
    too_many = assets.validate(
        fmt, AssetDraft(title="", body=_qa_body(13)), assets.Given()
    )
    in_band = assets.validate(
        fmt, AssetDraft(title="", body=_qa_body(6)), assets.Given()
    )

    assert any("6 bis 12" in fault for fault in too_few)
    assert any("6 bis 12" in fault for fault in too_many)
    assert in_band == []


def test_a_question_without_any_answer_is_a_fault():
    body = _qa_body(6) + "\nUnd was ist mit den Kosten?"

    faults = assets.validate(_qa(), AssetDraft(title="", body=body), assets.Given())

    assert any("ohne Antwort" in fault for fault in faults)


def test_an_open_answer_without_a_reason_is_a_fault():
    bare = _qa_body(6, open_answer="Noch offen.")
    reasoned = _qa_body(
        6, open_answer="Noch offen: die Zahl der Betroffenen ist nirgends belegt."
    )

    bare_faults = assets.validate(
        _qa(), AssetDraft(title="", body=bare), assets.Given()
    )
    reasoned_faults = assets.validate(
        _qa(), AssetDraft(title="", body=reasoned), assets.Given()
    )

    assert any("Begründung" in fault for fault in bare_faults)
    assert reasoned_faults == []


# --- DEC-3: stored first, checked after, released only past the guide ------------


def test_the_draft_is_stored_before_any_check_has_run(session):
    """The checker sees a committed row, and its failure loses nothing: the
    paid-for text stands as UNGEPRUEFT — and never as unbeanstandet."""
    client, crisis = _crisis_mandate(session)
    seen = {}

    def broken_checker(prompt):
        seen["stored"] = session.scalars(select(Asset)).first() is not None
        raise RuntimeError("Prüfer nicht erreichbar")

    row = assets.produce_crisis(
        session,
        _holding(),
        client,
        crisis,
        invoke=lambda *a, **k: _reply(),
        generate=broken_checker,
    )

    assert seen["stored"] is True
    assert row.check_state is CheckState.UNGEPRUEFT
    assert row.check_state is not CheckState.GEPRUEFT
    assert row.body  # the paid-for text is on the row


def test_release_is_locked_while_the_guide_check_has_not_answered(session):
    client, crisis = _crisis_mandate(session)
    row = assets.produce_crisis(
        session,
        _holding(),
        client,
        crisis,
        invoke=lambda *a, **k: _reply(),
        generate=lambda p: (_ for _ in ()).throw(RuntimeError("noch unterwegs")),
    )

    assert not assets.releasable(row)
    with pytest.raises(assets.Refused) as excinfo:
        assets.release(session, row, by="lucas")

    assert str(excinfo.value) == assets.GUIDE_UNANSWERED
    assert row.released_at is None


def test_a_crosscheck_still_running_does_not_lock_the_release(session):
    """DEC-3's asymmetry: the guide's answer gates, the Gegenprüfer may trail."""
    client, crisis = _crisis_mandate(session)
    row = assets.produce_crisis(
        session,
        _holding(),
        client,
        crisis,
        invoke=lambda *a, **k: _reply(),
        generate=lambda p: (_ for _ in ()).throw(RuntimeError("noch unterwegs")),
    )
    # The guide has answered; the crosscheck has not.
    row.guide_reviewed_by = "gemini-2.5-flash"
    row.guide_review_ok = True
    session.commit()
    assert not row.reviewed_by

    released = assets.release(session, row, by="lucas")

    assert released.released_at is not None
    assert released.released_by == "lucas"


def test_answered_checks_unlock_the_release(session):
    client, crisis = _crisis_mandate(session)

    row = assets.produce_crisis(
        session,
        _holding(),
        client,
        crisis,
        invoke=lambda *a, **k: _reply(),
        generate=lambda prompt: _ALL_CLEAR,
    )

    assert row.guide_reviewed_by
    assert row.check_state is CheckState.GEPRUEFT
    assert assets.releasable(row)
    assert assets.release(session, row, by="lucas").released_at is not None


# --- The migration: room for the anchor, and the old index survives --------------


def test_the_migration_adds_the_crisis_anchor(tmp_path, monkeypatch):
    """`alembic upgrade head` on a throwaway file leaves assets able to hold a
    crisis text — and the batch rebuild keeps the *old* partial index partial,
    which reflection could silently lose."""
    from alembic import command
    from alembic.config import Config

    from newspulse import config

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "migrated.db")

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}")
    try:
        columns = {c["name"]: c for c in inspect(engine).get_columns("assets")}
        assert "crisis_id" in columns
        assert columns["angle_id"]["nullable"] is True
        with engine.connect() as conn:
            indexes = {
                name: sql
                for name, sql in conn.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='assets' AND sql IS NOT NULL"
                ).all()
            }
        assert "WHERE released_at IS NULL" in indexes["ux_assets_angle_kind_unreleased"]
        assert (
            "crisis_id IS NOT NULL" in indexes["ux_assets_crisis_kind_unreleased"]
        )
    finally:
        engine.dispose()
