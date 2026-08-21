"""The guide check (newspulse.guide.check_guide) and what happens when it breaks.

The happy paths are exercised through the formats in ``test_assets.py``, where
they belong: the guide check is one half of ``assets.check()`` and a verdict that
never reaches a stored row is a verdict nobody sees. What lives here is the
checker's own contract, which is mostly about failure.

The rule the whole file circles: this check is the one that catches the mistake
that costs a mandate, and every way it can fail to produce a verdict must end in a
text that reads as unchecked. Silence must never render as approval, and a broken
second call must never take the first one's paid-for verdict down with it.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import assets, brain, guide
from newspulse.analyzer import ParseError
from newspulse.models import Base, CheckState, Client


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


_GUIDE = 'No-Go: das Wort günstig. Register: nüchtern, nie "wir freuen uns".'


def _client(session, *, comms_guide: str = _GUIDE) -> Client:
    client = Client(name="Alpha AG", industry="Neobroker", comms_guide=comms_guide)
    session.add(client)
    session.commit()
    return client


def _checkable() -> assets.Checkable:
    return assets.Checkable(
        kind="ein Text im Format Statement",
        title_label="Titel",
        title="Alpha AG baut aus",
        body="Wir bauen die Verwahrung aus.",
        thesis="Verfügbarkeit ist ein Risikoparameter.",
        overclaim="Die Kette ist unzuverlässig.",
        evidence="BELEGTE MELDUNGEN\n- (Börsen-Zeitung) Banken bauen Depots",
    )


def _review(**over) -> str:
    payload = {"send": True, "concerns": [], "fix": ""}
    payload.update(over)
    return json.dumps(payload)


def _verdict(**over) -> str:
    payload = {"ok": True, "breaches": []}
    payload.update(over)
    return json.dumps(payload)


def _boom(*args, **kwargs):
    raise RuntimeError("the second provider is unreachable")


# --- What the checker reads ----------------------------------------------------


def test_the_stored_guide_reaches_the_prompt_verbatim(session):
    """The failure being guarded against is silent omission: a check that ran
    against no rules answers "nothing to object to" in a perfectly normal voice."""
    client = _client(session)
    seen: list[str] = []

    guide.check_guide(
        client,
        title="Alpha AG baut aus",
        body="Das günstigste Angebot am Markt.",
        generate=lambda prompt, **k: seen.append(prompt) or _verdict(),
    )

    assert _GUIDE in seen[0]


def test_the_check_reads_only_the_guide_and_the_draft(session):
    """No article, no profile, no impulse. Every extra fact is another thing the
    model can reason its way around when it should be reading a rule literally."""
    client = _client(session)
    seen: list[str] = []

    guide.check_guide(
        client,
        title="Alpha AG baut aus",
        body="Wir bauen die Verwahrung aus.",
        generate=lambda prompt, **k: seen.append(prompt) or _verdict(),
    )

    assert "BELEGTE MELDUNGEN" not in seen[0]
    assert "Neobroker" not in seen[0]
    assert "Risikoparameter" not in seen[0]


def test_a_mandate_without_a_guide_is_not_checked_and_costs_no_call(session):
    """A distinct state, returned as one. "No objections" and "nothing to object
    with" must never be the same answer."""
    client = _client(session, comms_guide="")
    calls: list[str] = []

    result = guide.check_guide(
        client,
        title="",
        body="Wir bauen aus.",
        generate=lambda prompt, **k: calls.append(prompt) or _verdict(),
    )

    assert result is None
    assert calls == []


def test_unparseable_output_raises_rather_than_reading_as_clean(session):
    """The one answer this function may not give for a reply it did not
    understand is an empty breach list."""
    client = _client(session)

    with pytest.raises(ParseError):
        guide.check_guide(
            client,
            title="",
            body="Wir bauen aus.",
            generate=lambda *a, **k: "Klar, hier die Prüfung: alles in Ordnung!",
        )


def test_more_breaches_than_the_cap_are_truncated_rather_than_rejected(session):
    """The draft that breaks six rules is the one whose verdict matters most, so
    it must not be the one that fails to parse."""
    client = _client(session)
    six = [
        {"sentence": f"Satz {i}.", "rule": f"Regel {i}."}
        for i in range(6)
    ]

    verdict, _ = guide.check_guide(
        client,
        title="",
        body="Wir bauen aus.",
        generate=lambda *a, **k: _verdict(ok=False, breaches=six),
    )

    assert len(verdict.breaches) == 5
    assert verdict.breaches[0].sentence == "Satz 0."


# --- What happens to the other verdict when this one breaks --------------------


def test_a_failing_guide_check_leaves_the_crosscheck_intact(session):
    """The crosscheck has been paid for by the time the guide call is made.
    Throwing it away because a second call failed leaves the caller with less than
    it had a line earlier, and with an exception instead of a text."""
    client = _client(session)

    checked = assets.check(
        session,
        client,
        _checkable(),
        generate=lambda *a, **k: _review(send=False, concerns=["Zu werblich."]),
        guide_generate=_boom,
    )

    assert checked.review is not None
    assert checked.review.concerns == ["Zu werblich."]
    assert checked.reviewed_by
    assert checked.guide is None
    assert checked.guide_failed is True
    assert checked.guide_note == guide.CHECK_FAILED


def test_an_unparseable_guide_reply_leaves_the_crosscheck_intact(session):
    """Same isolation for the likelier failure: the call succeeds and the reply is
    prose."""
    client = _client(session)

    checked = assets.check(
        session,
        client,
        _checkable(),
        generate=lambda *a, **k: _review(),
        guide_generate=lambda *a, **k: "Der Text hält sich an den Guide.",
    )

    assert checked.review is not None
    assert checked.guide is None
    assert checked.guide_failed is True


def test_a_failed_guide_check_is_logged_at_error(session, caplog):
    """Nothing read this text against the mandate's own rules. That is the check
    whose absence costs a mandate, so it is not a WARNING."""
    client = _client(session)

    with caplog.at_level("ERROR"):
        assets.check(
            session,
            client,
            _checkable(),
            generate=lambda *a, **k: _review(),
            guide_generate=_boom,
        )

    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_a_failed_guide_check_stores_as_not_clean(session):
    """The half that makes the isolation an improvement rather than a swallow: a
    check that ran and broke may not render like a check that passed."""
    from newspulse.models import Angle, Asset

    client = _client(session)
    angle = Angle(
        client_id=client.id,
        generated_at=dt.datetime.now(dt.UTC),
        subject="Verfügbarkeit",
        message="Zwei Absätze.",
        context="Laut Börsen-Zeitung verlagert sich die Verwahrung.",
        thesis="Verfügbarkeit ist ein Risikoparameter.",
        overclaim="Die Kette ist unzuverlässig.",
        article_ids=[],
    )
    session.add(angle)
    session.commit()

    fmt = assets.definition("statement")
    checked = assets.check(
        session,
        client,
        _checkable(),
        generate=lambda *a, **k: _review(),
        guide_generate=_boom,
    )
    stored = assets.store(
        session,
        fmt,
        client,
        angle,
        assets.AssetDraft(
            title="Alpha AG baut aus", body="Wir bauen aus.",
            brain_version=brain.version(session),
        ),
        checked,
    )

    assert isinstance(stored, Asset)
    assert stored.guide_review == guide.CHECK_FAILED
    assert stored.guide_reviewed_by == ""
    assert stored.guide_review_ok is False
    assert stored.check_state is CheckState.EINWAND


def test_a_mandate_without_a_guide_still_reads_as_checked_not_broken(session):
    """The other half of the distinction: having nothing to check against is a
    state of the mandate, not a malfunction, and it keeps its own sentence."""
    client = _client(session, comms_guide="")

    checked = assets.check(
        session,
        client,
        _checkable(),
        generate=lambda *a, **k: _review(),
    )

    assert checked.guide is None
    assert checked.guide_failed is False
    assert checked.guide_note == guide.NO_GUIDE
