"""Das Issue-Register als Seite (RIS-02, DEC-6 A): the list, the map, the buttons.

Nothing here reaches a model and nothing reaches the network — the engine is
pinned in ``test_issues.py``; this file is about the *page*: DEC-6 A's
list-first layout, DEC-3's two buttons wired to the routes, and the heatmap's
named column.

Three of these tests protect a promise rather than a layout:

* an issue missing either value stands in the named "Ohne Bewertung" column
  and never inside the field — a dot at the origin would claim a grading
  nobody made;
* the row shows *who* set Wahrscheinlichkeit and Wirkung, the same discipline
  the profile keeps for every researched value;
* a closed issue stays readable with its reason and all its signals.

Coverage is seeded relative to the real clock on purpose: the routes read the
clock the routes read, and a fixed calendar rots.
"""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import i18n, issues
from newspulse.matching import title_hash
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    Crisis,
    Issue,
    IssueDismissal,
    Tonality,
)
from newspulse.web.app import create_app, get_db
from newspulse.web.routes import issues_view


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


@pytest.fixture
def mandate(session) -> Client:
    client = Client(name="Solaris AG", aliases=["Solaris"], industry="Solarenergie")
    session.add(client)
    session.commit()
    return client


# --- Builders ---------------------------------------------------------------------

#: See ``test_issues._MATTER``: five significant tokens, one filler word per
#: outlet, so the copies cluster instead of deduping.
_MATTER = "Verbraucherschützer rügen Vertragsklauseln bei Solaranbieter Solaris"


def _slug(title: str, source: str) -> str:
    return hashlib.sha1(f"{title}|{source}".encode()).hexdigest()[:12]


def _cover(
    session, client: Client, *, source: str, word: str, days_ago: float
) -> Article:
    title = f"{_MATTER} {word}"
    at = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)
    article = Article(
        title=title,
        url=f"https://example.de/{_slug(title, source)}",
        source=source,
        published_at=at,
        fetched_at=at,
        summary_text="Eine kurze Zusammenfassung.",
        title_hash=title_hash(title, source),
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            is_relevant=True,
            summary="Zusammenfassung.",
            category=Category.SONSTIGES,
            relevance_score=7,
            importance_score=6,
            tonality=Tonality.NEGATIV,
            analyzed_at=at,
        )
    )
    session.commit()
    return article


def _repetition(session, mandate) -> tuple[Article, Article]:
    monday = _cover(session, mandate, source="FAZ", word="offiziell", days_ago=3)
    friday = _cover(session, mandate, source="WDR", word="erneut", days_ago=1)
    return monday, friday


def _opened(session, mandate) -> Issue:
    _monday, friday = _repetition(session, mandate)
    opened = issues.accept(session, mandate, friday, by="lucas")
    assert opened is not None
    return opened


# --- The page ---------------------------------------------------------------------


def test_the_empty_register_is_a_statement_not_a_missing_page(web, mandate):
    page = web.get(f"/client/{mandate.id}/issues")
    assert page.status_code == 200
    assert "Kein offenes Issue" in page.text


def test_the_tab_stands_in_the_workspace_strip(web, mandate):
    page = web.get(f"/client/{mandate.id}/heute")
    assert f'href="/client/{mandate.id}/issues"' in page.text


def test_a_benchmark_gets_no_register(web, session, mandate):
    rival = Client(name="Konkurrent GmbH", aliases=[], is_competitor=True)
    session.add(rival)
    session.commit()
    assert web.get(f"/client/{rival.id}/issues").status_code == 404


def test_the_offer_names_the_repetition_and_carries_both_buttons(web, session, mandate):
    """DEC-3's offer: what the repetition consists of, and the two answers."""
    _repetition(session, mandate)
    page = web.get(f"/client/{mandate.id}/issues")
    assert "Issue?" in page.text
    assert "Dieselbe Sache an" in page.text
    assert 'action="/issues/accept"' in page.text
    assert 'action="/issues/dismiss"' in page.text


def test_accepting_opens_the_row_with_age_movement_and_count(web, session, mandate):
    _monday, friday = _repetition(session, mandate)
    resp = web.post(
        "/issues/accept",
        data={
            "client_id": mandate.id,
            "article_id": friday.id,
            "redirect_to": f"/client/{mandate.id}/issues",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert session.scalar(select(func.count()).select_from(Issue)) == 1
    page = web.get(f"/client/{mandate.id}/issues")
    assert "Tage alt" in page.text or "Tag alt" in page.text
    assert "letzte Bewegung" in page.text
    assert "2" in page.text and "Signale" in page.text
    assert "eröffnet von" in page.text and "mensch" in page.text


def test_dismissing_silences_the_offer(web, session, mandate):
    _monday, friday = _repetition(session, mandate)
    web.post(
        "/issues/dismiss",
        data={
            "client_id": mandate.id,
            "article_id": friday.id,
            "redirect_to": f"/client/{mandate.id}/issues",
        },
        follow_redirects=False,
    )
    assert session.scalar(select(func.count()).select_from(IssueDismissal)) == 1
    page = web.get(f"/client/{mandate.id}/issues")
    assert "Issue?" not in page.text
    assert session.scalar(select(func.count()).select_from(Issue)) == 0


def test_dismiss_refuses_an_article_never_analyzed_for_the_mandate(
    web, session, factory, mandate
):
    """A forged pair must not pre-silence a repetition that was never shown."""
    other = Client(name="Andere AG", aliases=[])
    session.add(other)
    session.commit()
    _monday, friday = _repetition(session, mandate)
    web.post(
        "/issues/dismiss",
        data={"client_id": other.id, "article_id": friday.id, "redirect_to": "/"},
        follow_redirects=False,
    )
    assert session.scalar(select(func.count()).select_from(IssueDismissal)) == 0


# --- Grading on the row -------------------------------------------------------------


def test_grading_shows_the_value_with_the_person_who_set_it(web, session, mandate):
    opened = _opened(session, mandate)
    web.post(
        f"/issues/{opened.id}/grade",
        data={
            "probability": 4,
            "impact": 3,
            "redirect_to": f"/client/{mandate.id}/issues",
        },
        follow_redirects=False,
    )
    page = web.get(f"/client/{mandate.id}/issues")
    assert "gesetzt von" in page.text
    session.refresh(opened)
    assert opened.probability == 4
    assert opened.impact == 3
    assert opened.probability_set_by == "mensch"


def test_an_out_of_scale_grade_is_refused_without_a_write(web, session, mandate):
    opened = _opened(session, mandate)
    web.post(
        f"/issues/{opened.id}/grade",
        data={"probability": 9, "redirect_to": "/"},
        follow_redirects=False,
    )
    session.refresh(opened)
    assert opened.probability is None


def test_an_ungraded_row_says_not_set_rather_than_a_number(web, session, mandate):
    _opened(session, mandate)
    page = web.get(f"/client/{mandate.id}/issues")
    assert "noch nicht gesetzt" in page.text


# --- The heatmap's named column -----------------------------------------------------


def test_an_ungraded_issue_stands_in_the_named_column_not_the_field(
    web, session, mandate
):
    """The acceptance verbatim: one missing value puts the issue in a named
    column beside the field, never at its origin."""
    opened = _opened(session, mandate)
    issues.grade(session, opened, by="lucas", probability=3)  # impact still unset
    page = web.get(f"/client/{mandate.id}/issues")
    ungraded = page.text.split("data-ungraded", 1)[1]
    assert opened.title in ungraded
    field = page.text.split('aria-label="Heatmap"', 1)[1].split("data-ungraded")[0]
    assert "heat__dot" not in field


def test_a_fully_graded_issue_is_plotted_on_its_cell(web, session, mandate):
    opened = _opened(session, mandate)
    issues.grade(session, opened, by="lucas", probability=2, impact=5)
    page = web.get(f"/client/{mandate.id}/issues")
    cell = page.text.split('data-cell="2-5"', 1)[1].split("</div>")[0]
    assert "heat__dot" in cell
    ungraded = page.text.split("data-ungraded", 1)[1]
    assert opened.title not in ungraded


# --- Closing and escalating ---------------------------------------------------------


def test_closing_without_a_reason_keeps_the_row_open_and_says_why(
    web, session, mandate
):
    opened = _opened(session, mandate)
    resp = web.post(
        f"/issues/{opened.id}/close",
        data={"reason": "  ", "redirect_to": f"/client/{mandate.id}/issues"},
        follow_redirects=True,
    )
    assert "fehlt die Begründung" in resp.text
    session.refresh(opened)
    assert opened.closed_at is None


def test_a_closed_issue_stays_readable_with_reason_and_signals(web, session, mandate):
    opened = _opened(session, mandate)
    web.post(
        f"/issues/{opened.id}/close",
        data={"reason": "Thema abgeebbt.", "redirect_to": "/"},
        follow_redirects=False,
    )
    page = web.get(f"/client/{mandate.id}/issues")
    assert "Frühere Issues" in page.text
    assert "Thema abgeebbt." in page.text
    assert _MATTER in page.text  # the signals' headlines are still on the page


def test_escalating_lands_on_the_crisis_with_the_prehistory(web, session, mandate):
    """The button hands over: the crisis exists, the issue points at it, and
    the crisis page's timeline reaches back to the issue's own signals."""
    opened = _opened(session, mandate)
    resp = web.post(f"/issues/{opened.id}/escalate", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/client/{mandate.id}/krise"
    session.refresh(opened)
    declared = session.get(Crisis, opened.crisis_id)
    assert declared is not None
    timeline = web.get(f"/client/{mandate.id}/krise?zeitleiste=1")
    assert "Issue eröffnet" in timeline.text


def test_a_stale_issue_id_is_a_redirect_not_a_500(web, mandate):
    assert (
        web.post("/issues/999/escalate", follow_redirects=False).status_code == 303
    )
    assert (
        web.post(
            "/issues/999/grade", data={"probability": 3, "redirect_to": "/"},
            follow_redirects=False,
        ).status_code
        == 303
    )


# --- Language ---------------------------------------------------------------------


def test_every_visible_string_on_the_page_is_translated():
    """The acceptance criterion, mechanically: every ``t("...")`` literal in
    the register's templates has an English entry."""
    import re
    from pathlib import Path

    templates = Path(issues_view.__file__).resolve().parents[1] / "templates"
    literals: set[str] = set()
    for name in ("client_issues.html", "_client_tabs.html"):
        text = (templates / name).read_text(encoding="utf-8")
        literals |= set(re.findall(r"""\bt\(\s*"([^"]+)"\s*\)""", text))
        literals |= set(re.findall(r"""\bt\(\s*'([^']+)'\s*\)""", text))
    assert literals, "the scan found nothing — the pattern rotted"
    known = set(i18n.known_keys())
    missing = sorted(s for s in literals if s not in known)
    assert not missing, f"untranslated strings: {missing}"


def test_the_page_renders_in_english_when_asked(web, session, mandate):
    _opened(session, mandate)
    web.post("/language/en?next=/", follow_redirects=False)
    page = web.get(f"/client/{mandate.id}/issues")
    assert "Open issues" in page.text
    assert "Not graded" in page.text
    assert "last movement" in page.text
