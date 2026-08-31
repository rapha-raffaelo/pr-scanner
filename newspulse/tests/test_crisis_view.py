"""Die Krisenseite (UHR-03): what the page shows, and what the two buttons do.

Nothing here reaches a model and nothing reaches the network. The crisis
arithmetic is pinned in ``test_crisis.py``; this file is about the *page* —
DEC-2's two columns — and about DEC-1's second button, which UHR-01 left
unwired: Verwerfen.

Five of these tests protect a promise rather than a layout:

* the Krise tab exists only for a mandate with a *declared* crisis — a
  dismissed proposal creates no tab, because nobody declared anything;
* Verwerfen silences the same story for this mandate and only this mandate,
  without creating a crisis row;
* a text nobody has checked renders as ungeprüft, never as clean;
* a missing crisis contact is a named gap linking into the kickoff, not an
  empty line;
* a closed crisis stays readable, with its reason and its full chronology.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import threading
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, crisis, i18n
from newspulse.matching import title_hash
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Asset,
    Base,
    Category,
    Client,
    ClientFact,
    Crisis,
    CrisisDismissal,
    Outreach,
    OutreachReply,
    OutreachState,
    Tonality,
)
from newspulse.web.app import create_app, get_db
from newspulse.web.routes import crisis_view

_BERLIN = ZoneInfo("Europe/Berlin")


# --- Fixtures ---------------------------------------------------------------------


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as open_session:
        yield open_session


@pytest.fixture
def web(factory):
    app = create_app()

    def _override():
        open_session = factory()
        try:
            yield open_session
        finally:
            open_session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    """Pin the display zone, so a test states the calendar it renders in."""
    monkeypatch.setattr(config, "LOCAL_ZONE", _BERLIN)


@pytest.fixture
def mandate(session) -> Client:
    client = Client(name="Solaris AG", aliases=["Solaris"], industry="Solarenergie")
    session.add(client)
    session.commit()
    return client


# --- Builders ---------------------------------------------------------------------
#
# Coverage is seeded relative to the real clock on purpose: ``propose`` and the
# page read the clock the route reads, and the DEC-1 page test in
# ``test_crisis.py`` learned the hard way that a fixed calendar rots.


def _slug(title: str, source: str) -> str:
    return hashlib.sha1(f"{title}|{source}".encode()).hexdigest()[:12]


def _cover(
    session,
    client: Client,
    *,
    source: str,
    title: str,
    hours_ago: float = 2,
    tonality: Tonality = Tonality.NEGATIV,
    category: Category = Category.SONSTIGES,
    importance: int = 6,
    summary: str = "Eine kurze Zusammenfassung.",
) -> Article:
    """One stored article plus this mandate's analysis of it."""
    at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)
    article = session.scalars(
        select(Article).where(Article.url == f"https://example.de/{_slug(title, source)}")
    ).first()
    if article is None:
        article = Article(
            title=title,
            url=f"https://example.de/{_slug(title, source)}",
            source=source,
            published_at=at,
            fetched_at=at,
            summary_text=summary,
            title_hash=title_hash(title, source),
        )
        session.add(article)
        session.flush()
    already = session.scalars(
        select(Analysis).where(
            Analysis.article_id == article.id, Analysis.client_id == client.id
        )
    ).first()
    if already is not None:
        return article
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            is_relevant=True,
            summary=summary,
            category=category,
            relevance_score=7,
            importance_score=importance,
            tonality=tonality,
            analyzed_at=at,
        )
    )
    session.commit()
    return article


#: The crisis story's headline. Long enough to clear the clusterer's
#: minimum-token gate, so syndicated copies of it group into one story.
_HEADLINE = "Verbraucherzentrale rügt Vertragsklauseln bei Solaranbieter Solaris"


def _trigger(session, client: Client) -> Article:
    """One article the analyzer filed as a crisis — enough for a proposal."""
    return _cover(
        session,
        client,
        source="WDR",
        title=_HEADLINE,
        category=Category.KRISE,
        importance=9,
        hours_ago=4,
    )


def _declared(session, client: Client) -> Crisis:
    return crisis.declare(session, client, _trigger(session, client), by="lucas")


def _fact(session, client: Client, key: str, value: str) -> None:
    session.add(ClientFact(client_id=client.id, key=key, value=value, filled_by="mensch"))
    session.commit()


def _reply(
    session,
    client: Client,
    *,
    body: str,
    state: OutreachState = OutreachState.ANTWORT,
    received_at: dt.datetime | None = None,
) -> OutreachReply:
    """A journalist's mail against a released letter of this mandate."""
    angle = Angle(
        client_id=client.id,
        generated_at=dt.datetime.now(dt.UTC),
        subject="Position",
        message="Zwei Absätze.",
        context="Kontext.",
        thesis="These.",
        overclaim="Zu viel.",
        article_ids=[],
    )
    session.add(angle)
    session.flush()
    letter = Outreach(
        angle_id=angle.id,
        client_id=client.id,
        journalist="Maren Kessler",
        outlet="Handelsblatt",
        subject="Rückfrage",
        message="Sehr geehrte Frau Kessler,",
        state=state,
    )
    session.add(letter)
    session.flush()
    reply = OutreachReply(
        outreach_id=letter.id,
        gmail_message_id=f"msg-{letter.id}",
        from_name="Maren Kessler",
        from_email="kessler@example.de",
        received_at=received_at or dt.datetime.now(dt.UTC),
        body=body,
    )
    session.add(reply)
    session.commit()
    return reply


def _crisis_text(
    session,
    client: Client,
    standing: Crisis,
    *,
    kind: str = "holding_statement",
    checked: bool = False,
    released: bool = False,
) -> Asset:
    row = Asset(
        client_id=client.id,
        angle_id=None,
        crisis_id=standing.id,
        kind=kind,
        body="Uns liegt die Abmahnung seit heute Morgen vor.",
        speaker="Jonas Feld",
    )
    if checked:
        row.reviewed_by = "zweitmodell"
        row.guide_reviewed_by = "zweitmodell"
    if released:
        row.released_at = dt.datetime.now(dt.UTC)
        row.released_by = "mensch"
    session.add(row)
    session.commit()
    return row


# --- The tab ----------------------------------------------------------------------


def test_the_krise_tab_appears_only_after_a_declaration(web, session, mandate):
    _trigger(session, mandate)
    before = web.get(f"/client/{mandate.id}")
    assert before.status_code == 200
    assert ">Krise</a>" not in before.text

    _declared(session, mandate)
    after = web.get(f"/client/{mandate.id}")
    assert ">Krise</a>" in after.text


def test_a_dismissed_proposal_creates_no_tab_and_no_page(web, session, mandate):
    """Nobody declared anything, so there is nothing worth a daily glance."""
    article = _trigger(session, mandate)
    crisis.dismiss(session, mandate, article, by="lucas")

    page = web.get(f"/client/{mandate.id}")
    assert ">Krise</a>" not in page.text
    assert web.get(f"/client/{mandate.id}/krise").status_code == 404


def test_the_page_is_a_404_for_a_mandate_that_never_had_one(web, session, mandate):
    assert web.get(f"/client/{mandate.id}/krise").status_code == 404


# --- The offer and its two buttons ------------------------------------------------


def test_the_offer_on_heute_carries_declare_and_dismiss(web, session, mandate):
    _trigger(session, mandate)
    page = web.get("/today")
    assert "Krise erklären" in page.text
    assert 'action="/crisis/dismiss"' in page.text
    assert "Verwerfen" in page.text


def test_the_offer_stands_on_the_mandantenkarte_with_both_buttons(web, session, mandate):
    _trigger(session, mandate)
    page = web.get(f"/client/{mandate.id}")
    assert "koffer" in page.text
    assert "Krise erklären" in page.text
    assert 'action="/crisis/dismiss"' in page.text


def test_dismiss_silences_the_same_story_for_this_mandate_only(web, factory, session):
    """The click DEC-1 priced the false alarm at — and its blast radius."""
    ours = Client(name="Solaris AG")
    theirs = Client(name="Helio GmbH")
    session.add_all([ours, theirs])
    session.commit()
    article = _trigger(session, ours)
    _cover(
        session,
        theirs,
        source="WDR",
        title=_HEADLINE,
        category=Category.KRISE,
        importance=9,
        hours_ago=4,
    )
    assert crisis.propose(session, ours) is not None

    resp = web.post(
        "/crisis/dismiss",
        data={"client_id": ours.id, "article_id": article.id, "redirect_to": "/today"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with factory() as fresh:
        ours_row = fresh.get(Client, ours.id)
        theirs_row = fresh.get(Client, theirs.id)
        assert crisis.propose(fresh, ours_row) is None
        # The neighbour looking at the same story still gets asked.
        assert crisis.propose(fresh, theirs_row) is not None
        # And no crisis row was invented on the way.
        assert fresh.scalar(select(func.count()).select_from(Crisis)) == 0

    assert "Krise erklären" not in web.get(f"/client/{ours.id}").text


def test_dismiss_is_idempotent(web, session, mandate):
    article = _trigger(session, mandate)
    for _ in range(2):
        web.post(
            "/crisis/dismiss",
            data={"client_id": mandate.id, "article_id": article.id, "redirect_to": "/"},
            follow_redirects=False,
        )
    assert (
        session.scalar(select(func.count()).select_from(CrisisDismissal)) == 1
    )


def test_dismiss_ignores_a_stale_or_foreign_target(web, session, mandate):
    """A row removed while the page sat open must cost nothing, not the page."""
    resp = web.post(
        "/crisis/dismiss",
        data={"client_id": mandate.id, "article_id": 4711, "redirect_to": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert session.scalar(select(func.count()).select_from(CrisisDismissal)) == 0


# --- The left column --------------------------------------------------------------


def test_coverage_is_grouped_by_story_with_its_pickup_count(web, session, mandate):
    _declared(session, mandate)
    # Two pickups of the trigger's own story — syndication, so same headline.
    _cover(session, mandate, source="Handelsblatt", title=_HEADLINE, hours_ago=2)
    _cover(session, mandate, source="taz", title=_HEADLINE, hours_ago=1)
    page = web.get(f"/client/{mandate.id}/krise")
    assert page.status_code == 200
    assert "3 Beiträge" in page.text
    assert "3 Aufgriffe" in page.text
    assert "Auslöser" in page.text
    assert "Handelsblatt" in page.text and "taz" in page.text


# --- The right column -------------------------------------------------------------


def test_an_unchecked_text_renders_as_ungeprueft_never_as_clean(web, session, mandate):
    standing = _declared(session, mandate)
    _crisis_text(session, mandate, standing, checked=False)
    page = web.get(f"/client/{mandate.id}/krise")
    assert "Holding Statement" in page.text
    assert "Ungeprüft" in page.text
    assert "kpill--ok" not in page.text.split("Holding Statement", 1)[1].split("</article>")[0]


def test_a_released_checked_text_shows_both_states(web, session, mandate):
    standing = _declared(session, mandate)
    _crisis_text(session, mandate, standing, kind="krisen_qa", checked=True, released=True)
    page = web.get(f"/client/{mandate.id}/krise")
    assert "Q&amp;A-Haltung" in page.text
    assert "Geprüft" in page.text
    assert "Freigegeben" in page.text


def test_an_open_request_stands_with_its_deadline(web, session, mandate):
    _declared(session, mandate)
    _reply(session, mandate, body="Drei Fragen zur Widerrufsfrist. Frist: heute 14:00.")
    page = web.get(f"/client/{mandate.id}/krise")
    assert "Anfrage" in page.text
    assert "Maren Kessler" in page.text
    assert "heute 14:00" in page.text


def test_a_request_naming_no_deadline_says_so(web, session, mandate):
    _declared(session, mandate)
    _reply(session, mandate, body="Können Sie das kommentieren?")
    page = web.get(f"/client/{mandate.id}/krise")
    assert "keine Frist genannt" in page.text


def test_a_resolved_request_no_longer_counts_as_open(web, session, mandate):
    _declared(session, mandate)
    _reply(session, mandate, body="Danke!", state=OutreachState.VEROEFFENTLICHT)
    page = web.get(f"/client/{mandate.id}/krise")
    assert "Anfrage:" not in page.text


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Frist: heute 14:00, danke.", "heute 14:00"),
        ("Deadline ist morgen 10:30", "morgen 10:30"),
        ("Antworten Sie bitte bis 14 Uhr.", "14 Uhr"),
        ("Redaktionsschluss: 03.09.", "03.09."),
        ("Kein Zeitdruck.", ""),
    ],
)
def test_the_deadline_is_read_as_the_journalist_wrote_it(body, expected):
    assert crisis_view._deadline_from(body) == expected


# --- The named gaps ---------------------------------------------------------------


def test_a_missing_crisis_contact_is_a_named_gap_linking_into_the_kickoff(
    web, session, mandate
):
    _declared(session, mandate)
    page = web.get(f"/client/{mandate.id}/krise")
    assert "Im Profil fehlt der Krisenkontakt." in page.text
    assert "Nicht im Profil hinterlegt." in page.text
    assert f"/client/{mandate.id}/kickoff" in page.text


def test_a_filled_crisis_contact_is_shown_and_not_flagged(web, session, mandate):
    _declared(session, mandate)
    _fact(session, mandate, "krisenkontakt", "Dr. Anne Wiegand, +49 170 4412290")
    page = web.get(f"/client/{mandate.id}/krise")
    assert "Dr. Anne Wiegand" in page.text
    assert "Im Profil fehlt der Krisenkontakt." not in page.text


def test_the_gap_box_names_the_formats_nothing_answers_yet(web, session, mandate):
    _declared(session, mandate)
    page = web.get(f"/client/{mandate.id}/krise")
    assert "Noch kein Text im Format" in page.text
    assert "Holding Statement" in page.text
    assert "Q&amp;A-Haltung" in page.text


# --- Closing, and the record ------------------------------------------------------


def test_closing_requires_a_reason(web, session, mandate):
    standing = _declared(session, mandate)
    web.post(
        f"/crisis/{standing.id}/close",
        data={"reason": "   ", "redirect_to": "/"},
        follow_redirects=False,
    )
    session.expire_all()
    assert session.get(Crisis, standing.id).closed_at is None


def test_a_closed_crisis_stays_readable_with_its_full_chronology(web, session, mandate):
    standing = _declared(session, mandate)
    _crisis_text(session, mandate, standing, checked=True, released=True)
    web.post(
        f"/crisis/{standing.id}/close",
        data={"reason": "Geklärt, die Kanzlei übernimmt.", "redirect_to": "/"},
        follow_redirects=False,
    )

    page = web.get(f"/client/{mandate.id}/krise")
    assert page.status_code == 200
    assert "Krise geschlossen" in page.text
    assert "Geklärt, die Kanzlei übernimmt." in page.text

    record = web.get(f"/client/{mandate.id}/krise?zeitleiste=1")
    # The bar above also says "Krise geschlossen", so the order is asserted
    # inside the timeline itself.
    chronology = record.text.split('class="ktl"', 1)[1]
    for entry in ("Krise erklärt", "Beitrag", "Text entworfen", "Text freigegeben",
                  "Krise geschlossen"):
        assert entry in chronology, f"the chronology lost {entry!r}"
    # Chronology, not a pile: declared before closed.
    assert chronology.index("Krise erklärt") < chronology.index("Krise geschlossen")


# --- The write button -------------------------------------------------------------

#: How long a test waits for the worker to reach its stub — only ever paid when
#: something is broken.
_WORKER_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def no_background_writer(monkeypatch):
    """Stop the Text-schreiben button from shelling out to a model in a test run.

    The stub releases the lock the way the real worker's ``finally`` does; one
    that only returned would hold it for the rest of the process and hang a
    later test instead of failing this one.
    """
    spawned: list[tuple[int, int]] = []
    done = threading.Event()

    def _stub(client_id: int, crisis_id: int) -> None:
        try:
            spawned.append((client_id, crisis_id))
            crisis_view._writing.release()
        finally:
            done.set()

    monkeypatch.setattr(crisis_view, "_run_crisis_texts", _stub)
    yield spawned, done
    assert not crisis_view._writing.locked(), "a test left the crisis writer held"


def test_the_write_button_hands_the_open_crisis_to_the_worker(
    web, session, mandate, no_background_writer
):
    spawned, done = no_background_writer
    standing = _declared(session, mandate)
    resp = web.post(f"/client/{mandate.id}/krise/text", follow_redirects=False)
    assert resp.status_code == 303
    assert done.wait(_WORKER_TIMEOUT), "the writer never ran"
    assert spawned == [(mandate.id, standing.id)]


def test_the_write_button_is_a_noop_without_an_open_crisis(
    web, session, mandate, no_background_writer
):
    spawned, _done = no_background_writer
    standing = _declared(session, mandate)
    crisis.close(session, standing, reason="Vorbei.")
    resp = web.post(f"/client/{mandate.id}/krise/text", follow_redirects=False)
    assert resp.status_code == 303
    assert spawned == []


# --- Language ---------------------------------------------------------------------


def test_every_visible_string_on_the_page_is_translated():
    """The acceptance criterion, mechanically: every ``t("...")`` literal in the
    crisis templates has an English entry. A missing one degrades to German —
    a working sentence, but a broken promise for this story."""
    import re
    from pathlib import Path

    templates = Path(crisis_view.__file__).resolve().parents[1] / "templates"
    literals: set[str] = set()
    for name in ("client_crisis.html", "_client_tabs.html"):
        text = (templates / name).read_text(encoding="utf-8")
        literals |= set(re.findall(r"""\bt\(\s*"([^"]+)"\s*\)""", text))
        literals |= set(re.findall(r"""\bt\(\s*'([^']+)'\s*\)""", text))
    assert literals, "the scan found nothing — the pattern rotted"
    known = set(i18n.known_keys())
    missing = sorted(s for s in literals if s not in known)
    assert not missing, f"untranslated strings: {missing}"


def test_the_page_renders_in_english_when_asked(web, session, mandate):
    _declared(session, mandate)
    web.post("/language/en?next=/", follow_redirects=False)
    page = web.get(f"/client/{mandate.id}/krise")
    assert "What is being said" in page.text
    assert "Who is reachable" in page.text
