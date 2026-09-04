"""The report surface and the artefact (newspulse.web.routes.report).

Interface-level: every test drives one route through ``TestClient`` against a
seeded in-memory database. Nothing here reaches a model — the one route that would
(generating a draft) goes through the module's injected ``_generate``, so the
whole path runs against a reply written in this file.

The load-bearing test is the one about a released report rendering identically
after the archive changes.
Everything else on the review surface is a control; that one is the promise the
feature makes to a client, and it is the only one that can be broken by a change
somewhere else entirely.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import i18n, job
from newspulse import report as reports
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    Report,
    ReportFindingKind,
    ReportState,
    Tonality,
)
from newspulse.report import Finding, ReportDraft
from newspulse.reporting import Period
from newspulse.web.app import create_app, get_db
from newspulse.web.routes import report as report_routes

#: The seeded month, fixed rather than relative to "now" so the arithmetic in the
#: assertions stays true next January. Bounded by *local* midnight, which is what
#: ``Period.month`` and therefore every route computes: a UTC-bounded July is a
#: different two hours of the archive on a Berlin host, and a fixture that used one
#: would test a period no button on the page can produce.
JULY = Period.month(2026, 7)

#: The document body, which is what the screen and the export must agree on. The
#: chrome around it — a back link on one, nothing on the other — is deliberately
#: outside it.
_DOC_BODY = re.compile(r'<article class="doc">.*</article>', re.DOTALL)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def http(factory):
    app = create_app()

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _piece(
    session,
    client,
    title,
    *,
    when=dt.datetime(2026, 7, 10, 9, 0, tzinfo=dt.UTC),
    source="Handelsblatt",
    tonality=Tonality.NEUTRAL,
) -> Analysis:
    article = Article(
        title=title,
        url=f"https://ex.de/{title}",
        source=source,
        published_at=when,
        fetched_at=when,
        summary_text="Zusammenfassung.",
        language="de",
        title_hash=title[:8],
    )
    session.add(article)
    session.flush()
    analysis = Analysis(is_relevant=True, 
        article_id=article.id,
        client_id=client.id,
        summary="Zusammenfassung.",
        category=Category.PRODUKT,
        relevance_score=5,
        importance_score=6,
        tonality=tonality,
    )
    session.add(analysis)
    session.flush()
    return analysis


@pytest.fixture
def seeded(factory):
    """One mandate with a comparison set, a seeded July, and a stored draft.

    Two findings: one kept, one already dropped, so the review surface's whole
    contract — a dropped finding stays visible, a kept one goes into the document —
    is exercised by every test that opens the page.
    """
    session = factory()
    mandate = Client(name="Arrakis AG", industry="Logistik")
    rival = Client(name="Caladan SE", industry="Logistik", is_competitor=True)
    session.add_all([mandate, rival])
    session.flush()
    mandate.competitors.append(rival)

    positive = _piece(
        session, mandate, "Arrakis liefert aus", tonality=Tonality.POSITIV
    )
    negative = _piece(
        session, mandate, "Arrakis unter Druck", source="FAZ", tonality=Tonality.NEGATIV
    )
    _piece(session, mandate, "Arrakis baut um")
    _piece(session, rival, "Caladan expandiert", source="Zeit")
    session.commit()

    stored = reports.store(
        session,
        mandate,
        ReportDraft(
            client_id=mandate.id,
            period=JULY,
            findings=(
                Finding(
                    kind=ReportFindingKind.SICHTBARKEIT,
                    claim="Arrakis war im Juli sichtbar.",
                    consequence="Die Frequenz sollte gehalten werden.",
                    evidence_ids=(positive.id, negative.id),
                    figures=("F1",),
                ),
                Finding(
                    kind=ReportFindingKind.RISIKO,
                    claim="Ein negativer Beitrag bleibt lokal.",
                    consequence="Beobachten, nicht reagieren.",
                    evidence_ids=(negative.id,),
                    figures=("F1",),
                ),
            ),
        ),
        now=dt.datetime(2026, 8, 1, 6, 10, tzinfo=dt.UTC),
    )
    dropped = stored.findings[1]
    dropped.kept = False
    dropped.drop_reason = "Zu dünn für den Jour fixe."
    session.commit()
    session.close()
    return {
        "client_id": mandate.id,
        "report_id": stored.id,
        "kept_id": stored.findings[0].id,
        "dropped_id": dropped.id,
        "positive_id": positive.id,
        "negative_id": negative.id,
    }


def _review(http, seeded) -> str:
    return http.get(f"/client/{seeded['client_id']}/berichte").text


def _document(http, seeded) -> str:
    return http.get(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}/dokument"
    ).text


def _body(html: str) -> str:
    match = _DOC_BODY.search(html)
    assert match, "the document has no <article class='doc'> body"
    return match.group(0)


# --- The review surface ----------------------------------------------------------


def test_the_review_surface_shows_each_finding_with_its_evidence(http, seeded):
    body = _review(http, seeded)
    assert "Arrakis war im Juli sichtbar." in body
    assert "Die Frequenz sollte gehalten werden." in body
    # The evidence, by headline, not by an id nobody can check.
    assert "Arrakis liefert aus" in body
    assert "Arrakis unter Druck" in body


def test_a_dropped_finding_stays_visible_as_dropped_with_its_reason(http, seeded):
    body = _review(http, seeded)
    assert "Ein negativer Beitrag bleibt lokal." in body, "a dropped finding vanished"
    assert "Verworfen" in body
    assert "Zu dünn für den Jour fixe." in body


def test_dropping_a_finding_records_the_reason_and_keeps_the_row(http, seeded, factory):
    http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}"
        f"/befund/{seeded['kept_id']}/verwerfen",
        data={"grund": "Der Anlass trägt keine Aussage."},
        follow_redirects=False,
    )
    with factory() as session:
        finding = session.get(Report, seeded["report_id"]).findings[0]
        assert finding.kept is False
        assert finding.drop_reason == "Der Anlass trägt keine Aussage."
        assert finding.claim == "Arrakis war im Juli sichtbar.", "the row was replaced"


def test_a_dropped_finding_can_be_taken_back_up(http, seeded, factory):
    http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}"
        f"/befund/{seeded['dropped_id']}/behalten",
        follow_redirects=False,
    )
    with factory() as session:
        finding = session.get(Report, seeded["report_id"]).findings[1]
        assert finding.kept is True
        assert finding.drop_reason == ""


def test_editing_a_finding_stores_the_new_wording_and_stamps_it(http, seeded, factory):
    http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}"
        f"/befund/{seeded['kept_id']}",
        data={
            "claim": "Arrakis war im Juli auffällig präsent.",
            "consequence": "Halten.",
        },
        follow_redirects=False,
    )
    with factory() as session:
        finding = session.get(Report, seeded["report_id"]).findings[0]
        assert finding.claim == "Arrakis war im Juli auffällig präsent."
        assert finding.consequence == "Halten."
        assert finding.edited_at is not None


def test_an_edit_stating_a_figure_the_tool_cannot_source_is_refused(
    http, seeded, factory
):
    """DEC-2 settled that reach is absent rather than estimated. A rule that only
    bound the model would be a rule the document does not have."""
    response = http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}"
        f"/befund/{seeded['kept_id']}",
        data={"claim": "Die Reichweite lag bei 1,2 Millionen.", "consequence": ""},
    )
    assert "reichweite" in response.text.lower()
    with factory() as session:
        finding = session.get(Report, seeded["report_id"]).findings[0]
        assert finding.claim == "Arrakis war im Juli sichtbar.", "the edit went through"


def test_an_edit_that_empties_the_claim_is_refused(http, seeded, factory):
    http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}"
        f"/befund/{seeded['kept_id']}",
        data={"claim": "   ", "consequence": "Halten."},
    )
    with factory() as session:
        assert (
            session.get(Report, seeded["report_id"]).findings[0].claim
            == "Arrakis war im Juli sichtbar."
        )


@pytest.mark.parametrize("zeitraum", ["0000-01", "9999-12", "2026-13", "2026-00", "x"])
def test_a_hand_edited_period_lands_on_last_month_rather_than_a_500(
    http, seeded, zeitraum
):
    """The pattern is not the whole of "readable": ``9999-12`` matches it and still
    leaves the calendar, because the exclusive end of that month is a year that
    does not exist."""
    response = http.get(
        f"/client/{seeded['client_id']}/berichte", params={"zeitraum": zeitraum}
    )
    assert response.status_code == 200


def test_the_period_picker_shows_the_month_the_page_is_showing(http, seeded, factory):
    """With no period asked for the page shows the newest report the mandate has,
    which can be older than the year the picker offers. The picker then has to name
    it, or the draft button would draft a month nobody is looking at."""
    with factory() as session:
        row = session.get(Report, seeded["report_id"])
        old = Period.month(2019, 3)
        row.period_start, row.period_end = old.start, old.end
        session.commit()
    body = _review(http, seeded)
    assert '<option value="2019-03" selected>' in body


def test_the_report_is_reachable_from_the_texte_tab_not_a_tab_of_its_own(http, seeded):
    """Berichte and Impulse became one tab.

    They were two, and read as two products doing the same thing at different
    rhythms — RauteOS drafts, the consultant keeps or discards, what is left is
    handed over. The strip now carries "Texte", and the rail on that page is
    where an occasion or a month is chosen. So the report is one click further
    away from the guide page and no click further from the work.
    """
    client_id = seeded["client_id"]

    # On the href, not on the word: "Berichte" is a substring of
    # "Berichterstattung", which the same page prints for its own reasons.
    strip = http.get(f"/client/{client_id}/guide").text
    tabs = strip.split('class="subtabs"', 1)[1].split("</nav>", 1)[0]
    assert f'/client/{client_id}/berichte' not in tabs, "no tab of its own any more"
    assert ">Texte<" in tabs
    assert f'/client/{client_id}/advice' in tabs

    rail = http.get(f"/client/{client_id}/advice").text
    assert f'/client/{client_id}/berichte' in rail, "the rail carries the period"


# --- Generating -------------------------------------------------------------------


#: The claim the stubbed generator returns, named so the assertions can check for
#: it without a copy of the sentence in each of them.
_CLAIM = "Der Juli war ein sichtbarer Monat."


def _reply(**overrides) -> str:
    finding = {
        "kind": "sichtbarkeit",
        "claim": _CLAIM,
        "consequence": "Die Frequenz halten.",
        "figures": ["F1"],
    }
    finding.update(overrides)
    return json.dumps({"findings": [finding]})


def test_generating_stores_a_draft_for_the_chosen_period(
    http, seeded, factory, monkeypatch
):
    monkeypatch.setattr(report_routes, "_generate", lambda prompt, **kw: _reply())
    response = http.post(
        f"/client/{seeded['client_id']}/berichte/erzeugen",
        data={"zeitraum": "2026-07"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with factory() as session:
        client = session.get(Client, seeded["client_id"])
        stored = reports.for_period(session, client.id, JULY)
        assert [f.claim for f in stored.findings] == [_CLAIM]
        assert stored.state is ReportState.ENTWURF


def test_a_failed_generation_says_so_rather_than_redirecting(
    http, seeded, monkeypatch
):
    """"Nothing to say about this month" and "the call failed" must not look alike
    to somebody about to send a document."""
    monkeypatch.setattr(report_routes, "_generate", lambda prompt, **kw: "not json")
    response = http.post(
        f"/client/{seeded['client_id']}/berichte/erzeugen", data={"zeitraum": "2026-07"}
    )
    assert response.status_code == 200
    assert "fehlgeschlagen" in response.text


# --- The document -----------------------------------------------------------------


def test_the_document_renders_only_the_kept_findings(http, seeded):
    body = _body(_document(http, seeded))
    assert "Arrakis war im Juli sichtbar." in body
    assert "Ein negativer Beitrag bleibt lokal." not in body


def test_the_document_carries_the_period_the_comparison_set_and_the_date(http, seeded):
    body = _body(_document(http, seeded))
    # The exclusive end printed as the last day it contains: a report headed
    # "1.7. bis 1.8." reads as covering a day it does not.
    assert "01.07.2026 bis 31.07.2026" in body
    assert "Erstellt am" in body
    assert "Caladan SE" in body, "the comparison set is not on the document"
    assert "Arrakis liefert aus" in body, "a finding lost its evidence"


def test_every_chart_has_a_table_view_and_every_segment_carries_its_own_figure(
    http, seeded
):
    """No value may be readable only as a colour: the split is checked before it
    goes to a client, sometimes on a black and white printout."""
    body = _body(_document(http, seeded))
    charts = body.count('<figure class="chart">')
    assert charts == 2, "expected the tonality and the share-of-voice chart"
    assert body.count("<details") == charts, "a chart has no table view"
    assert body.count("<table>") == charts
    # One legend entry per tonality, each with its own count beside its name.
    for tone in ("positiv", "neutral", "negativ"):
        assert f"sw--{tone}" in body
        assert f"seg--{tone}" in body
    assert "<b>1</b>" in body, "a tonality segment carries no figure of its own"


def test_the_share_of_voice_chart_prints_the_frozen_figure_and_its_complement(
    http, seeded, factory
):
    """One formatter for both halves of a bar, and the mandate's half is the one
    the tile above it already froze — a released document may not carry two
    renderings of the same number."""
    with factory() as session:
        client = session.get(Client, seeded["client_id"])
        row = session.get(Report, seeded["report_id"])
        document = reports.document(session, client, row)
    chart = report_routes._voice_chart(document)
    mandate, field = chart.segments
    frozen = next(f for f in document.figures if f.key == "share_of_voice")
    assert mandate.text == frozen.text
    assert field.text == reports.share_text(1 - frozen.value)
    body = _body(_document(http, seeded))
    assert frozen.text in body


def test_a_mandate_name_is_never_translated_on_its_own_report(http, factory, seeded):
    """A client called "Bericht" would otherwise be renamed to "Report" on an
    English page: the mandate's name is client data, not chrome."""
    with factory() as session:
        session.get(Client, seeded["client_id"]).name = "Bericht"
        session.commit()
    http.cookies.set(i18n.COOKIE_NAME, "en")
    body = _body(_document(http, seeded))
    # The chart's own chrome did switch, so this is not a page that simply failed
    # to translate — only the name held.
    assert "Show table" in body
    assert 'sw--mandat"></span>Bericht ' in body


def test_a_finding_a_person_rewrote_says_so_on_the_document(http, seeded):
    """The frozen finding carries whether it was edited, and a field written into
    every snapshot and shown on none of them is a promise the artefact breaks."""
    assert "redaktionell bearbeitet" not in _body(_document(http, seeded))
    http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}"
        f"/befund/{seeded['kept_id']}",
        data={"claim": "Arrakis war im Juli deutlich sichtbar.", "consequence": "Halten."},
        follow_redirects=False,
    )
    assert "redaktionell bearbeitet" in _body(_document(http, seeded))


def test_the_document_makes_no_external_request(http, seeded):
    """The vendored-assets rule, stricter here: this file is forwarded, and one
    that phoned home would tell a third party who reads the agency's reporting."""
    body = _document(http, seeded)
    for forbidden in ("<script", "<img", "stylesheet", "@import", "/static/"):
        assert forbidden not in body, forbidden


def test_the_export_carries_the_same_document_as_the_screen(http, seeded):
    screen = http.get(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}/dokument"
    )
    export = http.get(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}/dokument.html"
    )
    assert "attachment" in export.headers["content-disposition"]
    assert "bericht_Arrakis_AG_2026-07.html" in export.headers["content-disposition"]
    assert _body(export.text) == _body(screen.text)


def test_a_report_of_another_mandate_is_not_reachable_under_this_one(
    http, seeded, factory
):
    with factory() as session:
        other = Client(name="Giedi Prime GmbH")
        session.add(other)
        session.commit()
        other_id = other.id
    assert (
        http.get(f"/client/{other_id}/berichte/{seeded['report_id']}/dokument").status_code
        == 404
    )


# --- The release, and the freeze ---------------------------------------------------


def _release(http, seeded, *, by="Lucas") -> None:
    response = http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}/freigeben",
        data={"freigegeben_von": by},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_releasing_stamps_the_report_and_freezes_it(http, seeded, factory):
    _release(http, seeded)
    with factory() as session:
        row = session.get(Report, seeded["report_id"])
        assert row.state is ReportState.FREIGEGEBEN
        assert row.released_by == "Lucas"
        assert row.snapshot, "a released report was not frozen"


def test_a_released_report_renders_identically_after_the_archive_changes(
    http, seeded, factory
):
    """The promise the feature makes: a document a client has been sent still says
    next quarter what it said when it was sent."""
    _release(http, seeded)
    before = _body(_document(http, seeded))

    with factory() as session:
        # Re-triage: one of the cited pieces is dismissed, and the month gains
        # coverage it did not have when the report went out. Both would move every
        # figure and the evidence list of a draft.
        cited = session.get(Analysis, seeded["positive_id"])
        cited.dismissed_at = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
        client = session.get(Client, seeded["client_id"])
        for extra in range(3):
            _piece(session, client, f"Nachtrag {extra}", tonality=Tonality.NEGATIV)
        session.commit()

    assert _body(_document(http, seeded)) == before
    assert "Arrakis liefert aus" in before, "the frozen evidence is not on the page"
    assert "Nachtrag 0" not in _body(_document(http, seeded))


def test_a_released_report_is_never_told_its_findings_left_the_document(
    http, seeded, factory
):
    """The review page renders a released report from the frozen figures, so it may
    not carry a live reading of the evidence beside them: "steht nicht im Dokument"
    about a finding the document still prints is simply false."""
    _release(http, seeded)
    with factory() as session:
        for name in ("positive_id", "negative_id"):
            session.get(Analysis, seeded[name]).dismissed_at = dt.datetime(
                2026, 9, 1, tzinfo=dt.UTC
            )
        session.commit()
    body = _review(http, seeded)
    assert "steht nicht im Dokument" not in body
    assert "nicht mehr in der Berichterstattung" not in body
    # And the document it is talking about still prints the finding.
    assert "Arrakis war im Juli sichtbar." in _body(_document(http, seeded))


def test_a_report_released_before_the_snapshot_existed_freezes_on_first_read(
    http, seeded, factory
):
    """``release`` returns early on an already-released row, and the column was
    added after releasing existed. A row stamped without one would otherwise read
    off the live archive forever — unfrozen, silently."""
    with factory() as session:
        row = session.get(Report, seeded["report_id"])
        row.state = ReportState.FREIGEGEBEN
        row.released_at = dt.datetime(2026, 8, 2, 9, 0, tzinfo=dt.UTC)
        row.released_by = "Lucas"
        row.snapshot = None
        session.commit()

    before = _body(_document(http, seeded))
    with factory() as session:
        assert session.get(Report, seeded["report_id"]).snapshot, "still not frozen"
        session.get(Analysis, seeded["positive_id"]).dismissed_at = dt.datetime(
            2026, 9, 1, tzinfo=dt.UTC
        )
        session.commit()
    assert _body(_document(http, seeded)) == before


def test_the_review_surface_renders_a_report_stamped_without_a_date(
    http, seeded, factory
):
    """``is_released`` answers yes to a row labelled freigegeben with no stamp, so
    that a torn state fails closed. The page must then not fail open on a date
    filter that has nothing to format."""
    with factory() as session:
        row = session.get(Report, seeded["report_id"])
        row.state = ReportState.FREIGEGEBEN
        session.commit()
    body = _review(http, seeded)
    assert "Freigegeben" in body
    assert "/verwerfen" not in body


def test_a_draft_by_contrast_notices_that_its_ground_moved(http, seeded, factory):
    """The other half of the same rule: before the release, the report is a reading
    of the archive and re-reads it."""
    with factory() as session:
        cited = session.get(Analysis, seeded["positive_id"])
        cited.dismissed_at = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
        session.commit()
    body = _review(http, seeded)
    assert "Arrakis liefert aus" not in body, "a dismissed article still resolves"
    assert "nicht mehr in der Berichterstattung" in body


@pytest.mark.parametrize(
    "suffix,payload",
    [
        ("", {"claim": "Neu.", "consequence": ""}),
        ("/verwerfen", {"grund": "egal"}),
        ("/behalten", {}),
    ],
)
def test_a_released_report_cannot_be_edited_kept_or_dropped(
    http, seeded, factory, suffix, payload
):
    _release(http, seeded)
    response = http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}"
        f"/befund/{seeded['kept_id']}{suffix}",
        data=payload,
    )
    assert response.status_code == 409
    with factory() as session:
        finding = session.get(Report, seeded["report_id"]).findings[0]
        assert finding.claim == "Arrakis war im Juli sichtbar."
        assert finding.kept is True


def test_a_released_report_cannot_be_regenerated(http, seeded, factory, monkeypatch):
    _release(http, seeded)
    monkeypatch.setattr(
        report_routes,
        "_generate",
        lambda prompt, **kw: pytest.fail("a released report cost a model call"),
    )
    response = http.post(
        f"/client/{seeded['client_id']}/berichte/erzeugen", data={"zeitraum": "2026-07"}
    )
    assert response.status_code == 200
    assert "freigegeben" in response.text
    with factory() as session:
        assert [f.claim for f in session.get(Report, seeded["report_id"]).findings] == [
            "Arrakis war im Juli sichtbar.",
            "Ein negativer Beitrag bleibt lokal.",
        ]


def test_the_review_surface_offers_no_controls_once_released(http, seeded):
    assert "Freigeben" in _review(http, seeded)
    _release(http, seeded)
    body = _review(http, seeded)
    assert "Freigegeben" in body
    assert "/verwerfen" not in body
    assert "/behalten" not in body


# --- The scheduled draft (DEC-3 B) -------------------------------------------------


def test_the_sweep_drafts_last_months_report_for_every_mandate(
    factory, no_monthly_report_draft
):
    """"Zum Stichtag vorbereitet, nie verschickt": the work is done before it is
    needed, and nothing goes anywhere without a release."""
    draft_reports = no_monthly_report_draft
    with factory() as session:
        mandate = Client(name="Arrakis AG")
        rival = Client(name="Caladan SE", is_competitor=True)
        session.add_all([mandate, rival])
        session.flush()
        _piece(session, mandate, "Arrakis liefert aus")
        _piece(session, rival, "Caladan expandiert")
        session.commit()

        drafted = draft_reports(
            session,
            [mandate, rival],
            now=dt.datetime(2026, 8, 1, 6, 10, tzinfo=dt.UTC),
            generate=lambda prompt, **kw: _reply(),
        )
        assert drafted == 1, "a competitor was given a report of its own"
        stored = reports.for_period(session, mandate.id, JULY)
        assert stored.state is ReportState.ENTWURF
        assert stored.released_at is None, "a scheduled draft released itself"
        assert [f.claim for f in stored.findings] == [_CLAIM]


def test_the_scheduled_draft_does_not_replace_a_report_that_exists(
    factory, seeded, no_monthly_report_draft
):
    with factory() as session:
        client = session.get(Client, seeded["client_id"])
        drafted = no_monthly_report_draft(
            session,
            [client],
            now=dt.datetime(2026, 8, 1, 6, 10, tzinfo=dt.UTC),
            generate=lambda prompt, **kw: pytest.fail("an existing report was redrafted"),  # noqa: E501
        )
        assert drafted == 0


def test_the_scheduled_draft_stops_after_the_window(factory, no_monthly_report_draft):
    with factory() as session:
        mandate = Client(name="Arrakis AG")
        session.add(mandate)
        session.flush()
        _piece(session, mandate, "Arrakis liefert aus")
        session.commit()
        drafted = no_monthly_report_draft(
            session,
            [mandate],
            now=dt.datetime(2026, 8, 20, 6, 10, tzinfo=dt.UTC),
            generate=lambda prompt, **kw: pytest.fail("drafted outside the window"),
        )
        assert drafted == 0


@pytest.mark.parametrize("failing_first", [True, False])
def test_a_failing_report_draft_never_fails_the_sweep(
    factory, no_monthly_report_draft, caplog, failing_first
):
    """A report is not worth a failed sweep: the failure is logged, the transaction
    is rolled back, and the next mandate is tried.

    Both orders, because they fail differently. Failure-then-success only shows
    that a rollback leaves the *next* mandate a usable session. Success-then-
    failure is the one that would catch a rollback reaching backwards and taking
    an already-written report with it — it does not, because ``report.store``
    commits each draft as it is made, and this is the test that says so.
    """
    with factory() as session:
        broken = Client(name="Arrakis AG")
        sound = Client(name="Ix AG")
        session.add_all([broken, sound])
        session.flush()
        _piece(session, broken, "Arrakis liefert aus")
        _piece(session, sound, "Ix liefert aus")
        session.commit()

        def _explode(prompt, **kw):
            if "Arrakis" in prompt:
                raise RuntimeError("the model was unreachable")
            return _reply()

        order = [broken, sound] if failing_first else [sound, broken]
        drafted = no_monthly_report_draft(
            session,
            order,
            now=dt.datetime(2026, 8, 1, 6, 10, tzinfo=dt.UTC),
            generate=_explode,
        )
        assert drafted == 1, "one mandate's failure took the other down with it"
        assert reports.for_period(session, broken.id, JULY) is None
        assert reports.for_period(session, sound.id, JULY) is not None


def test_the_sweep_survives_a_broken_report_step(factory, monkeypatch):
    """The outer boundary: even a failure outside any one mandate leaves the run ok."""

    def _boom(*args, **kwargs):
        raise RuntimeError("the period arithmetic broke")

    monkeypatch.setattr(job, "_draft_reports", _boom)
    with factory() as session:
        session.add(Client(name="Arrakis AG"))
        session.commit()
        report = job.run(session, feeds=[], analyzer=None)
    assert report.status is not job.RunStatus.FAILED


# --- Language ----------------------------------------------------------------------


#: Every literal string the two new templates hand to ``t()``. The dynamic calls
#: — a metric label, a finding kind, a tonality — are checked separately below,
#: against the value sets they come from.
_LITERAL_T = re.compile(r"""\bt\(\s*["'](.+?)["']\s*\)""", re.DOTALL)


@pytest.mark.parametrize("name", ["report_review.html", "report_document.html"])
def test_every_german_string_in_the_new_templates_is_translated(name):
    """The criterion, checked against the templates rather than against a list
    somebody has to remember to extend."""
    from newspulse.web import app as web_app

    source = (web_app._TEMPLATES_DIR / name).read_text("utf-8")
    known = set(i18n.known_keys())
    missing = [text for text in _LITERAL_T.findall(source) if text not in known]
    assert not missing, missing


def test_every_label_the_report_renders_dynamically_is_translated():
    """The labels that reach ``t()`` as a value rather than as a literal: a new
    metric or a fifth finding kind must not silently render as its German value."""
    from newspulse.reporting import Direction, MetricKey, MetricValue

    # Membership rather than "translates to something else": "neutral" is the same
    # word in both languages, and a missing entry must still fail here.
    known = set(i18n.known_keys())
    for kind in ReportFindingKind:
        assert report_routes._KIND_LABELS[kind.value] in known, kind
    for key in MetricKey:
        assert MetricValue(key=key, client_id=1, figure=1.0).label in known, key
    for tone in Tonality:
        assert tone.value in known, tone
    # The word under a figure that says which way it moved. Rendered from the
    # frozen document rather than composed into a German phrase, so it reaches
    # ``t()`` as a value like the three sets above.
    for direction in Direction:
        assert direction.value in known, direction


#: Every German sentence the route module owns: the empty state, the four
#: refusals, and the chart chrome. They reach the page through ``t()`` as values
#: rather than as literals a template scan could see, so a fifth chart or a
#: reworded refusal would otherwise ship half-translated and pass the suite.
_ROUTE_STRINGS = (
    "_NO_REPORT",
    "_ERR_RELEASED",
    "_ERR_GENERATION_FAILED",
    "_ERR_EMPTY_CLAIM",
    "_ERR_FORBIDDEN_FIGURE",
)


def test_every_german_string_the_route_module_owns_is_translated(http, seeded, factory):
    known = set(i18n.known_keys())
    missing = [
        name for name in _ROUTE_STRINGS if getattr(report_routes, name) not in known
    ]
    assert not missing, missing
    # And the chart chrome, taken off charts actually built from a document rather
    # than off the literals in the source: a title, a unit and every segment label
    # the page will translate.
    with factory() as session:
        client = session.get(Client, seeded["client_id"])
        row = session.get(Report, seeded["report_id"])
        document = reports.document(session, client, row)
    charts = [
        report_routes._tonality_chart(document),
        report_routes._voice_chart(document),
    ]
    assert any(charts), "no chart was built, so nothing was checked"
    for chart in [c for c in charts if c]:
        assert chart.title in known, chart.title
        assert chart.unit in known, chart.unit
        for segment in chart.segments:
            if segment.translated:
                assert segment.label in known, segment.label


def test_every_new_german_string_has_an_english_entry(http, seeded):
    """A *mixed* page is the failure this guards: half the chrome switches and half
    does not, which reads as broken in a way a fully German page does not."""
    http.cookies.set(i18n.COOKIE_NAME, "en")
    review = _review(http, seeded)
    # "Bericht erzeugen" moved under the empty state, where the sentence
    # explaining why the page is blank is — this fixture has a report, so what
    # the bar offers here is the redraw.
    for english in ("Reports", "Released", "Dropped", "Regenerate"):
        assert english in review, english
    for german in ("Freigeben", "Verworfen,", "Neu erzeugen"):
        assert german not in review, german

    document = _document(http, seeded)
    for english in ("Figures", "Findings", "Comparison set", "Show table"):
        assert english in document, english
    for german in ("Kennzahlen", "Befunde", "Vergleichsumfeld", "Tabelle anzeigen"):
        assert german not in document, german
    # The findings themselves stay in the language they were written in.
    assert "Arrakis war im Juli sichtbar." in document


# --- The findings as a tick list ------------------------------------------------


def test_one_pass_settles_every_finding(http, seeded, factory):
    """"Behalten, ändern oder verwerfen — was übrig bleibt, wird das Dokument."

    The page said that in its lead and then asked it once per finding, with a
    page load between each. A tick list is the same sentence, answered once: the
    ticked one is kept and the one already out is taken back up, in a single pass.
    """
    http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}/befunde",
        data={"behalten": [seeded["dropped_id"]]},
        follow_redirects=False,
    )

    with factory() as session:
        by_id = {f.id: f for f in session.get(Report, seeded["report_id"]).findings}
    assert by_id[seeded["dropped_id"]].kept is True, "ticked, so back in"
    assert by_id[seeded["dropped_id"]].drop_reason == "", "and why it was out is gone"
    assert by_id[seeded["kept_id"]].kept is False, "unticked, so out"


def test_an_empty_selection_is_a_decision_and_not_an_oversight(http, seeded, factory):
    """What makes a tick list read as the document: absence decides.

    A form that only ever adds could never drop the last finding, and the reader
    who cleared every box would be told nothing happened.
    """
    http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}/befunde",
        data={},
        follow_redirects=False,
    )

    with factory() as session:
        findings = session.get(Report, seeded["report_id"]).findings
    assert [f.kept for f in findings] == [False, False]


def test_the_reason_is_read_only_for_what_is_actually_dropped(http, seeded, factory):
    """A reason under a kept finding is a note about a decision that was reversed,
    and printing it would contradict the page."""
    http.post(
        f"/client/{seeded['client_id']}/berichte/{seeded['report_id']}/befunde",
        data={
            "behalten": [seeded["kept_id"]],
            f"grund-{seeded['kept_id']}": "Sollte nie gelesen werden",
            f"grund-{seeded['dropped_id']}": "Trägt keine Aussage",
        },
        follow_redirects=False,
    )

    with factory() as session:
        by_id = {f.id: f for f in session.get(Report, seeded["report_id"]).findings}
    assert by_id[seeded["kept_id"]].drop_reason == ""
    assert by_id[seeded["dropped_id"]].drop_reason == "Trägt keine Aussage"


def test_a_released_report_draws_no_boxes(http, seeded, factory):
    """The frozen document is what it is; a tick there would promise an edit that
    the release deliberately made impossible."""
    from newspulse import report as reports

    with factory() as session:
        row = session.get(Report, seeded["report_id"])
        reports.release(session, row, by="Lucas")

    body = _review(http, seeded)

    assert 'name="behalten"' not in body
    assert "Auswahl speichern" not in body


def test_a_draft_draws_one_box_per_finding_and_one_button(http, seeded):
    """The positive half of the released test above, so neither can pass alone.

    The boxes reach the form at the foot by ``form=`` rather than wrapping the
    list, which keeps the findings a list of articles rather than a list of
    forms — and is what lets the reason field for a row travel with them.
    """
    body = _review(http, seeded)

    assert body.count('name="behalten"') == 2, "one per finding, kept and dropped"
    assert body.count("Auswahl speichern") == 1
    assert f'form="findings-{seeded["report_id"]}"' in body
    # The per-finding buttons are gone: that decision is the tick now.
    assert "Wieder aufnehmen" not in body
