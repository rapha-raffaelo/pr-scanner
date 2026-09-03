"""Die Stakeholder-Karte (RIS-03): the standing map, and the selection.

Nothing here reaches a model and nothing reaches the network — both model
calls in :mod:`newspulse.stakeholders` are exercised with injected ``invoke``
callables returning canned JSON, and the tests that must prove a call never
happened inject one that fails the test if it fires.

The disciplines under test, in order:

* **No profile, no invented map.** A mandate without profile entries gets
  ``None`` — the sentence about what is missing — and the model is never
  asked.
* **A proposal adds and never overwrites.** Every standing row is skipped by
  name, hand-set or proposed alike, and a proposed row never carries a
  contact: a guessed name would be called on the one evening it matters.
* **Every row says who set it**, and a person's edit makes the row the
  person's.
* **A selection is from the card, with a reason, or not at all** — and the
  one-sentence information need may come back empty rather than invented.
* **The order that is kept is the person's.** The proposal's order is an
  Empfehlung under the ``"modell"`` token; one resort renames every row.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import brain, stakeholders
from newspulse.matching import title_hash
from newspulse.models import (
    Article,
    Base,
    Client,
    ClientFact,
    Crisis,
    Issue,
    IssueSignal,
    Stakeholder,
    StakeholderLevel,
    StakeholderSelection,
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


def _fact(session, client: Client, key: str, value: str) -> None:
    session.add(ClientFact(client_id=client.id, key=key, value=value))
    session.commit()


def _article(session, title: str) -> Article:
    article = Article(
        title=title,
        url=f"https://example.de/{abs(hash(title))}",
        source="Handelsblatt",
        published_at=_NOW - dt.timedelta(days=2),
        fetched_at=_NOW - dt.timedelta(days=2),
        title_hash=title_hash(title, "Handelsblatt"),
    )
    session.add(article)
    session.commit()
    return article


def _issue(session, client: Client, *, title: str = "Vorwurf Vertragsklauseln") -> Issue:
    article = _article(session, f"{title} — Bericht")
    issue = Issue(
        client_id=client.id,
        title=title,
        opened_by="mensch",
        opened_at=_NOW - dt.timedelta(days=3),
        last_moved_at=_NOW - dt.timedelta(days=1),
    )
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


def _card_answer(*groups: dict) -> str:
    return json.dumps({"gruppen": list(groups)})


def _selection_answer(*rows: dict) -> str:
    return json.dumps({"auswahl": list(rows)})


def _never_invoked(prompt, timeout=None):  # pragma: no cover — asserting absence
    raise AssertionError("the model must not be called here")


# --- The proposal from the profile -------------------------------------------------


def test_a_mandate_without_profile_gets_none_and_the_model_is_never_asked(
    session, mandate
):
    """No invented map: the answer is the sentence about what is missing."""
    result = stakeholders.propose_card(session, mandate, invoke=_never_invoked)
    assert result is None
    assert session.scalars(select(Stakeholder)).all() == []


def test_the_proposal_adds_groups_under_the_model_token_without_a_contact(
    session, mandate
):
    _fact(session, mandate, "sitz", "Leipzig, Deutschland")
    answer = _card_answer(
        {
            "gruppe": "Anwohner am Standort Leipzig",
            "betroffenheit": "Wohnen neben dem Werksgelände.",
            "einfluss": "mittel",
            "kanal": "Anwohnerversammlung",
            # A model that volunteers a name must not land one on the map.
            "ansprechpartner": "Herr Erfunden",
        },
        {
            "gruppe": "Branchenverband Solarwirtschaft",
            "betroffenheit": "Spricht für die Branche des Mandats.",
            "einfluss": "hoch",
        },
    )
    added = stakeholders.propose_card(
        session, mandate, invoke=lambda prompt, timeout=None: answer, now=_NOW
    )
    assert [row.group_name for row in added] == [
        "Anwohner am Standort Leipzig",
        "Branchenverband Solarwirtschaft",
    ]
    for row in added:
        assert row.set_by == stakeholders.PROPOSED_BY_MODEL
        assert row.contact == ""
    assert added[0].einfluss is StakeholderLevel.MITTEL
    assert added[1].einfluss is StakeholderLevel.HOCH
    assert added[0].betroffenheit == "Wohnen neben dem Werksgelände."


def test_the_profile_lines_reach_the_prompt(session, mandate):
    _fact(session, mandate, "sitz", "Leipzig, Deutschland")
    seen: dict[str, str] = {}

    def _capture(prompt, timeout=None):
        seen["prompt"] = prompt
        return _card_answer()

    stakeholders.propose_card(session, mandate, invoke=_capture)
    assert "Leipzig, Deutschland" in seen["prompt"]
    assert "Solaris AG" in seen["prompt"]


def test_a_standing_row_is_never_overwritten_by_a_proposal(session, mandate):
    """A person set the row; the proposal must neither change nor double it —
    and a different casing of the same name is the same name."""
    _fact(session, mandate, "sitz", "Leipzig")
    stakeholders.save_row(
        session,
        mandate,
        group="Anwohner am Standort",
        betroffenheit="Vom Menschen formuliert.",
        contact="Frau Weber",
        by="berater@agentur.de",
    )
    answer = _card_answer(
        {
            "gruppe": "anwohner  am Standort",
            "betroffenheit": "Vom Modell formuliert.",
            "einfluss": "hoch",
        }
    )
    added = stakeholders.propose_card(
        session, mandate, invoke=lambda prompt, timeout=None: answer
    )
    assert added == []
    rows = session.scalars(select(Stakeholder)).all()
    assert len(rows) == 1
    assert rows[0].betroffenheit == "Vom Menschen formuliert."
    assert rows[0].contact == "Frau Weber"
    assert rows[0].set_by == "berater@agentur.de"


def test_a_group_with_an_unknown_level_is_dropped_not_guessed(session, mandate):
    _fact(session, mandate, "sitz", "Leipzig")
    answer = _card_answer(
        {"gruppe": "Anwohner", "einfluss": "gigantisch"},
        {"gruppe": "Kommune", "einfluss": "hoch"},
    )
    added = stakeholders.propose_card(
        session, mandate, invoke=lambda prompt, timeout=None: answer
    )
    assert [row.group_name for row in added] == ["Kommune"]


def test_both_prompts_compose_their_blocks_and_write_none_of_them_out():
    """The structure test the PRD asks for: the ``#blocks:`` header names what
    the prompt includes, the includes resolve, and no block body is pasted."""
    from importlib import resources

    for resource in (
        "prompts/stakeholder_map.txt",
        "prompts/stakeholder_select.txt",
    ):
        text = resources.files("newspulse").joinpath(resource).read_text("utf-8")
        assert brain.has_declaration(text), resource
        assert brain.included(text) == brain.declared(text), resource
        # The shipped block bodies must not be written out in the prompt.
        for key in brain.declared(text):
            body = resources.files("newspulse").joinpath(f"blocks/{key}.txt").read_text(
                "utf-8"
            )
            first_line = body.strip().splitlines()[0]
            assert first_line not in text, (resource, key)
        composed = brain.compose(text, source=brain.shipped())
        assert "{{brain:" not in composed


# --- A person edits the map --------------------------------------------------------


def test_save_row_records_the_person_and_a_proposal_then_skips_the_row(
    session, mandate
):
    _fact(session, mandate, "sitz", "Leipzig")
    answer = _card_answer({"gruppe": "Kommune", "einfluss": "hoch"})
    (proposed,) = stakeholders.propose_card(
        session, mandate, invoke=lambda prompt, timeout=None: answer
    )
    assert proposed.set_by == stakeholders.PROPOSED_BY_MODEL

    edited = stakeholders.save_row(
        session,
        mandate,
        group="Kommune Leipzig",
        betroffenheit="Genehmigt den Standort.",
        einfluss="hoch",
        contact="Amt für Wirtschaft",
        channel="Telefon",
        by="berater@agentur.de",
        row_id=proposed.id,
    )
    assert edited is not None
    assert edited.set_by == "berater@agentur.de"
    assert edited.group_name == "Kommune Leipzig"

    again = stakeholders.propose_card(
        session,
        mandate,
        invoke=lambda prompt, timeout=None: _card_answer(
            {"gruppe": "Kommune Leipzig", "einfluss": "mittel"}
        ),
    )
    assert again == []
    assert session.get(Stakeholder, edited.id).einfluss is StakeholderLevel.HOCH


def test_save_row_without_an_id_updates_the_standing_row_of_the_same_name(
    session, mandate
):
    first = stakeholders.save_row(
        session, mandate, group="Belegschaft", by="a@agentur.de"
    )
    second = stakeholders.save_row(
        session, mandate, group="  Belegschaft ", contact="Betriebsrat", by="b@agentur.de"
    )
    assert second.id == first.id
    assert second.contact == "Betriebsrat"
    assert len(session.scalars(select(Stakeholder)).all()) == 1


def test_save_row_refuses_an_out_of_set_level(session, mandate):
    with pytest.raises(ValueError):
        stakeholders.save_row(
            session, mandate, group="Anwohner", einfluss="enorm", by="mensch"
        )
    assert session.scalars(select(Stakeholder)).all() == []


def test_save_row_with_an_empty_group_writes_nothing(session, mandate):
    assert stakeholders.save_row(session, mandate, group="   ", by="mensch") is None
    assert session.scalars(select(Stakeholder)).all() == []


def test_delete_row_takes_its_selections_with_it(session, mandate):
    row = stakeholders.save_row(session, mandate, group="Anwohner", by="mensch")
    issue = _issue(session, mandate)
    session.add(
        StakeholderSelection(
            issue_id=issue.id,
            stakeholder_id=row.id,
            reason="Betroffen.",
            position=1,
            position_set_by="mensch",
        )
    )
    session.commit()
    assert stakeholders.delete_row(session, mandate, row.id) is True
    assert session.scalars(select(Stakeholder)).all() == []
    assert session.scalars(select(StakeholderSelection)).all() == []


# --- The selection at an issue -----------------------------------------------------


def _seed_card(session, mandate) -> list[Stakeholder]:
    for name, level in (
        ("Anwohner am Standort", "mittel"),
        ("Branchenverband", "hoch"),
        ("Belegschaft", "niedrig"),
    ):
        stakeholders.save_row(
            session,
            mandate,
            group=name,
            betroffenheit=f"{name} sind vom Standort berührt.",
            einfluss=level,
            by="mensch",
        )
    return stakeholders.card(session, mandate)


def test_the_selection_stores_reason_need_and_the_recommended_order(
    session, mandate
):
    _seed_card(session, mandate)
    issue = _issue(session, mandate)
    answer = _selection_answer(
        {
            "gruppe": "Belegschaft",
            "begruendung": "Der Vorwurf betrifft die eigenen Verträge.",
            "informationsbedarf": "Ob die eigenen Verträge betroffen sind.",
        },
        {
            "gruppe": "Branchenverband",
            "begruendung": "Der Verband spricht für die Branche des Mandats.",
            "informationsbedarf": "",
        },
    )
    rows = stakeholders.select_for(
        session, issue=issue, invoke=lambda prompt, timeout=None: answer, now=_NOW
    )
    assert [row.stakeholder.group_name for row in rows] == [
        "Belegschaft",
        "Branchenverband",
    ]
    assert [row.position for row in rows] == [1, 2]
    assert all(
        row.position_set_by == stakeholders.PROPOSED_BY_MODEL for row in rows
    )
    assert rows[0].reason == "Der Vorwurf betrifft die eigenen Verträge."
    assert rows[0].info_need == "Ob die eigenen Verträge betroffen sind."
    # No stored line supported a sentence — the honest row carries none.
    assert rows[1].info_need == ""
    assert stakeholders.order_is_recommendation(rows) is True


def test_a_group_the_map_does_not_hold_is_dropped(session, mandate):
    _seed_card(session, mandate)
    issue = _issue(session, mandate)
    answer = _selection_answer(
        {"gruppe": "Aufsichtsbehörde", "begruendung": "Klingt plausibel."},
        {"gruppe": "Belegschaft", "begruendung": "Eigene Verträge betroffen."},
    )
    rows = stakeholders.select_for(
        session, issue=issue, invoke=lambda prompt, timeout=None: answer
    )
    assert [row.stakeholder.group_name for row in rows] == ["Belegschaft"]


def test_a_group_without_a_reason_is_not_stored(session, mandate):
    _seed_card(session, mandate)
    issue = _issue(session, mandate)
    answer = _selection_answer(
        {"gruppe": "Belegschaft", "begruendung": "   "},
        {"gruppe": "Branchenverband", "begruendung": "Spricht für die Branche."},
    )
    rows = stakeholders.select_for(
        session, issue=issue, invoke=lambda prompt, timeout=None: answer
    )
    assert [row.stakeholder.group_name for row in rows] == ["Branchenverband"]
    assert session.scalars(select(StakeholderSelection)).all() == rows


def test_a_standing_selection_is_kept_and_the_model_is_not_asked_again(
    session, mandate
):
    """Re-asking would clobber the order a person may have set."""
    _seed_card(session, mandate)
    issue = _issue(session, mandate)
    first = stakeholders.select_for(
        session,
        issue=issue,
        invoke=lambda prompt, timeout=None: _selection_answer(
            {"gruppe": "Belegschaft", "begruendung": "Eigene Verträge betroffen."}
        ),
    )
    second = stakeholders.select_for(session, issue=issue, invoke=_never_invoked)
    assert [row.id for row in second] == [row.id for row in first]


def test_an_empty_map_selects_nothing_and_asks_no_model(session, mandate):
    issue = _issue(session, mandate)
    assert (
        stakeholders.select_for(session, issue=issue, invoke=_never_invoked) == []
    )


def test_a_selection_hangs_on_exactly_one_occasion(session, mandate):
    issue = _issue(session, mandate)
    with pytest.raises(ValueError):
        stakeholders.select_for(session, invoke=_never_invoked)
    with pytest.raises(ValueError):
        stakeholders.selection_for(session, issue=issue, crisis=issue)  # type: ignore[arg-type]


def test_a_crisis_carries_a_selection_of_its_own(session, mandate):
    _seed_card(session, mandate)
    article = _article(session, "Krisenauslöser Bericht")
    crisis = Crisis(client_id=mandate.id, article_id=article.id, declared_by="mensch")
    session.add(crisis)
    session.commit()
    rows = stakeholders.select_for(
        session,
        crisis=crisis,
        invoke=lambda prompt, timeout=None: _selection_answer(
            {"gruppe": "Branchenverband", "begruendung": "Spricht für die Branche."}
        ),
    )
    assert len(rows) == 1
    assert rows[0].crisis_id == crisis.id
    assert rows[0].issue_id is None


# --- The order that is kept is the person's ----------------------------------------


def _seed_selection(
    session, mandate, *, title: str = "Vorwurf Vertragsklauseln"
) -> tuple[Issue, list[StakeholderSelection]]:
    _seed_card(session, mandate)
    issue = _issue(session, mandate, title=title)
    answer = _selection_answer(
        {"gruppe": "Anwohner am Standort", "begruendung": "Wohnen am Standort."},
        {"gruppe": "Branchenverband", "begruendung": "Spricht für die Branche."},
        {"gruppe": "Belegschaft", "begruendung": "Eigene Verträge betroffen."},
    )
    rows = stakeholders.select_for(
        session, issue=issue, invoke=lambda prompt, timeout=None: answer
    )
    return issue, rows


def test_reorder_stores_the_persons_order_under_their_name(session, mandate):
    issue, rows = _seed_selection(session, mandate)
    ordered = [rows[2].id, rows[0].id, rows[1].id]
    result = stakeholders.reorder(
        session, issue=issue, ordered_ids=ordered, by="berater@agentur.de"
    )
    assert [row.id for row in result] == ordered
    assert [row.position for row in result] == [1, 2, 3]
    assert all(row.position_set_by == "berater@agentur.de" for row in result)
    assert stakeholders.order_is_recommendation(result) is False
    # And the stored order is what a fresh read hands back.
    fresh = stakeholders.selection_for(session, issue=issue)
    assert [row.id for row in fresh] == ordered


def test_reorder_refuses_an_order_that_names_the_wrong_rows(session, mandate):
    issue, rows = _seed_selection(session, mandate)
    with pytest.raises(ValueError):
        stakeholders.reorder(
            session, issue=issue, ordered_ids=[rows[0].id], by="mensch"
        )
    # Nothing moved: the standing order is untouched.
    fresh = stakeholders.selection_for(session, issue=issue)
    assert [row.position for row in fresh] == [1, 2, 3]
    assert stakeholders.order_is_recommendation(fresh) is True


# --- The page ----------------------------------------------------------------------


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


def test_a_mandate_without_profile_gets_the_sentence_and_the_link(
    web, session, mandate
):
    """No invented map on the page either: the sentence about what is missing,
    with the link to where it is filled in."""
    page = web.get(f"/client/{mandate.id}/issues")
    assert page.status_code == 200
    assert "keine Profilangaben hinterlegt" in page.text
    assert f"/client/{mandate.id}/profil" in page.text


def test_a_group_without_a_contact_is_a_named_gap_with_a_profile_link(
    web, session, mandate
):
    stakeholders.save_row(
        session, mandate, group="Anwohner am Standort", by="mensch"
    )
    page = web.get(f"/client/{mandate.id}/issues")
    assert "Kein Ansprechpartner benannt." in page.text
    assert f"/client/{mandate.id}/profil" in page.text


def test_every_map_row_shows_who_set_it(web, session, mandate):
    stakeholders.save_row(
        session,
        mandate,
        group="Branchenverband",
        contact="Herr Maier",
        by="berater@agentur.de",
    )
    page = web.get(f"/client/{mandate.id}/issues")
    assert "gesetzt von" in page.text
    assert "berater@agentur.de" in page.text


def test_the_save_route_writes_a_row_under_the_person_token(web, session, mandate):
    response = web.post(
        f"/client/{mandate.id}/stakeholder/save",
        data={
            "group": "Belegschaft",
            "betroffenheit": "Eigene Verträge.",
            "einfluss": "niedrig",
            "redirect_to": f"/client/{mandate.id}/issues",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    rows = session.scalars(select(Stakeholder)).all()
    assert len(rows) == 1
    assert rows[0].set_by == "mensch"
    assert rows[0].einfluss is StakeholderLevel.NIEDRIG


def test_the_recommended_order_is_marked_as_a_recommendation(web, session, mandate):
    _, rows = _seed_selection(session, mandate)
    page = web.get(f"/client/{mandate.id}/issues")
    assert "Reihenfolge: Empfehlung" in page.text
    # Every selected group stands with its stored reason.
    for row in rows:
        assert row.reason in page.text


def test_the_reorder_route_saves_the_persons_order(web, session, mandate):
    issue, rows = _seed_selection(session, mandate)
    response = web.post(
        f"/issues/{issue.id}/stakeholder/reihenfolge",
        data={
            "sid": [str(row.id) for row in rows],
            "pos": ["3", "1", "2"],
            "redirect_to": f"/client/{mandate.id}/issues",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    fresh = stakeholders.selection_for(session, issue=issue)
    assert [row.id for row in fresh] == [rows[1].id, rows[2].id, rows[0].id]
    assert all(row.position_set_by == "mensch" for row in fresh)
    page = web.get(f"/client/{mandate.id}/issues")
    assert "Reihenfolge: Empfehlung" not in page.text
    assert "Reihenfolge gesetzt von" in page.text


def test_the_selection_route_is_a_noop_without_a_map(web, session, mandate):
    """The button answers with the note rather than inventing a selection —
    and no model is reached, because there is nothing to select from."""
    issue = _issue(session, mandate)
    response = web.post(
        f"/issues/{issue.id}/stakeholder/auswahl",
        data={"redirect_to": f"/client/{mandate.id}/issues"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert session.scalars(select(StakeholderSelection)).all() == []
    page = web.get(f"/client/{mandate.id}/issues")
    assert "Keine Auswahl entstanden" in page.text


def test_the_empty_information_need_renders_as_a_named_absence(
    web, session, mandate
):
    _seed_card(session, mandate)
    issue = _issue(session, mandate)
    stakeholders.select_for(
        session,
        issue=issue,
        invoke=lambda prompt, timeout=None: _selection_answer(
            {"gruppe": "Belegschaft", "begruendung": "Eigene Verträge betroffen."}
        ),
    )
    page = web.get(f"/client/{mandate.id}/issues")
    assert "keine gespeicherte Angabe, aus der sich das ergibt" in page.text


def test_every_python_written_note_and_label_is_translated():
    """The strings written in Python and rendered through ``t(...)`` cannot be
    seen by a template scan, so they are held to the table here.

    The notes are walked off :data:`stakeholder_ui.NOTES` rather than copied
    into a list here: a sentence added to the feature without its English pair
    has to fail this test, and a list that has to be kept in step by hand is a
    list that stops being in step.
    """
    from newspulse import i18n
    from newspulse.web.routes import stakeholder_ui

    labels = (
        # The three levels render through ``t(...)`` off the stored value.
        "hoch",
        "mittel",
        "niedrig",
    )
    known = set(i18n.known_keys())
    for sentence in (*stakeholder_ui.NOTES, *labels):
        assert sentence in known, sentence


# --- What the review found -----------------------------------------------------------


def test_renaming_a_row_onto_a_case_variant_of_another_group_is_refused(
    session, mandate
):
    """``_norm`` is what this module means by one group, and the schema's
    UNIQUE compares the stored spelling — so the check has to be here, on the
    edit branch as well as on the add branch. Two rows ``_norm`` calls the same
    would collapse in ``select_for``'s lookup, and the loser would be
    unselectable for every issue and every crisis, silently."""
    stakeholders.save_row(session, mandate, group="Anwohner", by="mensch")
    verband = stakeholders.save_row(session, mandate, group="Verband", by="mensch")

    refused = stakeholders.save_row(
        session, mandate, group="anwohner", by="mensch", row_id=verband.id
    )

    assert refused is None
    session.expire_all()
    assert sorted(row.group_name for row in stakeholders.card(session, mandate)) == [
        "Anwohner",
        "Verband",
    ]


def test_a_person_editing_a_proposed_row_clears_the_model_stamp(session, mandate):
    """The row is the person's from that point: their Betroffenheit is their
    own text, and a stamp would claim a model call that never happened."""
    _fact(session, mandate, "sitz", "Leipzig, Deutschland")
    added = stakeholders.propose_card(
        session,
        mandate,
        invoke=lambda prompt, timeout=None: _card_answer(
            {"gruppe": "Anwohner", "betroffenheit": "Wohnen am Werk.", "einfluss": "mittel"}
        ),
    )
    proposed = added[0]
    assert proposed.brain_version is not None

    edited = stakeholders.save_row(
        session,
        mandate,
        group="Anwohner",
        betroffenheit="Wohnen seit dem Ausbau direkt an der Zufahrt.",
        by="berater@agentur.de",
        row_id=proposed.id,
    )

    assert edited.set_by == "berater@agentur.de"
    assert edited.brain_version is None


def test_a_cleared_position_field_answers_with_a_note_and_not_a_422(
    web, session, mandate
):
    """The position input is a number field a person can empty. FastAPI's raw
    validation JSON is not an answer somebody who pressed a button can act
    on — the page has to come back with the sentence."""
    issue, rows = _seed_selection(session, mandate)
    response = web.post(
        f"/issues/{issue.id}/stakeholder/reihenfolge",
        data={
            "sid": [str(row.id) for row in rows],
            "pos": ["1", "", "3"],
            "redirect_to": f"/client/{mandate.id}/issues",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert all(
        row.position_set_by == stakeholders.PROPOSED_BY_MODEL
        for row in stakeholders.selection_for(session, issue=issue)
    )
    assert "das Formular war unvollständig" in web.get(
        f"/client/{mandate.id}/issues"
    ).text


def test_two_rows_carrying_the_same_number_refuse_the_order(web, session, mandate):
    """A tie-break would be the tool guessing half the call order, which the
    page would then present as the person's — the one claim this feature exists
    not to make."""
    issue, rows = _seed_selection(session, mandate)
    response = web.post(
        f"/issues/{issue.id}/stakeholder/reihenfolge",
        data={
            "sid": [str(row.id) for row in rows],
            "pos": ["1", "1", "2"],
            "redirect_to": f"/client/{mandate.id}/issues",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    stored = stakeholders.selection_for(session, issue=issue)
    assert [row.id for row in stored] == [row.id for row in rows]
    assert all(
        row.position_set_by == stakeholders.PROPOSED_BY_MODEL for row in stored
    )
    assert "dieselbe Nummer" in web.get(f"/client/{mandate.id}/issues").text


def test_the_register_reads_every_selection_in_one_query(web, session, mandate):
    """The history half of the register is unbounded, so the selections cannot
    be read per row. One IN answers them all, and ``selectin`` brings the groups
    in one more."""
    from sqlalchemy import event

    _seed_selection(session, mandate)
    _seed_selection(session, mandate, title="Zweiter Vorwurf")
    _seed_selection(session, mandate, title="Dritter Vorwurf")

    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        if "stakeholder_selections" in statement:
            statements.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", _record)
    try:
        assert web.get(f"/client/{mandate.id}/issues").status_code == 200
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", _record)

    assert len(statements) == 1, statements


def _crisis(session, mandate) -> Crisis:
    article = _article(session, "Werk stillgelegt — Bericht")
    standing = Crisis(
        client_id=mandate.id, article_id=article.id, declared_by="mensch"
    )
    session.add(standing)
    session.commit()
    return standing


def test_the_crisis_page_offers_the_selection_and_keeps_the_persons_order(
    web, session, mandate
):
    """The acceptance names both occasions. The engine anchored a crisis from
    the start; this is the page that reaches it."""
    _seed_card(session, mandate)
    standing = _crisis(session, mandate)
    back = f"/client/{mandate.id}/krise?krise={standing.id}"

    page = web.get(f"/client/{mandate.id}/krise")
    assert "Stakeholder auswählen" in page.text

    rows = stakeholders.select_for(
        session,
        crisis=standing,
        invoke=lambda prompt, timeout=None: _selection_answer(
            {"gruppe": "Belegschaft", "begruendung": "Das eigene Werk steht still."},
            {"gruppe": "Branchenverband", "begruendung": "Spricht für die Branche."},
        ),
    )
    page = web.get(back)
    assert "Reihenfolge: Empfehlung" in page.text
    assert "Das eigene Werk steht still." in page.text

    response = web.post(
        f"/crisis/{standing.id}/stakeholder/reihenfolge",
        data={
            "sid": [str(row.id) for row in rows],
            "pos": ["2", "1"],
            "redirect_to": back,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    fresh = stakeholders.selection_for(session, crisis=standing)
    assert [row.id for row in fresh] == [rows[1].id, rows[0].id]
    assert all(row.position_set_by == "mensch" for row in fresh)
    assert "Reihenfolge gesetzt von" in web.get(back).text


def test_the_crisis_page_names_the_missing_map_with_the_link_to_it(
    web, session, mandate
):
    """No map, no selection — and the named absence rather than a mute block:
    the crisis morning selects from the card, so it says where the card is."""
    standing = _crisis(session, mandate)
    page = web.get(f"/client/{mandate.id}/krise?krise={standing.id}")
    assert "Noch keine Stakeholder-Karte" in page.text
    assert f"/client/{mandate.id}/issues" in page.text


def test_a_benchmarks_issue_button_spends_no_model_call(web, session, mandate):
    """A yardstick has no workspace page, so its buttons cannot act either —
    otherwise a hand-typed POST still spends a call writing for a company that
    will never receive one."""
    _seed_card(session, mandate)
    issue = _issue(session, mandate)
    mandate.is_competitor = True
    session.commit()

    response = web.post(
        f"/issues/{issue.id}/stakeholder/auswahl",
        data={"redirect_to": "/"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert session.scalars(select(StakeholderSelection)).all() == []
