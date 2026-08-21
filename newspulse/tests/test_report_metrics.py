"""The figures a report is allowed to make (newspulse.reporting).

Every number here is checked against one computed by hand in the fixture rather
than against itself: a seeded month with a known answer, so a metric that drifts
disagrees with the arithmetic instead of agreeing with the bug.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import outreach as ledger
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Base,
    Category,
    Client,
    Outreach,
    Tonality,
)
from newspulse.reporting import (
    FORBIDDEN_FIGURES,
    Direction,
    ForbiddenFigure,
    MetricKey,
    MetricValue,
    Period,
    attributed_coverage,
    is_forbidden,
    message_pull_through,
    period_metrics,
    recompute,
)

# The seeded month. Fixed rather than relative to "now" so the arithmetic in the
# assertions stays true next January.
JULY = Period(
    dt.datetime(2026, 7, 1, tzinfo=dt.UTC), dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
)


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
    relevance=5,
    summary="Zusammenfassung.",
    dismissed=False,
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
        relevance_score=relevance,
        importance_score=6,
        tonality=tonality,
        dismissed_at=dt.datetime.now(dt.UTC) if dismissed else None,
    )
    session.add(analysis)
    session.flush()
    return analysis


def _letter(session, client, journalist, *, released=None, outlet="Handelsblatt"):
    angle = Angle(
        client_id=client.id, subject="Impuls", message="Text", context="Kontext"
    )
    session.add(angle)
    session.flush()
    row = Outreach(
        angle_id=angle.id,
        client_id=client.id,
        journalist=journalist,
        outlet=outlet,
        subject="Betreff",
        message="Nachricht",
    )
    session.add(row)
    session.flush()
    if released is not None:
        ledger.release(session, row, when=released)
    session.commit()
    return row


@pytest.fixture
def mandate(session):
    """One mandate with a stored comparison set and a seeded July.

    Four own pieces in July: two in a Leitmedium, one positive, one negative. The
    rival holds one, so the month's share of voice is 4/5.
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


# --- Every figure carries its evidence -----------------------------------------


def test_every_figure_names_the_analyses_it_was_computed_from(session, mandate):
    client, _ = mandate
    metrics = period_metrics(session, client, JULY)

    assert metrics.coverage.figure == 4
    for value in metrics.values:
        assert value.analysis_ids, f"{value.key} stated a figure with no rows behind it"


def test_recomputing_from_the_cited_ids_alone_reproduces_every_figure(session, mandate):
    """The check the layer rests on: if the number and the evidence disagree, the
    evidence wins."""
    client, _ = mandate
    metrics = period_metrics(session, client, JULY)

    for value in metrics.values:
        assert recompute(session, value) == pytest.approx(value.figure), value.key


def test_recomputing_attributed_coverage_from_its_ids_reproduces_the_figure(
    session, mandate
):
    client, _ = mandate
    _letter(
        session,
        client,
        "Anna Muster",
        released=dt.datetime(2026, 7, 5, tzinfo=dt.UTC),
    )
    _piece(
        session,
        client,
        "A5",
        when=dt.datetime(2026, 7, 8, tzinfo=dt.UTC),
        author="Anna Muster",
    )
    session.commit()

    value = attributed_coverage(session, client, JULY)

    assert value.figure == 1
    assert recompute(session, value) == 1


def test_a_figure_shrinks_with_its_evidence_when_a_cited_article_is_dismissed(
    session, mandate
):
    """A claim whose ground moved must not keep standing on the old number."""
    client, _ = mandate
    metrics = period_metrics(session, client, JULY)
    dropped = session.get(Analysis, metrics.coverage.analysis_ids[0])
    dropped.dismissed_at = dt.datetime.now(dt.UTC)
    session.commit()

    assert recompute(session, metrics.coverage) == 3


# --- Share of voice -------------------------------------------------------------


def test_share_of_voice_uses_the_stored_comparison_set(session, mandate):
    client, _ = mandate
    metrics = period_metrics(session, client, JULY)

    assert metrics.share_of_voice.figure == pytest.approx(4 / 5)
    # Five rows: the denominator is part of the figure, so it is cited too.
    assert len(metrics.share_of_voice.analysis_ids) == 5


def test_a_company_in_the_same_industry_never_enters_the_share_by_itself(
    session, mandate
):
    """Never a set inferred from the industry: a benchmark is something somebody
    chose, and one the tool assembled is a number about a market nobody defined."""
    client, _ = mandate
    stranger = Client(name="Gamma AG", industry="Fintech", is_competitor=True)
    session.add(stranger)
    session.flush()
    for i in range(9):
        _piece(session, stranger, f"G{i}", source="FAZ")
    session.commit()

    assert period_metrics(session, client, JULY).share_of_voice.figure == pytest.approx(
        4 / 5
    )


def test_a_mandate_without_a_comparison_set_gets_no_share_and_says_why(session):
    client = Client(name="Solo AG")
    session.add(client)
    session.flush()
    _piece(session, client, "S1")
    session.commit()

    value = period_metrics(session, client, JULY).share_of_voice

    assert value.figure is None
    assert "Vergleichsumfeld" in value.note
    assert value not in period_metrics(session, client, JULY).values


# --- The comparison against the period before ------------------------------------


def test_the_previous_period_has_the_same_length_as_a_partial_month(session, mandate):
    """A report pulled on the 12th covers twelve days. Comparing those against a
    full month would show a collapse that only happened in the arithmetic."""
    client, _ = mandate
    partial = Period(
        dt.datetime(2026, 7, 1, tzinfo=dt.UTC), dt.datetime(2026, 7, 13, tzinfo=dt.UTC)
    )

    assert partial.previous.length == partial.length
    assert partial.previous.end == partial.start


def test_the_comparison_counts_only_the_matching_length_of_the_earlier_period(
    session, mandate
):
    client, _ = mandate
    # Twelve days before 1 July: one piece inside the comparison window, one older.
    _piece(session, client, "J1", when=dt.datetime(2026, 6, 25, tzinfo=dt.UTC))
    _piece(session, client, "J2", when=dt.datetime(2026, 6, 2, tzinfo=dt.UTC))
    session.commit()
    partial = Period(
        dt.datetime(2026, 7, 1, tzinfo=dt.UTC), dt.datetime(2026, 7, 13, tzinfo=dt.UTC)
    )

    coverage = period_metrics(session, client, partial).coverage

    assert coverage.figure == 4
    assert coverage.previous == 1
    assert coverage.direction is Direction.UP


def test_no_earlier_archive_means_an_unknown_direction_not_a_rise_from_zero(
    session, mandate
):
    """A mandate onboarded last week would otherwise read "12, vorher 0", which
    states a collapse that never happened."""
    client, _ = mandate
    coverage = period_metrics(session, client, JULY).coverage

    assert coverage.previous is None
    assert coverage.direction is Direction.UNKNOWN


def test_an_equal_month_reads_as_unchanged(session, mandate):
    client, _ = mandate
    for day in (2, 4, 6, 8):
        _piece(session, client, f"M{day}", when=dt.datetime(2026, 6, day, tzinfo=dt.UTC))
    session.commit()

    coverage = period_metrics(session, client, JULY).coverage

    assert (coverage.figure, coverage.previous) == (4, 4)
    assert coverage.direction is Direction.FLAT


# --- An empty month --------------------------------------------------------------


def test_a_period_with_no_coverage_returns_empty_metrics_rather_than_zeros(
    session, mandate
):
    """Zero coverage and no data are different statements about a month."""
    client, _ = mandate
    august = Period(
        dt.datetime(2026, 8, 1, tzinfo=dt.UTC), dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )

    metrics = period_metrics(session, client, august)

    assert metrics.empty is True
    assert metrics.values == ()
    assert metrics.coverage.figure is None
    assert metrics.note == "Keine Berichterstattung im Zeitraum."
    assert metrics.tonality == ()


def test_a_figure_that_is_absent_has_to_say_why():
    with pytest.raises(ValueError):
        MetricValue(key=MetricKey.COVERAGE, client_id=1, figure=None)


# --- Tonality --------------------------------------------------------------------


def test_the_tonality_split_carries_the_rows_of_each_tone(session, mandate):
    client, _ = mandate
    by_tone = {v.subject: v for v in period_metrics(session, client, JULY).tonality}

    assert by_tone["positiv"].figure == 1
    assert by_tone["negativ"].figure == 1
    assert by_tone["neutral"].figure == 2
    assert len(by_tone["neutral"].analysis_ids) == 2
    # The legacy tone has no rows here, so it is not a segment of the split.
    assert "unbekannt" not in by_tone


# --- Leitmedien ------------------------------------------------------------------


def test_lead_media_counts_only_coverage_in_a_tier_one_outlet(session, mandate):
    client, _ = mandate
    lead = period_metrics(session, client, JULY).lead_media

    assert lead.figure == 2  # FAZ and Handelsblatt; the trade title is not one
    assert len(lead.analysis_ids) == 2


# --- Attribution -----------------------------------------------------------------


def test_a_piece_counts_only_where_a_released_letter_preceded_it(session, mandate):
    client, _ = mandate
    _letter(session, client, "Anna Muster", released=dt.datetime(2026, 7, 2, tzinfo=dt.UTC))
    _piece(
        session,
        client,
        "AUS-ANSPRACHE",
        when=dt.datetime(2026, 7, 9, tzinfo=dt.UTC),
        author="Von Anna Muster",
    )
    session.commit()

    value = attributed_coverage(session, client, JULY)

    assert value.figure == 1
    assert len(value.analysis_ids) == 1
    assert len(value.outreach_ids) == 1


def test_a_draft_letter_attributes_nothing(session, mandate):
    """A letter nobody released caused nothing, whatever the byline says."""
    client, _ = mandate
    _letter(session, client, "Anna Muster")  # never released
    _piece(
        session,
        client,
        "ZUFALL",
        when=dt.datetime(2026, 7, 9, tzinfo=dt.UTC),
        author="Anna Muster",
    )
    session.commit()

    value = attributed_coverage(session, client, JULY)

    assert value.figure is None
    assert "freigegebenen Anschreiben" in value.note


def test_a_letter_released_after_the_piece_does_not_claim_it(session, mandate):
    client, _ = mandate
    _letter(session, client, "Anna Muster", released=dt.datetime(2026, 7, 20, tzinfo=dt.UTC))
    _piece(
        session,
        client,
        "VORHER",
        when=dt.datetime(2026, 7, 9, tzinfo=dt.UTC),
        author="Anna Muster",
    )
    session.commit()

    assert attributed_coverage(session, client, JULY).figure == 0


def test_a_piece_outside_the_attribution_window_does_not_count(session, mandate):
    client, _ = mandate
    _letter(session, client, "Anna Muster", released=dt.datetime(2026, 7, 1, tzinfo=dt.UTC))
    _piece(
        session,
        client,
        "SPAET",
        when=dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.UTC),
        author="Anna Muster",
    )
    session.commit()

    assert attributed_coverage(session, client, JULY, window_days=14).figure == 0
    assert attributed_coverage(session, client, JULY, window_days=45).figure == 1


def test_a_piece_matching_two_letters_is_counted_once(session, mandate):
    client, _ = mandate
    _letter(session, client, "Anna Muster", released=dt.datetime(2026, 7, 1, tzinfo=dt.UTC))
    _letter(session, client, "Anna Muster", released=dt.datetime(2026, 7, 3, tzinfo=dt.UTC))
    _piece(
        session,
        client,
        "EINMAL",
        when=dt.datetime(2026, 7, 9, tzinfo=dt.UTC),
        author="Anna Muster",
    )
    session.commit()

    value = attributed_coverage(session, client, JULY)

    assert value.figure == 1
    assert len(value.analysis_ids) == 1
    # Both letters stay as evidence: each of them is a reason the piece is claimed.
    assert len(value.outreach_ids) == 2


def test_another_journalists_byline_is_not_attributed(session, mandate):
    client, _ = mandate
    _letter(session, client, "Anna Muster", released=dt.datetime(2026, 7, 1, tzinfo=dt.UTC))
    _piece(
        session,
        client,
        "FREMD",
        when=dt.datetime(2026, 7, 9, tzinfo=dt.UTC),
        author="Bernd Beispiel",
    )
    session.commit()

    assert attributed_coverage(session, client, JULY).figure == 0


def test_releasing_a_letter_twice_keeps_the_first_stamp(session, mandate):
    client, _ = mandate
    first = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    row = _letter(session, client, "Anna Muster", released=first)

    ledger.release(session, row, when=dt.datetime(2026, 7, 20, tzinfo=dt.UTC))

    assert row.released_at == first
    assert row.released_by == "mensch"


# --- Message pull-through ---------------------------------------------------------


def test_message_pull_through_counts_the_coverage_carrying_each_key_message(
    session, mandate
):
    client, _ = mandate
    _piece(
        session,
        client,
        "BOTSCHAFT",
        summary="Die Buchhaltung läuft dort ohne Papier.",
    )
    session.commit()

    by_message = {v.subject: v for v in message_pull_through(session, client, JULY)}

    assert set(by_message) == {"Buchhaltung ohne Papier", "Wachstum aus eigener Kraft"}
    landed = by_message["Buchhaltung ohne Papier"]
    assert landed.figure == 1
    assert recompute(session, landed) == 1
    assert by_message["Wachstum aus eigener Kraft"].figure == 0


def test_a_no_go_is_never_counted_as_a_key_message(session, mandate):
    """Counting how often a No-Go appeared would invert the figure it belongs to."""
    client, _ = mandate
    subjects = {v.subject for v in message_pull_through(session, client, JULY)}

    assert not any("Renditeversprechen" in subject for subject in subjects)


def test_a_mandate_without_key_messages_gets_no_figure_and_says_why(session, mandate):
    client, _ = mandate
    client.comms_guide = ""
    session.commit()

    (value,) = message_pull_through(session, client, JULY)

    assert value.figure is None
    assert "Kernbotschaften" in value.note


# --- The guard --------------------------------------------------------------------


def test_no_metric_function_can_return_reach_impressions_or_advertising_value(
    session, mandate
):
    """The forbidden list is empty of results: there is no key to put reach under
    and no free-text label to hide it in."""
    client, _ = mandate
    metrics = period_metrics(session, client, JULY)
    produced = [
        *metrics.values,
        attributed_coverage(session, client, JULY),
        *message_pull_through(session, client, JULY),
    ]

    assert produced  # the assertion below is worthless against an empty list
    for value in produced:
        assert not is_forbidden(value.key.value)
        assert not is_forbidden(value.label)


def test_the_permitted_set_holds_none_of_the_forbidden_figures():
    for key in MetricKey:
        assert not is_forbidden(key.value)
        assert not is_forbidden(MetricValue(key=key, client_id=1, figure=0.0).label)


def test_a_figure_outside_the_permitted_set_cannot_be_built():
    with pytest.raises(ForbiddenFigure):
        MetricValue(key="reichweite", client_id=1, figure=1_200_000.0)


def test_the_names_the_trade_asks_for_are_all_refused():
    for name in FORBIDDEN_FIGURES:
        assert is_forbidden(name)
    for wording in ("Geschätzte Reichweite", "Werbeäquivalenz in EUR", "AVE"):
        assert is_forbidden(wording)


def test_a_permitted_figure_is_not_refused_by_the_guard():
    """The guard has to be narrow enough to leave the real figures alone."""
    for name in ("Beiträge", "Anteil am Marktgespräch", "Average der Wichtigkeit"):
        assert not is_forbidden(name)
