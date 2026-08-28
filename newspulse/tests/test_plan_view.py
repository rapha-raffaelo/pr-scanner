"""Der Redaktionsplan as a page: what a hook shows, and what an empty one says.

Nothing here reaches a model and nothing here reaches the network. The hooks are
written by hand, which is the point: this file is about what the *page* says
about a stored plan, and the plan's own arithmetic is pinned in ``test_plan.py``
against hand-counted fixtures.

Four of these tests protect a promise rather than a layout, and they are the
reason the rest exist:

* every hook links to the stored row it came from, and a hook whose row has been
  deleted says so and carries no link — a link that 404s looks checked;
* an undated hook never renders a day. The date rule (DEC-4) is the feature, and
  "01." under a hook whose source names only a month is exactly the invented date
  the whole engine was inverted to prevent;
* a discarded or moved hook keeps its state across a recompute, so the page a
  person decided on is the page they come back to;
* the downloaded document carries no link back into the application, because the
  recipient has no account to follow one into.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as htmllib
import re
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, i18n, plan
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Asset,
    Base,
    Category,
    Client,
    HookSource,
    HookState,
    MarketSignal,
    PlanHook,
    SignalKind,
    TopicHit,
)
from newspulse.web.app import create_app, get_db
from newspulse.web.routes import plan_view

#: The moment every test here plans from. Mid-August, so the six-month window
#: (2026-08 … 2027-01) crosses a year boundary and no month assertion can pass
#: by accident on a within-year window.
_NOW = dt.datetime(2026, 8, 24, 9, 0, tzinfo=dt.UTC)
_BERLIN = ZoneInfo("Europe/Berlin")

#: Spelled out rather than computed: the window is what the page renders, so
#: deriving it from the code under test would make the assertion tautological.
_WINDOW = ["2026-08", "2026-09", "2026-10", "2026-11", "2026-12", "2027-01"]


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
    """Pin the display zone, so a test states the calendar it counts in."""
    monkeypatch.setattr(config, "LOCAL_ZONE", _BERLIN)


@pytest.fixture(autouse=True)
def no_background_recompute(monkeypatch):
    """Stop the recompute button from starting a real worker in a test run.

    It shells out to a model and holds the sweep guard while it does — the same
    two reasons ``conftest`` neutralises the impulse button. The stub releases
    the lock the way the real worker's ``finally`` does; one that only returned
    would hold it for the rest of the process, which is invisible here and hangs
    a later test.
    """
    started: list[int] = []

    def _stub(client_id: int) -> None:
        started.append(client_id)
        plan_view._recomputing.release()

    monkeypatch.setattr(plan_view, "_run_recompute", _stub)
    return started


@pytest.fixture(autouse=True)
def forget_notes():
    """The click notes live in module memory; a leaked one reaches a later test."""
    yield
    plan_view._notes.clear()


@pytest.fixture
def mandate(session) -> Client:
    client = Client(
        name="Solarhaus AG",
        aliases=["Solarhaus"],
        industry="Solarenergie",
        keywords=["Wärmepumpe"],
    )
    session.add(client)
    session.commit()
    return client


# --- Seeding ----------------------------------------------------------------------


def _signal(session, client: Client, **fields) -> MarketSignal:
    title = fields.pop("title", "EEG-Novelle tritt in Kraft")
    signal = MarketSignal(
        client_id=client.id,
        kind=fields.pop("kind", SignalKind.REGULIERUNG),
        title=title,
        url=f"https://amt.example.de/{hashlib.sha1(title.encode()).hexdigest()[:12]}",
        **fields,
    )
    session.add(signal)
    session.commit()
    return signal


def _article(session, *, title: str, published_at: dt.datetime, source: str) -> Article:
    article = Article(
        title=title,
        url=f"https://presse.example.de/{hashlib.sha1(title.encode()).hexdigest()[:12]}",
        source=source,
        published_at=published_at,
        title_hash=hashlib.sha1(title.casefold().encode()).hexdigest(),
    )
    session.add(article)
    session.commit()
    return article


def _radar_hit(session, client: Client, *, title: str) -> TopicHit:
    article = _article(
        session, title=title, published_at=_NOW - dt.timedelta(days=3), source="pv-magazine"
    )
    hit = TopicHit(article_id=article.id, client_id=client.id)
    session.add(hit)
    session.commit()
    return hit


def _coverage(session, client: Client, *, title: str, published_at: dt.datetime) -> Analysis:
    article = _article(
        session, title=title, published_at=published_at, source="Handelsblatt"
    )
    analysis = Analysis(
        article_id=article.id,
        client_id=client.id,
        is_relevant=True,
        summary=title,
        category=Category.SONSTIGES,
        relevance_score=3,
        importance_score=7,
    )
    session.add(analysis)
    session.commit()
    return analysis


def _hook(session, client: Client, **fields) -> PlanHook:
    hook = PlanHook(
        client_id=client.id,
        source_kind=fields.pop("source_kind", HookSource.MARKTSIGNAL),
        source_id=fields.pop("source_id", 1),
        month=fields.pop("month", "2026-09"),
        day=fields.pop("day", 12),
        title=fields.pop("title", "Ende der Konsultation zur EEG-Novelle"),
        reason=fields.pop("reason", "Der Tag, an dem Fachjournalisten Stimmen suchen."),
        format=fields.pop("format", "statement"),
        **fields,
    )
    session.add(hook)
    session.commit()
    return hook


def _text(html: str) -> str:
    """The page's words: markup stripped, entities resolved, whitespace collapsed."""
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _at(web, mandate, path: str = "/plan"):
    """The plan page for this mandate, with the clock pinned where it belongs.

    The route reads the wall clock, so the window it renders is the window
    *today* has. Every test that asserts on a month therefore seeds against
    ``plan.month_window(now)`` rather than against ``_WINDOW`` — the two are the
    same list only in August 2026, and a suite that assumed so would go red in
    September.
    """
    return web.get(f"/client/{mandate.id}{path}")


def _window(now: dt.datetime | None = None) -> list[str]:
    return plan.month_window(now or dt.datetime.now(dt.UTC))


# --- The tab and the page ----------------------------------------------------------


def test_the_tab_strip_links_to_the_plan_page(web, session, mandate):
    page = _at(web, mandate)

    assert page.status_code == 200
    assert f'href="/client/{mandate.id}/plan"' in page.text
    assert "Redaktionsplan" in _text(page.text)


def test_a_benchmark_has_no_plan_page(web, session):
    """A yardstick is measured, never planned for: the workspace is a 404."""
    rival = Client(name="Zolar", is_competitor=True)
    session.add(rival)
    session.commit()

    assert web.get(f"/client/{rival.id}/plan").status_code == 404


def test_every_month_of_the_window_is_rendered_even_when_it_carries_nothing(
    web, session, mandate
):
    """DEC-5's whole argument: an empty month is an answer, not a gap."""
    _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=20))

    page = _at(web, mandate)

    for month in _window():
        assert f'id="monat-{month}"' in page.text
    assert len(_window()) == config.PLAN_MONTHS


# --- What one hook shows -----------------------------------------------------------


def test_a_hook_shows_its_date_its_reason_its_class_and_its_evidence(
    web, session, mandate
):
    signal = _signal(
        session,
        mandate,
        title="Ende der Konsultation zur EEG-Novelle",
        kind=SignalKind.REGULIERUNG,
        publisher="BMWK",
        effective_at=_NOW + dt.timedelta(days=30),
    )
    month = _window()[1]
    _hook(
        session,
        mandate,
        source_kind=HookSource.MARKTSIGNAL,
        source_id=signal.id,
        month=month,
        day=24,
        title="Ende der Konsultation zur EEG-Novelle",
        reason="Der Tag, an dem Fachjournalisten Stimmen aus der Branche suchen.",
    )

    words = _text(_at(web, mandate).text)

    assert "24" in words
    assert "Der Tag, an dem Fachjournalisten Stimmen aus der Branche suchen." in words
    assert "Regulierung" in words
    assert f"Marktsignal {signal.id}" in words


def test_a_hooks_evidence_links_to_the_page_that_holds_the_stored_row(
    web, session, mandate
):
    """The property that separates this plan from a pretty list: you can click a
    line and see where it came from."""
    signal = _signal(
        session, mandate, kind=SignalKind.VERANSTALTUNG,
        effective_at=_NOW + dt.timedelta(days=40),
    )
    _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )

    page = _at(web, mandate)

    assert f'href="/client/{mandate.id}/market#sig-veranstaltung"' in page.text


def test_an_archive_hook_links_into_the_month_of_its_stored_analysis(
    web, session, mandate
):
    """The archive's evidence is a month, so the link answers for that month."""
    analysis = _coverage(
        session,
        mandate,
        title="Wärmepumpe im Bestandsbau",
        published_at=dt.datetime(2025, 9, 14, 10, 0, tzinfo=dt.UTC),
    )
    _hook(
        session, mandate, source_kind=HookSource.VORJAHR, source_id=analysis.id,
        month=_window()[1], day=None, title="Wärmepumpe im Bestandsbau",
    )

    page = _at(web, mandate)

    assert "date_from=2025-09-01" in page.text
    assert "date_to=2025-09-30" in page.text
    assert f"Analyse {analysis.id}" in _text(page.text)


def test_a_theme_hook_names_its_radar_row_and_links_to_the_market_page(
    web, session, mandate
):
    hit = _radar_hit(session, mandate, title="Netzentgelte steigen erneut")
    _hook(
        session, mandate, source_kind=HookSource.THEMA, source_id=hit.id,
        month=_window()[0], day=None, title="Netzentgelte",
    )

    page = _at(web, mandate)

    assert f"Themen-Treffer {hit.id}" in _text(page.text)
    assert f'href="/client/{mandate.id}/market"' in page.text
    assert "Thema" in _text(page.text)


def test_a_hook_whose_stored_row_is_gone_says_so_instead_of_linking(
    web, session, mandate
):
    """A link that 404s looks checked. The absence of evidence is the news."""
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )
    session.delete(signal)
    session.commit()

    words = _text(_at(web, mandate).text)

    assert "Der Beleg zu diesem Haken ist nicht mehr auffindbar." in words
    assert f"Marktsignal {signal.id}" not in words


def test_an_undated_hook_renders_a_month_and_never_a_day(web, session, mandate):
    """The date rule, on the page. A source that names only a month must not come
    out as the first of it — that is the invented date DEC-4 exists to refuse."""
    analysis = _coverage(
        session, mandate, title="Jahresrückblicke der Fachpresse",
        published_at=dt.datetime(2025, 12, 3, 10, 0, tzinfo=dt.UTC),
    )
    month = _window()[2]
    _hook(
        session, mandate, source_kind=HookSource.VORJAHR, source_id=analysis.id,
        month=month, day=None, title="Jahresrückblicke der Fachpresse",
    )

    body = _at(web, mandate).text
    block = re.search(r'<div class="hook__d[^"]*">(.*?)</div>\s*</div>', body, re.S)

    assert block is not None
    assert "ohne Tag" in _text(block.group(0))
    assert "01" not in _text(block.group(0))


# --- The empty month and the mandate that cannot have a plan -----------------------


def test_an_empty_month_is_written_out_with_a_link_to_the_mandates_themes(
    web, session, mandate
):
    _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=20))

    page = _at(web, mandate)

    assert "month--empty" in page.text
    assert "Ein leerer Monat wird nicht gefüllt, er wird gezeigt" in _text(page.text)
    assert "Themen prüfen" in _text(page.text)
    assert f'href="/settings?edit={mandate.id}"' in page.text


def test_the_argument_for_an_empty_month_is_made_once_and_the_link_every_time(
    web, session, mandate
):
    """Five identical paragraphs down one page is how a sentence stops being
    read. Every empty month still says it is empty and still offers the fix."""
    body = _at(web, mandate).text
    words = _text(body)
    argument = "entweder ist hier wirklich nichts, oder dem Mandat fehlt ein Thema"

    assert words.count(argument) == 1
    assert words.count("Themen prüfen") == len(_window())


def test_the_page_offers_the_plan_as_a_download(web, session, mandate):
    page = _at(web, mandate)

    assert f'href="/client/{mandate.id}/plan.html"' in page.text
    assert "Als Dokument" in _text(page.text)


def test_a_mandate_without_themes_and_without_signals_is_told_what_it_is_missing(
    web, session
):
    """Six empty months would blame the market for an unconfigured mandate."""
    bare = Client(name="Nordwind GmbH", keywords=[], alert_topics=[])
    session.add(bare)
    session.commit()

    page = _at(web, bare)
    words = _text(page.text)

    assert "Für diesen Mandanten lässt sich noch kein Plan bauen." in words
    assert "Keine geprüften Themen hinterlegt." in words
    assert "Kein Marktsignal gefunden." in words
    assert f'href="/settings?edit={bare.id}"' in page.text
    assert f'href="/client/{bare.id}/market"' in page.text
    # The month stack is not rendered beside it: one answer, not two.
    assert "month--empty" not in page.text


def test_a_mandate_with_themes_but_no_signals_still_gets_a_plan(web, session, mandate):
    """One of the two inputs is enough — the gap notice is for having neither."""
    page = _at(web, mandate)

    assert "Für diesen Mandanten lässt sich noch kein Plan bauen." not in _text(page.text)
    assert "month--empty" in page.text


def test_a_mandate_with_signals_but_no_themes_still_gets_a_plan(web, session):
    bare = Client(name="Nordwind GmbH", keywords=[], alert_topics=[])
    session.add(bare)
    session.commit()
    _signal(session, bare, effective_at=_NOW + dt.timedelta(days=20))

    page = _at(web, bare)

    assert "Für diesen Mandanten lässt sich noch kein Plan bauen." not in _text(page.text)


# --- Deciding, and surviving a recompute -------------------------------------------


def test_discarding_a_hook_records_the_refusal_and_keeps_it_on_the_page(
    web, session, mandate
):
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )

    posted = web.post(
        f"/client/{mandate.id}/plan/{hook.id}/verwerfen", follow_redirects=False
    )

    assert posted.status_code == 303
    session.expire_all()
    assert session.get(PlanHook, hook.id).state is HookState.VERWORFEN
    assert "hook--out" in _at(web, mandate).text


def test_a_discarded_hook_keeps_its_state_across_a_recompute(web, session, mandate):
    """The contract a plan lives or dies by: a refusal that came back the next
    morning would train the reader to stop deciding."""
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=plan.month_key(signal.effective_at),
    )
    web.post(f"/client/{mandate.id}/plan/{hook.id}/verwerfen", follow_redirects=False)

    plan.recompute(session, mandate, invoke=_no_prose, now=dt.datetime.now(dt.UTC))

    session.expire_all()
    still = session.get(PlanHook, hook.id)
    assert still is not None
    assert still.state is HookState.VERWORFEN
    assert "hook--out" in _at(web, mandate).text


def test_a_moved_hook_keeps_its_month_across_a_recompute(web, session, mandate):
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    window = _window()
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=plan.month_key(signal.effective_at), day=24,
    )
    target = window[-1]

    moved = web.post(
        f"/client/{mandate.id}/plan/{hook.id}/verschieben",
        data={"monat": target},
        follow_redirects=False,
    )
    plan.recompute(session, mandate, invoke=_no_prose, now=dt.datetime.now(dt.UTC))

    assert moved.status_code == 303
    session.expire_all()
    still = session.get(PlanHook, hook.id)
    assert still.month == target
    # The day is cleared by the move: the source's date belongs to the source's
    # month, and carrying it over would date the hook to a day nobody named.
    assert still.day is None


def test_a_move_outside_the_window_is_refused_with_a_sentence(web, session, mandate):
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )

    web.post(
        f"/client/{mandate.id}/plan/{hook.id}/verschieben",
        data={"monat": "1999-01"},
        follow_redirects=False,
    )

    session.expire_all()
    assert session.get(PlanHook, hook.id).month == _window()[1]
    assert plan_view._NOT_A_PLAN_MONTH in _text(_at(web, mandate).text)


def test_one_mandates_hook_cannot_be_decided_from_another_mandates_url(
    web, session, mandate
):
    other = Client(name="Nordwind GmbH")
    session.add(other)
    session.commit()
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )

    refused = web.post(
        f"/client/{other.id}/plan/{hook.id}/verwerfen", follow_redirects=False
    )

    assert refused.status_code == 404
    session.expire_all()
    assert session.get(PlanHook, hook.id).state is HookState.VORGESCHLAGEN


def test_the_recompute_button_hands_the_work_to_a_worker(
    web, session, mandate, no_background_recompute
):
    posted = web.post(f"/client/{mandate.id}/plan/neu", follow_redirects=False)

    assert posted.status_code == 303
    assert no_background_recompute == [mandate.id]
    assert not plan_view.busy(), "the recompute left its lock held"


# --- "Text schreiben" --------------------------------------------------------------


def test_writing_from_a_hook_opens_the_picker_with_the_suggested_format_ticked(
    web, session, mandate
):
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1], format="statement",
    )

    opened = web.post(
        f"/client/{mandate.id}/plan/{hook.id}/text", follow_redirects=False
    )

    assert opened.status_code == 303
    angle = session.scalars(
        select(Angle).where(Angle.plan_hook_id == hook.id)
    ).one()
    assert f"eintrag=anlass-{angle.id}" in opened.headers["location"]
    assert "format=statement" in opened.headers["location"]

    picker = web.get(f"/client/{mandate.id}/advice?eintrag=anlass-{angle.id}&format=statement")
    ticked = re.search(
        r'<input class="fmtrow__tick"[^>]*value="statement"[^>]*>', picker.text, re.S
    )
    assert ticked is not None
    assert "checked" in ticked.group(0)


def test_the_hook_becomes_the_occasion_and_carries_its_own_words(web, session, mandate):
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1], title="EEG-Novelle tritt in Kraft",
        reason="Der härteste Termin im Halbjahr.",
    )

    web.post(f"/client/{mandate.id}/plan/{hook.id}/text", follow_redirects=False)

    angle = session.scalars(select(Angle).where(Angle.plan_hook_id == hook.id)).one()
    assert angle.subject == "EEG-Novelle tritt in Kraft"
    assert angle.message == "Der härteste Termin im Halbjahr."
    assert angle.client_id == mandate.id


def test_a_second_click_reuses_the_occasion_rather_than_opening_a_second_one(
    web, session, mandate
):
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )

    first = web.post(f"/client/{mandate.id}/plan/{hook.id}/text", follow_redirects=False)
    second = web.post(f"/client/{mandate.id}/plan/{hook.id}/text", follow_redirects=False)

    assert first.headers["location"] == second.headers["location"]
    assert (
        len(session.scalars(select(Angle).where(Angle.plan_hook_id == hook.id)).all())
        == 1
    )


def test_writing_from_a_hook_accepts_it_so_a_recompute_cannot_take_it_away(
    web, session, mandate
):
    """The occasion hangs on the hook, so the hook has to outlive the recompute
    that would otherwise delete it as an untouched proposal."""
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=plan.month_key(signal.effective_at),
    )

    web.post(f"/client/{mandate.id}/plan/{hook.id}/text", follow_redirects=False)
    plan.recompute(session, mandate, invoke=_no_prose, now=dt.datetime.now(dt.UTC))

    session.expire_all()
    still = session.get(PlanHook, hook.id)
    assert still is not None
    assert still.state is HookState.ANGENOMMEN


def test_a_released_text_marks_its_hook_as_done_on_the_page(web, session, mandate):
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )
    web.post(f"/client/{mandate.id}/plan/{hook.id}/text", follow_redirects=False)
    angle = session.scalars(select(Angle).where(Angle.plan_hook_id == hook.id)).one()
    session.add(
        Asset(
            client_id=mandate.id,
            angle_id=angle.id,
            kind="statement",
            title="Statement",
            body="Zwei Absätze.",
            reviewed_by="claude",
            guide_reviewed_by="claude",
            released_at=_NOW,
            released_by="lucas",
        )
    )
    session.commit()

    words = _text(_at(web, mandate).text)

    assert "Erledigt" in words
    assert "Statement freigegeben" in words


def test_a_format_the_registry_does_not_know_is_not_offered_as_a_preselection(
    web, session, mandate
):
    """``PlanHook.format`` is a plain string column, so a row can outlive the
    definition it names. A tick on a row that does not exist would be a dead
    click, so nothing is sent."""
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    hook = _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1], format="rundschreiben",
    )

    opened = web.post(f"/client/{mandate.id}/plan/{hook.id}/text", follow_redirects=False)

    assert "format=" not in opened.headers["location"]


# --- The document ------------------------------------------------------------------


def test_the_plan_downloads_as_a_document_named_for_the_mandate_and_the_span(
    web, session, mandate
):
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )

    document = _at(web, mandate, "/plan.html")
    window = _window()

    assert document.status_code == 200
    disposition = document.headers["content-disposition"]
    assert "attachment" in disposition
    assert f"redaktionsplan_Solarhaus_AG_{window[0]}_{window[-1]}.html" in disposition


def test_the_downloaded_document_carries_no_link_back_into_the_application(
    web, session, mandate
):
    """The recipient has no account to follow one into, and a plan whose every
    second line is a dead in-app link reads as broken software."""
    signal = _signal(
        session, mandate, kind=SignalKind.STUDIE,
        published_at=_NOW, effective_at=_NOW + dt.timedelta(days=30),
    )
    _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )

    body = _at(web, mandate, "/plan.html").text

    assert 'href="/' not in body
    assert f"/client/{mandate.id}" not in body
    assert "/settings" not in body
    assert "/static/" not in body
    # What it does carry: the evidence named, and the source's own public page.
    assert f"Marktsignal {signal.id}" in _text(body)
    assert signal.url in body


def test_the_document_says_an_empty_month_is_empty_rather_than_skipping_it(
    web, session, mandate
):
    body = _at(web, mandate, "/plan.html").text
    words = _text(body)

    for month in _window():
        assert plan_view.month_name(month) in words
    assert "Kein datiertes Signal, kein Archivmuster" in words


def test_the_document_wears_the_mandates_own_identity(web, session, mandate):
    body = _at(web, mandate, "/plan.html").text

    assert "Redaktionsplan: Solarhaus AG" in _text(body)
    # The monogram stands in for a logo the mandate has not uploaded, the same
    # way the report and the Pressespiegel do it.
    assert "brandmark--mono" in body


# --- Language -----------------------------------------------------------------------


def test_every_string_the_plan_page_shows_is_translated(web, session, mandate):
    """The risk with a two-language UI is a *mixed* one. Asserted against the
    table rather than against a rendered page, so a string added to the template
    and forgotten here fails where it was added."""
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=30))
    _hook(
        session, mandate, source_kind=HookSource.MARKTSIGNAL, source_id=signal.id,
        month=_window()[1],
    )
    known = set(i18n.known_keys())

    missing = [
        text
        for text in (
            "Redaktionsplan",
            "Plan",
            "Neu berechnen",
            "Als Dokument",
            "Text schreiben",
            "Beleg",
            "Leer.",
            "Themen prüfen",
            "kein Haken",
            "laufender Monat",
            "ohne Tag",
            "Erledigt",
            "Verschieben",
            "Verwerfen",
            "Der Beleg zu diesem Haken ist nicht mehr auffindbar.",
            "Für diesen Mandanten lässt sich noch kein Plan bauen.",
            *(klasse.label for klasse in plan_view.KLASSEN),
            *plan_view.STATE_LABELS.values(),
            *(plan_view.month_name(month) for month in _WINDOW),
        )
        if text not in known
    ]

    assert not missing, f"untranslated: {missing}"


def test_the_page_switches_language_with_the_cookie(web, session, mandate):
    web.cookies.set(i18n.COOKIE_NAME, "en")

    words = _text(_at(web, mandate).text)

    assert "Editorial plan" in words
    assert "Recompute" in words


# --- Stand-in for the one model call ------------------------------------------------


def _no_prose(prompt: str, **kwargs) -> str:
    """The hook prose call, answered with nothing.

    A legal answer: a hook exists because of its evidence, with or without prose.
    Used by the recompute tests so nothing in this file reaches a model.
    """
    return '{"hooks": []}'
