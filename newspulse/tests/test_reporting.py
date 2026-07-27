"""Share of voice and the Excel deliverable (newspulse.reporting)."""

from __future__ import annotations

import datetime as dt
import io

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Analysis, Article, Base, Category, Client
from newspulse.reporting import client_workbook, share_of_voice


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _add(session, client, title, *, days_ago=1, relevance=5, alert=False, source="FAZ"):
    when = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)
    art = Article(
        title=title, url=f"https://ex.de/{title}", source=source, published_at=when,
        fetched_at=when, summary_text="s", language="de", title_hash=title[:8],
        author="Anna Muster",
    )
    session.add(art)
    session.flush()
    session.add(
        Analysis(
            article_id=art.id, client_id=client.id, summary="Zusammenfassung.",
            category=Category.FINANZEN, relevance_score=relevance,
            importance_score=7, is_alert=alert,
        )
    )


@pytest.fixture
def portfolio(session):
    mandate = Client(name="Alpha AG")
    rival = Client(name="Beta AG", is_competitor=True)
    session.add_all([mandate, rival])
    session.flush()
    _add(session, mandate, "A1", alert=True)
    _add(session, mandate, "A2")
    _add(session, mandate, "A3")
    _add(session, rival, "B1")
    session.commit()
    return mandate, rival


def test_share_of_voice_counts_mandates_and_competitors_together(session, portfolio):
    voice = share_of_voice(session, days=30)
    by_name = {v.name: v for v in voice}
    assert by_name["Alpha AG"].mentions == 3
    assert by_name["Beta AG"].mentions == 1
    assert by_name["Beta AG"].is_competitor is True
    assert by_name["Alpha AG"].alerts == 1


def test_shares_are_a_fraction_of_the_monitored_set(session, portfolio):
    voice = share_of_voice(session, days=30)
    assert sum(v.share for v in voice) == pytest.approx(1.0)
    assert {v.name: round(v.share, 2) for v in voice}["Alpha AG"] == 0.75


def test_share_of_voice_respects_the_window(session, portfolio):
    mandate, _ = portfolio
    _add(session, mandate, "OLD", days_ago=200)
    session.commit()
    assert {v.name: v.mentions for v in share_of_voice(session, days=30)}["Alpha AG"] == 3


def test_irrelevant_analyses_never_reach_a_client_facing_number(session, portfolio):
    mandate, _ = portfolio
    _add(session, mandate, "NOISE", relevance=0)
    session.commit()
    assert {v.name: v.mentions for v in share_of_voice(session, days=30)}["Alpha AG"] == 3


def test_workbook_has_the_report_and_summary_sheets(session, portfolio):
    mandate, _ = portfolio
    book = load_workbook(io.BytesIO(client_workbook(session, mandate, days=30)))
    assert book.sheetnames == ["Berichterstattung", "Nach Kategorie", "Nach Publisher"]
    sheet = book["Berichterstattung"]
    headers = [cell.value for cell in sheet[1]]
    assert "Schlagzeile" in headers and "Autor" in headers and "Link" in headers
    assert sheet.max_row == 4  # header + three articles


def test_workbook_for_an_empty_month_is_still_a_readable_sheet(session):
    """An empty month must produce a correctly-shaped file, not a blank one the
    recipient cannot open."""
    c = Client(name="Leer AG")
    session.add(c)
    session.commit()
    book = load_workbook(io.BytesIO(client_workbook(session, c, days=30)))
    assert [cell.value for cell in book["Berichterstattung"][1]][0] == "Datum"
