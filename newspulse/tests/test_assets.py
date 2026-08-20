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

from newspulse import assets, guide, profile, prose
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


def _mandate(
    session,
    *,
    facts: dict[str, str] | None = None,
    comms_guide: str = "",
    articles: int = 2,
) -> tuple[Client, Angle]:
    """A mandate with an impulse, and exactly the profile the test needs.

    ``facts`` defaults to a filled profile because most tests are about something
    other than the refusal; the refusal tests pass an empty one deliberately.
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
            source="Börsen-Zeitung",
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
    "ceo": "Alexandra Prot, Geschäftsführerin",
    "geschaeftsfeld": "Verwahrung digitaler Vermögenswerte für Banken.",
}


def _drafted(**over) -> str:
    payload = {
        "title": "Alpha AG baut Verwahrung für Banken aus",
        "body": "Berlin, 20. August 2026. Alpha AG erweitert ihr Angebot.\n\n"
        "Der Schritt folgt auf die Verlagerung der Verwahrung.",
        "speaker": "Alexandra Prot, Geschäftsführerin",
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
            invoke=lambda prompt, **k: calls.append(prompt) or _drafted(),
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
        assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())

    assert "ceo" in [req.key for req in caught.value.missing]


def test_a_client_without_a_guide_gets_no_qa(session):
    """The Q&A's value is the questions nobody wants asked, and the guide is where
    those live. Without one there is nothing to build it from."""
    client, angle = _mandate(session, comms_guide="")
    fmt = assets.definition(AssetKind.QA)

    with pytest.raises(assets.RequirementsMissing) as caught:
        assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())

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

    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
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
            title="Verwahrung — die Begründung verschiebt sich",
            body="Berlin, 20. August 2026. Die Anprobe ist gewandert — und wird "
            "dort bezahlt.\n\nZweiter Absatz.",
        ),
    )

    stored = assets.store(session, fmt, client, angle, draft)

    assert not prose.has_dash(stored.title)
    assert not prose.has_dash(stored.body)
    assert stored.body.count("\n\n") == 1, "the paragraphs survive"


def test_the_attribution_is_the_profiles_and_not_the_models(session):
    """The one artefact here that cannot be repaired after it has gone out is a
    named person's quote. The prompt asks for the name verbatim; what is stored is
    the profile's, so the guarantee does not rest on the model complying."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)

    draft = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(speaker="Alexander Prot, CEO"),
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
        invoke=lambda *a, **k: _drafted(speaker="Dr. Erfunden, Sprecher"),
    )
    stored = assets.store(session, fmt, client, angle, draft)

    assert stored.speaker == ""


def test_rewriting_a_format_replaces_the_draft_it_supersedes(session):
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    first = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
    assets.store(session, fmt, client, angle, first)

    second = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(body="Berlin, 20. August 2026. Neu."),
    )
    assets.store(session, fmt, client, angle, second)

    rows = assets.for_angle(session, angle.id)
    assert len(rows) == 1
    assert "Neu." in rows[0].body


def test_a_released_asset_is_never_overwritten_by_a_rewrite(session):
    """Its text is the record of what actually went out. A redraft is a new row
    beside it, not a change to it."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.PRESSEMITTEILUNG)
    first = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
    released = assets.store(session, fmt, client, angle, first)
    released.released_at = dt.datetime.now(dt.UTC)
    released.released_by = "mensch"
    session.commit()

    second = assets.write(
        session, fmt, client, angle,
        invoke=lambda *a, **k: _drafted(body="Berlin, 20. August 2026. Neu."),
    )
    assets.store(session, fmt, client, angle, second)

    rows = assets.for_angle(session, angle.id)
    assert len(rows) == 2
    assert session.get(Asset, released.id).body.endswith("Verlagerung der Verwahrung.")


def test_a_reply_without_text_is_a_parse_error_rather_than_an_empty_asset(session):
    """"The draft failed" and "here is your draft" must never look alike."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)

    with pytest.raises(ParseError):
        assets.write(
            session, fmt, client, angle, invoke=lambda *a, **k: _drafted(body="   ")
        )


def test_the_texts_for_several_impulses_come_back_keyed_by_impulse(session):
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
    assets.store(session, fmt, client, angle, draft)

    grouped = assets.by_angle(session, [angle.id, angle.id + 99])

    assert list(grouped) == [angle.id]
    assert len(grouped[angle.id]) == 1


# --- The checks ----------------------------------------------------------------


def test_an_asset_with_neither_check_recorded_is_unchecked_not_clean(session):
    """The one state the page must never draw as a clean bill of health."""
    client, angle = _mandate(session)
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())

    stored = assets.store(session, fmt, client, angle, draft)

    assert stored.reviewed_by == ""
    assert stored.guide_reviewed_by == ""
    assert stored.check_state is CheckState.UNGEPRUEFT


def test_both_checks_run_over_a_format_and_are_stored_with_it(session):
    client, angle = _mandate(session, comms_guide="No-Go: das Wort günstig.")
    fmt = assets.definition(AssetKind.STATEMENT)
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
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
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
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
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
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
        invoke=lambda *a, **k: _drafted(body="Die Verwahrung wandert — zurück."),
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
        invoke=lambda *a, **k: _drafted(body="Eins — zwei."),
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
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())

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
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
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
        invoke=lambda *a, **k: _drafted(body="Eins — zwei."),
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
        invoke=lambda *a, **k: _drafted(body="Eins — zwei."),
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
        invoke=lambda *a, **k: _drafted(body="Eins — zwei."),
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
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())

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
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
    checked = assets.check(
        client,
        assets.checkable(session, fmt, angle, draft),
        generate=lambda *a, **k: _review(send=False, concerns=["Zu werblich."]),
        guide_generate=lambda *a, **k: _guide_verdict(),
    )
    assets.store(session, fmt, client, angle, draft, checked)

    again = assets.write(
        session, fmt, client, angle, invoke=lambda *a, **k: _drafted(body="Neu.")
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
    draft = assets.write(session, fmt, client, angle, invoke=lambda *a, **k: _drafted())
    monkeypatch.setattr(config, "review_configured", lambda: False)

    with pytest.raises(RuntimeError, match="Zweitmodell"):
        assets.crosscheck(client, assets.checkable(session, fmt, angle, draft))
