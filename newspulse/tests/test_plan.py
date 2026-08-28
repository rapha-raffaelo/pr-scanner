"""The editorial plan's engine: what may become a hook, and what may not.

Nothing here reaches a model and nothing here reaches the network. Every test
that needs prose injects it as a string, which is also the only way to check the
property DEC-4 turns on: that a date the model puts in its answer has nowhere to
land. A test that let a real model answer could not tell "the date was refused"
from "the model happened not to offer one".

The clock is injected rather than patched, for the reason the PRD's test strategy
gives: a plan is six months counted from *a* month, and a suite that reads the
wall clock is a suite whose window tests mean something different in December.

Two calculations are checked against a hand-counted file rather than against
themselves:

* the previous-year rule, against ``fixtures/plan/vorjahr_archive.json`` — a year
  of one mandate's archive with the visible count of every month written out by a
  person, including the dismissed and irrelevant rows that must not count;
* nothing else, because nothing else counts anything. The signal rule and the
  resonance rule are thresholds over rows the test seeds one by one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, job, plan
from newspulse.analyzer import AnalyzerError
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    HookSource,
    HookState,
    MarketSignal,
    PlanHook,
    Setting,
    SignalKind,
    TopicHit,
)

_ARCHIVE = Path(__file__).parent / "fixtures" / "plan" / "vorjahr_archive.json"

#: The moment every test in this file plans from: a Monday morning in the middle
#: of August, so the window (2026-08 … 2027-01) crosses a year boundary and the
#: month arithmetic cannot pass by accident on a within-year window.
_NOW = dt.datetime(2026, 8, 24, 9, 0, tzinfo=dt.UTC)
_BERLIN = ZoneInfo("Europe/Berlin")

#: The six months ``_NOW`` plans, spelled out rather than computed — the window
#: is the thing under test, so deriving it from the code under test would make
#: every window assertion tautological.
_WINDOW = ["2026-08", "2026-09", "2026-10", "2026-11", "2026-12", "2027-01"]


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as open_session:
        yield open_session


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    """Pin the display zone, so a test states the calendar it is counting in.

    ``config`` resolves the zone once at import and honours ``TZ``, so a host in
    another zone would otherwise move the month boundaries the fixture was
    counted against.
    """
    monkeypatch.setattr(config, "LOCAL_ZONE", _BERLIN)


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    """The suite's standing rule, enforced for this file rather than assumed.

    Three tests here drive ``job._recompute_plans``, which calls the engine with
    its *real* default generator. They are safe only because a mandate with no
    evidence never reaches a model call — and that is a property under test, not
    a given, so the boundary is closed off rather than trusted. The breach is
    reported at teardown, where the engine's deliberate catch-all around its
    model call cannot swallow it.
    """
    from newspulse import analyzer

    reached: list[str] = []

    def _forbidden(prompt: str, **kwargs) -> str:
        reached.append(prompt)
        raise AnalyzerError("this suite does not shell out")

    monkeypatch.setattr(analyzer, "invoke_claude_cli", _forbidden)
    yield
    assert not reached, "a test reached the real `claude` CLI"


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
    """One market signal. ``url`` is derived so a test never has to invent one."""
    title = fields.pop("title", "Verordnung tritt in Kraft")
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


def _radar_hit(session, client: Client, *, title: str, days_ago: int) -> TopicHit:
    """One stored radar article carrying a theme — the resonance the plan reads."""
    article = _article(
        session,
        title=title,
        published_at=_NOW - dt.timedelta(days=days_ago),
        source="pv-magazine",
    )
    hit = TopicHit(article_id=article.id, client_id=client.id)
    session.add(hit)
    session.commit()
    return hit


def _coverage(
    session,
    client: Client,
    *,
    title: str,
    published_at: dt.datetime,
    source: str = "Handelsblatt",
    relevance: int = 3,
    importance: int = 5,
    dismissed: bool = False,
) -> Analysis:
    article = _article(session, title=title, published_at=published_at, source=source)
    analysis = Analysis(
        article_id=article.id,
        client_id=client.id,
        is_relevant=relevance >= 1,
        summary=title,
        category=Category.SONSTIGES,
        relevance_score=relevance,
        importance_score=importance,
        dismissed_at=_NOW if dismissed else None,
    )
    session.add(analysis)
    session.commit()
    return analysis


# --- Stand-ins for the one model call ---------------------------------------------


class _Generator:
    """The one model call, faked: it answers with the entries it was handed and
    records every prompt it was given.

    It *records* rather than refuses, even where a test forbids the call
    outright, and that is the load-bearing detail of this whole file. The engine
    catches every exception around its model call on purpose — a backend that is
    down must cost the prose and never the dates — so a stand-in that raised
    "nobody may call me" would be swallowed by that handler and the test would
    pass on the strength of the very thing it meant to forbid. A counter cannot
    be swallowed.
    """

    def __init__(self, *entries: dict) -> None:
        self.entries = list(entries)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, **kwargs) -> str:
        self.prompts.append(prompt)
        return json.dumps({"hooks": self.entries})

    @property
    def calls(self) -> int:
        return len(self.prompts)

    @property
    def prompt(self) -> str:
        assert self.prompts, "the model was never asked"
        return self.prompts[-1]


@pytest.fixture
def unasked() -> _Generator:
    """A generator the plan is expected never to reach."""
    return _Generator()


def _prose(ref: str = "K1", reason: str = "Das Mandat betreibt genau das.", **extra):
    return {"ref": ref, "reason": reason, "format": "statement", **extra}


# --- The dated market signal ------------------------------------------------------


def test_a_future_signal_becomes_a_hook_carrying_its_row_and_its_day(session, mandate):
    """The hardest hook: a regulation whose date has not arrived.

    Month *and* day, because the source carries both — and the id of the signal
    row, because a hook that cannot be clicked back to its evidence is the thing
    DEC-4 forbids.
    """
    signal = _signal(
        session,
        mandate,
        title="Heizungsverordnung tritt in Kraft",
        effective_at=dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.UTC),
    )

    hooks = plan.recompute(session, mandate, invoke=_Generator(_prose()), now=_NOW)

    assert len(hooks) == 1
    hook = hooks[0]
    assert hook.source_kind is HookSource.MARKTSIGNAL
    assert hook.source_id == signal.id
    assert (hook.month, hook.day) == ("2026-11", 12)
    assert hook.title == "Heizungsverordnung tritt in Kraft"
    assert hook.state is HookState.VORGESCHLAGEN


def test_a_signal_whose_dates_have_all_passed_is_no_hook(session, mandate, unasked):
    """A regulation that already took effect is news, not a plan entry."""
    _signal(
        session,
        mandate,
        title="Verordnung galt ab Juni",
        effective_at=_NOW - dt.timedelta(days=60),
        deadline_at=_NOW - dt.timedelta(days=90),
    )

    assert plan.recompute(session, mandate, invoke=unasked, now=_NOW) == []
    assert unasked.calls == 0


def test_a_signal_beyond_the_six_month_window_is_no_hook(session, mandate, unasked):
    """Dated, future, and still not in this plan: the window is six months."""
    _signal(
        session,
        mandate,
        title="Verordnung tritt 2027 in Kraft",
        effective_at=dt.datetime(2027, 6, 1, 8, 0, tzinfo=dt.UTC),
    )

    assert plan.recompute(session, mandate, invoke=unasked, now=_NOW) == []


def test_the_earlier_of_a_deadline_and_an_effective_date_dates_the_hook(
    session, mandate
):
    """"You may still speak" comes before "it now applies to you", so it is the
    date the plan has to surface."""
    _signal(
        session,
        mandate,
        kind=SignalKind.VERANSTALTUNG,
        title="Call for Speakers",
        deadline_at=dt.datetime(2026, 9, 18, 12, 0, tzinfo=dt.UTC),
        effective_at=dt.datetime(2026, 12, 3, 8, 0, tzinfo=dt.UTC),
    )

    hooks = plan.recompute(session, mandate, invoke=_Generator(_prose()), now=_NOW)

    assert (hooks[0].month, hooks[0].day) == ("2026-09", 18)


# --- The theme with measured resonance --------------------------------------------


def test_a_theme_below_the_resonance_floor_is_no_hook(session, mandate, unasked):
    """Two mentions is not what the trade press writes about."""
    for index in range(plan.THEME_RESONANCE_MIN - 1):
        _radar_hit(session, mandate, title=f"Wärmepumpe im Bestand {index}", days_ago=10)

    assert plan.recompute(session, mandate, invoke=unasked, now=_NOW) == []
    assert unasked.calls == 0


def test_a_resonant_theme_carries_a_month_and_no_day(session, mandate):
    """AC 3, on the source that has no day to carry.

    The evidence is a stored radar row, and the hook lands in the current month
    because "the press writes about this now" is the only date that evidence
    supports.
    """
    hits = [
        _radar_hit(session, mandate, title=f"Wärmepumpe im Bestand {index}", days_ago=day)
        for index, day in enumerate((30, 20, 10))
    ]

    hooks = plan.recompute(session, mandate, invoke=_Generator(_prose()), now=_NOW)

    assert len(hooks) == 1
    hook = hooks[0]
    assert hook.source_kind is HookSource.THEMA
    assert hook.day is None
    assert hook.month == _WINDOW[0]
    # The newest matching radar row, so a recompute against an unchanged archive
    # names the same evidence again.
    assert hook.source_id == hits[-1].id
    assert hook.title == "Wärmepumpe"


def test_resonance_outside_the_lookback_does_not_count(session, mandate, unasked):
    """Three mentions last spring is not what the press writes about now."""
    stale = plan.THEME_LOOKBACK.days + 10
    for index in range(plan.THEME_RESONANCE_MIN):
        _radar_hit(
            session, mandate, title=f"Wärmepumpe im Bestand {index}", days_ago=stale
        )

    assert plan.recompute(session, mandate, invoke=unasked, now=_NOW) == []


def test_radar_material_that_does_not_carry_the_term_is_not_resonance(
    session, mandate, unasked
):
    """The count is over articles carrying the theme, not over the radar."""
    for index in range(6):
        _radar_hit(session, mandate, title=f"Netzentgelte steigen {index}", days_ago=5)

    assert plan.recompute(session, mandate, invoke=unasked, now=_NOW) == []


# --- The previous year, against a hand-counted archive ----------------------------


@pytest.fixture
def archive() -> dict:
    return json.loads(_ARCHIVE.read_text("utf-8"))


@pytest.fixture
def seeded_archive(session, mandate, archive) -> Client:
    """A year of coverage, exactly as the fixture file writes it out."""
    for row in archive["articles"]:
        _coverage(
            session,
            mandate,
            title=row["title"],
            published_at=dt.datetime.fromisoformat(row["published_at"]),
            source=row["source"],
            relevance=row["relevance"],
            importance=row["importance"],
            dismissed=row["dismissed"],
        )
    return mandate


def test_the_previous_year_rule_matches_the_hand_counted_archive(
    session, seeded_archive, archive
):
    """AC 8, and the only test in this file whose expectation is a file.

    The months that carry are read out of the fixture, never recomputed from its
    rows — a test that re-derived the count would be checking the archive against
    itself. The fixture's dismissed and irrelevant rows are what make the
    assertion sharp: three of them carry the highest importance in their month, so
    an implementation that counted them would both add a month and cite the wrong
    row.
    """
    hooks = plan.recompute(
        session,
        seeded_archive,
        invoke=_Generator(*(_prose(f"K{i}") for i in range(1, 4))),
        now=_NOW,
    )

    vorjahr = [h for h in hooks if h.source_kind is HookSource.VORJAHR]
    assert [h.month for h in vorjahr] == archive["carried_plan_months"]
    # A previous-year month names a month and nothing finer, so no day is invented.
    assert all(h.day is None for h in vorjahr)


def test_a_carried_month_cites_the_weightiest_visible_analysis(
    session, seeded_archive, archive
):
    """The evidence a carried month resolves to, against the fixture's own answer.

    Highest importance, then lowest id — and never a dismissed row, which is why
    the fixture gives its dismissed articles the top importance of their month.
    """
    hooks = plan.recompute(
        session,
        seeded_archive,
        invoke=_Generator(*(_prose(f"K{i}") for i in range(1, 4))),
        now=_NOW,
    )

    cited = {}
    for hook in hooks:
        if hook.source_kind is not HookSource.VORJAHR:
            continue
        analysis = session.get(Analysis, hook.source_id)
        cited[hook.month] = session.get(Article, analysis.article_id).title
    assert cited == archive["evidence_headline_per_plan_month"]


def test_a_month_the_archive_did_not_carry_stays_empty(session, seeded_archive, archive):
    """AC 6: an empty month is an answer, not a slot to fill.

    The fixture's 2026-12 is the pointed case — its previous-year counterpart has
    five stored analyses and only two visible ones, so a plan that filled the
    month would be filling it off rows a person already threw out.
    """
    plan.recompute(
        session,
        seeded_archive,
        invoke=_Generator(*(_prose(f"K{i}") for i in range(1, 4))),
        now=_NOW,
    )

    months = dict(plan.read(session, seeded_archive, now=_NOW))
    for month in archive["empty_plan_months"]:
        assert months[month] == [], month


# --- Evidence resolution ----------------------------------------------------------


def test_an_unresolvable_source_id_is_refused_for_every_hook_class(session, mandate):
    """AC 2's second half, at the guard that enforces it.

    ``_resolves`` is the last gate before a hook is written, and it is a gate
    rather than a warning: a hook whose evidence does not resolve is not a weak
    hook, it is not a hook. Checked per class, because each one points into a
    different table and a mapping that lost an entry would fail silently.
    """
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=20))
    hit = _radar_hit(session, mandate, title="Wärmepumpe im Bestand", days_ago=3)
    analysis = _coverage(
        session, mandate, title="Rückblick", published_at=_NOW - dt.timedelta(days=365)
    )

    stored = {
        HookSource.MARKTSIGNAL: signal.id,
        HookSource.THEMA: hit.id,
        HookSource.VORJAHR: analysis.id,
    }
    for kind, source_id in stored.items():
        assert plan._resolves(session, kind, source_id) is True, kind
        assert plan._resolves(session, kind, source_id + 9999) is False, kind


def test_every_stored_hook_resolves_to_a_row(session, seeded_archive):
    """The same rule as an invariant over a real recompute, so the guard cannot
    be satisfied by a code path that simply never reaches it."""
    hooks = plan.recompute(
        session,
        seeded_archive,
        invoke=_Generator(*(_prose(f"K{i}") for i in range(1, 4))),
        now=_NOW,
    )

    assert hooks
    assert all(plan._resolves(session, h.source_kind, h.source_id) for h in hooks)


# --- What the model is and is not allowed to contribute ---------------------------


def test_a_date_the_model_names_is_discarded_rather_than_stored(session, mandate):
    """AC 4, with the generation injected: the model volunteers a date and a
    source, and neither reaches the row.

    This is the property the whole feature turns on, so the fake answer is
    deliberately generous — an ISO date, a month, a day and a "beleg" — and the
    assertion is that the hook still carries the *signal's* November date and the
    *signal's* id. The schema has no field any of those could have landed in,
    which is the mechanism; this test is what keeps it that way.
    """
    signal = _signal(
        session,
        mandate,
        title="Konsultation schließt",
        deadline_at=dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.UTC),
    )

    hooks = plan.recompute(
        session,
        mandate,
        invoke=_Generator(
            _prose(
                datum="2026-12-24",
                date="2026-12-24",
                month="2026-12",
                day=24,
                beleg="https://erfunden.example/nichts",
                source_id=4242,
            )
        ),
        now=_NOW,
    )

    hook = hooks[0]
    assert (hook.month, hook.day) == ("2026-11", 12)
    assert hook.source_id == signal.id
    assert hook.source_kind is HookSource.MARKTSIGNAL
    # What the model was allowed to write did land, so the test is not passing
    # because the whole answer was thrown away.
    assert hook.reason == "Das Mandat betreibt genau das."
    assert hook.format == "statement"


def test_the_prompt_shows_the_dates_and_asks_only_for_prose(session, mandate):
    """The other side of the same rule: the model is *told* the date rather than
    asked for one, so its answer has nothing to add to."""
    _signal(
        session,
        mandate,
        title="Konsultation schließt",
        deadline_at=dt.datetime(2026, 9, 18, 12, 0, tzinfo=dt.UTC),
    )
    invoke = _Generator(_prose())

    plan.recompute(session, mandate, invoke=invoke, now=_NOW)

    assert "K1: Marktsignal" in invoke.prompt
    assert "18.09.2026" in invoke.prompt
    assert "Solarhaus AG" in invoke.prompt


def test_a_format_the_registry_does_not_know_is_dropped(session, mandate):
    """The plan page pre-selects this key in the format picker, so an invented
    one would break exactly that click."""
    _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=20))

    hooks = plan.recompute(
        session,
        mandate,
        invoke=_Generator({"ref": "K1", "reason": "Passt.", "format": "kalenderblatt"}),
        now=_NOW,
    )

    assert hooks[0].format == ""
    assert hooks[0].reason == "Passt."


def test_a_failed_model_call_costs_the_prose_and_not_the_date(session, mandate):
    """The hook exists because of its evidence, with or without prose."""
    signal = _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=20))

    def _broken(prompt: str, **kwargs) -> str:
        raise AnalyzerError("no backend today")

    hooks = plan.recompute(session, mandate, invoke=_broken, now=_NOW)

    assert len(hooks) == 1
    assert hooks[0].source_id == signal.id
    assert hooks[0].reason == ""


def test_a_mandate_without_evidence_spends_no_model_call(session, mandate, unasked):
    """No stored signal, no measured resonance, no archive: nothing to say and
    nothing to pay for.

    The counter is the assertion, not the empty list: a plan that called the
    model and then stored nothing would still return ``[]``.
    """
    assert plan.recompute(session, mandate, invoke=unasked, now=_NOW) == []
    assert unasked.calls == 0


# --- Recompute against a person's decisions ---------------------------------------


def _one_signal_hook(session, mandate) -> PlanHook:
    _signal(
        session,
        mandate,
        title="Konsultation schließt",
        deadline_at=dt.datetime(2026, 9, 18, 12, 0, tzinfo=dt.UTC),
    )
    return plan.recompute(session, mandate, invoke=_Generator(_prose()), now=_NOW)[0]


def test_an_untouched_proposal_is_replaced_by_a_recompute(session, mandate):
    """The half of the contract that lets the plan move at all.

    Identity is asserted through the prose rather than through the row id:
    SQLite hands the deleted row's rowid straight back to the insert that
    follows, so "the id changed" would be a claim about the storage engine and
    not about the plan.
    """
    _one_signal_hook(session, mandate)

    second = plan.recompute(
        session, mandate, invoke=_Generator(_prose(reason="Neu begründet.")), now=_NOW
    )

    assert len(second) == 1
    assert second[0].reason == "Neu begründet."
    assert len(session.scalars(select(PlanHook)).all()) == 1


def test_an_untouched_proposal_whose_source_fell_out_of_the_window_is_dropped(
    session, mandate, unasked
):
    """The deletion half of the same contract, where a replacement cannot hide it.

    The signal's deadline has passed by the time the second recompute runs, so
    the source proposes nothing — and the stale proposal has to go rather than
    stand in the plan as a date nobody will act on again.
    """
    _one_signal_hook(session, mandate)

    plan.recompute(
        session,
        mandate,
        invoke=unasked,
        now=dt.datetime(2026, 9, 19, 9, 0, tzinfo=dt.UTC),
    )

    assert session.scalars(select(PlanHook)).all() == []


@pytest.mark.parametrize("decide", [plan.accept, plan.discard])
def test_a_decided_hook_survives_a_recompute_and_is_not_proposed_again(
    session, mandate, decide, unasked
):
    """AC 5. A "verworfen" that came back the next morning would train the reader
    to stop deciding at all, so the row is what stops the next proposal."""
    hook = _one_signal_hook(session, mandate)
    decide(session, hook, now=_NOW)
    state_before = hook.state

    plan.recompute(session, mandate, invoke=unasked, now=_NOW)

    # A source a surviving hook already points at is not proposed a second time,
    # so there is nothing fresh to write prose about either.
    assert unasked.calls == 0
    survivors = session.scalars(select(PlanHook)).all()
    assert [h.id for h in survivors] == [hook.id]
    assert survivors[0].state is state_before
    assert survivors[0].decided_at == _NOW


def test_a_moved_hook_survives_a_recompute_in_the_month_a_person_chose(
    session, mandate, unasked
):
    """A move is a touch even while the state is still the machine's: the hook is
    a person's proposal now."""
    hook = _one_signal_hook(session, mandate)
    plan.move(session, hook, "2026-12", now=_NOW)

    plan.recompute(session, mandate, invoke=unasked, now=_NOW)

    # A source a surviving hook already points at is not proposed a second time,
    # so there is nothing fresh to write prose about either.
    assert unasked.calls == 0
    survivors = session.scalars(select(PlanHook)).all()
    assert [h.id for h in survivors] == [hook.id]
    assert survivors[0].month == "2026-12"
    assert survivors[0].state is HookState.VORGESCHLAGEN
    assert survivors[0].moved_at == _NOW


def test_moving_a_hook_to_another_month_clears_the_day(session, mandate):
    """The source's date belongs to the source's month; carrying the day into a
    month a person chose would date the hook to a day nobody named."""
    hook = _one_signal_hook(session, mandate)
    assert hook.day == 18

    plan.move(session, hook, "2026-12", now=_NOW)

    assert hook.day is None


def test_moving_a_hook_inside_its_own_month_keeps_its_day(session, mandate):
    """Nothing about the date changed, so nothing about the date is thrown away."""
    hook = _one_signal_hook(session, mandate)

    plan.move(session, hook, "2026-09", now=_NOW)

    assert hook.day == 18


@pytest.mark.parametrize("month", ["2026-13", "2026", "2026-1", "Dezember", "2026-ab"])
def test_move_refuses_anything_that_is_not_a_plan_month(session, mandate, month):
    hook = _one_signal_hook(session, mandate)

    with pytest.raises(ValueError):
        plan.move(session, hook, month, now=_NOW)


# --- The window -------------------------------------------------------------------


def test_the_plan_reaches_six_months_from_the_current_one(session, mandate):
    """AC 7's first half, against a window written out by hand."""
    assert [month for month, _ in plan.read(session, mandate, now=_NOW)] == _WINDOW


def test_an_older_hook_falls_out_of_the_plan_without_being_deleted(session, mandate):
    """AC 7's second half. The hook leaves the *read*, never the table — a plan
    that deleted last quarter's accepted hooks would delete the record of what
    the agency actually did."""
    signal = _signal(session, mandate, effective_at=_NOW - dt.timedelta(days=200))
    old = PlanHook(
        client_id=mandate.id,
        source_kind=HookSource.MARKTSIGNAL,
        source_id=signal.id,
        month="2026-02",
        day=4,
        title="Im Februar erledigt",
        state=HookState.ANGENOMMEN,
        decided_at=_NOW - dt.timedelta(days=180),
    )
    session.add(old)
    session.commit()

    months = dict(plan.read(session, mandate, now=_NOW))

    assert "2026-02" not in months
    assert all(not hooks for hooks in months.values())
    assert session.get(PlanHook, old.id) is not None


def test_a_recompute_leaves_hooks_before_the_window_alone(session, mandate, unasked):
    """The delete is bounded by the window for the same reason the read is."""
    signal = _signal(session, mandate, effective_at=_NOW - dt.timedelta(days=200))
    old = PlanHook(
        client_id=mandate.id,
        source_kind=HookSource.MARKTSIGNAL,
        source_id=signal.id,
        month="2026-02",
        title="Im Februar vorgeschlagen",
    )
    session.add(old)
    session.commit()

    plan.recompute(session, mandate, invoke=unasked, now=_NOW)

    assert session.get(PlanHook, old.id) is not None


def test_a_dated_hook_sorts_before_an_undated_one_in_its_month(session, mandate):
    """An undated hook rendered above the 5th would read as "before the 5th",
    which is a claim its source does not make."""
    _signal(
        session,
        mandate,
        title="Konsultation schließt",
        deadline_at=dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.UTC),
    )
    for index, day in enumerate((30, 20, 10)):
        _radar_hit(session, mandate, title=f"Wärmepumpe im Bestand {index}", days_ago=day)

    plan.recompute(
        session, mandate, invoke=_Generator(_prose("K1"), _prose("K2")), now=_NOW
    )

    current = dict(plan.read(session, mandate, now=_NOW))[_WINDOW[0]]
    assert [hook.day for hook in current] == [28, None]


def test_month_key_files_a_moment_under_the_month_a_reader_lived_through(session):
    """Local, not UTC: a regulation effective at midnight on the first belongs to
    the month a Berlin reader files it under, not to the UTC evening before."""
    midnight_in_berlin = dt.datetime(2026, 9, 30, 22, 30, tzinfo=dt.UTC)

    assert plan.month_key(midnight_in_berlin) == "2026-10"


# --- The weekly window ------------------------------------------------------------


def test_a_mandate_that_was_never_computed_is_due(session, mandate):
    assert plan.due(session, mandate, now=_NOW) is True


def test_a_mandate_computed_inside_the_window_is_not_due_again(
    session, mandate, unasked
):
    plan.recompute(session, mandate, invoke=unasked, now=_NOW)

    later = _NOW + plan.PLAN_REFRESH_AFTER - dt.timedelta(hours=1)
    assert plan.due(session, mandate, now=later) is False


def test_a_mandate_is_due_again_once_the_window_has_passed(session, mandate, unasked):
    plan.recompute(session, mandate, invoke=unasked, now=_NOW)

    later = _NOW + plan.PLAN_REFRESH_AFTER
    assert plan.due(session, mandate, now=later) is True


def test_an_unreadable_stamp_makes_the_mandate_due(session, mandate):
    """A hand-edited or older value costs one extra recompute, never a plan that
    silently stops refreshing."""
    session.add(
        Setting(key=f"plan_computed_at:{mandate.id}", value="letzten Donnerstag")
    )
    session.commit()

    assert plan.due(session, mandate, now=_NOW) is True


def test_a_recompute_whose_model_call_failed_still_closes_the_window(session, mandate):
    """The failure the weekly window exists to bound.

    The stamp goes down at the *start* of a recompute, so a backend that is down
    costs one attempt a week rather than one attempt a night — which is the whole
    difference between a degraded plan and a bill.
    """
    _signal(session, mandate, effective_at=_NOW + dt.timedelta(days=20))

    def _broken(prompt: str, **kwargs) -> str:
        raise AnalyzerError("no backend today")

    plan.recompute(session, mandate, invoke=_broken, now=_NOW)

    assert plan.due(session, mandate, now=_NOW) is False


# --- The sweep's wiring -----------------------------------------------------------


def test_the_sweep_recomputes_a_due_plan(session, mandate, no_plan_recompute):
    """The stage the autouse fixture normally stubs out, driven directly.

    No evidence is seeded, so no model call can be spent — what is under test is
    that the sweep reaches ``plan.recompute`` at all, and the stamp it leaves
    behind is the proof.
    """
    recompute_plans = no_plan_recompute

    assert recompute_plans(session, [mandate], now=_NOW) == 1
    assert plan.due(session, mandate, now=_NOW) is False


def test_the_sweep_leaves_a_yardstick_out_of_the_plan(session, no_plan_recompute):
    """A competitor is tracked to compare its share of the conversation; nobody
    plans its months."""
    rival = Client(name="Zolar", is_competitor=True)
    session.add(rival)
    session.commit()

    assert no_plan_recompute(session, [rival], now=_NOW) == 0
    assert plan.due(session, rival, now=_NOW) is True


def test_the_sweep_defers_the_mandates_beyond_its_per_run_cap(
    session, no_plan_recompute
):
    """On the first morning after a deploy every mandate is due at once, and a
    deferred mandate is simply still due tomorrow."""
    mandates = []
    for index in range(job._PLANS_PER_SWEEP + 2):
        client = Client(name=f"Mandat {index}")
        session.add(client)
        mandates.append(client)
    session.commit()

    assert no_plan_recompute(session, mandates, now=_NOW) == job._PLANS_PER_SWEEP
    assert [plan.due(session, c, now=_NOW) for c in mandates[job._PLANS_PER_SWEEP :]] == [
        True,
        True,
    ]


def test_a_failing_recompute_does_not_cost_the_other_mandates(
    session, no_plan_recompute, monkeypatch
):
    """A missing plan is a stale tab, not a broken morning."""
    first = Client(name="Bricht ab")
    second = Client(name="Läuft durch")
    session.add_all([first, second])
    session.commit()
    real = plan.recompute

    def _explodes(inner_session, client, **kwargs):
        if client.name == "Bricht ab":
            raise RuntimeError("die Neuberechnung ist gescheitert")
        return real(inner_session, client, **kwargs)

    monkeypatch.setattr(plan, "recompute", _explodes)

    assert no_plan_recompute(session, [first, second], now=_NOW) == 1
    assert plan.due(session, second, now=_NOW) is False
