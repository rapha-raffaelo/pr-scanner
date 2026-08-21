"""The guide check: does a finished letter break a rule this client wrote down?

The crosscheck weighs invention and overclaiming, which are judgements about the
world. A No-Go is not a judgement — the client wrote it down — so it is checked
in its own pass, with its own verdict, and quoted against the line of the guide
it breaks.

Driven with an injected ``generate``, like every other generator here, so the
whole path runs without touching a model: prompt build, parse, verdict, and the
worker that calls it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import guide, outreach
from newspulse.analyzer import ParseError
from newspulse.models import Angle, Base, Client, Outreach
from newspulse.schemas import MAX_BREACHES, PersonalMessage
from newspulse.web.app import create_app, get_db

#: A guide with one no-go a letter can visibly break, written the way the
#: distillation is told to write them: as a prohibition, and concrete.
_GUIDE = (
    "Positionierung: Wir machen Verwahrung nachweisbar sicher.\n"
    "Kernbotschaften: Sicherheit ist prüfbar · Regulierung ist kein Hindernis\n"
    "No-Gos: Keine Renditeversprechen. Keine Aussagen über Kursverläufe.\n"
    "Tonalität: nüchtern, in der dritten Person."
)

_OFFENDING = (
    "Sehr geehrter Herr Nelson,\n\n"
    "Unsere Verwahrung sichert Ihren Lesern acht Prozent Rendite im Jahr.\n\n"
    "Mit freundlichen Grüßen\nAlpha AG"
)

_OBEDIENT = (
    "Sehr geehrter Herr Nelson,\n\n"
    "Die Alpha AG legt ihre Verwahrkette quartalsweise offen und lässt sie "
    "prüfen.\n\n"
    "Mit freundlichen Grüßen\nAlpha AG"
)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as sess:
        yield sess


@pytest.fixture
def web(factory):
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _mandate(session, *, comms_guide: str = _GUIDE) -> tuple[Client, Angle]:
    client = Client(
        name="Alpha AG",
        industry="Neobroker",
        keywords=["Verwahrung"],
        comms_guide=comms_guide,
    )
    session.add(client)
    session.flush()
    angle = Angle(
        client_id=client.id,
        generated_at=dt.datetime.now(dt.UTC),
        subject="Verfügbarkeit als Risikoparameter",
        message="Zwei Absätze Positionierung.",
        context="Laut CoinDesk stand die Kette kurz still.",
        thesis="Die Liveness der Kette ist ein eigener Risikoparameter.",
        overclaim="Solana ist unzuverlässig, also wandert Liquidität ab.",
    )
    session.add(angle)
    session.commit()
    return client, angle


def _letter(body: str = _OBEDIENT) -> PersonalMessage:
    return PersonalMessage(
        subject="Verwahrung, quartalsweise geprüft",
        message=body,
        hook="Hat über Verwahrung geschrieben.",
    )


def _verdict(**over) -> str:
    payload = {"ok": True, "breaches": []}
    payload.update(over)
    return json.dumps(payload)


def _never_called(prompt: str, **kwargs) -> str:
    raise AssertionError("the model was called for a client with no guide")


# --- What the prompt is allowed to see -------------------------------------------


def test_the_stored_guide_reaches_the_prompt_verbatim(session):
    """Silent omission is the failure this whole feature guards against: a check
    that ran against nothing looks exactly like a check that found nothing."""
    client, _ = _mandate(session)
    seen: list[str] = []

    guide.check_guide(
        client, _letter(), generate=lambda p, **k: seen.append(p) or _verdict()
    )

    assert "Keine Renditeversprechen." in seen[0]
    assert "Keine Aussagen über Kursverläufe." in seen[0]
    assert "Tonalität: nüchtern, in der dritten Person." in seen[0]


def test_the_prompt_carries_the_letter_it_is_asked_about(session):
    client, _ = _mandate(session)
    seen: list[str] = []

    guide.check_guide(
        client,
        _letter(_OFFENDING),
        generate=lambda p, **k: seen.append(p) or _verdict(),
    )

    assert "Verwahrung, quartalsweise geprüft" in seen[0]  # the subject
    assert "acht Prozent Rendite im Jahr" in seen[0]       # the body


def test_the_prompt_carries_no_article_no_profile_and_no_angle(session):
    """A rule is read literally or not at all. Every extra fact in the prompt is
    another thing the model can reason its way around."""
    client, angle = _mandate(session)
    seen: list[str] = []

    guide.check_guide(
        client, _letter(), generate=lambda p, **k: seen.append(p) or _verdict()
    )

    assert "Neobroker" not in seen[0]                    # no profile
    assert "Themen:" not in seen[0]
    assert angle.thesis not in seen[0]                   # no angle
    assert angle.overclaim not in seen[0]
    assert "Verfügbarkeit als Risikoparameter" not in seen[0]
    assert "CoinDesk" not in seen[0]                     # no article


# --- The three states ------------------------------------------------------------


def test_a_broken_no_go_names_the_draft_sentence_and_the_guide_line(session):
    """Quoted twice, so the objection can be judged in a second rather than
    taken on faith."""
    client, _ = _mandate(session)

    verdict, model = guide.check_guide(
        client,
        _letter(_OFFENDING),
        generate=lambda *a, **k: _verdict(
            ok=False,
            breaches=[
                {
                    "draft": "Unsere Verwahrung sichert Ihren Lesern acht Prozent "
                             "Rendite im Jahr.",
                    "guide": "No-Gos: Keine Renditeversprechen.",
                }
            ],
        ),
    )

    assert verdict is not None
    assert verdict.ok is False
    assert verdict.breaches[0].draft.startswith("Unsere Verwahrung sichert")
    assert "Keine Renditeversprechen" in verdict.breaches[0].guide
    assert model  # and which model said so


def test_an_obedient_draft_is_ok_with_an_empty_breach_list(session):
    """Distinct from the no-guide case: something read this and had nothing to
    say, which is not the same as nothing having read it."""
    client, _ = _mandate(session)

    verdict, model = guide.check_guide(
        client, _letter(), generate=lambda *a, **k: _verdict()
    )

    assert verdict is not None
    assert verdict.ok is True
    assert verdict.breaches == []
    assert model
    assert (verdict, model) != guide.NOT_CHECKED


def test_a_client_without_a_guide_is_not_checked_and_no_model_is_called(session):
    """There is nothing to check against, and pretending otherwise would put a
    clean bill of health on a letter nothing read."""
    client, _ = _mandate(session, comms_guide="")

    result = guide.check_guide(client, _letter(), generate=_never_called)

    assert result == (None, "")
    assert result == guide.NOT_CHECKED


def test_a_guide_of_only_whitespace_counts_as_no_guide(session):
    """A saved-but-empty field is the same state as an unwritten one, and the
    model would otherwise be asked to check a text against a blank page."""
    client, _ = _mandate(session, comms_guide="   \n  ")

    result = guide.check_guide(client, _letter(), generate=_never_called)

    assert result == guide.NOT_CHECKED


# --- What the reply is trusted with ----------------------------------------------


def test_a_listed_breach_is_never_reported_as_ok(session):
    """``ok`` is recomputed, not believed. A reply that objects and calls itself
    fine would render as an approval over its own objection."""
    client, _ = _mandate(session)

    verdict, _ = guide.check_guide(
        client,
        _letter(_OFFENDING),
        generate=lambda *a, **k: _verdict(
            ok=True,
            breaches=[
                {"draft": "Acht Prozent Rendite.", "guide": "Keine Renditeversprechen."}
            ],
        ),
    )

    assert verdict.ok is False


def test_unparseable_output_raises_rather_than_reading_as_clean(session):
    client, _ = _mandate(session)

    with pytest.raises(ParseError):
        guide.check_guide(client, _letter(), generate=lambda *a, **k: "Klingt gut!")


def test_more_breaches_than_fit_are_cut_rather_than_thrown_away(session, caplog):
    """The draft that draws six objections is the last one allowed to come back
    as "not checked": a cap that voids the verdict makes the worst letter the
    quietest one."""
    client, _ = _mandate(session)
    reported = [
        {"draft": f"Satz {n}.", "guide": "No-Gos: Keine Renditeversprechen."}
        for n in range(MAX_BREACHES + 1)
    ]

    with caplog.at_level(logging.INFO, logger="newspulse.guide"):
        verdict, model = guide.check_guide(
            client,
            _letter(_OFFENDING),
            generate=lambda *a, **k: _verdict(ok=False, breaches=reported),
        )

    assert verdict is not None
    assert verdict.ok is False
    assert len(verdict.breaches) == MAX_BREACHES
    assert verdict.breaches[0].draft == "Satz 0."  # the gravest, as the prompt asks
    assert model
    assert "keeping the first" in caplog.text  # and the cut is never silent


def test_a_breach_with_an_empty_quote_is_refused_like_a_missing_one(session):
    """An empty side renders as an accusation with nothing under it, and it would
    still flip ``ok``. Discarding the verdict yields the honest not-checked state;
    dropping the breach could turn an objection into an approval."""
    client, _ = _mandate(session)

    with pytest.raises(ParseError):
        guide.check_guide(
            client,
            _letter(_OFFENDING),
            generate=lambda *a, **k: _verdict(
                ok=False,
                breaches=[{"draft": "", "guide": "No-Gos: Keine Renditeversprechen."}],
            ),
        )


def test_a_reply_that_misses_the_schema_raises(session):
    """A breach with only one side quoted is not checkable, and a half-parsed
    verdict is discarded rather than shown."""
    client, _ = _mandate(session)

    with pytest.raises(ParseError):
        guide.check_guide(
            client,
            _letter(),
            generate=lambda *a, **k: json.dumps(
                {"ok": False, "breaches": [{"draft": "Acht Prozent Rendite."}]}
            ),
        )


def test_without_a_second_model_the_check_refuses_rather_than_passes(
    session, monkeypatch
):
    """Same posture as the crosscheck: a check that silently did not run is worse
    than none, because the page would show a letter with no objections."""
    from newspulse import config

    client, _ = _mandate(session)
    monkeypatch.setattr(config, "review_configured", lambda: False)

    with pytest.raises(RuntimeError, match="Zweitmodell"):
        guide.check_guide(client, _letter())


# --- The pass inside the worker that writes the letter ---------------------------


def test_a_failed_check_returns_the_not_checked_state_and_logs_at_error(
    session, monkeypatch, caplog
):
    """The worker's own isolation: a letter that exists is worth more than a
    verdict on it, and a defect must not be silent."""
    from newspulse import gemini
    from newspulse.analyzer import BackendError
    from newspulse.web.routes import advisory

    client, _ = _mandate(session)

    def _explode(prompt: str, **kwargs) -> str:
        raise BackendError("Gemini unreachable")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini, "generate", _explode)

    with caplog.at_level(logging.ERROR, logger="newspulse.web.routes.advisory"):
        result = advisory._guide_check(client, _letter())

    assert result == guide.NOT_CHECKED
    assert "guide check failed" in caplog.text


def test_no_second_model_is_a_warning_rather_than_an_error(
    session, monkeypatch, caplog
):
    """Nothing failed. A key is not set, on every letter this deployment writes,
    and the crosscheck already puts that sentence on the page — at ERROR it would
    be noise over a defect that is not there."""
    from newspulse import config
    from newspulse.web.routes import advisory

    client, _ = _mandate(session)
    monkeypatch.setattr(config, "review_configured", lambda: False)

    with caplog.at_level(logging.DEBUG, logger="newspulse.web.routes.advisory"):
        result = advisory._guide_check(client, _letter())

    assert result == guide.NOT_CHECKED
    levels = [
        r.levelno for r in caplog.records if r.name == "newspulse.web.routes.advisory"
    ]
    assert levels == [logging.WARNING]
    assert "guide check failed" not in caplog.text


def test_the_letter_is_checked_in_the_form_it_will_be_stored_in(session, monkeypatch):
    """A breach quotes the draft's sentence so it can be found in the letter
    beside it, and ``outreach.store`` writes the dash-free text. Checking the
    draft before that step would quote a sentence that is nowhere on the page."""
    from newspulse import gemini
    from newspulse.web.routes import advisory

    client, _ = _mandate(session)
    seen: list[str] = []

    def _capture(prompt: str, **kwargs) -> str:
        seen.append(prompt)
        return _verdict()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini, "generate", _capture)

    advisory._guide_check(client, _letter("Die Verwahrkette — geprüft — steht offen."))

    assert "Die Verwahrkette — geprüft — steht offen." not in seen[0]
    assert "Die Verwahrkette, geprüft, steht offen." in seen[0]


def test_a_failing_guide_check_leaves_the_crosscheck_and_the_letter_intact(
    factory, web, monkeypatch, caplog, no_background_message
):
    """Two checks, two failure modes. The letter is stored and readable either
    way, and the crosscheck's verdict survives the guide check's failure."""
    from newspulse import gemini
    from newspulse.analyzer import BackendError
    from newspulse.web.routes import advisory

    with factory() as setup:
        client, angle = _mandate(setup)
        client_id, angle_id = client.id, angle.id

    @contextlib.contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    prompts: list[str] = []

    def _fake_generate(prompt: str, **kwargs) -> str:
        # Routed on the guide's own text rather than on a heading: this test's
        # guide reaches exactly one of the two prompts (the drafting call is
        # patched out below, and the crosscheck template has no guide slot), while
        # a heading is one word away from the one guide.for_prompt() writes and
        # would send the failure to the wrong check without saying so.
        prompts.append(prompt)
        if "Keine Renditeversprechen." in prompt:
            raise BackendError("Gemini unreachable")
        return json.dumps({"send": True, "concerns": [], "fix": ""})

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(advisory, "get_session", _session)
    monkeypatch.setattr(gemini, "generate", _fake_generate)
    monkeypatch.setattr(
        outreach, "draft", lambda *a, **k: _letter(_OFFENDING)
    )

    advisory._writing.acquire()
    with caplog.at_level(logging.ERROR, logger="newspulse.web.routes.advisory"):
        no_background_message(client_id, angle_id, "Jason Nelson", "Börsen-Zeitung")
    advisory._last_message_error.pop(client_id, None)

    assert len(prompts) == 2, "both checks were asked, and asked separately"
    with factory() as check:
        stored = check.scalars(select(Outreach)).one()
        assert "acht Prozent Rendite" in stored.message
        assert stored.reviewed_by, "the crosscheck's verdict survived"
    assert "guide check failed" in caplog.text
    assert "acht Prozent Rendite" in web.get(f"/client/{client_id}/advice").text


def test_the_worker_reports_the_three_guide_states_distinctly():
    """None of them may read as either of the others — the whole point of a
    separate not-checked state."""
    from newspulse.schemas import GuideBreach, GuideVerdict
    from newspulse.web.routes import advisory

    clean = advisory._guide_state(GuideVerdict(ok=True), "gemini-2.5-flash")
    broken = advisory._guide_state(
        GuideVerdict(ok=False, breaches=[GuideBreach(draft="a", guide="b")]),
        "gemini-2.5-flash",
    )
    unchecked = advisory._guide_state(None, "")

    assert clean != broken != unchecked != clean
    assert "not checked" in unchecked
    assert "1 breach" in broken
