"""The KI-Sichtbarkeit page, and the weekly measurement riding the daily sweep.

Nothing here reaches a model and nothing here reaches the network. The stored
rows are written by hand — that is the point: this file is about what the page
*says* about a measurement, and the measurement itself is pinned in
``test_visibility.py`` against captured answers.

Three of these tests protect a wrong number rather than a broken page, and they
are the reason the rest exist:

* a provider that failed must render as "nicht gemessen" naming it, never as
  "nicht genannt" — the two are different facts about the same week, and only one
  of them is about the mandate;
* a question whose result did not change must be counted and not listed, because
  a movement panel that lists everything reports no movement at all;
* a mandate with one measurement must be told there is nothing to compare
  against, rather than shown a flat line it would read as stability.
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, i18n, job, visibility
from newspulse.models import (
    Base,
    Client,
    Run,
    RunStatus,
    VisibilityAnswer,
    VisibilityBand,
    VisibilityQuestion,
    VisibilityRun,
)
from newspulse.web.app import create_app, get_db
from newspulse.web.routes import visibility_view

_NOW = dt.datetime(2026, 8, 24, 9, 0, tzinfo=dt.UTC)
_WEEK = dt.timedelta(days=7)

_TEMPLATES = Path(visibility_view.__file__).resolve().parents[1] / "templates"

_CLAUDE = visibility.PROVIDER_CLAUDE
_GEMINI = visibility.PROVIDER_GEMINI

_AUSWAHL = "Welche Anbieter für Solaranlagen mit Speicher gibt es in Deutschland?"
_KATEGORIE = "Lohnt sich eine Photovoltaikanlage 2026 noch?"
_PROBLEM = "Was tun bei Verschattung auf dem Dach?"


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
def feature_on(monkeypatch):
    """The feature and its window, pinned so a test states what it relies on."""
    monkeypatch.setattr(config, "VISIBILITY_ENABLED", True)
    monkeypatch.setattr(config, "VISIBILITY_EVERY_DAYS", 7)


@pytest.fixture
def mandate(session) -> Client:
    """Enpal with one stored competitor, and one company that is neither."""
    client = Client(name="Enpal", aliases=["Enpal B.V."], industry="Solarenergie")
    rival = Client(name="Zolar", is_competitor=True)
    session.add_all([client, rival])
    session.flush()
    client.competitors.append(rival)
    session.commit()
    return client


# --- Builders ---------------------------------------------------------------------


def _question(
    session, client: Client, text: str, band: VisibilityBand = VisibilityBand.KATEGORIE
) -> VisibilityQuestion:
    row = VisibilityQuestion(
        client_id=client.id, text=text, band=band, accepted=True, accepted_at=_NOW
    )
    session.add(row)
    session.commit()
    return row


def _run(
    session,
    client: Client,
    *,
    at: dt.datetime,
    cells,
    asked=(_CLAUDE, _GEMINI),
    failed=(),
    unread: int = 0,
) -> VisibilityRun:
    """One stored measurement. ``cells`` is (question, provider, position, companies)."""
    run = VisibilityRun(
        client_id=client.id,
        ran_at=at,
        providers_asked=list(asked),
        providers_failed=list(failed),
        finished_at=at,
        answers_unread=unread,
    )
    for question, provider, position, companies, *rest in cells:
        run.answers.append(
            VisibilityAnswer(
                question_id=question.id,
                provider=provider,
                answer=f"Antwort auf {question.text}",
                named=position is not None,
                position=position,
                companies=list(companies),
                rivals=[c for c in companies if c == "Zolar"],
                sources=list(rest[0]) if rest else [],
            )
        )
    session.add(run)
    session.commit()
    return run


def _text(html: str) -> str:
    """The page's words: markup stripped, entities resolved, whitespace collapsed.

    Entities are resolved because the template writes ``&times;`` and ``&uarr;``,
    and a test asserting on what a reader sees must assert on what the browser
    renders rather than on the source spelling of it.
    """
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", html)).split())


# --- The tab and the page ---------------------------------------------------------


def test_the_tab_strip_links_to_the_visibility_page(web, session, mandate):
    """A tab, not a hidden URL: the page is part of the mandate's workspace."""
    page = web.get(f"/client/{mandate.id}/ki")

    assert page.status_code == 200
    assert f'href="/client/{mandate.id}/ki"' in page.text
    assert "KI-Sichtbarkeit" in _text(page.text)


def test_the_visibility_tab_is_marked_active_on_its_own_page(web, session, mandate):
    assert re.search(
        rf'href="/client/{mandate.id}/ki" class="active"', web.get(f"/client/{mandate.id}/ki").text
    )


def test_a_benchmark_has_no_visibility_page_of_its_own(web, session, mandate):
    """``is_competitor`` is a yardstick, and the sidebar and portfolio leave it out.

    A page of its own would invite somebody to accept a set for a company nobody
    reports to and spend a weekly measurement on it.
    """
    benchmark = session.scalar(select(Client).where(Client.is_competitor.is_(True)))

    assert web.get(f"/client/{benchmark.id}/ki").status_code == 404


def test_a_benchmark_still_appears_inside_the_mandates_ranking(web, session, mandate):
    """The exclusion is of the *page*, not of the company: a competitor named in an
    answer is exactly what the ranking exists to show."""
    question = _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    _run(
        session,
        mandate,
        at=_NOW,
        cells=[(question, _CLAUDE, 2, ["Zolar", "Enpal"])],
        asked=[_CLAUDE],
    )

    body = _text(web.get(f"/client/{mandate.id}/ki").text)

    assert "Zolar" in body
    assert "Wer den Markt besetzt" in body


# --- The standing -----------------------------------------------------------------


def test_the_page_states_the_share_of_questions_that_named_the_mandate(
    web, session, mandate
):
    """Four questions, three of which name Enpal on at least one provider: 75 %."""
    questions = [
        _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL),
        _question(session, mandate, _KATEGORIE),
        _question(session, mandate, _PROBLEM, VisibilityBand.PROBLEM),
        _question(session, mandate, "Wer baut Wärmepumpen ein?"),
    ]
    _run(
        session,
        mandate,
        at=_NOW,
        cells=[
            (questions[0], _CLAUDE, 2, ["Zolar", "Enpal"]),
            (questions[1], _CLAUDE, 1, ["Enpal"]),
            (questions[2], _CLAUDE, 3, ["Zolar", "EON Solar", "Enpal"]),
            (questions[3], _CLAUDE, None, ["Zolar"]),
        ],
        asked=[_CLAUDE],
    )

    body = _text(web.get(f"/client/{mandate.id}/ki").text)

    assert "75 %" in body
    assert "3 von 4 gemessenen Fragen" in body


def test_the_ranking_marks_the_mandate_among_the_companies_named(web, session, mandate):
    """The mandate stays in the same list as everybody else: its rank among the
    companies an assistant names *is* the finding."""
    questions = [
        _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL),
        _question(session, mandate, _KATEGORIE),
    ]
    _run(
        session,
        mandate,
        at=_NOW,
        cells=[
            (questions[0], _CLAUDE, 2, ["Zolar", "Enpal"]),
            (questions[1], _CLAUDE, None, ["Zolar", "EON Solar"]),
        ],
        asked=[_CLAUDE],
    )

    html = web.get(f"/client/{mandate.id}/ki").text

    # Zolar was named in both questions, Enpal in one, EON Solar in one.
    assert "Zolar" in html and "EON Solar" in html
    assert re.search(r'vis-bar__n vis-bar__n--us">\s*Enpal', html), (
        "the mandate's own row is not marked in the ranking"
    )
    assert "2 / 2" in _text(html) and "1 / 2" in _text(html)


def test_the_stated_sources_appear_with_their_counts(web, session, mandate):
    questions = [
        _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL),
        _question(session, mandate, _KATEGORIE),
    ]
    _run(
        session,
        mandate,
        at=_NOW,
        cells=[
            (questions[0], _CLAUDE, 1, ["Enpal"], ["Verbraucherzentrale", "Finanztip"]),
            (questions[1], _CLAUDE, 1, ["Enpal"], ["Verbraucherzentrale"]),
        ],
        asked=[_CLAUDE],
    )

    body = _text(web.get(f"/client/{mandate.id}/ki").text)

    assert "Verbraucherzentrale 2×" in body
    assert "Finanztip 1×" in body


# --- Movement ---------------------------------------------------------------------


def test_movement_lists_the_changed_question_and_counts_the_unchanged_ones(
    web, session, mandate
):
    """Two questions, one of which moved. The other is a number, not a row."""
    moved = _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    still = _question(session, mandate, _KATEGORIE)
    _run(
        session,
        mandate,
        at=_NOW - _WEEK,
        cells=[
            (moved, _CLAUDE, 3, ["Zolar", "EON Solar", "Enpal"]),
            (still, _CLAUDE, 1, ["Enpal"]),
        ],
        asked=[_CLAUDE],
    )
    _run(
        session,
        mandate,
        at=_NOW,
        cells=[
            (moved, _CLAUDE, 2, ["Zolar", "Enpal"]),
            (still, _CLAUDE, 1, ["Enpal"]),
        ],
        asked=[_CLAUDE],
    )

    body = _text(web.get(f"/client/{mandate.id}/ki").text)

    assert _AUSWAHL in body
    assert "1 von 2 Fragen verändert" in body
    assert "1 Frage(n) sind unverändert" in body
    assert _KATEGORIE not in body.split("Alle Fragen")[0], (
        "an unchanged question was listed in the movement panel instead of counted"
    )


def test_a_movement_names_what_changed_and_on_which_provider(web, session, mandate):
    question = _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    _run(
        session,
        mandate,
        at=_NOW - _WEEK,
        cells=[(question, _CLAUDE, 3, ["Zolar", "EON Solar", "Enpal"])],
        asked=[_CLAUDE],
    )
    _run(
        session,
        mandate,
        at=_NOW,
        cells=[(question, _CLAUDE, 2, ["Zolar", "Enpal"])],
        asked=[_CLAUDE],
    )

    body = _text(web.get(f"/client/{mandate.id}/ki").text)

    assert f"Position 3 auf 2 bei {_CLAUDE}." in body


def test_a_question_a_provider_failed_on_is_not_compared_as_a_loss(web, session, mandate):
    """The outage case, and the one this comparison exists to keep out.

    Gemini named the mandate last week and was down this week. Comparing the
    missing cell against the stored one would report "nicht mehr genannt" — an
    outage read as a loss, in the direction a client acts on.
    """
    question = _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    _run(
        session,
        mandate,
        at=_NOW - _WEEK,
        cells=[
            (question, _CLAUDE, 1, ["Enpal"]),
            (question, _GEMINI, 2, ["Zolar", "Enpal"]),
        ],
    )
    _run(
        session,
        mandate,
        at=_NOW,
        cells=[(question, _CLAUDE, 1, ["Enpal"])],
        failed=[_GEMINI],
    )

    body = _text(web.get(f"/client/{mandate.id}/ki").text)

    assert f"Bei {_GEMINI} nicht mehr genannt." not in body
    assert "Nichts hat sich verändert" in body


def test_one_measurement_says_there_is_nothing_to_compare_against(web, session, mandate):
    """A single point is not a direction, and a flat line through it reads as one."""
    question = _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    _run(session, mandate, at=_NOW, cells=[(question, _CLAUDE, 1, ["Enpal"])], asked=[_CLAUDE])

    html = web.get(f"/client/{mandate.id}/ki").text

    assert "100 %" in _text(html)
    assert "Erst eine Messung: es gibt noch nichts, wogegen sich das vergleichen ließe." in _text(html)
    assert "<polyline" not in html, "a trend was drawn from a single measurement"


def test_two_measurements_draw_the_trend(web, session, mandate):
    question = _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    _run(session, mandate, at=_NOW - _WEEK, cells=[(question, _CLAUDE, None, ["Zolar"])], asked=[_CLAUDE])
    _run(session, mandate, at=_NOW, cells=[(question, _CLAUDE, 1, ["Enpal"])], asked=[_CLAUDE])

    html = web.get(f"/client/{mandate.id}/ki").text

    assert "<polyline" in html
    assert "Erst eine Messung" not in _text(html)


# --- The three states of one cell -------------------------------------------------


def test_a_failed_provider_renders_as_nicht_gemessen_and_names_it(web, session, mandate):
    """The distinction the whole feature turns on, at the one place it is shown."""
    question = _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    _run(
        session,
        mandate,
        at=_NOW,
        cells=[(question, _CLAUDE, 1, ["Enpal"])],
        failed=[_GEMINI],
    )

    html = web.get(f"/client/{mandate.id}/ki").text
    body = _text(html)

    assert re.search(
        rf'vis-cell--unmeasured"><b>{_GEMINI}</b>\s*nicht gemessen', html
    ), "the provider that failed is not rendered as 'nicht gemessen' beside its name"
    assert f"{_GEMINI} nicht genannt" not in body


def test_an_answer_without_the_mandate_renders_as_nicht_genannt(web, session, mandate):
    question = _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    _run(
        session,
        mandate,
        at=_NOW,
        cells=[(question, _CLAUDE, None, ["Zolar"])],
        asked=[_CLAUDE],
    )

    html = web.get(f"/client/{mandate.id}/ki").text

    assert re.search(rf'vis-cell--unnamed"><b>{_CLAUDE}</b>\s*nicht genannt', html)
    assert "nicht gemessen" not in _text(html)


# --- The proposal flow ------------------------------------------------------------


def test_an_empty_set_shows_the_proposal_flow_rather_than_an_empty_chart(
    web, session, mandate
):
    body = _text(web.get(f"/client/{mandate.id}/ki").text)

    assert "Noch kein Fragensatz" in body
    assert "Fragen vorschlagen" in body
    assert "Wer den Markt besetzt" not in body


def test_proposing_renders_the_questions_and_stores_none_of_them(
    web, session, mandate, monkeypatch
):
    """The rule ``rivals.py`` set and this feature inherits: a click stores, a
    proposal does not."""
    offered = [
        visibility.Proposal(text=_AUSWAHL, band=VisibilityBand.AUSWAHL),
        visibility.Proposal(text=_PROBLEM, band=VisibilityBand.PROBLEM),
    ]
    monkeypatch.setattr(visibility, "propose", lambda session, client: offered)

    page = web.post(f"/client/{mandate.id}/ki/vorschlag")

    assert page.status_code == 200
    assert _AUSWAHL in _text(page.text)
    assert _PROBLEM in _text(page.text)
    assert session.scalars(select(VisibilityQuestion)).all() == []


def test_accepting_a_proposed_question_is_what_stores_it(web, session, mandate):
    answer = web.post(
        f"/client/{mandate.id}/ki/fragen",
        data={"text": _AUSWAHL, "band": "auswahl"},
        follow_redirects=False,
    )

    assert answer.status_code == 303
    stored = session.scalars(select(VisibilityQuestion)).all()
    assert [(row.text, row.band, row.accepted) for row in stored] == [
        (_AUSWAHL, VisibilityBand.AUSWAHL, True)
    ]


def test_rejecting_a_question_retires_it_and_keeps_what_it_measured(
    web, session, mandate
):
    """Retired, never deleted: the movement panel compares against a week whose
    questions still have to resolve."""
    question = _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    _run(session, mandate, at=_NOW, cells=[(question, _CLAUDE, 1, ["Enpal"])], asked=[_CLAUDE])

    answer = web.post(
        f"/client/{mandate.id}/ki/fragen/{question.id}/verwerfen", follow_redirects=False
    )

    assert answer.status_code == 303
    session.expire_all()
    assert session.get(VisibilityQuestion, question.id).accepted is False
    assert len(session.scalars(select(VisibilityAnswer)).all()) == 1


# --- The measurement inside the daily sweep ---------------------------------------


def test_the_sweep_measures_a_mandate_whose_window_is_open(session, mandate, monkeypatch):
    _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    measured: list[str] = []

    def _measure(open_session, client, *, now):
        measured.append(client.name)
        return VisibilityRun(client_id=client.id, ran_at=now, providers_asked=[_CLAUDE])

    monkeypatch.setattr(visibility, "measure", _measure)

    assert job._measure_visibility(session, [mandate], now=_NOW) == 1
    assert measured == ["Enpal"]


def test_the_sweep_spends_nothing_on_a_mandate_that_is_not_due(session, mandate, monkeypatch):
    """A mandate with no accepted question is not due, and no call is spent on it."""
    monkeypatch.setattr(
        visibility,
        "measure",
        lambda *args, **kwargs: pytest.fail("a measurement was spent on a mandate not due"),
    )

    assert job._measure_visibility(session, [mandate], now=_NOW) == 0


def test_the_sweep_leaves_a_benchmark_unmeasured(session, mandate, monkeypatch):
    benchmark = session.scalar(select(Client).where(Client.is_competitor.is_(True)))
    _question(session, benchmark, _AUSWAHL, VisibilityBand.AUSWAHL)
    monkeypatch.setattr(
        visibility,
        "measure",
        lambda *args, **kwargs: pytest.fail("a benchmark was measured"),
    )

    assert job._measure_visibility(session, [benchmark], now=_NOW) == 0


def test_a_failed_measurement_does_not_mark_the_sweep_degraded(session, mandate, monkeypatch):
    """A missed measurement is a missing figure on one tab, not a broken morning."""
    _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    run = Run(started_at=_NOW, finished_at=_NOW, status=RunStatus.OK, errors=[])
    session.add(run)
    session.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("claude is down")

    monkeypatch.setattr(visibility, "measure", _boom)

    assert job._measure_visibility(session, [mandate], now=_NOW) == 0
    session.expire_all()
    stored = session.get(Run, run.id)
    assert stored.status is RunStatus.OK
    assert stored.errors == []


def test_the_measurement_is_wired_into_the_sweeps_post_run_stages():
    """A stage nobody calls is a stage that never runs. Pinned by name, because
    that is exactly the wiring that has been forgotten before."""
    assert "_measure_visibility" in job._post_run.__code__.co_names


def test_the_measurement_is_skipped_when_the_feature_is_off(session, mandate, monkeypatch):
    _question(session, mandate, _AUSWAHL, VisibilityBand.AUSWAHL)
    monkeypatch.setattr(config, "VISIBILITY_ENABLED", False)
    monkeypatch.setattr(
        visibility,
        "measure",
        lambda *args, **kwargs: pytest.fail("a measurement ran with the feature off"),
    )

    assert job._measure_visibility(session, [mandate], now=_NOW) == 0


# --- Every German string on the page has an English one ---------------------------


def test_every_german_string_on_the_visibility_page_is_translated():
    """A page that switches its nav and keeps its panel heads German reads as
    broken, which is worse than a page wholly in one language."""
    known = set(i18n.known_keys())
    called = {
        match[1]
        for name in ("client_visibility.html", "_client_tabs.html")
        for match in re.findall(
            r"""t\(\s*("|')(.+?)\1\s*\)""", (_TEMPLATES / name).read_text(), re.S
        )
    }

    assert called, "the visibility template calls t() — the scan must not find none"
    assert not sorted(called - known), (
        "German strings on the visibility page with no English entry in i18n._EN: "
        f"{sorted(called - known)}"
    )
