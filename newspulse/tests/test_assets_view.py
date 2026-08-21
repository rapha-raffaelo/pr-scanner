"""Writing, editing and releasing a text (FMT-03).

Two levels, deliberately. The acts themselves — write-check-store, edit, re-check,
release — are driven straight against :mod:`newspulse.assets` with an injected
``invoke`` and an injected ``generate``, so a whole generation runs in
milliseconds and without a subprocess. The surface and its routes go through
FastAPI's TestClient against a seeded in-memory database, which is where the
locking, the refusals and the per-format state actually live.

The tests worth reading twice are the ones about an edited text. A verdict on
screen belongs to the words that were checked, and the failure this story exists
to prevent is a green "keine Einwände" standing over a paragraph a human rewrote
afterwards.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import analyzer, assets, config, i18n, profile
from newspulse.models import (
    Angle,
    Article,
    Asset,
    AssetKind,
    Base,
    CheckState,
    Client,
)
from newspulse.schemas import AssetDraft
from newspulse.web.app import create_app, get_db
from newspulse.web.routes import assets_view

_SPEAKER = "Alexandra Prot, Geschäftsführerin"
_HEADLINE = "Verwahrung im Wandel: Banken bauen eigene Depots"
_GUIDE = 'No-Go: das Wort günstig. Register: nüchtern, nie "wir freuen uns".'


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


@pytest.fixture
def web(factory):
    """A TestClient whose routes read the fixture database."""
    app = create_app()

    def _override():
        sess = factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_module_state():
    """The notes and the progress record are process-wide, like the impulse's.

    One test's refusal showing up in the next one's page is the kind of failure
    that takes an afternoon to find, so both are emptied around every test.
    """
    assets_view._notes.clear()
    assets_view._progress.clear()
    yield
    assets_view._notes.clear()
    assets_view._progress.clear()
    assert not assets_view._generating.locked(), "a test left the write lock held"


class _Worker:
    """What the routes handed to the background worker.

    Carries the real worker as well, so the two tests that are *about* it call it
    deliberately rather than reaching around the patch this installed.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.done = threading.Event()
        self.real_write = assets_view._run_write

    def record(self, *args) -> None:
        self.calls.append(args)
        # The real worker releases in its ``finally``; the routes rely on that.
        assets_view._generating.release()
        self.done.set()

    def wait(self, timeout: float = 2.0) -> list[tuple]:
        """The calls, once the thread the route started has run."""
        self.done.wait(timeout)
        return self.calls


@pytest.fixture(autouse=True)
def worker(monkeypatch):
    """Stand in for the worker, which shells out to a model per format.

    The real one is exercised deliberately by the two tests that are about it;
    they patch the session and the CLI boundary rather than reaching around this.
    """
    stub = _Worker()
    monkeypatch.setattr(assets_view, "_run_write", stub.record)
    monkeypatch.setattr(assets_view, "_run_recheck", stub.record)
    return stub


def _mandate(
    session, *, facts: dict[str, str] | None = None, comms_guide: str = _GUIDE
) -> tuple[Client, Angle]:
    """A mandate whose profile is filled and whose impulse rests on two stories."""
    client = Client(
        name="Alpha AG",
        industry="Neobroker",
        keywords=["Verwahrung"],
        comms_guide=comms_guide,
    )
    session.add(client)
    session.flush()
    for key, value in (facts if facts is not None else _PROFILE).items():
        profile.save(session, client, key, value)

    article_ids = []
    for i in range(2):
        article = Article(
            title=f"{_HEADLINE} ({i})",
            url=f"https://ex.de/field-{i}",
            source="Börsen-Zeitung",
            published_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
            fetched_at=dt.datetime.now(dt.UTC),
            summary_text="Die Verwahrung verlagert sich zurück zu den Banken.",
            language="de",
            title_hash=f"field{i:05d}",
        )
        session.add(article)
        session.flush()
        article_ids.append(article.id)

    angle = Angle(
        client_id=client.id,
        generated_at=dt.datetime.now(dt.UTC),
        subject="Verfügbarkeit als Risikoparameter",
        message="Zwei Absätze Positionierung.",
        context="Laut Börsen-Zeitung verlagert sich die Verwahrung.",
        thesis="Die Liveness der Kette ist ein eigener Risikoparameter.",
        overclaim="Solana ist unzuverlässig, also wandert Liquidität ab.",
        article_ids=article_ids,
    )
    session.add(angle)
    session.commit()
    return client, angle


_PROFILE = {
    "ceo": _SPEAKER,
    "geschaeftsfeld": "Verwahrung digitaler Vermögenswerte für Banken.",
    "sitz": "Berlin",
}


def _statement_reply(**over) -> str:
    """One model reply carrying the statement's declared structure."""
    payload = {
        "title": "",
        "body": (
            "Die Verwahrung wandert zu den Banken. Wir halten diese Entwicklung "
            "für richtig. Verfügbarkeit ist dabei ein eigener Risikoparameter, "
            f"kein Detail.\n\n{_SPEAKER}"
        ),
        "speaker": _SPEAKER,
    }
    payload.update(over)
    return json.dumps(payload)


def _review(**over) -> str:
    payload = {"send": True, "concerns": [], "fix": ""}
    payload.update(over)
    return json.dumps(payload)


def _guide_verdict(**over) -> str:
    payload = {"ok": True, "breaches": []}
    payload.update(over)
    return json.dumps(payload)


def _produce(session, client, angle, kind=AssetKind.STATEMENT, **over) -> Asset:
    """One text, written and read the way the worker writes and reads it."""
    return assets.produce(
        session,
        assets.definition(kind),
        client,
        angle,
        invoke=lambda *a, **k: _statement_reply(**over),
        generate=lambda *a, **k: _review(),
        guide_generate=lambda *a, **k: _guide_verdict(),
    )


# --- The whole act: write, check, store ----------------------------------------


def test_producing_a_text_writes_checks_and_stores_it_in_one_act(session):
    client, angle = _mandate(session)

    stored = _produce(session, client, angle)

    assert stored.body.startswith("Die Verwahrung wandert")
    assert stored.speaker == _SPEAKER
    assert stored.check_state is CheckState.GEPRUEFT
    assert stored.edited_at is None
    assert stored.released_at is None


def test_a_text_whose_check_could_not_run_is_stored_as_unchecked(session):
    """The model call is spent and the draft is good: losing it would be worse.

    What must not happen is the text rendering as though something had read it.
    """
    client, angle = _mandate(session)
    said: list[str] = []

    def _no_second_model(*args, **kwargs):
        raise RuntimeError("kein Zweitmodell hinterlegt")

    stored = assets.produce(
        session,
        assets.definition(AssetKind.STATEMENT),
        client,
        angle,
        invoke=lambda *a, **k: _statement_reply(),
        generate=_no_second_model,
        note=said.append,
    )

    assert stored.body
    assert stored.check_state is CheckState.UNGEPRUEFT
    assert any("gegengelesen" in reason for reason in said)


def test_the_second_models_own_failure_class_does_not_lose_the_text(session):
    """The realistic version of the test above, and the one that used to escape.

    Every way :func:`newspulse.gemini.generate` actually fails — host unreachable,
    timeout, HTTP error, empty completion — raises ``BackendError``, which is an
    ``AnalyzerError`` and *not* a ``RuntimeError``. A handler that named only
    ``ParseError``/``RuntimeError``/``OSError`` therefore caught the case nobody
    hits and let the common one throw the paid-for draft away.
    """
    client, angle = _mandate(session)
    said: list[str] = []

    stored = assets.produce(
        session,
        assets.definition(AssetKind.STATEMENT),
        client,
        angle,
        invoke=lambda *a, **k: _statement_reply(),
        generate=_raises(analyzer.BackendError("Gemini unreachable: timed out")),
        note=said.append,
    )

    assert stored.body.startswith("Die Verwahrung wandert")
    assert stored.check_state is CheckState.UNGEPRUEFT
    assert any("gegengelesen" in reason for reason in said)


def test_a_guide_check_that_dies_on_the_backend_keeps_the_crosscheck(session):
    """The crosscheck has been paid for by then; a second failure must not spend it.

    Same blind spot, one layer in: ``guide.check_guide`` reaches the same provider
    and raises the same class.
    """
    client, angle = _mandate(session)

    stored = assets.produce(
        session,
        assets.definition(AssetKind.STATEMENT),
        client,
        angle,
        invoke=lambda *a, **k: _statement_reply(),
        generate=lambda *a, **k: _review(),
        guide_generate=_raises(analyzer.BackendError("Gemini HTTP 503")),
    )

    assert stored.reviewed_by, "the verdict that did arrive is kept"
    assert stored.review_ok
    assert not stored.guide_review_ok, "and the one that did not never reads as clean"
    assert stored.check_state is CheckState.EINWAND


def test_a_refused_format_leaves_the_text_that_already_stood(session):
    """The acceptance in one test: a failed run must not cost the last good text."""
    client, angle = _mandate(session)
    standing = _produce(session, client, angle)
    profile.save(session, client, "ceo", "")

    with pytest.raises(assets.RequirementsMissing):
        _produce(session, client, angle, body="Ein neuer Text.")

    session.expire_all()
    again = session.get(Asset, standing.id)
    assert again.body.startswith("Die Verwahrung wandert"), "the old text stands"
    assert len(assets.for_angle(session, angle.id)) == 1


# --- The state one text is in --------------------------------------------------


def test_an_unwritten_format_is_not_a_draft(session):
    assert assets.state_of(None) is assets.AssetState.UNGESCHRIEBEN


def test_a_checked_text_is_told_apart_from_one_nothing_read(session):
    client, angle = _mandate(session)

    checked = _produce(session, client, angle)
    assert assets.state_of(checked) is assets.AssetState.GEPRUEFT

    unread = assets.store(
        session,
        assets.definition(AssetKind.TALKING_POINTS),
        client,
        angle,
        assets.write(
            session,
            assets.definition(AssetKind.TALKING_POINTS),
            client,
            angle,
            invoke=lambda *a, **k: _talking_points_reply(),
        ),
    )
    assert assets.state_of(unread) is assets.AssetState.ENTWURF


def _talking_points_reply() -> str:
    return json.dumps(
        {
            "title": "Talking Points",
            "body": (
                "1. Verfügbarkeit ist ein eigener Risikoparameter.\n"
                "Brücke: Zurück zur These, dass Liveness ein Risiko ist.\n"
                "2. Wer verwahrt, trägt das Risiko.\n"
                "Brücke: Zurück zur These, dass Liveness ein Risiko ist.\n\n"
                "Nicht sagen\nDass die Kette unzuverlässig sei."
            ),
            "speaker": "",
        }
    )


def test_a_released_text_is_shown_as_released(session):
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)

    assets.release(session, stored, by="lucas")

    assert assets.state_of(stored) is assets.AssetState.FREIGEGEBEN


# --- Editing -------------------------------------------------------------------


def test_an_edit_is_stored_and_marked_as_a_humans_with_a_timestamp(session):
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)

    before = dt.datetime.now(dt.UTC)
    assets.edit(session, stored, title="", body="Der Satz, den ich sagen will.")

    assert stored.body == "Der Satz, den ich sagen will."
    assert stored.edited_at is not None and stored.edited_at >= before


def test_an_edit_drops_the_verdicts_that_were_given_on_other_words(session):
    """A text a human changed after the check was cleared is a text nothing read."""
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)
    assert stored.reviewed_by, "the fixture is only useful if it was checked"

    assets.edit(session, stored, title="", body="Etwas ganz anderes.")

    assert stored.reviewed_by == ""
    assert stored.guide_reviewed_by == ""
    assert stored.check_state is CheckState.UNGEPRUEFT
    assert assets.needs_recheck(stored)


def test_an_edited_text_cannot_be_released_until_it_is_read_again(session):
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)
    assets.edit(session, stored, title="", body="Etwas ganz anderes.")

    assert not assets.releasable(stored)
    with pytest.raises(assets.Refused, match="Zweitmodell"):
        assets.release(session, stored, by="lucas")


def test_a_recheck_reads_the_edited_text_and_clears_it_for_release(session):
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)
    assets.edit(session, stored, title="", body="Der Satz, den ich sagen will.")
    seen: list[str] = []

    assets.recheck(
        session,
        client,
        stored,
        generate=lambda prompt, **k: seen.append(prompt) or _review(),
        guide_generate=lambda prompt, **k: seen.append(prompt) or _guide_verdict(),
    )

    assert len(seen) == 2, "both checks ran again"
    assert all("Der Satz, den ich sagen will." in prompt for prompt in seen), (
        "the checkers read the edited text, not the one the model wrote"
    )
    assert not assets.needs_recheck(stored)
    assert assets.releasable(stored)


def test_a_recheck_that_could_not_run_says_so_instead_of_locking_the_text(session):
    """The deadlock this used to be, and the asymmetry underneath it.

    ``needs_recheck`` was "edited, and unchecked". A re-check that could not run
    left both halves true forever, so on an installation with no second model a
    consultant could write a text and edit it and never release it — while an
    unedited draft whose check had failed the same way stayed releasable. The tool
    blocked the human exactly where a human had taken responsibility for the words
    and waved through the one only a model had seen.

    So the attempt is the record. It is not a clean one: no reviewer is named, the
    state stays UNGEPRUEFT, the verdict is not ok, and the sentence on the row
    says whoever releases now releases unread.
    """
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)
    assets.edit(session, stored, title="", body="Der Satz, den ich sagen will.")

    assets.recheck(
        session,
        client,
        stored,
        generate=_raises(analyzer.BackendError("Gemini unreachable: timed out")),
    )

    assert assets.CHECK_UNAVAILABLE in stored.review
    assert "Gemini unreachable" in stored.review, "and why, for the person fixing it"
    assert stored.reviewed_by == "", "nothing read it, so nobody is named"
    assert not stored.review_ok, "it draws with the warning dot, never the clean one"
    assert stored.check_state is CheckState.UNGEPRUEFT
    assert stored.edited_at is not None, "the human edit is still on the record"
    assert not assets.needs_recheck(stored), "the check has run, and could not"
    assert assets.releasable(stored), "so the decision is the consultant's"


def test_a_recheck_that_could_not_run_is_still_offered_a_second_attempt(factory, web):
    """A text nothing has read keeps the button that would have it read."""
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        assets.edit(session, stored, title="", body="Der Satz, den ich sagen will.")
        assets.recheck(
            session, client, stored, generate=_raises(RuntimeError("kein Zweitmodell"))
        )
        client_id, asset_id = client.id, stored.id

    body = web.get(f"/client/{client_id}/advice").text

    assert f"/asset/{asset_id}/recheck" in body, "try again"
    assert f"/asset/{asset_id}/release" in body, "or release it, having been told"
    assert assets.CHECK_UNAVAILABLE in body


def test_a_draft_nothing_read_can_be_sent_back_without_being_rewritten(factory, web):
    """The re-check was gated on ``edited_at``, which is not what makes a text unread.

    A draft written while the second model was down is unread through nobody's
    fault, and the only way back to a checked state was "Neu schreiben" — a second
    full model call that throws away a text there was nothing wrong with.
    """
    with factory() as session:
        client, angle = _mandate(session)
        stored = assets.produce(
            session,
            assets.definition(AssetKind.STATEMENT),
            client,
            angle,
            invoke=lambda *a, **k: _statement_reply(),
            generate=_raises(analyzer.BackendError("Gemini unreachable")),
        )
        client_id, asset_id = client.id, stored.id
        assert stored.edited_at is None

    body = web.get(f"/client/{client_id}/advice").text

    assert f"/asset/{asset_id}/recheck" in body


def test_the_house_style_reaches_an_edit_as_well(session):
    """The dash rule is about what leaves the building, not about who typed it."""
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)

    assets.edit(session, stored, title="", body="Ein Satz — mit Gedankenstrich.")

    assert "—" not in stored.body


def test_a_released_text_takes_no_edit(session):
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)
    assets.release(session, stored, by="lucas")

    with pytest.raises(assets.Refused, match="freigegeben"):
        assets.edit(session, stored, title="", body="Doch noch etwas anderes.")

    assert stored.body.startswith("Die Verwahrung wandert")


# --- Releasing -----------------------------------------------------------------


def test_a_release_records_who_and_when(session):
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)

    before = dt.datetime.now(dt.UTC)
    assets.release(session, stored, by="lucas")

    assert stored.released is True
    assert stored.released_by == "lucas"
    assert stored.released_at >= before


def test_releasing_twice_keeps_the_first_release(session):
    """The second click on a button that had not repainted yet is not an error."""
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)
    assets.release(session, stored, by="lucas")
    first = stored.released_at

    assets.release(session, stored, by="somebody else")

    assert stored.released_at == first
    assert stored.released_by == "lucas"


def test_a_release_that_lands_during_a_rewrite_still_stands_on_the_strip(session):
    """The one way the immutability rule could be lost without anything refusing.

    A release that lands while the worker for that same format is inside its model
    call leaves ``store`` with no replaceable draft, so the worker's text is
    inserted *beside* the released row and is the newer of the two. Picked on
    recency, the released text dropped off the strip, the package counted nothing
    as released, and every control that refuses on a released row went live again
    over the record of what actually went out.
    """
    client, angle = _mandate(session)
    stored = _produce(session, client, angle)
    assets.release(session, stored, by="lucas")

    _produce(session, client, angle)  # the worker's store() lands after it

    standing = assets.current(session, [angle.id])[angle.id][AssetKind.STATEMENT]
    assert standing.id == stored.id, "the released record was replaced on the surface"
    assert standing.released is True


# --- The surface ---------------------------------------------------------------


def test_the_strip_shows_every_format_and_the_state_it_is_in(factory, web):
    """"Welche sind geschrieben, welche freigegeben, welche gibt es noch nicht." """
    with factory() as session:
        client, angle = _mandate(session)
        _produce(session, client, angle)
        client_id = client.id

    body = web.get(f"/client/{client_id}/advice").text

    for fmt in assets.FORMATS:
        assert f'data-pane="pane-{angle.id}-{fmt.key}"' in body, fmt.key
    assert f'data-pane="pane-{angle.id}-anschreiben"' in body, "the letter is a tab too"
    assert "fmt-dot--geprueft" in body, "the written statement is checked"
    assert "fmt-dot--ungeschrieben" in body, "the six others do not exist yet"
    assert "1 von 7 Formaten geschrieben" in " ".join(body.split())


def test_an_impulse_with_nothing_written_gets_the_mocks_invitation(factory, web):
    """DEC-1's empty package: a thesis and no text is an invitation, not a void.

    Seven empty tabs answer a question nobody asked yet. The strip stays below it
    so a single format can still be picked.
    """
    with factory() as session:
        client, angle = _mandate(session)
        client_id, angle_id = client.id, angle.id

    body = " ".join(web.get(f"/client/{client_id}/advice").text.split())

    assert "Zu diesem Anlass gibt es eine These und noch keinen Text." in body
    assert "Paket schreiben" in body
    assert "noch nichts geschrieben" in body
    assert f'data-pane="pane-{angle_id}-qa"' in body, "the strip is still there"


def test_a_package_with_a_text_gets_the_head_and_not_the_invitation(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        _produce(session, client, angle)
        client_id = client.id

    body = " ".join(web.get(f"/client/{client_id}/advice").text.split())

    assert "Paket schreiben" not in body
    assert "1 von 7 Formaten geschrieben" in body


def test_a_reader_on_another_impulse_is_told_why_the_buttons_are_out(factory, web):
    """The lock is process-wide and the notice is not.

    Without this line the reader sees dead buttons and no cause, which reads as a
    broken page rather than as a queue of one.
    """
    with factory() as session:
        client, angle = _mandate(session)
        client_id, angle_id = client.id, angle.id
    assets_view._generating.acquire()
    try:
        # Running on some *other* impulse, so this one's notice stays down.
        assets_view._progress[client_id] = assets_view.Progress(
            angle_id=angle_id + 999, label="Pressemitteilung", done=0, total=1
        )
        body = " ".join(web.get(f"/client/{client_id}/advice").text.split())
    finally:
        assets_view._generating.release()

    assert "Es wird gerade ein anderer Text geschrieben" in body
    assert "Wird geschrieben: Pressemitteilung" not in body


def test_a_format_that_cannot_be_written_yet_says_which_field_is_missing(
    factory, web
):
    """DEC-2 on screen: no text, and the refusal names the field to go and fill."""
    with factory() as session:
        client, angle = _mandate(session, facts={})
        client_id = client.id

    body = " ".join(web.get(f"/client/{client_id}/advice").text.split())

    assert "Noch nicht schreibbar." in body
    assert "Wer spricht" in body or "Es fehlt" in body
    assert f'href="/client/{client_id}/profil"' in body


def test_the_release_control_stays_out_of_reach_until_the_edit_is_read(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        assets.edit(session, stored, title="", body="Etwas ganz anderes.")
        client_id, asset_id = client.id, stored.id

    body = web.get(f"/client/{client_id}/advice").text

    assert f"/asset/{asset_id}/release" not in body, "no release button"
    assert f"/asset/{asset_id}/recheck" in body, "the way back to one is offered"
    assert assets.UNREAD_SINCE_EDIT in body


def test_a_released_text_offers_neither_a_rewrite_nor_an_edit(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        assets.release(session, stored, by="lucas")
        client_id, asset_id = client.id, stored.id

    body = web.get(f"/client/{client_id}/advice").text

    assert f"/asset/{asset_id}/rewrite" not in body
    assert f"/asset/{asset_id}/edit" not in body
    assert "Freigegeben" in body


# --- The routes ----------------------------------------------------------------


def test_one_format_is_written_on_its_own(factory, web, worker):
    with factory() as session:
        client, angle = _mandate(session)
        client_id, angle_id = client.id, angle.id

    resp = web.post(
        f"/client/{client_id}/impulse/{angle_id}/assets",
        data={"kind": "qa"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/client/{client_id}/advice#impulse-{angle_id}"
    assert worker.wait() == [(client_id, angle_id, ("qa",))]
    # Recorded by the route, not by the worker the fixture replaced: the page the
    # redirect lands on has to carry the notice, or nothing ever asks again.
    assert assets_view.progress_for(client_id).label == "Q&A"


def test_writing_the_missing_ones_skips_what_is_already_there(factory, web, worker):
    with factory() as session:
        client, angle = _mandate(session)
        _produce(session, client, angle)
        client_id, angle_id = client.id, angle.id

    web.post(f"/client/{client_id}/impulse/{angle_id}/assets", data={"kind": ""})

    (_, _, kinds), = worker.wait()
    assert AssetKind.STATEMENT.value not in kinds, "the statement already exists"
    assert AssetKind.TALKING_POINTS.value in kinds


def test_a_format_nobody_defined_is_not_a_route_to_write_one(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        client_id, angle_id = client.id, angle.id

    resp = web.post(
        f"/client/{client_id}/impulse/{angle_id}/assets", data={"kind": "podcast"}
    )

    assert resp.status_code == 404


def test_a_second_request_while_one_runs_is_refused_rather_than_queued(
    factory, web, worker
):
    with factory() as session:
        client, angle = _mandate(session)
        client_id, angle_id = client.id, angle.id

    assets_view._generating.acquire()
    try:
        web.post(
            f"/client/{client_id}/impulse/{angle_id}/assets", data={"kind": "statement"}
        )
        assert worker.calls == [], "nothing was queued behind the running one"
        body = " ".join(web.get(f"/client/{client_id}/advice").text.split())
    finally:
        assets_view._generating.release()

    assert "nicht angenommen" in body


def test_rewriting_a_draft_writes_that_format_again(factory, web, worker):
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        client_id, angle_id, asset_id = client.id, angle.id, stored.id

    web.post(
        f"/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/rewrite"
    )

    assert worker.wait() == [(client_id, angle_id, (AssetKind.STATEMENT.value,))]


def test_a_row_whose_format_the_registry_forgot_does_not_wedge_the_lock(factory, web):
    """``Asset.kind`` is a plain string column, so a row can outlive its definition.

    ``definition`` is loud on a kind it does not know, and it used to be called
    between the acquire and the thread that releases: one POST on such a row held
    the process-wide write lock for good, and every write, re-write and re-check
    for every mandate was refused with the "busy" sentence until a restart.
    """
    with factory() as session:
        client, angle = _mandate(session)
        row = Asset(
            client_id=client.id, angle_id=angle.id, kind="pressemappe", body="Text."
        )
        session.add(row)
        session.commit()
        client_id, angle_id, asset_id = client.id, angle.id, row.id

    resp = web.post(f"/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/rewrite")

    assert resp.status_code == 404
    assert not assets_view._generating.locked(), "the write lock is held forever"


def test_rewriting_a_released_text_is_refused(factory, web, worker):
    """The letter's immutability rule: the released text is the record."""
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        assets.release(session, stored, by="lucas")
        client_id, angle_id, asset_id = client.id, angle.id, stored.id

    web.post(f"/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/rewrite")
    body = " ".join(web.get(f"/client/{client_id}/advice").text.split())

    assert worker.calls == []
    assert "Freigegebene Texte werden weder geändert noch ersetzt" in body


def test_asking_for_a_second_reading_hands_the_text_to_the_worker(
    factory, web, worker
):
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        assets.edit(session, stored, title="", body="Etwas ganz anderes.")
        client_id, angle_id, asset_id = client.id, angle.id, stored.id

    web.post(f"/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/recheck")

    # The impulse travels with the call rather than being read off the row: it is
    # where a failure has to be written, and the row is what may be unreachable.
    assert worker.wait() == [(client_id, angle_id, asset_id)]


def test_editing_through_the_route_stores_the_text_and_marks_it(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        client_id, angle_id, asset_id = client.id, angle.id, stored.id

    resp = web.post(
        f"/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/edit",
        data={"title": "", "body": "Der Satz, den ich sagen will."},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    with factory() as session:
        again = session.get(Asset, asset_id)
        assert again.body == "Der Satz, den ich sagen will."
        assert again.edited_at is not None
        assert again.check_state is CheckState.UNGEPRUEFT


def test_an_empty_edit_is_not_allowed_to_destroy_the_text(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        client_id, angle_id, asset_id = client.id, angle.id, stored.id

    web.post(
        f"/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/edit",
        data={"title": "", "body": "   "},
    )

    with factory() as session:
        assert session.get(Asset, asset_id).body.startswith("Die Verwahrung wandert")


def test_releasing_through_the_route_records_the_release(factory, web, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        client_id, angle_id, asset_id = client.id, angle.id, stored.id

    web.post(f"/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/release")

    with factory() as session:
        again = session.get(Asset, asset_id)
        assert again.released is True
        assert again.released_by == "lucas"


def test_the_release_control_is_out_while_a_text_is_being_written(factory, web):
    """The other half of the rule above, on the page rather than in the store.

    Every other control on this strip goes out while the process-wide lock is
    held; this one did not, so the only click that could overwrite a released row
    was the only one still live.
    """
    with factory() as session:
        client, angle = _mandate(session)
        _produce(session, client, angle)
        client_id = client.id

    assets_view._generating.acquire()
    try:
        page = web.get(f"/client/{client_id}/advice").text
    finally:
        assets_view._generating.release()

    forms = re.findall(r'/release"[^<]*<button[^>]*>', page, re.S)
    assert forms, "the release control is not on the page at all"
    assert all("disabled" in form for form in forms), forms


def test_an_edit_posted_against_a_text_that_was_rewritten_is_refused(factory, web):
    """``store`` reuses the row, so the stale form posts to the right id.

    An edit panel opened before a re-write would put the words that reader was
    looking at back over the ones the model has since written, and stamp them
    ``edited_at`` — a human's approval of a text no human has seen.
    """
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        opened_on = assets.version(stored)
        client_id, angle_id, asset_id = client.id, angle.id, stored.id
        # The re-write the reader never saw: same row, different text.
        _produce(
            session,
            client,
            angle,
            body=(
                "Der zweite Anlauf steht. Die Verwahrung wandert zu den Banken. "
                "Verfügbarkeit bleibt ein eigener Risikoparameter."
                f"\n\n{_SPEAKER}"
            ),
        )

    web.post(
        f"/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/edit",
        data={"title": "", "body": "Was ich vorhin gelesen habe.", "seen": opened_on},
    )

    with factory() as session:
        again = session.get(Asset, asset_id)
        assert again.body.startswith("Der zweite Anlauf steht")
        assert again.edited_at is None, "and it is not stamped as a human's"
    body = " ".join(web.get(f"/client/{client_id}/advice").text.split())
    assert "nicht gespeichert" in body


def test_a_release_posted_after_an_edit_is_refused(factory, web):
    """The control is gone from the page, and the rule is enforced anyway: a form
    can be posted from a page that was rendered before the edit."""
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        assets.edit(session, stored, title="", body="Etwas ganz anderes.")
        client_id, angle_id, asset_id = client.id, angle.id, stored.id

    web.post(f"/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/release")

    with factory() as session:
        assert session.get(Asset, asset_id).released is False
    body = " ".join(web.get(f"/client/{client_id}/advice").text.split())
    assert "noch einmal lesen" in body


def test_a_text_belonging_to_another_impulse_is_refused(factory, web):
    """Both ids come off the URL; without the pair check one mandate's page could
    release another's press release."""
    with factory() as session:
        client, angle = _mandate(session)
        stored = _produce(session, client, angle)
        other = Angle(
            client_id=client.id,
            generated_at=dt.datetime.now(dt.UTC),
            subject="Ein anderer Anlass",
            message="Text",
            context="ctx",
            thesis="Eine andere These.",
            overclaim="",
            article_ids=[],
        )
        session.add(other)
        session.commit()
        client_id, other_id, asset_id = client.id, other.id, stored.id

    resp = web.post(
        f"/client/{client_id}/impulse/{other_id}/asset/{asset_id}/release"
    )

    assert resp.status_code == 404


def test_the_page_reports_which_format_is_being_written(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        client_id, angle_id = client.id, angle.id
    assets_view._progress[client_id] = assets_view.Progress(
        angle_id=angle_id, label="Pressemitteilung", done=1, total=6
    )

    body = " ".join(web.get(f"/client/{client_id}/advice").text.split())

    assert "Wird geschrieben: Pressemitteilung (2 von 6)" in body
    # Fetches itself until it is done, and replaces this package only: every
    # other impulse's tab strip and open edit form is left alone.
    assert f'hx-trigger="every 3s [!npEditing({angle_id})]"' in body
    assert f'hx-target="#pack-{angle_id}"' in body


def test_the_poller_holds_off_while_somebody_is_editing_that_package(factory, web):
    """The swap would take the textarea and everything typed into it.

    Editing in place is why this surface exists rather than a card with a copy
    button, so the refresh gives way to it: the trigger asks whether an edit panel
    on *this* package is open, and the notice keeps saying what is happening.
    """
    with factory() as session:
        client, angle = _mandate(session)
        client_id, angle_id = client.id, angle.id
    assets_view._progress[client_id] = assets_view.Progress(
        angle_id=angle_id, label="Pressemitteilung", done=0, total=1
    )

    body = web.get(f"/client/{client_id}/advice").text

    assert "window.npEditing = function" in body, "the trigger's own condition"
    assert "details.fmtedit[open]" in body


# --- The worker itself ---------------------------------------------------------


def test_the_worker_reports_a_failed_format_and_keeps_the_last_good_text(
    factory, session, monkeypatch, worker
):
    """The one path the routes cannot show: the model call itself failing.

    Driven against the real worker with the subprocess boundary stubbed, because
    what is being pinned is that a failure inside it neither loses the stored text
    nor leaves the lock held for the rest of the process.
    """
    client, angle = _mandate(session)
    standing = _produce(session, client, angle)
    client_id, angle_id, kind = client.id, angle.id, standing.kind

    monkeypatch.setattr(assets_view, "get_session", lambda: _sessions(factory))
    monkeypatch.setattr(
        analyzer,
        "invoke_claude_cli",
        _raises(analyzer.BackendError("claude CLI not found on PATH")),
    )

    assets_view._generating.acquire()
    worker.real_write(client_id, angle_id, (kind,))

    assert not assets_view._generating.locked(), "the lock is released either way"
    assert not assets_view._progress, "and the notice does not stay up"
    assert "nicht geschrieben" in assets_view._notes[(angle_id, kind)]
    session.expire_all()
    assert session.get(Asset, standing.id).body.startswith("Die Verwahrung wandert")


def test_the_worker_writes_every_format_it_was_given(
    factory, session, monkeypatch, worker
):
    """One bad format does not take the rest of the package down with it."""
    client, angle = _mandate(session, facts={"ceo": _SPEAKER})
    client_id, angle_id = client.id, angle.id
    monkeypatch.setattr(assets_view, "get_session", lambda: _sessions(factory))
    monkeypatch.setattr(
        analyzer, "invoke_claude_cli", lambda prompt, **k: _statement_reply()
    )
    # No second model, so both texts land unchecked rather than not at all.
    monkeypatch.setattr(config, "review_configured", lambda: False)

    assets_view._generating.acquire()
    worker.real_write(
        client_id,
        angle_id,
        (AssetKind.PRESSEMITTEILUNG.value, AssetKind.STATEMENT.value),
    )

    session.expire_all()
    written = {row.kind for row in assets.for_angle(session, angle_id)}
    assert written == {AssetKind.STATEMENT.value}, "the statement stands"
    refused = assets_view._notes[(angle_id, AssetKind.PRESSEMITTEILUNG.value)]
    assert "Sitz (im Profil)" in refused, "and the one that could not names its field"


def test_a_format_whose_commit_failed_does_not_poison_the_rest_of_the_run(
    factory, session, monkeypatch, worker
):
    """A caught exception is not a clean session.

    ``produce`` ends in ``store`` -> ``commit``, and the two commits most likely to
    fail here are real: the partial unique index on one unreleased draft per
    format per impulse, and SQLite's "database is locked" against the cron sweep,
    which runs in another process and is not covered by the run guard. Either
    leaves the transaction in ``PendingRollbackError``, and without a rollback
    every later format in the same run dies on the poisoned session — one bad
    third format losing formats four through six, which is the exact failure the
    per-format handler exists to prevent.
    """
    client, angle = _mandate(session, facts={"ceo": _SPEAKER})
    client_id, angle_id = client.id, angle.id
    monkeypatch.setattr(assets_view, "get_session", lambda: _sessions(factory))

    seen: list[str] = []

    def _produce_or_break(sess, fmt, cl, ang, **kwargs):
        seen.append(fmt.key)
        if len(seen) == 1:
            # Two unreleased drafts of one format on one impulse: exactly what
            # ux_assets_angle_kind_unreleased refuses, and it refuses at commit.
            for _ in range(2):
                sess.add(
                    Asset(
                        client_id=cl.id, angle_id=ang.id, kind=fmt.key,
                        title="Doppelt", body="Zweimal derselbe Entwurf.",
                    )
                )
            sess.commit()
        # Every later format asks the database something, which is what dies on a
        # session nobody rolled back.
        return assets.store(
            sess, fmt, cl, ang, AssetDraft(title="Titel", body="Text.", speaker="")
        )

    monkeypatch.setattr(assets, "produce", _produce_or_break)

    assets_view._generating.acquire()
    worker.real_write(
        client_id,
        angle_id,
        (AssetKind.PRESSEMITTEILUNG.value, AssetKind.STATEMENT.value),
    )

    assert seen == [AssetKind.PRESSEMITTEILUNG.value, AssetKind.STATEMENT.value], (
        "the run reached the second format"
    )
    assert "nicht geschrieben" in assets_view._notes[
        (angle_id, AssetKind.PRESSEMITTEILUNG.value)
    ], "the one that failed says so"
    assert (angle_id, AssetKind.STATEMENT.value) not in assets_view._notes, (
        "and the one after it went through on a clean session"
    )
    session.expire_all()
    assert {row.kind for row in assets.for_angle(session, angle_id)} == {
        AssetKind.STATEMENT.value
    }


def test_a_busy_refusal_recorded_during_a_run_does_not_outlive_it(session, monkeypatch):
    """The refusal was still on screen beside the text the run went on to write.

    Clicking a format while that same format is being written records the "busy"
    sentence under its key — and the run had already forgotten that key when it
    started, so nothing dropped it again. The consultant read "der Auftrag wurde
    nicht angenommen" over a finished text.
    """
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    monkeypatch.setattr(config, "review_configured", lambda: False)

    def _model(prompt, **kwargs):
        # The second click, landing while this call is out.
        assets_view._remember(angle.id, fmt.key, assets_view._BUSY)
        return _statement_reply()

    monkeypatch.setattr(analyzer, "invoke_claude_cli", _model)

    assets_view._write_one(session, client, angle, fmt)

    assert not assets_view._said(angle.id, fmt.key)
    standing = assets.current(session, [angle.id])[angle.id][fmt.key]
    assert standing.body.startswith("Die Verwahrung wandert"), "and the text is there"


class _sessions:
    """``get_session()`` against the fixture database, as a context manager."""

    def __init__(self, factory) -> None:
        self._session = factory()

    def __enter__(self):
        return self._session

    def __exit__(self, *exc) -> None:
        self._session.close()


def _raises(error: Exception):
    def _boom(*args, **kwargs):
        raise error

    return _boom


# --- The interface language ----------------------------------------------------


_ADVICE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src/newspulse/web/templates/advice.html"
)


def _package_markup() -> str:
    """The package block of the impulse page, without the page around it."""
    text = _ADVICE.read_text(encoding="utf-8")
    return text[text.index('<div class="pack"') : text.index("{# /pack #}")]


def test_every_german_string_on_the_package_has_an_english_entry():
    """A half-translated surface looks broken in a way a German one does not."""
    # ``(?<![\w.])`` so ``.split("\n")`` in the page's script is not read as a
    # translation call: it ends in ``t(`` like ``t(`` does.
    strings = set(re.findall(r'(?<![\w.])t\(\s*"([^"]+)"\s*\)', _package_markup()))
    assert strings, "the markup moved; this guard is reading the wrong block"
    missing = sorted(s for s in strings if s not in i18n.known_keys())
    assert not missing, f"no English entry for: {missing}"


def test_the_strings_the_code_holds_are_translated_too():
    """The state names and the refusals are written in Python, not in markup.

    Which is exactly why they are the ones that go missing: nothing that reads the
    template for German strings can see them, so the gate above passes over a page
    whose every refusal is still in German.
    """
    from newspulse.web.routes.assets_view import (
        _BUSY,
        _REMEDIES,
        _STATE_LABELS,
        _SWEEP_RUNNING,
    )

    for label in _STATE_LABELS.values():
        assert label in i18n.known_keys(), label
    for remedy in _REMEDIES.values():
        assert remedy.label in i18n.known_keys(), remedy.label
    for sentence in (
        assets.UNREAD_SINCE_EDIT,
        assets.CHECK_UNAVAILABLE,
        assets.RELEASED_IS_FINAL,
        assets.STALE_EDIT,
        _BUSY,
        _SWEEP_RUNNING,
    ):
        assert sentence in i18n.known_keys(), sentence
    # ``recheck`` stores CHECK_UNAVAILABLE as a line of its own with the backend's
    # words under it, and the page renders that field line by line through ``t()``.
    # Interpolated into one string and printed raw, as it was, the entry above
    # existed and could never be looked up: an English reader read the German.
    assert "{{ t(line) }}" in _package_markup(), "the verdict block prints it raw"


def test_no_german_string_is_translated_twice():
    """Python keeps the last entry for a repeated key and drops the first silently.

    Harmless only while both say the same thing; the next person to reword one of
    them rewords the one that loses. Guarded for the strings this surface holds
    rather than for the whole dict, which carries older duplicates.
    """
    source = (
        pathlib.Path(i18n.__file__).read_text(encoding="utf-8").split("\n")
    )
    keys = [
        m.group(1)
        for line in source
        if (m := re.match(r'\s{4}"((?:[^"\\]|\\.)*)":\s', line))
    ]
    ours = {
        *_STATE_NAMES,
        "Das Paket zu diesem Anlass",
        "Fehlende schreiben",
        "Paket schreiben",
        "Gegenlesen lassen",
    }
    twice = sorted(k for k in ours if keys.count(k) > 1)
    assert not twice, f"translated twice, so one of them is dead: {twice}"


_STATE_NAMES = ("Nicht geschrieben", "Entwurf", "Geprüft", "Freigegeben")
