"""The personalised message (newspulse.outreach) and the button that asks for it.

This is what the "Empfehlungen" panel became. The consultant's report on the pair
was that the difference between them could not be stated — and the reason is that
a recommendation described work where the impulse beside it did some. So the
panel went and this took its place: the same position, written at one journalist,
using the mandate's own coverage as what makes a stranger's pitch credible.

Driven with an injected ``invoke``, like every other generator here, so the whole
path runs without a subprocess: prompt build, parse, persistence, page.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import outreach
from newspulse.analyzer import ParseError
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Base,
    Category,
    Client,
    Outreach,
)
from newspulse.pitch import PitchTarget
from newspulse.web.app import create_app, get_db


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
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _mandate(session, *, coverage: int = 0) -> tuple[Client, Angle]:
    client = Client(name="Alpha AG", industry="Neobroker", keywords=["Verwahrung"])
    session.add(client)
    session.flush()
    for i in range(coverage):
        art = Article(
            title=f"Alpha AG baut Verwahrung aus {i}",
            url=f"https://ex.de/own-{i}",
            source="Börsen-Zeitung",
            published_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=3),
            fetched_at=dt.datetime.now(dt.UTC),
            summary_text=None,
            language="de",
            title_hash=f"own{i:05d}",
        )
        session.add(art)
        session.flush()
        session.add(
            Analysis(
                article_id=art.id, client_id=client.id, summary="s",
                category=Category.PRODUKT, relevance_score=6,
                importance_score=6, is_alert=False,
            )
        )
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


_TARGET = PitchTarget(
    outlet="Börsen-Zeitung",
    journalist="Jason Nelson",
    reason="schreibt über das Themenfeld",
    evidence=("Was ist Arc? Die Stablecoin-Kette von Circle",),
    about_client=0,
)


def _reply(**over) -> str:
    payload = {
        "subject": "Verfügbarkeit als Risikoparameter",
        "message": "Sehr geehrter Herr Nelson,\n\nzwei Absätze.\n\nAlpha AG",
        "hook": "Hat vergangene Woche über Circles Kette geschrieben.",
    }
    payload.update(over)
    return json.dumps(payload)


# --- The prompt carries what makes a pitch personal ------------------------------


def test_the_thesis_and_its_overclaim_both_reach_the_prompt(session):
    """The letter has to stand on the position, and the position is defined as
    much by the claim it rejects as by the one it makes. A pitch that asserts the
    overclaim has lost the thing that made the impulse worth sending."""
    client, angle = _mandate(session)
    seen: list[str] = []

    outreach.draft(
        session, client, angle, _TARGET,
        invoke=lambda prompt, **k: seen.append(prompt) or _reply(),
    )

    assert "Die Liveness der Kette ist ein eigener Risikoparameter." in seen[0]
    assert "Solana ist unzuverlässig" in seen[0]


def test_the_recipients_own_headlines_reach_the_prompt(session):
    """"Ihr Beitrag zu X" written from nothing is the failure mode of every mail
    merge. The model gets the headlines it may refer to, and nothing else."""
    client, angle = _mandate(session)
    seen: list[str] = []

    outreach.draft(
        session, client, angle, _TARGET,
        invoke=lambda prompt, **k: seen.append(prompt) or _reply(),
    )

    assert "Jason Nelson" in seen[0]
    assert "Was ist Arc? Die Stablecoin-Kette von Circle" in seen[0]


def test_the_mandates_own_coverage_reaches_the_prompt(session):
    """This is the half the "Empfehlungen" panel used to work from. It is not
    discarded — it is the evidence that this company is a subject the press
    already takes seriously, which is what a journalist weighs."""
    client, angle = _mandate(session, coverage=2)
    seen: list[str] = []

    outreach.draft(
        session, client, angle, _TARGET,
        invoke=lambda prompt, **k: seen.append(prompt) or _reply(),
    )

    assert "Alpha AG baut Verwahrung aus 0" in seen[0]


def test_a_mandate_nobody_writes_about_is_told_so_rather_than_left_silent(session):
    """An absent block reads to the model as an absent fact. Say it: a letter that
    implies the company is known when it is not gets deleted on sight."""
    client, angle = _mandate(session, coverage=0)
    seen: list[str] = []

    outreach.draft(
        session, client, angle, _TARGET,
        invoke=lambda prompt, **k: seen.append(prompt) or _reply(),
    )

    assert "Keine in den letzten Monaten" in seen[0]


def test_without_a_recipient_the_model_is_forbidden_from_inventing_one(session):
    """Most feeds carry no byline. A general letter is a fine answer; a letter to
    a plausible invented name at a plausible invented desk is not."""
    client, angle = _mandate(session)
    seen: list[str] = []

    outreach.draft(
        session, client, angle, None,
        invoke=lambda prompt, **k: seen.append(prompt) or _reply(),
    )

    assert "Erfinde keinen Namen" in seen[0]


# --- The trust boundary ----------------------------------------------------------


def test_a_reply_that_is_not_json_is_a_parse_error(session):
    client, angle = _mandate(session)

    with pytest.raises(ParseError):
        outreach.draft(session, client, angle, _TARGET, invoke=lambda *a, **k: "Klar!")


def test_a_fenced_reply_is_still_read(session):
    """Wrapping JSON in ```json is a habit, not an error, and there is no retry
    here to absorb it."""
    client, angle = _mandate(session)

    message = outreach.draft(
        session, client, angle, _TARGET,
        invoke=lambda *a, **k: f"```json\n{_reply()}\n```",
    )

    assert message.message.startswith("Sehr geehrter Herr Nelson")


def test_an_empty_message_is_a_failure_not_a_blank_card(session):
    """Unlike an impulse, this one has no honest empty answer: the judgement about
    whether there is something to say was already made upstream."""
    client, angle = _mandate(session)

    with pytest.raises(ParseError):
        outreach.draft(
            session, client, angle, _TARGET, invoke=lambda *a, **k: _reply(message="  ")
        )


# --- Persistence -----------------------------------------------------------------


def test_rewriting_for_the_same_recipient_replaces_rather_than_stacks(session):
    """Two drafts at the same journalist are two attempts at one pitch, and a card
    that grows a copy every click turns a page into a backlog."""
    client, angle = _mandate(session)

    for text in ("Erster Versuch.", "Zweiter Versuch."):
        message = outreach.draft(
            session, client, angle, _TARGET, invoke=lambda *a, **k: _reply(message=text)
        )
        outreach.store(session, client, angle, message, _TARGET)

    rows = session.scalars(select(Outreach)).all()
    assert len(rows) == 1
    assert rows[0].message == "Zweiter Versuch."


def test_a_second_recipient_gets_its_own_message(session):
    """The same position reads differently to the reporter who covered the story
    it answers than to a trade title that never has."""
    client, angle = _mandate(session)
    other = PitchTarget(
        outlet="Handelsblatt", journalist=None, reason="", evidence=(), about_client=0
    )

    for target in (_TARGET, other):
        message = outreach.draft(
            session, client, angle, target, invoke=lambda *a, **k: _reply()
        )
        outreach.store(session, client, angle, message, target)

    assert len(outreach.for_angle(session, angle.id)) == 2


def test_by_angle_groups_without_a_query_per_card(session):
    client, angle = _mandate(session)
    message = outreach.draft(
        session, client, angle, _TARGET, invoke=lambda *a, **k: _reply()
    )
    outreach.store(session, client, angle, message, _TARGET)

    grouped = outreach.by_angle(session, [angle.id, angle.id + 99])

    assert list(grouped) == [angle.id]
    assert grouped[angle.id][0].journalist == "Jason Nelson"


def _element(html: str, marker: str) -> str:
    """The text of the div carrying ``marker``, with its nested tags balanced.

    Splitting on the first ``</div>`` is what a naive version does, and it stops
    at the subject line — which would let the hook drift into the copy target
    without anything noticing.
    """
    rest = html[html.index(marker) + len(marker):]
    depth, out = 1, []
    while depth:
        opened, closed = rest.find("<div"), rest.find("</div>")
        assert closed != -1, "unbalanced markup"
        cut = min(x for x in (opened, closed) if x != -1)
        out.append(rest[:cut])
        depth += 1 if cut == opened else -1
        rest = rest[cut + (4 if cut == opened else 6):]
    return "".join(out)


# --- The page --------------------------------------------------------------------


def test_the_impulse_offers_the_button_next_to_its_thesis(factory, web):
    """"Vielleicht können wir einen großen Button neben die Thesen machen mit:
    Personalisierte Nachricht erzeugen." """
    with factory() as session:
        client, angle = _mandate(session)
        client_id, angle_id = client.id, angle.id

    body = web.get(f"/client/{client_id}/advice").text

    assert "Personalisierte Nachricht erzeugen" in body
    assert f'action="/client/{client_id}/impulse/{angle_id}/message"' in body


def test_a_written_message_is_rendered_with_its_recipient(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        message = outreach.draft(
            session, client, angle, _TARGET, invoke=lambda *a, **k: _reply()
        )
        outreach.store(session, client, angle, message, _TARGET)
        client_id = client.id

    body = web.get(f"/client/{client_id}/advice").text

    assert "Sehr geehrter Herr Nelson" in body
    assert "Jason Nelson" in body
    assert "Börsen-Zeitung" in body


def test_the_reason_for_the_recipient_stays_out_of_the_copied_text(factory, web):
    """The hook is for the consultant. The copy button takes the element it is
    pointed at, so the hook must live outside that element or it lands in an
    inbox."""
    with factory() as session:
        client, angle = _mandate(session)
        message = outreach.draft(
            session, client, angle, _TARGET, invoke=lambda *a, **k: _reply()
        )
        stored = outreach.store(session, client, angle, message, _TARGET)
        client_id, row_id = client.id, stored.id

    body = web.get(f"/client/{client_id}/advice").text
    copied = _element(body, f'id="letter-text-{row_id}"')

    assert "zwei Absätze." in copied
    assert "Hat vergangene Woche" not in copied
    assert "Hat vergangene Woche" in body  # but the consultant still sees it


def test_the_button_posts_and_comes_back_to_the_impulse(factory, web):
    with factory() as session:
        client, angle = _mandate(session)
        client_id, angle_id = client.id, angle.id

    resp = web.post(
        f"/client/{client_id}/impulse/{angle_id}/message",
        data={"journalist": "Jason Nelson", "outlet": "Börsen-Zeitung"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/client/{client_id}/advice#impulse-{angle_id}"


def test_an_impulse_belonging_to_another_mandate_is_refused(factory, web):
    """The angle id comes off the URL; without the check one mandate's page could
    write a letter onto another's position."""
    with factory() as session:
        client, angle = _mandate(session)
        stranger = Client(name="Beta AG", aliases=[], keywords=[], alert_topics=[])
        session.add(stranger)
        session.commit()
        stranger_id, angle_id = stranger.id, angle.id

    resp = web.post(
        f"/client/{stranger_id}/impulse/{angle_id}/message",
        data={"journalist": "", "outlet": ""},
        follow_redirects=False,
    )

    assert resp.status_code == 404


def test_the_recommendations_panel_is_gone(factory, web):
    """"Was jetzt wegfällt sind die Empfehlungen. Aber das ist wirklich nicht ganz
    klar wo der unterschied liegt." Two panels, one legible purpose between
    them — so there is one panel."""
    with factory() as session:
        client, _ = _mandate(session, coverage=3)
        client_id = client.id

    body = web.get(f"/client/{client_id}/advice").text

    assert "Empfehlungen" not in body
    assert "Letzte 90 Tage" not in body  # nor the window picker that fed it


# --- What the page says when there is no impulse ---------------------------------


def test_radar_hits_that_are_all_own_coverage_are_named_as_such(factory, web):
    """"Hier bei IB-7 wird auch einfach immer noch kein Impuls angezeigt."

    The page reported "the radar collected 2 market items and has not made an
    opening of them yet", which reads as laziness on the tool's part. It was
    counting every topic hit ever recorded, while the draft reads a different set
    entirely: inside the window, minus coverage of the mandate itself. For a small
    company whose themes are written close to its own name those two numbers are 2
    and 0, and the second one is the one that explains the empty column.
    """
    import datetime as dt

    from newspulse.models import TopicHit

    with factory() as session:
        client = Client(name="IB-7", aliases=[], keywords=["IB-7 Beauty"], alert_topics=[])
        session.add(client)
        session.flush()
        article = Article(
            title="IB-7 eröffnet Standort", url="https://ex.de/ib7",
            source="Kosmetik-Journal", published_at=dt.datetime.now(dt.UTC),
            fetched_at=dt.datetime.now(dt.UTC), summary_text=None,
            language="de", title_hash="ib700001",
        )
        session.add(article)
        session.flush()
        # A radar hit that is also coverage of the mandate: found by its themes,
        # useless as material to position against.
        session.add(TopicHit(client_id=client.id, article_id=article.id,
                             found_at=dt.datetime.now(dt.UTC)))
        session.add(
            Analysis(
                article_id=article.id, client_id=client.id, summary="s",
                category=Category.PRODUKT, relevance_score=7,
                importance_score=7, is_alert=False,
            )
        )
        session.commit()
        client_id = client.id

    body = " ".join(web.get(f"/client/{client_id}/advice").text.split())

    assert "Kein verwertbares Marktmaterial" in body
    assert "keinen Anlass daraus gemacht" not in body
    # And the remedy sits where the diagnosis is read.
    assert "Passende Themen vorschlagen" in body


def test_usable_material_without_a_draft_still_points_at_the_button(factory, web):
    """The other case, and it needs the opposite response: there *is* something to
    work with, so the answer is to ask for a draft rather than to fix the themes."""
    import datetime as dt

    from newspulse.models import TopicHit

    with factory() as session:
        client = Client(name="Alpha AG", aliases=[], keywords=["Verwahrung"], alert_topics=[])
        session.add(client)
        session.flush()
        article = Article(
            title="Verwahrung wird reguliert", url="https://ex.de/markt",
            source="Börsen-Zeitung", published_at=dt.datetime.now(dt.UTC),
            fetched_at=dt.datetime.now(dt.UTC), summary_text=None,
            language="de", title_hash="mkt00001",
        )
        session.add(article)
        session.flush()
        session.add(TopicHit(client_id=client.id, article_id=article.id,
                             found_at=dt.datetime.now(dt.UTC)))
        session.commit()
        client_id = client.id

    body = " ".join(web.get(f"/client/{client_id}/advice").text.split())

    assert "verwertbare Marktmeldung(en)" in body
    assert "Kein verwertbares Marktmaterial" not in body
