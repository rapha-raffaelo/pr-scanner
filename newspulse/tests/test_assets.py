"""What a format is (newspulse.assets): the shape, the refusal and the storage.

Driven with an injected ``invoke`` and an injected ``generate``, like every other
generator here, so the whole path runs without a subprocess and without a network
call: requirements, prompt build, parse, both checks, persistence.

The tests worth reading twice are the refusal ones. A format that invents a
spokesperson rather than declining to write is the worst artefact this feature can
produce, and it is the one failure that looks completely fine on screen.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import re

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import assets, guide, i18n, profile, prose
from newspulse.analyzer import ParseError
from newspulse.models import (
    Angle,
    Article,
    Asset,
    AssetKind,
    Base,
    CheckState,
    Client,
)
from newspulse.pitch import PitchTarget
from newspulse.schemas import AssetDraft


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    return factory()


_HEADLINE = "Verwahrung im Wandel: Banken bauen eigene Depots"
_SNIPPET = "Laut Marktbeobachtern verlagert sich die Verwahrung zurück zu Banken."
_OUTLET = "Börsen-Zeitung"
_SPEAKER = "Alexandra Prot, Geschäftsführerin"


_BYLINE = "Marie Faber"


def _mandate(
    session,
    *,
    facts: dict[str, str] | None = None,
    comms_guide: str = "",
    articles: int = 2,
    author: str | None = None,
) -> tuple[Client, Angle]:
    """A mandate with an impulse, and exactly the profile the test needs.

    ``facts`` defaults to a filled profile because most tests are about something
    other than the refusal; the refusal tests pass an empty one deliberately.

    ``author`` puts a byline on the impulse's stories, which is what makes
    ``pitch.targets_for`` able to name a recipient. Off by default, because most
    feeds carry no byline and the formats that do not need one must not quietly
    depend on the fixture having one.
    """
    client = Client(
        name="Alpha AG",
        industry="Neobroker",
        keywords=["Verwahrung"],
        comms_guide=comms_guide,
    )
    session.add(client)
    session.flush()

    for key, value in (facts if facts is not None else _FULL_PROFILE).items():
        profile.save(session, client, key, value)

    article_ids = []
    for i in range(articles):
        article = Article(
            title=f"{_HEADLINE} ({i})",
            url=f"https://ex.de/field-{i}",
            source=_OUTLET,
            author=author,
            published_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
            fetched_at=dt.datetime.now(dt.UTC),
            summary_text=_SNIPPET,
            language="de",
            title_hash=f"field{i:05d}",
        )
        session.add(article)
        session.flush()
        article_ids.append(article.id)

    angle = Angle(
        client_id=client.id,
        generated_at=dt.datetime.now(dt.UTC),
        subject="Verfügbarkeit als Risikoparameter",
        message="Zwei Absätze Positionierung.",
        context="Laut Börsen-Zeitung verlagert sich die Verwahrung.",
        thesis="Die Liveness der Kette ist ein eigener Risikoparameter.",
        overclaim="Solana ist unzuverlässig, also wandert Liquidität ab.",
        article_ids=article_ids,
    )
    session.add(angle)
    session.commit()
    return client, angle


_FULL_PROFILE = {
    "ceo": _SPEAKER,
    "geschaeftsfeld": "Verwahrung digitaler Vermögenswerte für Banken.",
}


# --- Fixture model output, one shape per format --------------------------------
#
# Six builders rather than one payload, because as of FMT-02 there is no such
# thing as "a draft": there is a release, which owes a dateline and an attributed
# quote, and a set of talking points, which owes bridges and a "Nicht sagen". A
# shared fixture could only satisfy all six by satisfying none of their contracts,
# and then every test in this file would be driving output the writer refuses.
#
# Each builder takes the one part its tests vary and holds the rest of the
# contract fixed, so a test about the house style or the crosscheck says what it
# is about and does not restate a press release to get there.


def _release_body(*, lead: str = "Alpha AG erweitert ihr Angebot für Banken.") -> str:
    return "\n\n".join(
        (
            f"Berlin, 20. August 2026. {lead}",
            "Der Schritt folgt auf die Verlagerung der Verwahrung zu den Banken.",
            f'"Verfügbarkeit ist ein eigener Risikoparameter", sagt {_SPEAKER}.',
            "Über die Alpha AG: Sie verwahrt digitale Vermögenswerte für Banken.",
        )
    )


def _statement_body(*, opening: str = "Die Verwahrung wandert zu den Banken.") -> str:
    return (
        f"{opening} Wir halten diese Entwicklung für richtig. "
        "Verfügbarkeit ist dabei ein eigener Risikoparameter, kein Detail."
        f"\n\n{_SPEAKER}"
    )


def _qa_body(*, touchy: str = "Ist Ihr Angebot am Ende einfach günstig?") -> str:
    return (
        "## Zum Geschäft\n"
        "Was ändert sich für Banken?\n"
        "Sie verwahren wieder selbst und tragen das Risiko dafür.\n\n"
        "## Zur Position\n"
        "Warum jetzt?\n"
        "Weil die Verfügbarkeit der Kette zum Risikoparameter geworden ist.\n\n"
        "## Wo es unangenehm wird\n"
        f"[heikel] {touchy}\n"
        "Nein. Wir sprechen über Risiko, nicht über den Preis."
    )


def _talking_points_body(*, points: int = 3, bridges: int | None = None) -> str:
    bridges = points if bridges is None else bridges
    lines = []
    for i in range(1, points + 1):
        lines.append(f"{i}. Verfügbarkeit ist ein eigener Risikoparameter ({i}).")
        if i <= bridges:
            lines.append("Brücke: Zurück zur These, dass Liveness ein Risiko ist.")
    return (
        "\n".join(lines)
        + "\n\nNicht sagen\nDass die Kette unzuverlässig sei und Liquidität abwandere."
    )


def _gastbeitrag_body(*, opening: str = "Ich halte die Debatte für verkürzt.") -> str:
    """A guest article of about the length one actually is.

    Repeated to land in the middle of the accepted band rather than just inside
    it: a fixture that only barely passes turns any edit to the sentence into a
    failure about the length, which is not what the test that reads it is about.
    """
    argument = (
        "Wir sehen in der Verwahrung eine Verschiebung, die weniger mit Technik "
        "zu tun hat als mit Haftung. Wer verwahrt, trägt das Risiko, und dieses "
        "Risiko lässt sich nicht auslagern, indem man es einer Kette überlässt. "
    )
    return f"{opening} " + argument * 16


def _briefing_body(*, outlet: str = _OUTLET, journalist: str = _BYLINE) -> str:
    return (
        f"Wer fragt\n{journalist}, {outlet}.\n\n"
        f"Was zuletzt erschien\n{_HEADLINE} (0)\n\n"
        "Womit zu rechnen ist\nFragen nach der Haftung bei Ausfällen.\n\n"
        "Was gesagt werden soll\nVerfügbarkeit ist ein eigener Risikoparameter.\n\n"
        "Wo es unangenehm wird\nDie Frage nach dem Preis."
    )


_BODIES = {
    AssetKind.PRESSEMITTEILUNG: _release_body,
    AssetKind.STATEMENT: _statement_body,
    AssetKind.QA: _qa_body,
    AssetKind.TALKING_POINTS: _talking_points_body,
    AssetKind.GASTBEITRAG: _gastbeitrag_body,
    AssetKind.INTERVIEW_BRIEFING: _briefing_body,
}


def _drafted(fmt=None, **over) -> str:
    """One model reply, carrying the structure ``fmt`` declares.

    ``fmt`` may be a definition or a kind. Without one the reply is a press
    release, which is what a test that does not care is testing against.
    """
    kind = getattr(fmt, "kind", fmt) or AssetKind.PRESSEMITTEILUNG
    payload = {
        "title": "Alpha AG baut Verwahrung für Banken aus",
        "body": _BODIES[AssetKind(kind)](),
        "speaker": _SPEAKER,
    }
    payload.update(over)
    return json.dumps(payload)


def _review(**over) -> str:
    payload = {"send": True, "concerns": [], "fix": ""}
    payload.update(over)
    return json.dumps(payload)


def _guide_verdict(**over) -> str:
    payload = {"ok": True, "breaches": []}
    payload.update(over)
    return json.dumps(payload)


# --- A format is data ----------------------------------------------------------


def test_every_declared_format_has_a_definition_and_a_prompt_file():
    """The registry and the value set say the same thing, and every definition
    points at a prompt that exists."""
    assert set(assets.REGISTRY) == {kind.value for kind in AssetKind}
    for fmt in assets.FORMATS:
        assert fmt.template().template.strip(), f"{fmt.key} has an empty prompt"
        assert fmt.structure, f"{fmt.key} declares no structure"


def test_the_writer_branches_on_no_individual_format():
    """The load-bearing half of "a seventh format is a definition and a prompt":
    no function in the writing path may name a format.

    Matched on word boundaries rather than as a substring: ``qa`` is two letters
    and would otherwise collide with an ordinary word in a future docstring, and a
    guard that fails for a reason it does not name gets deleted rather than read.
    """
    path = (
        inspect.getsource(assets.write)
        + inspect.getsource(assets.prompt_for)
        + inspect.getsource(assets.store)
    )
    for fmt in assets.FORMATS:
        assert not re.search(rf"\b{re.escape(fmt.key)}\b", path)
        assert not re.search(rf"\b{re.escape(fmt.name)}\b", path)


def test_a_seventh_format_needs_only_a_definition_and_a_prompt(session):
    """Registered here in the test rather than in the module, which is the point:
    the writer has never heard of this format and writes it anyway."""
    client, angle = _mandate(session)
    seventh = assets.FormatDef(
        kind="leserbrief",
        name="Leserbrief",
        description="Eine Zuschrift an die Redaktion.",
        prompt="prompts/statement.txt",
        requires=(assets.Requirement(assets.Source.PROFIL, "ceo"),),
        structure=("Kurz.",),
        speaker_key="ceo",
    )

    draft = assets.write(
        session, seventh, client, angle, invoke=lambda *a, **k: _drafted()
    )
    stored = assets.store(session, seventh, client, angle, draft)

    assert stored.kind == "leserbrief"
    assert stored.body.startswith("Berlin")


# --- What a format needs -------------------------------------------------------


def test_requirements_met_names_exactly_the_missing_fields(session):
    """A press release needs a spokesperson and a fact. With neither on file both
    are reported, and nothing else is."""
    client, angle = _mandate(session, facts={})
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)

    readiness = assets.requirements_met(session, fmt, client, angle)

    assert not readiness.ok
    assert [req.key for req in readiness.missing] == ["ceo", "geschaeftsfeld"]


def test_requirements_met_is_satisfied_by_a_filled_profile(session):
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)

    readiness = assets.requirements_met(session, fmt, client, angle)

    assert readiness.ok
    assert readiness.reason == ""


def test_requirements_met_reports_the_impulse_fields_without_an_impulse(session):
    """The surface asks which formats a mandate could write before an impulse is
    picked. What the impulse would supply is missing, not assumed."""
    client, _ = _mandate(session)
    fmt = assets.definition(AssetKind.TALKING_POINTS)

    readiness = assets.requirements_met(session, fmt, client, angle=None)

    assert [req.key for req in readiness.missing] == ["thesis", "overclaim"]


def test_a_list_requirement_counts_rather_than_checks_presence(session):
    """A guest article needs two pieces of evidence under it. One is not two."""
    client, angle = _mandate(session, articles=1)
    fmt = assets.definition(AssetKind.GASTBEITRAG)

    readiness = assets.requirements_met(session, fmt, client, angle)

    assert [req.key for req in readiness.missing] == ["article_ids"]


def test_a_refusal_says_how_many_a_list_requirement_wants(session):
    """A refusal has to name work the consultant can do. Told only "Belegte
    Meldungen" while looking at an impulse that visibly has a story attached, he
    reads a bug rather than an instruction."""
    client, angle = _mandate(session, articles=1)
    fmt = assets.definition(AssetKind.GASTBEITRAG)

    reason = assets.requirements_met(session, fmt, client, angle).reason

    assert f"mindestens {assets._MIN_GASTBEITRAG_EVIDENCE}" in reason
    assert "Belegte Meldungen" in reason


def test_a_missing_requirement_writes_nothing_and_names_the_field(session):
    """DEC-2, as it was recommended: refuse and say why. The model is never
    reached, so a fabricated spokesperson cannot even be generated."""
    client, angle = _mandate(session, facts={})
    fmt = assets.definition(AssetKind.STATEMENT)
    calls: list[str] = []

    with pytest.raises(assets.RequirementsMissing) as caught:
        assets.write(
            session, fmt, client, angle,
            invoke=lambda prompt, **k: calls.append(prompt) or _drafted(fmt),
        )

    assert calls == [], "the model was asked despite a missing requirement"
    assert "Geschäftsführung" in str(caught.value)
    assert "Profil" in str(caught.value)
    assert [req.key for req in caught.value.missing] == ["ceo"]
    assert session.scalars(select(Asset)).all() == []


def test_a_guest_article_needs_the_person_it_appears_under(session):
    """A Gastbeitrag is printed under a byline. Without a name on file the prompt
    would ask for one and the only place to find it is the free-text profile
    block, which is the inference the refusal rule exists to forbid."""
    client, angle = _mandate(session, facts={})
    fmt = assets.definition(AssetKind.GASTBEITRAG)

    with pytest.raises(assets.RequirementsMissing) as caught:
        assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))

    assert "ceo" in [req.key for req in caught.value.missing]


def test_a_client_without_a_guide_gets_no_qa(session):
    """The Q&A's value is the questions nobody wants asked, and the guide is where
    those live. Without one there is nothing to build it from."""
    client, angle = _mandate(session, comms_guide="")
    fmt = assets.definition(AssetKind.QA)

    with pytest.raises(assets.RequirementsMissing) as caught:
        assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))

    assert "Kommunikations-Guide" in str(caught.value)


# --- The prompt ----------------------------------------------------------------


def test_the_prompt_draws_only_on_headlines_snippets_and_profile_facts(session):
    """The Leistungsschutzrecht rule, held at the one place it could break.

    Upstream it is a schema guarantee: ``articles`` has no body column at all. So
    what this pins is that the prompt is built from the two fields that exist and
    from the profile, and that a stored snippet travels as the feed carried it.
    """
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)

    prompt = assets.prompt_for(session, fmt, client, angle)

    assert _HEADLINE in prompt
    assert _SNIPPET in prompt
    assert "Alexandra Prot" in prompt
    assert "Verwahrung digitaler Vermögenswerte" in prompt
    # There is no column a body could come from, so there is no way one reaches
    # a prompt. Pinned, because a future "full_text" column would break the rule
    # silently everywhere rather than loudly here.
    for forbidden in ("body", "content", "full_text", "text"):
        assert not hasattr(Article, forbidden), f"Article grew a {forbidden} column"


def test_a_long_feed_snippet_is_cut_before_it_reaches_the_prompt(session):
    """A snippet longer than a snippet is a scraper's output. This is the line it
    would have to cross."""
    client, angle = _mandate(session)
    article = session.get(Article, angle.article_ids[0])
    article.summary_text = "A" * 5000
    session.commit()
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)

    prompt = assets.prompt_for(session, fmt, client, angle)

    assert "A" * assets._MAX_SNIPPET_CHARS in prompt
    assert "A" * (assets._MAX_SNIPPET_CHARS + 1) not in prompt


def test_the_evidence_keeps_the_impulses_order_and_counts_stories_not_ids(session):
    """``IN`` hands rows back in database order, and the impulse ranked them: that
    ranking is what the prompt is for. The cap counts what resolved, so an id that
    no longer exists does not silently cost the brief a story."""
    client, angle = _mandate(session, articles=assets._MAX_EVIDENCE + 1)
    angle.article_ids = [999_001, *reversed(angle.article_ids)]
    session.commit()

    block = assets._evidence_block(session, angle)

    headlines = [line for line in block.splitlines() if line.startswith("- (")]
    assert len(headlines) == assets._MAX_EVIDENCE, "a dead id ate a slot"
    assert headlines[0].endswith(f"({assets._MAX_EVIDENCE})"), "the order is the DB's"


def test_the_prompt_states_the_structure_the_definition_declares(session):
    """The contract lives in the definition and travels into the prompt from
    there, so a format cannot promise one shape and ask for another."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.TALKING_POINTS)

    prompt = assets.prompt_for(session, fmt, client, angle)

    for line in fmt.structure:
        assert line in prompt
    assert "Nicht sagen" in prompt


def test_an_impulse_whose_stories_are_gone_says_so_rather_than_promising_them(
    session,
):
    """The evidence header promises that what follows is everything known. An
    impulse can name ids that no longer resolve, and printing that promise over an
    empty list is how a model concludes it may fill one."""
    client, angle = _mandate(session)
    angle.article_ids = [999_001, 999_002]
    session.commit()
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)

    prompt = assets.prompt_for(session, fmt, client, angle)

    assert "auf keine Berichterstattung berufen" in prompt
    assert "Schlagzeilen und Feed-Anrisse" not in prompt


def test_the_guide_reaches_the_writing_prompt(session):
    client, angle = _mandate(session, comms_guide="No-Go: das Wort günstig.")
    fmt = assets.definition(AssetKind.QA)

    prompt = assets.prompt_for(session, fmt, client, angle)

    assert "No-Go: das Wort günstig." in prompt


# --- Storage -------------------------------------------------------------------


def test_a_stored_asset_carries_the_angle_it_came_from(session):
    """Every text traceable to the position it argues, and through the angle to
    the coverage under that position."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)

    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    stored = assets.store(session, fmt, client, angle, draft)

    assert stored.angle_id == angle.id
    assert stored.client_id == client.id
    assert stored.kind == AssetKind.PRESSEMITTEILUNG


def test_prose_plain_is_applied_to_the_title_and_the_body(session):
    """The prompt asks and the model relapses. One call site for six formats."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(
            fmt,
            title="Verwahrung — die Begründung verschiebt sich",
            body=_release_body(lead="Die Anprobe ist gewandert — und wird dort bezahlt."),
        ),
    )

    stored = assets.store(session, fmt, client, angle, draft)

    assert not prose.has_dash(stored.title)
    assert not prose.has_dash(stored.body)
    assert stored.body.count("\n\n") == 3, "the paragraphs survive"


def test_the_attribution_is_the_profiles_and_not_the_models(session):
    """The one artefact here that cannot be repaired after it has gone out is a
    named person's quote. The prompt asks for the name verbatim; what is stored is
    the profile's, so the guarantee does not rest on the model complying."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)

    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(fmt, speaker="Alexander Prot, CEO"),
    )
    stored = assets.store(session, fmt, client, angle, draft)

    assert draft.speaker == "Alexandra Prot, Geschäftsführerin"
    assert stored.speaker == "Alexandra Prot, Geschäftsführerin"


def test_a_format_that_quotes_nobody_stores_no_attribution(session):
    """Talking points name no profile field for attribution, so there is no
    profile-backed name to store, and the model's answer is not one."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.TALKING_POINTS)

    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(fmt, speaker="Dr. Erfunden, Sprecher"),
    )
    stored = assets.store(session, fmt, client, angle, draft)

    assert stored.speaker == ""


def test_rewriting_a_format_replaces_the_draft_it_supersedes(session):
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    first = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    assets.store(session, fmt, client, angle, first)

    second = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(fmt, body=_release_body(lead="Neu ist der zweite Anlauf.")),
    )
    assets.store(session, fmt, client, angle, second)

    rows = assets.for_angle(session, angle.id)
    assert len(rows) == 1
    assert "Neu ist" in rows[0].body


def test_a_released_asset_is_never_overwritten_by_a_rewrite(session):
    """Its text is the record of what actually went out. A redraft is a new row
    beside it, not a change to it."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    first = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    released = assets.store(session, fmt, client, angle, first)
    released.released_at = dt.datetime.now(dt.UTC)
    released.released_by = "mensch"
    session.commit()

    second = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(fmt, body=_release_body(lead="Neu ist der zweite Anlauf.")),
    )
    assets.store(session, fmt, client, angle, second)

    rows = assets.for_angle(session, angle.id)
    assert len(rows) == 2
    assert "Neu ist" not in session.get(Asset, released.id).body


def test_a_reply_without_text_is_a_parse_error_rather_than_an_empty_asset(session):
    """"The draft failed" and "here is your draft" must never look alike."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)

    with pytest.raises(ParseError):
        assets.write(
            session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt, body="   ")
        )


def test_the_texts_for_several_impulses_come_back_keyed_by_impulse(session):
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    assets.store(session, fmt, client, angle, draft)

    grouped = assets.by_angle(session, [angle.id, angle.id + 99])

    assert list(grouped) == [angle.id]
    assert len(grouped[angle.id]) == 1


# --- The checks ----------------------------------------------------------------


def test_an_asset_with_neither_check_recorded_is_unchecked_not_clean(session):
    """The one state the page must never draw as a clean bill of health."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))

    stored = assets.store(session, fmt, client, angle, draft)

    assert stored.reviewed_by == ""
    assert stored.guide_reviewed_by == ""
    assert stored.check_state is CheckState.UNGEPRUEFT


def test_both_checks_run_over_a_format_and_are_stored_with_it(session):
    client, angle = _mandate(session, comms_guide="No-Go: das Wort günstig.")
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    seen: list[str] = []

    checked = assets.check(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda prompt, **k: seen.append(prompt) or _review(),
        guide_generate=lambda prompt, **k: seen.append(prompt) or _guide_verdict(),
    )
    stored = assets.store(session, fmt, client, angle, draft, checked)

    assert len(seen) == 2, "both checks ran"
    assert stored.reviewed_by
    assert stored.guide_reviewed_by
    assert stored.check_state is CheckState.GEPRUEFT


def test_the_crosscheck_sees_the_text_and_what_it_may_claim(session):
    """It can only judge invention if it knows what was provable."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    seen: list[str] = []

    assets.crosscheck(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda prompt, **k: seen.append(prompt) or _review(),
    )

    assert "Alpha AG baut Verwahrung für Banken aus" in seen[0]  # the text itself
    assert _HEADLINE in seen[0]                                  # what is provable
    assert "Solana ist unzuverlässig" in seen[0]                 # the overclaim
    assert "Schlagzeile" in seen[0]                              # named as its kind


def test_the_crosscheck_is_shown_the_profile_the_format_was_written_from(session):
    """The formats that quote a person are written *from* the profile, and the
    checker is told an attributed quote nobody backed is the unrepairable
    mistake. Without the profile it cannot tell the backed one from the invented
    one, and that comparison is the whole reason this check runs."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    seen: list[str] = []

    assets.crosscheck(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda prompt, **k: seen.append(prompt) or _review(),
    )

    assert "Alexandra Prot, Geschäftsführerin" in seen[0]  # the attribution is backed
    assert "Verwahrung digitaler Vermögenswerte" in seen[0]


def test_both_checkers_read_the_text_the_reader_will_see(session):
    """A guide breach is stored with the sentence it objects to quoted verbatim.
    Read off the raw reply, that quote is a sentence the house-style rewrite has
    since changed, and it is not on the page it points at."""
    client, angle = _mandate(session, comms_guide="No-Go: das Wort günstig.")
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(fmt, body=_statement_body(opening="Die Verwahrung wandert — zurück.")),
    )
    seen: list[str] = []

    assets.check(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda *a, **k: _review(),
        guide_generate=lambda prompt, **k: seen.append(prompt) or _guide_verdict(),
    )

    assert "Die Verwahrung wandert, zurück." in seen[0]
    assert "—" not in seen[0]


def test_an_asset_carrying_an_objection_never_renders_as_checked(session):
    """The checker's own flag is not trusted against findings it never made. The
    mechanical dash concern is added after it has answered, so a reply of "send,
    no concerns" would otherwise store an objection on a row drawn as clean."""
    client, angle = _mandate(session, comms_guide="No-Go: das Wort günstig.")
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(fmt, body=_statement_body(opening="Eins — zwei.")),
    )

    checked = assets.check(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda *a, **k: _review(send=True),
        guide_generate=lambda *a, **k: _guide_verdict(),
    )
    stored = assets.store(session, fmt, client, angle, draft, checked)

    assert assets._DASH_CONCERN in stored.review
    assert stored.check_state is CheckState.EINWAND


def test_a_guide_breach_is_stored_with_both_halves_quoted(session):
    """An objection nobody can check in ten seconds gets clicked away."""
    client, angle = _mandate(session, comms_guide="No-Go: das Wort günstig.")
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))

    checked = assets.check(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda *a, **k: _review(),
        guide_generate=lambda *a, **k: _guide_verdict(
            ok=False,
            breaches=[
                {
                    "sentence": "Das günstigste Angebot am Markt.",
                    "rule": "No-Go: das Wort günstig.",
                }
            ],
        ),
    )
    stored = assets.store(session, fmt, client, angle, draft, checked)

    assert "Das günstigste Angebot am Markt." in stored.guide_review
    assert "No-Go: das Wort günstig." in stored.guide_review
    assert stored.guide_review_ok is False
    assert stored.check_state is CheckState.EINWAND


def test_a_mandate_without_a_guide_is_told_the_check_could_not_run(session):
    """"Nothing objected" and "nothing to object with" must not read alike."""
    client, angle = _mandate(session, comms_guide="")
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    guide_calls: list[str] = []

    checked = assets.check(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda *a, **k: _review(),
        guide_generate=lambda prompt, **k: guide_calls.append(prompt)
        or _guide_verdict(),
    )
    stored = assets.store(session, fmt, client, angle, draft, checked)

    assert guide_calls == [], "there was nothing to check against"
    assert stored.guide_review == guide.NO_GUIDE
    assert stored.guide_reviewed_by == ""


def test_the_guide_check_reads_the_text_against_the_written_rules(session):
    client, angle = _mandate(session, comms_guide="No-Go: das Wort günstig.")
    seen: list[str] = []

    verdict, model = guide.check_guide(
        client,
        title="Alpha AG baut aus",
        body="Wir bauen aus.",
        generate=lambda prompt, **k: seen.append(prompt) or _guide_verdict(),
    )

    assert "No-Go: das Wort günstig." in seen[0]
    assert "Wir bauen aus." in seen[0]
    assert verdict.ok is True
    assert model


def test_a_listed_breach_overrules_the_checkers_own_ok_flag(session):
    """A verdict that names a breach and still says ok would clear a text nobody
    cleared."""
    client, _ = _mandate(session, comms_guide="No-Go: das Wort günstig.")

    verdict, _ = guide.check_guide(
        client,
        title="",
        body="Das günstigste Angebot am Markt.",
        generate=lambda *a, **k: _guide_verdict(
            ok=True,
            breaches=[{"sentence": "Das günstigste Angebot.", "rule": "No-Go."}],
        ),
    )

    assert verdict.ok is False


def test_a_dash_is_caught_even_if_the_checker_misses_it(session):
    """Mechanical, and the one the reader spots first."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(fmt, body=_statement_body(opening="Eins — zwei.")),
    )

    review, _ = assets.crosscheck(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda *a, **k: _review(),
    )

    assert any("Gedankenstrich" in concern for concern in review.concerns)


def test_the_checker_reads_the_text_that_will_be_stored(session):
    """Not the reply as it came back. An objection quoting a sentence the house
    style has since rewritten sends the reader hunting for words that are not on
    the page."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(fmt, body=_statement_body(opening="Eins — zwei.")),
    )
    seen: list[str] = []

    assets.crosscheck(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda prompt, **k: seen.append(prompt) or _review(),
    )

    assert "Eins, zwei." in seen[0]
    assert "Eins — zwei." not in seen[0]


def test_the_mechanical_finding_survives_a_full_concern_list(session):
    """The cap drops one of the checker's judgements, never the one finding here
    that is not a judgement: the model was not trusted with it in the first
    place."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(fmt, body=_statement_body(opening="Eins — zwei.")),
    )

    review, _ = assets.crosscheck(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda *a, **k: _review(concerns=[f"Einwand {i}." for i in range(5)]),
    )

    assert len(review.concerns) == 5
    assert "Gedankenstrich" in review.concerns[0]


def test_more_concerns_than_the_cap_are_truncated_rather_than_rejected(session):
    """A checker that finds six things about a bad draft must not be the one call
    that produces no verdict at all."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))

    review, _ = assets.crosscheck(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda *a, **k: _review(
            send=False, concerns=[f"Einwand {i}." for i in range(6)]
        ),
    )

    assert len(review.concerns) == 5
    assert review.send is False


def test_rewriting_clears_the_verdicts_of_the_text_they_replace(session):
    """A verdict must never stand over a text it never read."""
    client, angle = _mandate(session, comms_guide="No-Go: das Wort günstig.")
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    checked = assets.check(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda *a, **k: _review(send=False, concerns=["Zu werblich."]),
        guide_generate=lambda *a, **k: _guide_verdict(),
    )
    assets.store(session, fmt, client, angle, draft, checked)

    again = assets.write(
        session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt, body=_statement_body(opening="Neu ist der zweite Anlauf."))
    )
    stored = assets.store(session, fmt, client, angle, again)

    assert stored.review == ""
    assert stored.reviewed_by == ""
    assert stored.review_ok is True
    assert stored.guide_reviewed_by == ""
    assert stored.check_state is CheckState.UNGEPRUEFT


def test_without_a_second_model_the_check_refuses_rather_than_passes(
    session, monkeypatch
):
    """A check that silently did not run is worse than none."""
    from newspulse import config

    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted(fmt))
    monkeypatch.setattr(config, "review_configured", lambda: False)

    with pytest.raises(RuntimeError, match="Zweitmodell"):
        assets.crosscheck(client, assets.checkable(session, fmt, angle, draft))


# --- The structural contract, format by format ----------------------------------
#
# The validators are exercised directly here rather than through ``write``: what
# is being pinned is what each format owes, and routing every case through a model
# call and a database would say the same thing five times more slowly. The three
# behaviours around them that are *not* per-format — the retry, the refusal, the
# nothing-is-stored — go through ``write`` once each, further down.


_NOGO_GUIDE = 'No-Go: das Wort günstig. Register: nüchtern, nie "wir freuen uns".'

_TARGET = PitchTarget(
    outlet=_OUTLET,
    journalist=_BYLINE,
    reason="hat die Meldung geschrieben",
    evidence=(_HEADLINE,),
    about_client=0,
)


def _draft_of(fmt, **over) -> AssetDraft:
    """The fixture reply for ``fmt`` as the validator sees it."""
    return AssetDraft.model_validate(json.loads(_drafted(fmt, **over)))


def _given(**over) -> assets.Given:
    fields = {
        "speaker": _SPEAKER,
        "nogos": (_NOGO_GUIDE.split(".")[0] + ".",),
        "recipient": _TARGET,
    }
    fields.update(over)
    return assets.Given(**fields)


def test_every_format_holds_its_output_to_a_contract():
    """"Each format is only as good as the shape it is held to." A definition
    without a validator states its structure in the prompt and hopes."""
    for fmt in assets.FORMATS:
        assert fmt.validator is not None, f"{fmt.key} asserts no structure"
        assert fmt.structure, f"{fmt.key} declares no structure"


@pytest.mark.parametrize("kind", list(AssetKind))
def test_each_format_accepts_output_that_carries_its_declared_structure(kind):
    """The other half of every rejection test below: a text that does carry the
    shape passes, so the validators cannot be satisfying themselves by refusing
    everything."""
    fmt = assets.definition(kind)

    assert assets.validate(fmt, _draft_of(fmt), _given()) == []


# --- Pressemitteilung ----------------------------------------------------------


def test_a_release_without_a_dateline_or_a_boilerplate_is_rejected():
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    draft = _draft_of(
        fmt,
        body="Alpha AG erweitert ihr Angebot.\n\nDas ist gut.\n\n"
        f'"Ein Zitat mit genug Text darin", sagt {_SPEAKER}.',
    )

    faults = assets.validate(fmt, draft, _given())

    assert any("Dateline" in fault for fault in faults)
    assert any("Boilerplate" in fault for fault in faults)


def test_a_release_without_a_headline_is_rejected():
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)

    faults = assets.validate(fmt, _draft_of(fmt, title="  "), _given())

    assert any("Schlagzeile" in fault for fault in faults)


def test_a_release_quote_attributed_to_anyone_else_is_rejected():
    """The acceptance this whole feature is built around: a quote may carry the
    name the profile holds and no other. Checked in the paragraph the quote is in,
    because the spokesperson is named in half of these boilerplates and "the name
    appears somewhere below" would clear an invented CFO two paragraphs up."""
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    body = _release_body().replace(f"sagt {_SPEAKER}", "sagt Dr. Erfunden, Finanzchef")
    body += f"\n\nÜber die Alpha AG: Geführt von {_SPEAKER}."

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given())

    assert faults == [f"Das Zitat ist nicht {_SPEAKER} zugeschrieben."]


def test_a_surname_hiding_inside_an_ordinary_word_does_not_count_as_an_attribution():
    """Matched on a word boundary, not as a substring. Half the surnames a German
    profile holds live inside everyday words, and a substring match clears a quote
    attributed to an invented CFO as long as the paragraph says "langfristig"."""
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    speaker = "Michael Lang, Geschäftsführer"
    body = "\n\n".join(
        (
            "Berlin, 20. August 2026. Alpha AG erweitert ihr Angebot für Banken.",
            "Der Schritt folgt auf die Verlagerung der Verwahrung zu den Banken.",
            '"Verfügbarkeit ist ein eigener Risikoparameter", sagt Dr. Erfunden, '
            "Finanzchef, und denkt dabei langfristig.",
            "Über die Alpha AG: Sie verwahrt digitale Vermögenswerte für Banken.",
        )
    )

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given(speaker=speaker))

    assert faults == [f"Das Zitat ist nicht {speaker} zugeschrieben."]


def test_a_surname_in_the_genitive_still_counts_as_an_attribution():
    """"Langs Einschätzung" names the same person, and a refusal here costs two
    paid calls and delivers no text."""
    assert assets._names("Das ist Langs Einschätzung.", "Michael Lang")


def test_a_release_with_no_quote_at_all_is_rejected():
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    body = "\n\n".join(
        (
            "Berlin, 20. August 2026. Alpha AG erweitert ihr Angebot.",
            "Der Schritt folgt auf die Verlagerung der Verwahrung.",
            "Über die Alpha AG: Sie verwahrt digitale Vermögenswerte.",
        )
    )

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given())

    assert any("kein Zitat" in fault for fault in faults)


def test_no_release_is_written_at_all_without_a_named_spokesperson(session):
    """The empty-spokesperson case, end to end: not a release with a gap in it,
    not a release quoting the company. No release, no model call, no row.

    The field is submitted blank rather than left out, which is how a consultant
    actually produces this state: he deletes a wrong machine-filled name, and
    "this is not known" has to mean the same thing as never having been asked.
    """
    client, angle = _mandate(
        session, facts={"ceo": "   ", "geschaeftsfeld": "Verwahrung."}
    )
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    calls: list[str] = []

    with pytest.raises(assets.RequirementsMissing) as caught:
        assets.write(
            session, fmt, client, angle,
            invoke=lambda prompt, **k: calls.append(prompt) or _drafted(fmt),
        )

    assert calls == []
    assert [req.key for req in caught.value.missing] == ["ceo"]
    assert session.scalars(select(Asset)).all() == []


# --- Statement -----------------------------------------------------------------


def test_a_statement_beyond_five_sentences_is_rejected():
    """A desk prints a statement whole or picks two sentences out of it. Past five
    it is the desk choosing which two, and nobody in the mandate finds out until
    it is printed."""
    fmt = assets.definition(AssetKind.STATEMENT)
    body = " ".join(f"Satz Nummer {i} steht hier." for i in range(7))

    faults = assets.validate(fmt, _draft_of(fmt, body=f"{body}\n\n{_SPEAKER}"), _given())

    assert faults == [
        f"Das Statement hat 7 Sätze, verlangt sind "
        f"{assets._MIN_STATEMENT_SENTENCES} bis {assets._MAX_STATEMENT_SENTENCES}."
    ]


def test_a_statement_without_the_attribution_under_it_is_rejected():
    fmt = assets.definition(AssetKind.STATEMENT)
    body = _statement_body().replace(f"\n\n{_SPEAKER}", "")

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given())

    assert any(_SPEAKER in fault for fault in faults)


def test_a_statement_that_clears_its_throat_first_is_rejected():
    """"Wir haben mit Interesse zur Kenntnis genommen" is the sound of a text with
    nothing to say, and it is the first sentence a desk cuts."""
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = _draft_of(
        fmt,
        body=_statement_body(
            opening="Wir haben mit Interesse zur Kenntnis genommen, was geschah."
        ),
    )

    faults = assets.validate(fmt, draft, _given())

    assert any("Vorlauf" in fault for fault in faults)


# --- Q&A -----------------------------------------------------------------------


def test_a_qa_that_avoids_the_nogos_is_rejected():
    """Its value is the questions nobody wants asked. One that asks around them is
    decoration, and decoration is what gets clicked away in the actual moment."""
    fmt = assets.definition(AssetKind.QA)
    draft = _draft_of(fmt, body=_qa_body(touchy="Wie läuft das Geschäft?"))

    faults = assets.validate(fmt, draft, _given())

    assert faults == ["Keine Frage rührt an die No-Gos des Guides. Genau die werden gestellt."]


def test_a_qa_with_nothing_marked_as_uncomfortable_is_rejected():
    fmt = assets.definition(AssetKind.QA)
    draft = _draft_of(fmt, body=_qa_body().replace("[heikel] ", ""))

    faults = assets.validate(fmt, draft, _given())

    assert any("unangenehm" in fault for fault in faults)


def test_a_qa_with_ungrouped_questions_is_rejected():
    fmt = assets.definition(AssetKind.QA)
    draft = _draft_of(fmt, body=_qa_body().replace("## ", ""))

    faults = assets.validate(fmt, draft, _given())

    assert any("gruppiert" in fault for fault in faults)


def test_the_nogo_terms_are_the_words_that_name_the_subject():
    """Matched on the No-Go's nouns, not on its grammar: a check that accepted
    "das" as touching a No-Go would clear every Q&A ever written."""
    terms = assets.nogo_terms(("No-Go: das Wort günstig.",))

    assert terms == ["günstig"]


def test_the_nogos_are_read_out_of_the_guide_the_consultant_wrote(session):
    """Out of the guide rather than a field of their own, because that is where a
    consultant writes them and a second field is the one nobody fills."""
    client, _ = _mandate(session, comms_guide=_NOGO_GUIDE)

    assert assets.nogos(client) == ("No-Go: das Wort günstig.",)


# --- Talking Points ------------------------------------------------------------


def test_more_talking_points_than_the_bound_are_rejected():
    """Past the bound a consultant in a green room is reading a document instead
    of remembering three things."""
    fmt = assets.definition(AssetKind.TALKING_POINTS)
    body = _talking_points_body(points=assets.MAX_TALKING_POINTS + 1)

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given())

    assert any(str(assets.MAX_TALKING_POINTS) in fault for fault in faults)


def test_a_talking_point_without_a_bridge_is_rejected():
    """The bridge is the format. Points without one are a list of opinions."""
    fmt = assets.definition(AssetKind.TALKING_POINTS)
    body = _talking_points_body(points=3, bridges=2)

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given())

    assert faults == [
        "3 Punkte, aber 2 Brücken zurück zur These. "
        'Jeder Punkt braucht eine Zeile "Brücke: …".'
    ]


def test_the_not_thesis_has_to_be_an_explicit_nicht_sagen_section():
    fmt = assets.definition(AssetKind.TALKING_POINTS)
    body = _talking_points_body().split("\n\nNicht sagen")[0]

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given())

    assert any("Nicht sagen" in fault for fault in faults)


def test_what_must_not_be_said_is_not_counted_against_the_bound():
    """The "Nicht sagen" section is a numbered list as often as not, and counting
    it against the cap would refuse four good points for having three things to
    avoid."""
    fmt = assets.definition(AssetKind.TALKING_POINTS)
    body = _talking_points_body(points=assets.MAX_TALKING_POINTS)
    body += "\n1. Nicht: die Kette sei unzuverlässig.\n2. Nicht: Liquidität wandert ab."

    assert assets.validate(fmt, _draft_of(fmt, body=body), _given()) == []


# --- Gastbeitrag ---------------------------------------------------------------


def test_a_guest_article_with_a_dateline_is_rejected():
    """It would then be a press release with a byline on it, which is the one
    thing an op-ed desk will not print."""
    fmt = assets.definition(AssetKind.GASTBEITRAG)
    body = f"Berlin, 20. August 2026. {_gastbeitrag_body()}"

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given())

    assert any("Dateline" in fault for fault in faults)


def test_a_guest_article_that_opens_with_a_news_hook_is_rejected():
    fmt = assets.definition(AssetKind.GASTBEITRAG)
    draft = _draft_of(
        fmt,
        body=_gastbeitrag_body(opening="Vergangene Woche wurde bekannt, dass es kippt."),
    )

    faults = assets.validate(fmt, draft, _given())

    assert any("Nachrichtenaufhänger" in fault for fault in faults)


def test_a_guest_article_in_the_third_person_is_rejected():
    """Without a first person it is a company text with somebody's name under it,
    which is what every agency guest article turns into."""
    fmt = assets.definition(AssetKind.GASTBEITRAG)
    body = _gastbeitrag_body().replace("Ich ", "Man ").replace("Wir ", "Man ")

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given())

    assert any("erste" in fault.casefold() for fault in faults)


def test_a_guest_article_far_off_the_commissioned_length_is_rejected():
    fmt = assets.definition(AssetKind.GASTBEITRAG)

    faults = assets.validate(
        fmt, _draft_of(fmt, body="Ich halte die Debatte für verkürzt."), _given()
    )

    assert any(str(assets.GASTBEITRAG_CHARS) in fault for fault in faults)


# --- Interview-Briefing --------------------------------------------------------


def test_the_briefing_recipient_is_the_one_pitch_already_names(session):
    """Same list the letter is addressed with, so a briefing and a pitch cannot
    disagree about who covers this field."""
    client, angle = _mandate(session, author=_BYLINE)

    target = assets.recipient(session, client, angle)

    assert target is not None
    assert (target.outlet, target.journalist) == (_OUTLET, _BYLINE)


def test_the_briefing_prompt_carries_the_outlet_and_that_bylines_headlines(session):
    """Who is asking and what they wrote lately, off the same coverage pitch.py
    assembles. A briefing naming a piece the journalist did not write is read out
    loud in the first minute of the interview."""
    client, angle = _mandate(session, author=_BYLINE)
    fmt = assets.definition(AssetKind.INTERVIEW_BRIEFING)

    prompt = assets.prompt_for(session, fmt, client, angle)
    block = prompt.split("WER FRAGT")[1].split("KOMMUNIKATIONS-GUIDE")[0]

    assert _OUTLET in block
    assert _BYLINE in block
    assert f"{_HEADLINE} (0)" in block


def test_a_briefing_for_a_desk_with_no_byline_says_so_rather_than_inventing_one(
    session,
):
    """Most feeds carry no byline. The honest version of that is a sentence, not a
    plausible name."""
    client, angle = _mandate(session)
    target = PitchTarget(
        outlet=_OUTLET, journalist=None, reason="", evidence=(), about_client=0
    )

    block = assets._recipient_block(session, target)

    assert _OUTLET in block
    assert "Erfinde" in block


def test_no_briefing_is_written_when_the_coverage_names_no_medium(session):
    """The outlet is a requirement like the release's spokesperson is: a briefing
    for an invented masthead is worse than none."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.INTERVIEW_BRIEFING)
    calls: list[str] = []

    with pytest.raises(assets.RequirementsMissing) as caught:
        assets.write(
            session, fmt, client, angle,
            invoke=lambda prompt, **k: calls.append(prompt) or _drafted(fmt),
        )

    assert calls == []
    assert [req.key for req in caught.value.missing] == ["outlet"]
    assert "Medium" in str(caught.value)


def test_a_briefing_that_never_names_the_outlet_is_rejected():
    fmt = assets.definition(AssetKind.INTERVIEW_BRIEFING)
    draft = _draft_of(fmt, body=_briefing_body(outlet="einer Fachredaktion"))

    faults = assets.validate(fmt, draft, _given())

    assert faults == [f"Das Medium ({_OUTLET}) kommt im Briefing nicht vor."]


def test_a_briefing_missing_one_of_its_sections_is_rejected():
    fmt = assets.definition(AssetKind.INTERVIEW_BRIEFING)
    body = _briefing_body().split("Wo es unangenehm wird")[0]

    faults = assets.validate(fmt, _draft_of(fmt, body=body), _given())

    assert faults == ["Es fehlen die Abschnitte: Wo es unangenehm wird."]


# --- One retry, then a refusal that says why -----------------------------------


def test_output_that_misses_the_structure_is_retried_once(session):
    """The analyzer's budget for a batch, for the same reason: a second failure is
    evidence about the model rather than about the weather."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    replies = iter([_drafted(fmt, body="Ohne alles."), _drafted(fmt)])
    seen: list[str] = []

    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda prompt, **k: seen.append(prompt) or next(replies),
    )

    assert len(seen) == 2, "the miss was not retried"
    assert draft.body.startswith("Berlin")


def test_the_retry_says_what_the_first_attempt_was_missing(session):
    """Re-asking the identical question the identical way is how the identical
    answer comes back. The complaint is already a sentence; it travels."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    replies = iter([_drafted(fmt, body="Ohne alles."), _drafted(fmt)])
    seen: list[str] = []

    assets.write(
        session, fmt, client, angle,
        invoke=lambda prompt, **k: seen.append(prompt) or next(replies),
    )

    assert "DER VORIGE VERSUCH WURDE ABGELEHNT" not in seen[0]
    assert "Dateline" in seen[1]
    assert "Boilerplate" in seen[1]


def test_a_second_miss_refuses_with_a_reason_and_stores_nothing(session):
    """The failure this replaces is a release with no quote in it, stored, checked
    and rendered as a draft somebody has to read to the end to reject."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    reasons: list[str] = []
    calls: list[str] = []

    with pytest.raises(assets.Malformed) as caught:
        assets.write(
            session, fmt, client, angle,
            invoke=lambda prompt, **k: calls.append(prompt) or _drafted(
                fmt, body="Ohne alles."
            ),
            note=reasons.append,
        )

    assert len(calls) == 2
    assert "Pressemitteilung nicht geschrieben" in caught.value.reason
    assert "Dateline" in caught.value.reason
    assert reasons == [caught.value.reason], "the reason was not handed over to store"
    assert session.scalars(select(Asset)).all() == []


def test_a_reply_that_never_parses_ends_in_the_same_refusal(session):
    """Not valid JSON and valid JSON in the wrong shape are one outcome for the
    reader: nothing was written, and here is why."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    calls: list[str] = []

    with pytest.raises(assets.Malformed):
        assets.write(
            session, fmt, client, angle,
            invoke=lambda prompt, **k: calls.append(prompt) or "kein JSON",
        )

    assert len(calls) == 2


def test_a_refused_requirement_is_handed_to_the_caller_as_a_reason_too(session):
    """Both refusals reach the same place, so the surface has one field to store
    and one sentence to show whichever way nothing was written."""
    client, angle = _mandate(session, facts={})
    fmt = assets.definition(AssetKind.STATEMENT)
    reasons: list[str] = []

    with pytest.raises(assets.RequirementsMissing):
        assets.write(
            session, fmt, client, angle,
            invoke=lambda *a, **k: _drafted(fmt),
            note=reasons.append,
        )

    assert reasons and "Geschäftsführung" in reasons[0]


def test_a_malformed_reply_is_still_a_parse_error_to_an_older_caller(session):
    """Malformed inherits ParseError on purpose: the retry precedent stays one
    concept, and a caller that already handles "the model did not answer usably"
    keeps working."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)

    with pytest.raises(ParseError):
        assets.write(
            session, fmt, client, angle,
            invoke=lambda *a, **k: _drafted(fmt, body="Ein Satz ohne alles."),
        )


# --- The formats on the page ---------------------------------------------------


def test_every_format_name_and_description_has_an_english_entry():
    """The chrome switches language; a card that stays German in an English UI
    reads as broken rather than as untranslated."""
    for fmt in assets.FORMATS:
        assert i18n.translate(fmt.name, "en"), fmt.name
        assert i18n.translate(fmt.description, "en") != fmt.description, fmt.key
