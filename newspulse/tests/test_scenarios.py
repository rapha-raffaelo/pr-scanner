"""Szenarien, Auslöser und Reaktionsoptionen (RIS-04).

Nothing here reaches a model and nothing reaches the network: both model calls
in :mod:`newspulse.scenarios` are exercised with injected ``invoke`` callables
returning canned JSON, and the tests that must prove a call never happened
inject one that fails the test if it fires.

The disciplines under test, in order:

* **A scenario is never a forecast.** Three courses per issue, the likelihood
  as a word out of a closed set and never as a percentage, and a narrative
  written as a statement of fact is refused rather than stored.
* **A trigger is machine-checkable or it is not a trigger** (DEC-5 option A).
  A course whose triggers all fall outside the closed set is not stored at
  all, and every member of the set has a reader that answers it off stored
  rows.
* **A fired trigger fires once, ever.** The latch is a column, so a second
  sweep and a fresh process both find it already fired.
* **Nothing is numbered or dated that the stored lines do not carry.**
* **"Nicht reagieren" is on the list**, graded on the same three columns, and
  a set without it is refused; the recommendation names a speed out of six.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import brain, scenarios
from newspulse.matching import title_hash
from newspulse.models import (
    RESPONSE_OPTIONS_MIN,
    Angle,
    Article,
    Base,
    Client,
    ClientFact,
    EscalationPotential,
    Issue,
    IssueSignal,
    Outreach,
    OutreachReply,
    ResponseOption,
    ResponseSpeed,
    Scenario,
    ScenarioKind,
    ScenarioLikelihood,
    Stakeholder,
    StakeholderLevel,
    TriggerCondition,
)

_NOW = dt.datetime(2026, 9, 3, 8, 0, tzinfo=dt.UTC)


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
def mandate(session) -> Client:
    client = Client(name="Solaris AG", aliases=["Solaris"], industry="Solarenergie")
    session.add(client)
    session.commit()
    return client


def _article(
    session,
    title: str,
    *,
    source: str = "Rheinische Post",
    summary: str | None = None,
) -> Article:
    article = Article(
        title=title,
        url=f"https://example.de/{abs(hash((title, source)))}",
        source=source,
        published_at=_NOW - dt.timedelta(days=2),
        fetched_at=_NOW - dt.timedelta(days=2),
        summary_text=summary,
        title_hash=title_hash(title, source),
    )
    session.add(article)
    session.commit()
    return article


def _issue(session, client: Client, *, articles: list[Article] | None = None) -> Issue:
    """One open issue with its founding signals, the way the register opens it."""
    rows = articles if articles is not None else [_article(session, "Vorwurf im Werk")]
    issue = Issue(
        client_id=client.id,
        title="Vorwurf Vertragsklauseln",
        opened_by="mensch",
        opened_at=_NOW - dt.timedelta(days=3),
        last_moved_at=_NOW - dt.timedelta(days=1),
    )
    for article in rows:
        issue.signals.append(
            IssueSignal(
                article_id=article.id,
                reason="Teil der angenommenen Wiederholung.",
                attached_by="mensch",
                attached_at=_NOW - dt.timedelta(days=1),
                happened_at=article.published_at,
            )
        )
    session.add(issue)
    session.commit()
    return issue


def _group(session, client: Client, name: str = "Anwohner am Standort") -> Stakeholder:
    row = Stakeholder(
        client_id=client.id,
        group_name=name,
        betroffenheit="Wohnen neben dem Werksgelände.",
        einfluss=StakeholderLevel.MITTEL,
        set_by="mensch",
        set_at=_NOW,
    )
    session.add(row)
    session.commit()
    return row


def _course(
    *,
    art: str = "wahrscheinlicher",
    verlauf: str = "Die Kritik könnte sich fortsetzen und weitere Medien erreichen.",
    wahrscheinlichkeit: str = "möglich",
    ausloeser: list[str] | None = None,
    stakeholder: list[str] | None = None,
    kommunikationsbedarf: str = "",
) -> dict:
    return {
        "art": art,
        "verlauf": verlauf,
        "wahrscheinlichkeit": wahrscheinlichkeit,
        "ausloeser": ["zweites_medium"] if ausloeser is None else ausloeser,
        "stakeholder": stakeholder or [],
        "kommunikationsbedarf": kommunikationsbedarf,
    }


def _answer(*courses: dict) -> str:
    return json.dumps({"szenarien": list(courses)})


def _three_courses() -> str:
    return _answer(
        _course(art="bester", verlauf="Die Sache könnte ohne Nachlauf enden."),
        _course(art="wahrscheinlicher"),
        _course(
            art="schlechtester",
            verlauf="Eine Behörde würde ein Verfahren eröffnen.",
            wahrscheinlichkeit="unwahrscheinlich",
            ausloeser=["leitmedium"],
        ),
    )


def _option(
    *,
    option: str = "Sprecher gibt ein Hintergrundgespräch",
    nutzen: str = "Der Vorwurf wird eingeordnet.",
    risiko: str = "Das Thema bleibt in der Berichterstattung.",
    eskalationspotenzial: str = "mittel",
    nicht_reagieren: bool = False,
    empfohlen: bool = False,
    geschwindigkeit: str = "",
) -> dict:
    return {
        "option": option,
        "nutzen": nutzen,
        "risiko": risiko,
        "eskalationspotenzial": eskalationspotenzial,
        "nicht_reagieren": nicht_reagieren,
        "empfohlen": empfohlen,
        "geschwindigkeit": geschwindigkeit,
    }


def _silence(**kwargs) -> dict:
    base = {
        "option": "Nicht reagieren",
        "nutzen": "Der Sache wird keine Öffentlichkeit verschafft.",
        "risiko": "Ein Vorwurf bleibt unwidersprochen stehen.",
        "eskalationspotenzial": "niedrig",
        "nicht_reagieren": True,
    }
    return _option(**{**base, **kwargs})


def _options_answer(*options: dict) -> str:
    return json.dumps({"optionen": list(options)})


def _three_options() -> str:
    return _options_answer(
        _option(),
        _option(option="Schriftliche Stellungnahme", eskalationspotenzial="hoch"),
        _silence(empfohlen=True, geschwindigkeit="vorbereiten und beobachten"),
    )


def _never_called(*args, **kwargs):
    raise AssertionError("the model was asked, and it must not have been")


# --- Three courses, and what makes one a scenario ---------------------------------


def test_three_courses_are_stored_one_per_kind(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session, issue, invoke=lambda *a, **k: _three_courses(), now=_NOW
    )
    assert [row.kind for row in stored] == [
        ScenarioKind.BESTER,
        ScenarioKind.WAHRSCHEINLICHER,
        ScenarioKind.SCHLECHTESTER,
    ]
    assert session.scalar(select(Scenario.issue_id).limit(1)) == issue.id


def test_the_likelihood_is_stored_as_one_of_the_words(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session, issue, invoke=lambda *a, **k: _three_courses(), now=_NOW
    )
    assert all(isinstance(row.likelihood, ScenarioLikelihood) for row in stored)
    assert stored[1].likelihood is ScenarioLikelihood.MOEGLICH


def test_a_percentage_as_the_likelihood_drops_the_course(session, mandate):
    """The whole point of the closed word set: a percentage out of a model
    claims an accuracy that does not exist, and it is the number that is quoted
    back four weeks later. It is never stored, not even rounded to a word."""
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(_course(wahrscheinlichkeit="65%")),
        now=_NOW,
    )
    assert stored == []
    assert session.scalars(select(Scenario)).all() == []


def test_a_course_written_as_a_statement_of_fact_is_discarded(session, mandate):
    """"Jedes Szenario ist im Text als Szenario gekennzeichnet", with the
    injected generation the acceptance asks for: a course that reads as a
    finding about the future is quoted back as one, so it is refused."""
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(
            _course(
                verlauf="Die Behörde eröffnet ein Verfahren und der Vorstand tritt zurück."
            )
        ),
        now=_NOW,
    )
    assert stored == []


def test_a_hedged_course_is_kept(session, mandate):
    """The other half of the same rule, so the guard is not simply refusing
    everything: the same sentence in the conditional is stored."""
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(
            _course(
                verlauf="Die Behörde könnte ein Verfahren eröffnen, der Vorstand "
                "müsste sich äußern."
            )
        ),
        now=_NOW,
    )
    assert len(stored) == 1
    assert "könnte" in stored[0].narrative


def test_the_affected_groups_come_from_the_standing_map(session, mandate):
    _group(session, mandate)
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(
            _course(stakeholder=["Anwohner am Standort", "Erfundener Verband"])
        ),
        now=_NOW,
    )
    names = [row.stakeholder.group_name for row in stored[0].groups]
    assert names == ["Anwohner am Standort"]


def test_a_second_generation_keeps_the_set_that_stands(session, mandate):
    """Idempotent, and the reason is the trigger latch: re-asking would replace
    narratives a consultant has read into a meeting and re-arm conditions that
    have already been reported."""
    issue = _issue(session, mandate)
    scenarios.generate_scenarios(
        session, issue, invoke=lambda *a, **k: _three_courses(), now=_NOW
    )
    again = scenarios.generate_scenarios(session, issue, invoke=_never_called, now=_NOW)
    assert len(again) == 3


def test_clearing_the_courses_lets_a_person_ask_again(session, mandate):
    issue = _issue(session, mandate)
    scenarios.generate_scenarios(
        session, issue, invoke=lambda *a, **k: _three_courses(), now=_NOW
    )
    assert scenarios.clear_scenarios(session, issue) == 3
    assert scenarios.stored_scenarios(session, issue) == []


# --- The figure rule ---------------------------------------------------------------


def test_a_figure_the_stored_lines_do_not_carry_drops_the_course(session, mandate):
    """"Das Modell liefert keine Zahl und kein Datum, die nicht in einer
    benannten Zeile stehen", with the injected generation."""
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(
            _course(
                verlauf="Bis zum 15. Oktober könnten 4000 Haushalte betroffen sein."
            )
        ),
        now=_NOW,
    )
    assert stored == []


def test_a_figure_a_stored_line_does_carry_is_kept(session, mandate):
    """The other half: the guard measures against the very lines the prompt was
    shown, so a number quoted out of a headline survives."""
    article = _article(session, "Werk 3 in der Kritik: Anwohner beschweren sich")
    issue = _issue(session, mandate, articles=[article])
    stored = scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(
            _course(verlauf="Die Kritik an Werk 3 könnte sich ausweiten.")
        ),
        now=_NOW,
    )
    assert len(stored) == 1


# --- DEC-5: the closed set of triggers ---------------------------------------------


def test_a_course_without_a_checkable_trigger_is_not_stored(session, mandate):
    """DEC-5 option A in one test: a trigger that is only well phrased is never
    fired and is therefore not a trigger, so the course it hangs on is not
    evidence of anything and is refused."""
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(
            _course(ausloeser=["wenn die Stimmung im Ort kippt"])
        ),
        now=_NOW,
    )
    assert stored == []


def test_an_unknown_trigger_beside_a_known_one_leaves_the_known_one(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(
            _course(ausloeser=["etwas Erfundenes", "leitmedium"])
        ),
        now=_NOW,
    )
    assert [t.condition for t in stored[0].triggers] == [TriggerCondition.LEITMEDIUM]


def test_every_condition_of_the_closed_set_has_a_reader():
    """A member added to the enum without a reader here would look like a
    trigger and never fire, which is the exact state DEC-5 was decided
    against."""
    assert set(scenarios.CONDITION_READERS) == set(TriggerCondition)
    assert set(scenarios.CONDITION_LABELS) == set(TriggerCondition)


# --- Firing, once, and never again -------------------------------------------------


def _with_trigger(
    session, mandate, condition: TriggerCondition, *, articles=None
) -> Issue:
    """One issue carrying one course whose only trigger is ``condition``."""
    issue = _issue(session, mandate, articles=articles)
    scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(_course(ausloeser=[condition.value])),
        now=_NOW,
    )
    return issue


def test_a_second_independent_outlet_fires_the_condition(session, mandate):
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.ZWEITES_MEDIUM,
        articles=[
            _article(session, "Vorwurf im Werk", source="Rheinische Post"),
            _article(session, "Vorwurf im Werk, zweiter Bericht", source="WDR"),
        ],
    )
    fired = scenarios.check_triggers(session, mandate, issue, now=_NOW)
    assert [row.condition for row in fired] == [TriggerCondition.ZWEITES_MEDIUM]
    assert fired[0].fired_at == _NOW
    assert "WDR" in fired[0].fired_note or "Rheinische" in fired[0].fired_note


def test_one_outlet_alone_does_not_fire_the_second_outlet_condition(session, mandate):
    issue = _with_trigger(session, mandate, TriggerCondition.ZWEITES_MEDIUM)
    assert scenarios.check_triggers(session, mandate, issue, now=_NOW) == []


def test_a_top_tier_outlet_fires_the_leitmedium_condition(session, mandate):
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.LEITMEDIUM,
        articles=[_article(session, "Vorwurf im Werk", source="FAZ")],
    )
    fired = scenarios.check_triggers(session, mandate, issue, now=_NOW)
    assert [row.condition for row in fired] == [TriggerCondition.LEITMEDIUM]


def test_the_mandate_in_a_headline_fires_its_condition(session, mandate):
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.MANDAT_IN_UEBERSCHRIFT,
        articles=[_article(session, "Solaris AG unter Druck im Werk")],
    )
    fired = scenarios.check_triggers(session, mandate, issue, now=_NOW)
    assert [row.condition for row in fired] == [
        TriggerCondition.MANDAT_IN_UEBERSCHRIFT
    ]
    assert "Solaris AG" in fired[0].fired_note


def test_a_headline_that_does_not_name_the_mandate_does_not_fire(session, mandate):
    issue = _with_trigger(session, mandate, TriggerCondition.MANDAT_IN_UEBERSCHRIFT)
    assert scenarios.check_triggers(session, mandate, issue, now=_NOW) == []


def test_a_reply_in_the_connected_mailbox_fires_the_enquiry_condition(
    session, mandate
):
    issue = _with_trigger(session, mandate, TriggerCondition.MEDIENANFRAGE)
    angle = Angle(
        client_id=mandate.id,
        generated_at=_NOW,
        subject="Thema",
        message="Text",
        context="",
        thesis="",
        overclaim="",
        statements=[],
    )
    session.add(angle)
    session.flush()
    letter = Outreach(
        angle_id=angle.id,
        client_id=mandate.id,
        generated_at=_NOW,
        journalist="Mara Wolf",
        outlet="WDR",
        subject="Anfrage",
        message="Sehr geehrte Frau Wolf,",
    )
    session.add(letter)
    session.flush()
    session.add(
        OutreachReply(
            outreach_id=letter.id,
            gmail_message_id="m-1",
            from_name="Mara Wolf",
            from_email="mara@wdr.de",
            received_at=_NOW - dt.timedelta(hours=2),
            body="Haben Sie eine Stellungnahme?",
        )
    )
    session.commit()

    fired = scenarios.check_triggers(session, mandate, issue, now=_NOW)
    assert [row.condition for row in fired] == [TriggerCondition.MEDIENANFRAGE]
    assert "Mara Wolf" in fired[0].fired_note


def test_a_management_name_from_the_profile_fires_its_condition(session, mandate):
    session.add(
        ClientFact(client_id=mandate.id, key="ceo", value="Anna Berger, Vorstand")
    )
    session.commit()
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.MANAGEMENT_GENANNT,
        articles=[
            _article(
                session,
                "Vorwurf im Werk",
                summary="Anna Berger weist die Vorwürfe zurück.",
            )
        ],
    )
    fired = scenarios.check_triggers(session, mandate, issue, now=_NOW)
    assert [row.condition for row in fired] == [TriggerCondition.MANAGEMENT_GENANNT]
    assert "Anna Berger" in fired[0].fired_note


def test_a_management_name_is_never_guessed_where_the_profile_is_empty(
    session, mandate
):
    """The profile is the only source: a person this tool inferred to be an
    executive would put a stranger's name on a red mark at an issue."""
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.MANAGEMENT_GENANNT,
        articles=[
            _article(session, "Vorwurf im Werk", summary="Anna Berger äußert sich.")
        ],
    )
    assert scenarios.check_triggers(session, mandate, issue, now=_NOW) == []


def test_a_management_name_beside_its_role_still_fires(session, mandate):
    """The field is "Namen *und Rollen* der Geschäftsführung", so a name and
    its function in one line is the expected input. The role goes on the other
    side of the split; a part carrying it would be a string no headline can
    contain, and the condition would look like a trigger and never fire."""
    session.add(
        ClientFact(
            client_id=mandate.id,
            key="ceo",
            value="Geschäftsführung: Anna Berger (CEO) und Tom Klein",
        )
    )
    session.commit()
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.MANAGEMENT_GENANNT,
        articles=[
            _article(
                session,
                "Vorwurf im Werk",
                summary="Tom Klein bestätigt die Prüfung.",
            )
        ],
    )
    fired = scenarios.check_triggers(session, mandate, issue, now=_NOW)
    assert [row.condition for row in fired] == [TriggerCondition.MANAGEMENT_GENANNT]
    assert fired[0].fired_note == "Tom Klein"


def test_a_role_word_alone_never_fires_the_management_condition(session, mandate):
    """"Vorstand" stands in a large share of German business coverage, and the
    latch only fires once: a firing on a role word would report a word and
    spend the trigger the real event was armed for."""
    session.add(
        ClientFact(client_id=mandate.id, key="ceo", value="Vorstand, Pressestelle")
    )
    session.commit()
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.MANAGEMENT_GENANNT,
        articles=[
            _article(
                session,
                "Vorwurf im Werk",
                summary="Der Vorstand äußert sich nicht zu den Vorwürfen.",
            )
        ],
    )
    assert scenarios.check_triggers(session, mandate, issue, now=_NOW) == []


def test_a_name_inside_a_longer_word_is_not_a_naming(session, mandate):
    """The ingest's matcher, not ``in``: the lookarounds are what stop "Berger"
    being found inside "Bergerhoff"."""
    session.add(
        ClientFact(client_id=mandate.id, key="ceo", value="Anna Berger, Vorstand")
    )
    session.commit()
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.MANAGEMENT_GENANNT,
        articles=[
            _article(
                session,
                "Vorwurf im Werk",
                summary="Die Kanzlei Annabergerhoff prüft die Vorwürfe.",
            )
        ],
    )
    assert scenarios.check_triggers(session, mandate, issue, now=_NOW) == []


def test_a_fired_trigger_does_not_fire_a_second_time(session, mandate):
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.LEITMEDIUM,
        articles=[_article(session, "Vorwurf im Werk", source="FAZ")],
    )
    assert len(scenarios.check_triggers(session, mandate, issue, now=_NOW)) == 1
    later = _NOW + dt.timedelta(days=1)
    assert scenarios.check_triggers(session, mandate, issue, now=later) == []


def test_a_fired_trigger_survives_a_restart(session, factory, mandate):
    """The acceptance says "auch nicht nach einem Neustart", so the latch is a
    column and not a set in memory: a fresh session against the same database
    finds the condition already fired and reports nothing."""
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.LEITMEDIUM,
        articles=[_article(session, "Vorwurf im Werk", source="FAZ")],
    )
    assert len(scenarios.check_triggers(session, mandate, issue, now=_NOW)) == 1

    with factory() as fresh:
        again = fresh.get(Issue, issue.id)
        client = fresh.get(Client, mandate.id)
        assert scenarios.check_triggers(fresh, client, again, now=_NOW) == []
        assert len(scenarios.fired_triggers(fresh, again)) == 1


def test_the_mark_says_what_matched(session, mandate):
    """A red mark that cannot say what it saw is one nobody can act on, and the
    CHECK on the table holds that against every future writer."""
    issue = _with_trigger(
        session,
        mandate,
        TriggerCondition.LEITMEDIUM,
        articles=[_article(session, "Vorwurf im Werk", source="FAZ")],
    )
    scenarios.check_triggers(session, mandate, issue, now=_NOW)
    marks = scenarios.fired_triggers(session, issue)
    assert len(marks) == 1
    assert marks[0].fired_note.strip()


# --- The response options ----------------------------------------------------------


def test_three_options_are_stored_with_the_silence_among_them(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_options(
        session, issue, invoke=lambda *a, **k: _three_options(), now=_NOW
    )
    assert len(stored) == RESPONSE_OPTIONS_MIN
    assert any(row.no_response for row in stored)
    assert [row.position for row in stored] == [1, 2, 3]


def test_every_option_carries_benefit_risk_and_escalation(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_options(
        session, issue, invoke=lambda *a, **k: _three_options(), now=_NOW
    )
    for row in stored:
        assert row.benefit.strip()
        assert row.risk.strip()
        assert isinstance(row.escalation, EscalationPotential)


def test_a_set_without_the_silence_is_not_stored(session, mandate):
    """A tool that can only propose acting proposes acting, and the most
    expensive mistake in this trade is the statement that gives a matter the
    publicity it did not yet have. So the set is refused rather than shown."""
    issue = _issue(session, mandate)
    stored = scenarios.generate_options(
        session,
        issue,
        invoke=lambda *a, **k: _options_answer(
            _option(),
            _option(option="Schriftliche Stellungnahme"),
            _option(
                option="Interview", empfohlen=True, geschwindigkeit="heute"
            ),
        ),
        now=_NOW,
    )
    assert stored == []
    assert session.scalars(select(ResponseOption)).all() == []


def test_fewer_than_three_options_are_not_stored(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_options(
        session,
        issue,
        invoke=lambda *a, **k: _options_answer(
            _option(), _silence(empfohlen=True, geschwindigkeit="heute")
        ),
        now=_NOW,
    )
    assert stored == []


def test_the_recommendation_names_a_speed_from_the_closed_set(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_options(
        session, issue, invoke=lambda *a, **k: _three_options(), now=_NOW
    )
    chosen = scenarios.recommendation(stored)
    assert chosen is not None
    assert chosen.speed is ResponseSpeed.VORBEREITEN


def test_a_speed_outside_the_set_costs_the_recommendation_not_the_option(
    session, mandate
):
    """"Schnell" is not one of the six. The option is still an option; what it
    cannot be is the recommendation, because a recommendation that does not say
    how fast is what the closed set exists to prevent."""
    issue = _issue(session, mandate)
    stored = scenarios.generate_options(
        session,
        issue,
        invoke=lambda *a, **k: _options_answer(
            _option(),
            _option(option="Schriftliche Stellungnahme"),
            _silence(empfohlen=True, geschwindigkeit="schnell"),
        ),
        now=_NOW,
    )
    assert len(stored) == 3
    assert scenarios.recommendation(stored) is None
    assert all(row.speed is None for row in stored)


def test_two_recommendations_leave_exactly_one(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_options(
        session,
        issue,
        invoke=lambda *a, **k: _options_answer(
            _option(empfohlen=True, geschwindigkeit="sofort"),
            _option(
                option="Schriftliche Stellungnahme",
                empfohlen=True,
                geschwindigkeit="heute",
            ),
            _silence(),
        ),
        now=_NOW,
    )
    assert [row.recommended for row in stored] == [True, False, False]
    assert stored[0].speed is ResponseSpeed.SOFORT
    assert stored[1].speed is None


def test_an_option_with_an_invented_figure_is_dropped(session, mandate):
    """The same rule the courses take. Dropping the option costs the set its
    third member, so nothing is stored at all — which is the honest answer for
    an answer that invented a number."""
    issue = _issue(session, mandate)
    stored = scenarios.generate_options(
        session,
        issue,
        invoke=lambda *a, **k: _options_answer(
            _option(nutzen="Erreicht 12000 Anwohner."),
            _option(option="Schriftliche Stellungnahme"),
            _silence(empfohlen=True, geschwindigkeit="heute"),
        ),
        now=_NOW,
    )
    assert stored == []


def test_a_second_generation_keeps_the_options_that_stand(session, mandate):
    issue = _issue(session, mandate)
    scenarios.generate_options(
        session, issue, invoke=lambda *a, **k: _three_options(), now=_NOW
    )
    again = scenarios.generate_options(session, issue, invoke=_never_called, now=_NOW)
    assert len(again) == 3


def test_clearing_the_options_lets_a_person_ask_again(session, mandate):
    issue = _issue(session, mandate)
    scenarios.generate_options(
        session, issue, invoke=lambda *a, **k: _three_options(), now=_NOW
    )
    assert scenarios.clear_options(session, issue) == 3
    assert scenarios.stored_options(session, issue) == []


# --- The stamp ---------------------------------------------------------------------


def test_a_stored_course_says_which_standards_it_was_written_under(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_scenarios(
        session, issue, invoke=lambda *a, **k: _three_courses(), now=_NOW
    )
    assert all(row.brain_version == brain.version(session) for row in stored)


def test_a_stored_option_says_which_standards_it_was_written_under(session, mandate):
    issue = _issue(session, mandate)
    stored = scenarios.generate_options(
        session, issue, invoke=lambda *a, **k: _three_options(), now=_NOW
    )
    assert all(row.brain_version == brain.version(session) for row in stored)


# --- Every visible string is in the table ------------------------------------------


def test_every_visible_string_this_feature_writes_is_translated():
    """The notes and the labels are written in Python, so nothing that reads
    the templates for German strings can see them. They are walked off the
    module constants rather than copied, so a sentence added without its
    English pair fails here rather than on the evening a reader has the page in
    English."""
    from newspulse import i18n
    from newspulse.web.routes import issues_view

    known = set(i18n.known_keys())
    words = [
        *scenarios.KIND_LABELS.values(),
        *scenarios.CONDITION_LABELS.values(),
        *(member.value for member in ScenarioLikelihood),
        *(member.value for member in ResponseSpeed),
        *(member.value for member in EscalationPotential),
    ]
    for sentence in (*issues_view.SCENARIO_NOTES, *words):
        assert sentence in known, sentence


# --- The register page renders what was stored -------------------------------------


@pytest.fixture
def web(factory):
    from fastapi.testclient import TestClient

    from newspulse.web.app import create_app, get_db

    app = create_app()

    def _override():
        open_session = factory()
        try:
            yield open_session
        finally:
            open_session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _seeded_issue(session, mandate) -> Issue:
    """One issue carrying a full set: three courses, a fired mark, three options."""
    _group(session, mandate)
    issue = _issue(
        session, mandate, articles=[_article(session, "Vorwurf im Werk", source="FAZ")]
    )
    scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(
            _course(
                art="wahrscheinlicher",
                ausloeser=["leitmedium", "medienanfrage"],
                stakeholder=["Anwohner am Standort"],
                kommunikationsbedarf="Die Anwohner müssten erfahren, was geprüft wird.",
            )
        ),
        now=_NOW,
    )
    scenarios.generate_options(
        session, issue, invoke=lambda *a, **k: _three_options(), now=_NOW
    )
    scenarios.check_triggers(session, mandate, issue, now=_NOW)
    return issue


def test_the_register_renders_the_courses_and_the_options(session, mandate, web):
    _seeded_issue(session, mandate)
    body = web.get(f"/client/{mandate.id}/issues").text

    assert "Szenarien" in body
    assert "Wahrscheinlicher Verlauf" in body
    # The likelihood as a word, and nowhere as a percentage: that is the whole
    # reason the column holds an enum of four words.
    assert "möglich" in body
    assert "Reaktionsoptionen" in body
    assert "nicht reagieren" in body
    assert "vorbereiten und beobachten" in body


def test_the_register_marks_every_course_as_a_scenario(session, mandate, web):
    """"Jedes Szenario ist im Text als Szenario gekennzeichnet" — the stored
    narrative had to read as one, and the page states it besides."""
    _seeded_issue(session, mandate)
    body = web.get(f"/client/{mandate.id}/issues").text
    assert ">Szenario</span>" in body


def test_the_register_carries_the_mark_a_fired_condition_left(session, mandate, web):
    _seeded_issue(session, mandate)
    body = web.get(f"/client/{mandate.id}/issues").text
    assert "Ausgelöst" in body
    assert "Leitmedium: FAZ" in body
    # The condition that has not fired is still named, and not as a mark.
    assert "eine Medienanfrage im verbundenen Postfach" in body


def test_the_register_renders_in_english_without_german_leftovers(
    session, mandate, web
):
    from newspulse import i18n

    _seeded_issue(session, mandate)
    web.cookies.set(i18n.COOKIE_NAME, "en")
    body = web.get(f"/client/{mandate.id}/issues").text

    assert "Scenarios" in body
    assert "Likely case" in body
    assert "no response" in body
    assert "prepare and watch" in body
    for leftover in ("Szenarien", "Reaktionsoptionen", "Eskalationspotenzial"):
        assert leftover not in body, leftover


def test_the_options_button_says_the_courses_come_first(session, mandate, web):
    """The options are developed against the courses, so pressing the button on
    an issue that has none says so rather than spending a call."""
    from newspulse.web.routes import issues_view, stakeholder_ui

    issue = _issue(session, mandate)
    web.post(f"/issues/{issue.id}/optionen", data={"redirect_to": "/"})
    assert stakeholder_ui.pop_note(mandate.id) == issues_view.SCENARIOS_FIRST


def test_discarding_the_courses_takes_the_options_with_them(session, mandate, web):
    """A list of answers to a question no longer on the page is worse than no
    list, so the two go together — and no model call is spent either way."""
    issue = _seeded_issue(session, mandate)
    web.post(
        f"/issues/{issue.id}/szenarien/verwerfen",
        data={"redirect_to": f"/client/{mandate.id}/issues"},
    )
    # The route wrote through its own session, so this one is asked again
    # rather than trusted: an identity-mapped row would answer with what the
    # test put there and prove nothing about what the button did.
    session.expire_all()
    assert scenarios.stored_scenarios(session, issue) == []
    assert scenarios.stored_options(session, issue) == []


# --- The notification: once, and never again ---------------------------------------


def test_no_firing_sends_nothing_at_all():
    """The promise every channel in this tool keeps: a quiet morning is
    genuinely silent, which is what keeps the channel trusted for the mornings
    that are not."""
    from newspulse import notify

    result = notify.notify_triggers([], notify.NotifyConfig(channel=notify.Channel.OFF))
    assert not result.sent
    assert result.reason == "no-triggers"


def test_a_firing_is_delivered_with_what_matched():
    from newspulse import notify

    sent: list[notify.AlertSummary] = []
    fired = [
        notify.FiredTrigger(
            client_name="Solaris AG",
            issue_title="Vorwurf Vertragsklauseln",
            scenario="wahrscheinlicher",
            condition="leitmedium",
            note="Leitmedium: FAZ",
        )
    ]
    result = notify.notify_triggers(
        fired,
        notify.NotifyConfig(channel=notify.Channel.DESKTOP),
        send_desktop=sent.append,
    )
    assert result.sent
    assert len(sent) == 1
    assert "Leitmedium: FAZ" in sent[0].body
    assert "Solaris AG" in sent[0].subject


def test_a_delivery_error_never_raises():
    from newspulse import notify

    def _broken(summary):
        raise RuntimeError("notifier is gone")

    result = notify.notify_triggers(
        [
            notify.FiredTrigger(
                client_name="Solaris AG",
                issue_title="Vorwurf",
                scenario="bester",
                condition="leitmedium",
                note="Leitmedium: FAZ",
            )
        ],
        notify.NotifyConfig(channel=notify.Channel.DESKTOP),
        send_desktop=_broken,
    )
    assert not result.sent
    assert result.reason == "delivery-error"


# --- The sweep's stage -------------------------------------------------------------


def test_the_sweep_stage_fires_once_and_reports_it(session, mandate):
    """:func:`newspulse.job._check_scenario_triggers` drives the whole path the
    sweep takes: fire what holds, hand it to the channel, and never fire it
    again on the next morning."""
    from newspulse import job, notify

    issue = _issue(
        session, mandate, articles=[_article(session, "Vorwurf im Werk", source="FAZ")]
    )
    scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(_course(ausloeser=["leitmedium"])),
        now=_NOW,
    )
    off = notify.NotifyConfig(channel=notify.Channel.OFF)

    assert job._check_scenario_triggers(
        session, [mandate], now=_NOW, notify_config=off
    ) == 1
    later = _NOW + dt.timedelta(days=1)
    assert job._check_scenario_triggers(
        session, [mandate], now=later, notify_config=off
    ) == 0


def test_the_sweep_stage_skips_a_benchmark(session, mandate):
    """A competitor is tracked to compare coverage, and no register is kept on
    it — the same line the reputation sweep and the linking pass draw."""
    from newspulse import job, notify

    rival = Client(name="Helios GmbH", is_competitor=True)
    session.add(rival)
    session.commit()
    issue = _issue(
        session, rival, articles=[_article(session, "Vorwurf bei Helios", source="FAZ")]
    )
    scenarios.generate_scenarios(
        session,
        issue,
        invoke=lambda *a, **k: _answer(_course(ausloeser=["leitmedium"])),
        now=_NOW,
    )
    fired = job._check_scenario_triggers(
        session, [rival], now=_NOW, notify_config=notify.NotifyConfig(channel=notify.Channel.OFF)
    )
    assert fired == 0
