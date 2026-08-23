"""Findings, each carrying its evidence (newspulse.report).

The safety argument of this layer is an inversion, and these tests are about that
inversion rather than about prose quality: the model names which *figures* a claim
stands on, the code attaches the rows underneath it, and anything that comes back
without rows never reaches a human.

No test here reaches a model. ``generate`` is injected everywhere, so the whole
path runs against a reply written in the test.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import outreach as ledger
from newspulse import report as report_module
from newspulse import reporting
from newspulse.analyzer import ParseError
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Base,
    Category,
    Client,
    Outreach,
    Report,
    ReportFinding,
    ReportFindingKind,
    ReportState,
    Tonality,
)
from newspulse.report import (
    Finding,
    ReportDraft,
    ReportReleased,
    build_prompt,
    citable_figures,
    findings,
    for_period,
    release,
    resolve,
    store,
)
from newspulse.reporting import MetricKey, Period

# The seeded month, fixed rather than relative to "now" so the arithmetic in the
# assertions stays true next January. The same July test_report_metrics.py seeds.
JULY = Period(
    dt.datetime(2026, 7, 1, tzinfo=dt.UTC), dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
)
AUGUST = Period(
    dt.datetime(2026, 8, 1, tzinfo=dt.UTC), dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _piece(
    session,
    client,
    title,
    *,
    when=dt.datetime(2026, 7, 10, 9, 0, tzinfo=dt.UTC),
    source="Handelsblatt",
    author="",
    tonality=Tonality.NEUTRAL,
    summary="Zusammenfassung.",
) -> Analysis:
    article = Article(
        title=title,
        url=f"https://ex.de/{title}",
        source=source,
        published_at=when,
        fetched_at=when,
        summary_text=summary,
        language="de",
        title_hash=title[:8],
        author=author,
    )
    session.add(article)
    session.flush()
    analysis = Analysis(
        article_id=article.id,
        client_id=client.id,
        summary=summary,
        category=Category.PRODUKT,
        relevance_score=5,
        importance_score=6,
        tonality=tonality,
    )
    session.add(analysis)
    session.flush()
    return analysis


def _letter(session, client, journalist, *, released):
    angle = Angle(
        client_id=client.id, subject="Impuls", message="Text", context="Kontext"
    )
    session.add(angle)
    session.flush()
    row = Outreach(
        angle_id=angle.id,
        client_id=client.id,
        journalist=journalist,
        outlet="Handelsblatt",
        subject="Betreff",
        message="Nachricht",
    )
    session.add(row)
    session.flush()
    ledger.release(session, row, at=released)
    session.commit()
    return row


@pytest.fixture
def mandate(session):
    """One mandate with a comparison set and a seeded July.

    Four own pieces: two in a Leitmedium, one positive, one negative, two neutral.
    The rival holds one, so the month's share of voice is 4/5.
    """
    client = Client(
        name="Alpha AG",
        industry="Fintech",
        comms_guide=(
            "Positionierung: Der unabhängige Anbieter für den Mittelstand.\n"
            "Kernbotschaften: Buchhaltung ohne Papier · Wachstum aus eigener Kraft\n"
            "No-Gos: keine Renditeversprechen\n"
        ),
    )
    rival = Client(name="Beta AG", industry="Fintech", is_competitor=True)
    session.add_all([client, rival])
    session.flush()
    client.competitors.append(rival)

    _piece(session, client, "A1", source="FAZ", tonality=Tonality.POSITIV)
    _piece(session, client, "A2", source="Handelsblatt", tonality=Tonality.NEGATIV)
    _piece(session, client, "A3", source="Kosmetik Journal")
    _piece(session, client, "A4", source="Kosmetik Journal")
    _piece(session, rival, "B1", source="FAZ")
    session.commit()
    return client, rival


# --- Test helpers ----------------------------------------------------------------


def _ref(figures, key: MetricKey, subject: str = "") -> str:
    """The reference the prompt offers one figure under.

    Looked up rather than hard-coded, so a test says which figure it means instead
    of encoding the order the figure set happens to be built in.
    """
    for ref, value in figures.items():
        if value.key is key and value.subject == subject:
            return ref
    raise AssertionError(f"{key} {subject!r} is not a citable figure here")


def _reply(*proposed: dict, captured: list | None = None):
    """A stand-in ``generate`` returning one fixed reply, optionally keeping the prompt."""

    def generate(prompt: str, **kwargs) -> str:
        if captured is not None:
            captured.append(prompt)
        return json.dumps({"findings": list(proposed)})

    return generate


def _refuse(prompt: str, **kwargs) -> str:
    raise AssertionError("the model was called when it should not have been")


def _finding(
    figures: list[str],
    *,
    kind: str = "sichtbarkeit",
    claim: str = "Der Mandant war im Juli sichtbar wie nie.",
    consequence: str = "Die Frequenz trägt bis zum Herbst.",
) -> dict:
    return {
        "kind": kind,
        "claim": claim,
        "consequence": consequence,
        "figures": figures,
    }


# --- Every finding carries its evidence -------------------------------------------


def test_a_stored_finding_carries_the_rows_behind_the_figure_it_cites(session, mandate):
    """The inversion: the model named a figure, the code attached the rows."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    coverage = _ref(figures, MetricKey.COVERAGE)

    draft = findings(
        session, client, JULY, generate=_reply(_finding([coverage]))
    )
    stored = store(session, client, draft)

    (kept,) = stored.findings
    assert kept.evidence_ids
    # Exactly the four analyses the coverage figure was computed from.
    assert tuple(kept.evidence_ids) == tuple(
        sorted(figures[coverage].analysis_ids)
    )


def test_evidence_is_attached_by_the_code_and_never_quoted_by_the_model(
    session, mandate
):
    """A reply naming ids of its own does not get them: the field is not read, so
    an id the model invented cannot become a citation."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    negative = _ref(figures, MetricKey.TONALITY, "negativ")
    invented = dict(_finding([negative], kind="risiko"), evidence_ids=[9999])

    draft = findings(session, client, JULY, generate=_reply(invented))

    (kept,) = draft.findings
    assert 9999 not in kept.evidence_ids
    assert kept.evidence_ids == tuple(figures[negative].analysis_ids)


def test_a_finding_returned_without_evidence_is_discarded(session, mandate, caplog):
    """A claim resting on nothing is not a weak finding, it is not a finding."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)

    with caplog.at_level(logging.INFO, logger="newspulse.report"):
        draft = findings(
            session,
            client,
            JULY,
            generate=_reply(
                _finding([]),
                _finding([_ref(figures, MetricKey.COVERAGE)]),
            ),
        )

    assert len(draft.findings) == 1
    assert "discarded" in caplog.text


def test_the_discard_is_logged_rather_than_shown(session, mandate, caplog):
    """A consultant told "something was thrown away" learns nothing he can act on,
    and a rejected claim rendered with a warning is a claim that was rendered."""
    client, _ = mandate

    with caplog.at_level(logging.INFO, logger="newspulse.report"):
        draft = findings(session, client, JULY, generate=_reply(_finding([])))

    assert draft.findings == ()
    assert caplog.records
    # Nothing about the discarded claim travels back to the caller.
    assert "Der Mandant war im Juli sichtbar" not in draft.note


def test_a_figure_with_no_rows_under_it_cannot_carry_a_finding(session, mandate):
    """A key message nothing carried is a real figure at zero, and a claim built on
    it would have an empty evidence list. It goes the same way as any other."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    silent = _ref(figures, MetricKey.MESSAGE, "Wachstum aus eigener Kraft")
    assert figures[silent].figure == 0
    assert figures[silent].analysis_ids == ()

    draft = findings(
        session, client, JULY, generate=_reply(_finding([silent], kind="botschaft"))
    )

    assert draft.findings == ()


def test_a_share_of_voice_finding_cites_only_the_mandates_own_coverage(
    session, mandate
):
    """The share-of-voice metric is computed over the whole comparison set, because
    the denominator is part of the figure. A citation is narrower: this is the
    document Alpha AG receives, and a rival's headline printed as the ground under
    Alpha's claim is the one failure here a client would see before we did."""
    client, rival = mandate
    figures = citable_figures(session, client, JULY)
    voice = _ref(figures, MetricKey.SHARE_OF_VOICE)

    stored = store(
        session, client, findings(session, client, JULY, generate=_reply(_finding([voice])))
    )
    (view,) = resolve(session, stored)

    assert {row.headline for row in view.evidence} == {"A1", "A2", "A3", "A4"}
    rival_rows = set(
        session.scalars(select(Analysis.id).where(Analysis.client_id == rival.id))
    )
    assert rival_rows
    assert not rival_rows & set(stored.findings[0].evidence_ids)


def test_a_rivals_article_dismissed_does_not_weaken_this_mandates_finding(
    session, mandate
):
    """Weakened means *this* claim's ground moved. A competitor's piece leaving the
    archive changes the market, not the evidence Alpha AG was shown."""
    client, rival = mandate
    figures = citable_figures(session, client, JULY)
    stored = store(
        session,
        client,
        findings(
            session,
            client,
            JULY,
            generate=_reply(_finding([_ref(figures, MetricKey.SHARE_OF_VOICE)])),
        ),
    )

    dropped = session.scalars(
        select(Analysis).where(Analysis.client_id == rival.id)
    ).one()
    dropped.dismissed_at = dt.datetime.now(dt.UTC)
    session.commit()

    (view,) = resolve(session, stored)
    assert view.weakened is False
    assert len(view.evidence) == 4


# --- Only the figures RPT-01 produced ----------------------------------------------


def test_a_finding_citing_a_figure_outside_the_metric_set_is_rejected(session, mandate):
    """Rejected whole rather than filtered down: the sentence was written about a
    number that does not exist, so the rest of it is not trustworthy either."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)

    draft = findings(
        session,
        client,
        JULY,
        generate=_reply(_finding([_ref(figures, MetricKey.COVERAGE), "F99"])),
    )

    assert draft.findings == ()


def test_a_claim_naming_a_figure_this_tool_may_not_produce_is_rejected(session, mandate):
    """Reach is not in the figure set, and a claim can still name it in words. The
    same guard the metric layer applies to a caption applies to a sentence."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    coverage = _ref(figures, MetricKey.COVERAGE)

    draft = findings(
        session,
        client,
        JULY,
        generate=_reply(
            _finding([coverage], claim="Die Reichweite lag bei 1,2 Millionen Kontakten."),
            _finding(
                [coverage],
                claim="Vier Beiträge im Juli.",
                consequence="Das entspricht einem Werbewert von 40.000 Euro.",
            ),
        ),
    )

    assert draft.findings == ()


def test_the_citable_set_holds_only_figures_that_state_a_number(session, mandate):
    """"We hold no July" is not a figure to build a claim on."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)

    assert figures
    for value in figures.values():
        assert value.figure is not None
    # No released letter in the fixture, so attribution states no figure at all and
    # is absent rather than citable at zero.
    assert not any(v.key is MetricKey.ATTRIBUTED for v in figures.values())


def test_attributed_coverage_becomes_citable_once_a_letter_earned_it(session, mandate):
    client, _ = mandate
    _letter(session, client, "Anna Muster", released=dt.datetime(2026, 7, 2, tzinfo=dt.UTC))
    _piece(
        session,
        client,
        "AUS-ANSPRACHE",
        when=dt.datetime(2026, 7, 9, tzinfo=dt.UTC),
        author="Anna Muster",
    )
    session.commit()
    figures = citable_figures(session, client, JULY)
    attributed = _ref(figures, MetricKey.ATTRIBUTED)

    draft = findings(
        session, client, JULY, generate=_reply(_finding([attributed], kind="wirkung"))
    )

    (kept,) = draft.findings
    assert kept.kind is ReportFindingKind.WIRKUNG
    assert len(kept.evidence_ids) == 1


# --- Findings are typed -------------------------------------------------------------


def test_a_risk_finding_and_a_visibility_finding_are_distinguishable_unread(
    session, mandate
):
    client, _ = mandate
    figures = citable_figures(session, client, JULY)

    draft = findings(
        session,
        client,
        JULY,
        generate=_reply(
            _finding([_ref(figures, MetricKey.COVERAGE)], kind="sichtbarkeit"),
            _finding([_ref(figures, MetricKey.TONALITY, "negativ")], kind="risiko"),
        ),
    )
    stored = store(session, client, draft)

    kinds = [finding.kind for finding in stored.findings]
    assert kinds == [ReportFindingKind.SICHTBARKEIT, ReportFindingKind.RISIKO]
    assert kinds[0] is not kinds[1]


def test_one_sloppy_finding_is_dropped_alone_rather_than_losing_the_report(
    session, mandate, caplog
):
    """Reject-whole is reserved for a bad ``kind``, because it costs the four good
    findings beside the bad one and nothing above retries. A finding that omits its
    claim, or names its reference by number, is the model being sloppy about one
    sentence and goes the way every other unusable finding goes: out, with a line
    in the log."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    coverage = _ref(figures, MetricKey.COVERAGE)

    with caplog.at_level(logging.INFO, logger="newspulse.report"):
        draft = findings(
            session,
            client,
            JULY,
            generate=_reply(
                _finding([coverage], claim="Vier Beiträge im Juli."),
                # No ``claim`` key at all, and the reference given as the number it
                # was printed under rather than as its name.
                {
                    "kind": "risiko",
                    "consequence": "Das wird teurer.",
                    "figures": [int(coverage[1:])],
                },
            ),
        )

    (kept,) = draft.findings
    assert kept.claim == "Vier Beiträge im Juli."
    assert "no claim" in caplog.text


def test_a_kind_outside_the_closed_set_is_not_parsed_into_a_report(session, mandate):
    client, _ = mandate
    figures = citable_figures(session, client, JULY)

    with pytest.raises(ParseError):
        findings(
            session,
            client,
            JULY,
            generate=_reply(
                _finding([_ref(figures, MetricKey.COVERAGE)], kind="stimmungsbild")
            ),
        )


def test_a_key_message_in_the_clients_own_wording_may_be_named_in_a_finding(session):
    """RPT-01 lets a guide say "Reichweite in der Zielgruppe" — the client's own
    sentence, not a figure this tool produced — and the prompt duly offers it as a
    figure to write a botschaft finding about. The finding it asked for must
    survive the guard that let the figure exist, while a reach *claim* beside the
    quote is still refused."""
    client = Client(
        name="Gamma AG",
        industry="Fintech",
        comms_guide="Kernbotschaften: Reichweite in der Zielgruppe\n",
    )
    session.add(client)
    session.flush()
    _piece(
        session,
        client,
        "G1",
        summary="Der Anbieter erzielt Reichweite in der Zielgruppe des Mittelstands.",
    )
    session.commit()

    figures = citable_figures(session, client, JULY)
    message = _ref(figures, MetricKey.MESSAGE, "Reichweite in der Zielgruppe")
    assert figures[message].analysis_ids

    draft = findings(
        session,
        client,
        JULY,
        generate=_reply(
            _finding(
                [message],
                kind="botschaft",
                claim=(
                    "Die Kernbotschaft Reichweite in der Zielgruppe trägt einen "
                    "Beitrag."
                ),
                consequence="Die Linie hält.",
            ),
            _finding(
                [message],
                kind="botschaft",
                claim="Die Reichweite lag bei 1,2 Millionen Kontakten.",
            ),
        ),
    )

    (kept,) = draft.findings
    assert kept.kind is ReportFindingKind.BOTSCHAFT
    assert "Reichweite in der Zielgruppe" in kept.claim


def test_a_key_message_does_not_excuse_a_reach_claim_in_another_finding(session):
    """The carve-out is for the claim that quotes the message, not for the report.
    A mandate whose guide legitimately names a forbidden term in a key message
    would otherwise switch the RPT-01 guard off for every sentence beside it, and
    a sichtbarkeit finding citing only the coverage figure could invent a reach
    number under cover of a botschaft it never mentioned."""
    client = Client(
        name="Delta AG",
        industry="Medien",
        comms_guide="Kernbotschaften: Reichweite in der Zielgruppe\n",
    )
    session.add(client)
    session.flush()
    _piece(
        session,
        client,
        "D1",
        summary="Der Anbieter erzielt Reichweite in der Zielgruppe.",
    )
    session.commit()

    figures = citable_figures(session, client, JULY)
    assert _ref(figures, MetricKey.MESSAGE, "Reichweite in der Zielgruppe")

    draft = findings(
        session,
        client,
        JULY,
        generate=_reply(
            _finding(
                # The coverage figure, not the message: this finding has no claim
                # on the client's own wording.
                [_ref(figures, MetricKey.COVERAGE)],
                kind="sichtbarkeit",
                claim="Die Reichweite in der Zielgruppe erreichte 1,2 Millionen Kontakte.",
            )
        ),
    )

    assert draft.findings == ()


def test_a_reference_echoed_in_the_brackets_it_was_shown_in_is_read(session, mandate):
    """The prompt renders every figure as ``[F1]``. A model handing the form back
    has invented nothing, and reject-whole is for a reference that does not exist,
    not for a formatting habit that would empty a report."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    coverage = _ref(figures, MetricKey.COVERAGE)

    draft = findings(
        session, client, JULY, generate=_reply(_finding([f"[{coverage}]"]))
    )

    (kept,) = draft.findings
    assert kept.figures == (coverage,)
    assert kept.evidence_ids == tuple(sorted(figures[coverage].analysis_ids))


# --- Evidence that moved ------------------------------------------------------------


def test_dismissing_a_cited_article_leaves_the_finding_weakened(session, mandate):
    """Not a claim that silently keeps standing on ground that moved."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    draft = findings(
        session,
        client,
        JULY,
        generate=_reply(_finding([_ref(figures, MetricKey.COVERAGE)])),
    )
    stored = store(session, client, draft)

    dropped = session.get(Analysis, stored.findings[0].evidence_ids[0])
    dropped.dismissed_at = dt.datetime.now(dt.UTC)
    session.commit()

    (view,) = resolve(session, stored)
    assert view.weakened is True
    assert view.missing == 1
    assert len(view.evidence) == 3
    assert view.unsupported is False


def test_deleting_every_cited_article_leaves_the_finding_unsupported(session, mandate):
    """A claim with nothing left is not weak, it is out."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    negative = _ref(figures, MetricKey.TONALITY, "negativ")
    draft = findings(
        session, client, JULY, generate=_reply(_finding([negative], kind="risiko"))
    )
    stored = store(session, client, draft)

    cited = session.get(Analysis, stored.findings[0].evidence_ids[0])
    session.delete(session.get(Article, cited.article_id))
    session.commit()

    (view,) = resolve(session, stored)
    assert view.unsupported is True
    assert view.weakened is True
    assert view.evidence == ()


def test_resolve_reads_a_row_written_outside_the_generation_path_honestly(
    session, mandate
):
    """``store`` and ``_accept`` are not the only ways a finding row comes to exist:
    a restore, or the edit route this feature gets next, writes straight to the
    table. Two shapes that cannot arrive from a draft must still read correctly —
    a claim with no ids at all is not intact, and one id cited twice is one piece
    of evidence, not two."""
    client, _ = mandate
    cited = session.scalars(
        select(Analysis).where(Analysis.client_id == client.id)
    ).first()
    row = store(session, client, ReportDraft(client_id=client.id, period=JULY))
    row.findings.append(
        ReportFinding(
            kind=ReportFindingKind.SICHTBARKEIT,
            claim="Nichts darunter.",
            evidence_ids=[],
        )
    )
    row.findings.append(
        ReportFinding(
            kind=ReportFindingKind.SICHTBARKEIT,
            claim="Zweimal derselbe Beleg.",
            evidence_ids=[cited.id, cited.id],
        )
    )
    session.commit()

    groundless, duplicated = resolve(session, row)

    # Nothing failed to resolve, because nothing was cited. That must not read as
    # a claim standing on the whole of its ground.
    assert groundless.missing == 0
    assert groundless.unsupported is True
    assert groundless.weakened is True
    # One headline, printed once, and no phantom loss counted against it.
    assert len(duplicated.evidence) == 1
    assert duplicated.missing == 0
    assert duplicated.weakened is False


def test_an_untouched_finding_resolves_to_the_coverage_it_was_drafted_on(
    session, mandate
):
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    draft = findings(
        session,
        client,
        JULY,
        generate=_reply(_finding([_ref(figures, MetricKey.LEAD_MEDIA)])),
    )
    stored = store(session, client, draft)

    (view,) = resolve(session, stored)

    assert view.weakened is False
    assert {row.source for row in view.evidence} == {"FAZ", "Handelsblatt"}
    assert all(row.url for row in view.evidence)


def test_a_finding_the_consultant_dropped_is_not_rendered(session, mandate):
    """The row stays — what was proposed and rejected is part of how a report was
    arrived at — but the row surviving is not the sentence surviving."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    coverage = _ref(figures, MetricKey.COVERAGE)
    stored = store(
        session,
        client,
        findings(
            session,
            client,
            JULY,
            generate=_reply(
                _finding([coverage], claim="Erste Aussage."),
                _finding([coverage], claim="Zweite Aussage."),
            ),
        ),
    )

    stored.findings[0].kept = False
    session.commit()

    views = resolve(session, stored)
    assert [view.finding.claim for view in views] == ["Zweite Aussage."]
    # Dropped, not deleted: the report still knows what it was offered.
    assert len(stored.findings) == 2


# --- Silence is an answer ------------------------------------------------------------


def test_a_month_with_no_coverage_produces_no_findings_and_a_stated_reason(
    session, mandate
):
    """And no model call: there is nothing to interpret, and asking anyway invites
    three findings about a month that held nothing."""
    client, _ = mandate

    draft = findings(session, client, AUGUST, generate=_refuse)

    assert draft.findings == ()
    assert draft.note == "Keine Berichterstattung im Zeitraum."


def test_an_empty_month_is_stored_as_a_report_with_its_reason(session, mandate):
    client, _ = mandate

    stored = store(session, client, findings(session, client, AUGUST, generate=_refuse))

    assert stored.findings == []
    assert stored.note == "Keine Berichterstattung im Zeitraum."
    assert stored.state is ReportState.ENTWURF


def test_a_month_whose_findings_were_all_discarded_says_so_rather_than_going_blank(
    session, mandate
):
    """"Nothing was written about you" and "what was written carries no statement"
    are different sentences to put in front of a client."""
    client, _ = mandate

    draft = findings(session, client, JULY, generate=_reply(_finding([])))

    assert draft.findings == ()
    assert draft.note != "Keine Berichterstattung im Zeitraum."
    assert "keine belegbare Aussage" in draft.note


def test_an_empty_list_from_the_model_is_an_acceptable_answer(session, mandate):
    client, _ = mandate

    draft = findings(session, client, JULY, generate=_reply())

    assert draft.findings == ()
    assert draft.note


def test_an_unusable_reply_raises_rather_than_reading_as_a_quiet_month(session, mandate):
    """"Nothing to say" and "the generation failed" must not look alike to the
    person about to send a document."""
    client, _ = mandate

    with pytest.raises(ParseError):
        findings(session, client, JULY, generate=lambda prompt, **kw: "kein JSON")


# --- One report per period -----------------------------------------------------------


def test_the_same_period_generated_twice_replaces_the_draft(session, mandate):
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    first = store(
        session,
        client,
        findings(
            session,
            client,
            JULY,
            generate=_reply(_finding([_ref(figures, MetricKey.COVERAGE)])),
        ),
    )

    second = store(
        session,
        client,
        findings(
            session,
            client,
            JULY,
            generate=_reply(
                _finding(
                    [_ref(figures, MetricKey.TONALITY, "negativ")],
                    kind="risiko",
                    claim="Zwei negative Beiträge stehen im Juli.",
                )
            ),
        ),
    )

    assert second.id == first.id
    assert len(second.findings) == 1
    assert second.findings[0].kind is ReportFindingKind.RISIKO
    assert for_period(session, client.id, JULY).id == first.id


def test_a_released_report_is_never_replaced(session, mandate):
    """The same rule the ledger applies to a released letter: this is the artefact
    the client was sent."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    draft = findings(
        session,
        client,
        JULY,
        generate=_reply(_finding([_ref(figures, MetricKey.COVERAGE)])),
    )
    released = release(session, store(session, client, draft))

    with pytest.raises(ReportReleased):
        store(session, client, findings(session, client, JULY, generate=_reply()))

    kept = for_period(session, client.id, JULY)
    assert kept.id == released.id
    assert len(kept.findings) == 1
    assert kept.state is ReportState.FREIGEGEBEN


def test_a_report_carrying_a_release_stamp_is_never_replaced(session, mandate):
    """``store`` gates on the fact rather than on the label for it. A row whose
    ``released_at`` is set without its ``state`` — a restore, a route somebody
    writes next — is still a document that went out."""
    client, _ = mandate
    stored = store(session, client, findings(session, client, AUGUST, generate=_refuse))
    stored.released_at = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    session.commit()
    assert stored.state is ReportState.ENTWURF

    with pytest.raises(ReportReleased):
        store(session, client, findings(session, client, AUGUST, generate=_refuse))


def test_a_report_labelled_released_is_never_replaced(session, mandate):
    """The other half of the torn release. ``released_at`` without ``state`` is
    caught above; this is the label without the stamp, which is the worse of the
    two because the label is what a consultant sees. Overwriting it would leave a
    document reading *freigegeben* whose sentences changed underneath it."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    first = store(
        session,
        client,
        findings(
            session,
            client,
            JULY,
            generate=_reply(_finding([_ref(figures, MetricKey.COVERAGE)])),
        ),
    )
    assert len(first.findings) == 1
    first.state = ReportState.FREIGEGEBEN
    first.released_at = None
    session.commit()

    with pytest.raises(ReportReleased):
        store(session, client, findings(session, client, JULY, generate=_reply()))

    kept = for_period(session, client.id, JULY)
    assert len(kept.findings) == 1
    assert kept.state is ReportState.FREIGEGEBEN


def test_a_finding_without_evidence_cannot_be_stored(session, mandate):
    """``ReportFinding.evidence_ids`` says it is never empty, and that sentence is
    the safety argument of the feature. The discard rule keeps one out of a
    generated draft; this is the boundary the table itself is behind."""
    client, _ = mandate
    draft = ReportDraft(
        client_id=client.id,
        period=JULY,
        findings=(
            Finding(
                kind=ReportFindingKind.SICHTBARKEIT,
                claim="Behauptung ohne Beleg.",
                consequence="",
                evidence_ids=(),
                figures=(),
            ),
        ),
    )

    with pytest.raises(ValueError):
        store(session, client, draft)

    assert for_period(session, client.id, JULY) is None


def test_a_draft_generated_for_another_mandate_is_not_filed_under_this_one(
    session, mandate
):
    """The draft carries the id it was measured for. Filing it elsewhere would put
    one company's coverage under another company's name."""
    client, rival = mandate
    draft = findings(session, client, JULY, generate=_reply())

    with pytest.raises(ValueError):
        store(session, rival, draft)

    assert for_period(session, rival.id, JULY) is None


def test_releasing_a_report_twice_keeps_the_first_stamp(session, mandate):
    client, _ = mandate
    stored = store(session, client, findings(session, client, AUGUST, generate=_refuse))
    first = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)

    release(session, stored, when=first)
    release(session, stored, by="jemand anderes", when=dt.datetime(2026, 9, 9, tzinfo=dt.UTC))

    assert stored.released_at == first
    assert stored.released_by == ledger.DEFAULT_RELEASED_BY


def test_two_different_periods_are_two_reports(session, mandate):
    client, _ = mandate
    july = store(session, client, findings(session, client, JULY, generate=_reply()))
    august = store(session, client, findings(session, client, AUGUST, generate=_refuse))

    assert july.id != august.id


# --- The prompt composes the shared standards ------------------------------------------


def test_the_prompt_composes_the_guide_rather_than_restating_it(session, mandate):
    client, _ = mandate
    captured: list[str] = []

    findings(session, client, JULY, generate=_reply(captured=captured))

    (prompt,) = captured
    assert "Buchhaltung ohne Papier" in prompt
    assert "KOMMUNIKATIONS-GUIDE DES MANDANTEN" in prompt


def test_the_prompt_composes_the_forbidden_figures_from_the_guard(session, mandate):
    """The standard for what may never be stated lives in reporting.py. A prompt
    that retyped it would be a second copy, drifting from the first."""
    client, _ = mandate
    captured: list[str] = []

    findings(session, client, JULY, generate=_reply(captured=captured))

    (prompt,) = captured
    for term in ("reichweite", "werbewert", "impressionen"):
        assert term in prompt.casefold()


def test_the_prompt_template_itself_names_no_forbidden_figure(session, mandate):
    """Composed, not restated: the template carries placeholders, and the list of
    what this tool may not say arrives from the guard at build time."""
    template = report_module._prompt_template().template

    assert "$forbidden" in template
    assert "$comms_guide" in template
    assert not reporting.is_forbidden(template)


def test_the_prompt_offers_the_figures_with_the_rows_behind_each(session, mandate):
    client, _ = mandate
    captured: list[str] = []

    findings(session, client, JULY, generate=_reply(captured=captured))

    (prompt,) = captured
    assert "[F1] Beiträge: 4" in prompt
    assert "belegende Zeilen" in prompt
    # Share of voice reads as the document prints it, not as a raw fraction.
    assert "80.0 %" in prompt


def test_the_prompt_carries_the_months_headlines_without_citable_numbers(
    session, mandate
):
    """Context for the reading, deliberately unnumbered: the evidence is attached
    from the figures, so a headline is not something the model can cite."""
    client, _ = mandate

    prompt = build_prompt(
        session, client, JULY, citable_figures(session, client, JULY)
    )

    assert "A1" in prompt and "A4" in prompt
    assert "[0]" not in prompt


# --- House style ------------------------------------------------------------------------


def test_a_dash_in_a_claim_is_rewritten_before_it_is_stored(session, mandate):
    """The house rule, enforced rather than requested: the prompt asks and the
    model relapses, and this text goes to a client under the agency's name."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)

    draft = findings(
        session,
        client,
        JULY,
        generate=_reply(
            _finding(
                [_ref(figures, MetricKey.COVERAGE)],
                claim="Vier Beiträge — mehr als im Juni.",
            )
        ),
    )

    (kept,) = draft.findings
    assert "—" not in kept.claim
    assert "Vier Beiträge, mehr als im Juni." == kept.claim


def test_a_report_carries_at_most_the_handful_it_is_worth(session, mandate, caplog):
    """And says so in the log. The cap is a drop like any other, and these are
    findings that passed every guard: a consultant looking for an expected sixth
    statement has nowhere else to find out what happened to it."""
    client, _ = mandate
    figures = citable_figures(session, client, JULY)
    coverage = _ref(figures, MetricKey.COVERAGE)

    with caplog.at_level(logging.INFO, logger="newspulse.report"):
        draft = findings(
            session,
            client,
            JULY,
            generate=_reply(
                *[_finding([coverage], claim=f"Aussage {i}.") for i in range(8)]
            ),
        )

    assert len(draft.findings) == report_module.MAX_FINDINGS
    assert "keeps 5 of 8" in caplog.text
    assert "Aussage 7." in caplog.text


# --- The migrated schema ------------------------------------------------------------------


def test_the_migration_chain_has_exactly_one_head():
    """The failure mode of the reserved revision id, caught here rather than on the
    deploy.

    0028 chains from 0018 because the ids in between belong to features already
    building against them. Alembic chains on ``down_revision``, so that is correct
    in itself — but a sibling story landing 0019 with the *same* ``down_revision``
    gives the tree two heads, and ``alembic upgrade head`` then refuses to run at
    all. Nothing in this worktree can see that coming; it appears at merge. So the
    invariant is asserted rather than assumed, and whoever merges the two gets a
    red suite instead of a broken release: one of the revisions has to be
    re-pointed at the other."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))

    heads = ScriptDirectory.from_config(cfg).get_heads()

    assert len(heads) == 1, f"the migration tree has {len(heads)} heads: {heads}"


def test_the_migration_creates_the_report_tables_the_models_expect(tmp_path, monkeypatch):
    """The hand-authored revision owns the schema, per the house rule. A column
    that exists only in ``models.py`` is a report that works in this file and fails
    on the deployed database, which is the one the client's document comes from."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from newspulse import config as app_config

    db_path = tmp_path / "migrated.db"
    # env.py derives the URL from config.DATABASE_PATH at call time.
    monkeypatch.setattr(app_config, "DATABASE_PATH", db_path)
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert {"reports", "report_findings"} <= set(inspector.get_table_names())
        for table, model in (("reports", Report), ("report_findings", ReportFinding)):
            migrated = {column["name"] for column in inspector.get_columns(table)}
            assert migrated == {column.name for column in model.__table__.columns}
    finally:
        engine.dispose()
