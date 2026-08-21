"""The deep-dive profile, and the button that fills it from the web.

Two properties are load-bearing and both are about provenance: a fact a machine
read on the internet and a fact the consultant knows must never look alike, and
the machine must never overwrite the human.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import profile as profiles
from newspulse import profile_refresh
from newspulse.analyzer import ParseError
from newspulse.models import Base, Client, ClientFact, ProfileProposal
from newspulse.web.app import create_app, get_db


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


@pytest.fixture
def web(factory):
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _client(session) -> Client:
    c = Client(name="Qonto", aliases=[], industry="Neobank", country="DE",
               website="https://qonto.com", keywords=[], alert_topics=[])
    session.add(c)
    session.commit()
    return c


def _answer(**fields) -> tuple[str, list[tuple[str, str]]]:
    return json.dumps({"felder": fields}), [("https://qonto.com/ueber-uns", "Qonto")]


# --- Research proposes, it does not write ----------------------------------


def test_research_stores_nothing(session):
    """The same posture as the theme and competitor suggestions: these values
    shape months of monitoring and every generated text downstream."""
    client = _client(session)

    found = profiles.research(client, generate=lambda p: _answer(ceo="Alexandre Prot"))

    assert [(f.key, f.value) for f in found] == [("ceo", "Alexandre Prot")]
    assert session.query(ClientFact).count() == 0


def test_the_source_travels_with_the_value(session):
    """A profile field filled from the internet is only worth having if the reader
    can open the page it came from."""
    client = _client(session)

    found = profiles.research(client, generate=lambda p: _answer(sitz="Paris"))

    assert found[0].source_url == "https://qonto.com/ueber-uns"
    assert found[0].source_title == "Qonto"


def test_a_key_outside_the_field_list_is_dropped(session):
    """The model is asked for fourteen fields. An invented fifteenth is not a
    bonus, it is a value nothing renders and nobody audits."""
    client = _client(session)

    found = profiles.research(
        client, generate=lambda p: _answer(ceo="A. Prot", lieblingsfarbe="blau")
    )

    assert [f.key for f in found] == ["ceo"]


def test_the_kickoff_only_fields_are_never_asked_of_the_web(session):
    """Who may be quoted and who picks up the phone at seven in the evening are
    kick-off answers. A grounded model asked for them returns a switchboard
    number or a plausible name, and both are used as if somebody had checked."""
    client = _client(session)
    seen: list[str] = []

    profiles.research(client, generate=lambda p: (seen.append(p), _answer(ceo="x"))[1])

    for key in ("sprecher", "zielmedien", "krisenkontakt"):
        assert key not in seen[0], key


def test_a_kickoff_only_field_offered_by_the_model_anyway_is_dropped(session):
    """The prompt does not list them, but a model that was not asked can still
    volunteer — and a guessed after-hours number is dialled the one evening it
    matters."""
    client = _client(session)

    found = profiles.research(
        client,
        generate=lambda p: _answer(
            ceo="A. Prot", krisenkontakt="Zentrale: +49 30 000000", sprecher="Der CEO"
        ),
    )

    assert [f.key for f in found] == ["ceo"]


def test_a_reply_that_is_not_json_is_a_parse_error(session):
    client = _client(session)

    with pytest.raises(ParseError):
        profiles.research(client, generate=lambda p: ("Klar!", []))


def test_the_prompt_carries_what_we_already_know(session):
    """Name alone finds the wrong company. The website and the aliases are what
    disambiguate a two-word brand."""
    client = _client(session)
    client.aliases = ["Qonto SAS"]
    seen: list[str] = []

    profiles.research(
        client, generate=lambda p: (seen.append(p), _answer(ceo="x"))[1]
    )

    assert "https://qonto.com" in seen[0]
    assert "Qonto SAS" in seen[0]
    assert "Neobank" in seen[0]


# --- Storage and provenance -------------------------------------------------


def test_saving_empty_clears_rather_than_storing_a_blank(session):
    """Deleting a wrong machine answer means "this is not known"; a row holding an
    empty string would keep claiming the field had been dealt with."""
    client = _client(session)
    profiles.save(session, client, "ceo", "Falscher Name", filled_by="gemini")

    profiles.save(session, client, "ceo", "   ")

    assert profiles.stored(session, client.id) == {}


def test_filling_the_same_field_twice_replaces_it(session):
    client = _client(session)
    profiles.save(session, client, "sitz", "Berlin")
    profiles.save(session, client, "sitz", "Paris")

    facts = profiles.stored(session, client.id)
    assert facts["sitz"].value == "Paris"
    assert session.query(ClientFact).count() == 1


def test_an_unknown_field_is_refused(session):
    client = _client(session)

    assert profiles.save(session, client, "lieblingsfarbe", "blau") is None
    assert session.query(ClientFact).count() == 0


# --- The page ---------------------------------------------------------------


def test_the_page_shows_the_source_of_a_machine_filled_line(factory, web):
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "ceo", "Alexandre Prot",
                      source_url="https://qonto.com/ueber-uns", source_title="Qonto",
                      filled_by="gemini-2.5-flash")
        client_id = client.id

    body = web.get(f"/client/{client_id}/profil").text

    assert "Alexandre Prot" in body
    assert "gemini-2.5-flash" in body
    assert "https://qonto.com/ueber-uns" in body


def test_a_hand_typed_line_carries_no_citation(factory, web):
    """His own knowledge is the strongest provenance in the building and needs no
    link; printing "Von mensch" beside it would be noise."""
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "ceo", "Weiß ich vom Kick-off")
        client_id = client.id

    body = web.get(f"/client/{client_id}/profil").text

    assert "Weiß ich vom Kick-off" in body
    assert "mensch" not in body


def test_saving_the_form_makes_a_changed_line_the_consultants(factory, web):
    """Editing a machine answer makes it his, because that is what it now is —
    and the AI must not come back and overwrite it."""
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "sitz", "Zug",
                      source_url="https://x.de", filled_by="gemini-2.5-flash")
        client_id = client.id

    web.post(f"/client/{client_id}/profil", data={"sitz": "Paris"},
             follow_redirects=False)

    with factory() as session:
        fact = profiles.stored(session, client_id)["sitz"]
        assert fact.value == "Paris"
        assert fact.filled_by == "mensch"
        assert fact.source_url == ""


def test_an_untouched_machine_line_keeps_its_source_through_a_save(factory, web):
    """Saving the form must not launder every citation off the page."""
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "sitz", "Zug", source_url="https://x.de",
                      source_title="X", filled_by="gemini-2.5-flash")
        client_id = client.id

    web.post(f"/client/{client_id}/profil", data={"sitz": "Zug"}, follow_redirects=False)

    with factory() as session:
        fact = profiles.stored(session, client_id)["sitz"]
        assert fact.filled_by == "gemini-2.5-flash"
        assert fact.source_url == "https://x.de"


def _propose(session, client_id, **values) -> dict[str, int]:
    """Put proposals on file for a client, the way a refresh would.

    Returns the row id per field: every button on the review page names the rows
    it was drawn with rather than the fields, so a test that posts a field name
    is testing something the page cannot do.

    Each row gets a source unless the test says otherwise, because a proposal
    with none is not shown at all — a machine asserting something it cannot back
    up is not a decision anyone should be asked to make.
    """
    rows = [
        ProfileProposal(
            client_id=client_id,
            key=key,
            value=value,
            source_url=url,
            source_title=title,
            proposed_at=dt.datetime(2026, 8, 19, 6, 10, tzinfo=dt.UTC),
            proposed_by="gemini-2.5-flash",
        )
        for key, (value, url, title) in values.items()
    ]
    session.add_all(rows)
    session.commit()
    return {row.key: row.id for row in rows}


def test_accepting_a_proposal_stamps_the_fact_as_the_humans(factory, web):
    """The model proposed it; the person decided it, and the decision is what is
    worth recording. A fact he vouched for must also stop being proposed over."""
    with factory() as session:
        client = _client(session)
        client_id = client.id
        ids = _propose(
            session, client_id,
            ceo=("Alexandre Prot", "https://qonto.com", "Qonto"),
            sitz=("Paris", "https://qonto.com", "Qonto"),
        )

    web.post(f"/client/{client_id}/profil/accept", data={"pid": ids["ceo"]},
             follow_redirects=False)

    with factory() as session:
        facts = profiles.stored(session, client_id)
        left = profile_refresh.outstanding(session, client_id)
    assert set(facts) == {"ceo"}, "only the chosen field is taken"
    assert facts["ceo"].filled_by == profiles.BY_HAND
    # The source still travels with it: he decided, the web is where it was read.
    assert facts["ceo"].source_url == "https://qonto.com"
    # The one left behind stays on offer rather than vanishing with the click.
    assert [p.key for p in left] == ["sitz"]


def test_a_field_the_consultant_filled_is_not_proposed_over(factory, web):
    """The machine may fill a blank and may correct itself. It may not overrule
    the person it works for.

    The web's version is on the page — that is the DEC-2 rule, contradiction
    rather than silence — but it is drawn under the "never overwritten" heading
    and carries no accept button, where an ordinary offer carries one.
    """
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "ceo", "Weiß ich besser")
        client_id = client.id
        ids = _propose(
            session, client_id,
            ceo=("Jemand anderes", "https://irgendwo.de", "Irgendwo"),
            sitz=("Paris", "https://qonto.com", "Qonto"),
        )

    body = web.get(f"/client/{client_id}/profil").text

    assert "Paris" in body, "the ordinary offer is drawn"
    assert "Weiß ich besser" in body, "and his own value still stands beside it"
    accept_forms = body.split('/profil/accept"')[1:]
    offered = {form.split("</form>")[0] for form in accept_forms}
    assert not any(f'value="{ids["ceo"]}"' in form for form in offered), (
        "no button on the page offers to write the machine over him"
    )
    assert any(f'value="{ids["sitz"]}"' in form for form in offered)


def test_posting_a_hand_filled_key_to_accept_does_not_overwrite_it(factory, web):
    """The page draws no accept button for a hand-filled field, but the form body
    is not the page: a tab left open while the value was typed in elsewhere posts
    a row nobody chose. The write boundary refuses it rather than trusting the
    render filter, which is one stale POST away from being walked past."""
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "ceo", "Weiß ich besser")
        client_id = client.id
        ids = _propose(
            session, client_id,
            ceo=("Jemand anderes", "https://irgendwo.de", "Irgendwo"),
            sitz=("Paris", "https://qonto.com", "Qonto"),
        )

    web.post(f"/client/{client_id}/profil/accept",
             data={"pid": [ids["ceo"], ids["sitz"]]}, follow_redirects=False)

    with factory() as session:
        facts = profiles.stored(session, client_id)
        left = {p.key for p in profile_refresh.outstanding(session, client_id)}
    assert facts["ceo"].value == "Weiß ich besser", "the human's value stands"
    assert facts["ceo"].filled_by == profiles.BY_HAND, "and it is still his"
    assert facts["sitz"].value == "Paris", "the field nobody typed into is taken"
    # The refused one is still on file: it is a contradiction PRF-02 renders, not
    # something to silently drop because the accept did not honour it.
    assert left == {"ceo"}


def test_only_contradictions_left_still_offers_a_way_to_clear_them(factory, web):
    """A proposal against a hand-filled field gets no accept button, so a client
    whose profile was typed in entirely by hand used to accumulate rows that were
    invisible *and* unclearable — the discard button lived inside the block that
    the filter had just emptied."""
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "ceo", "Weiß ich besser")
        client_id = client.id
        ids = _propose(
            session, client_id, ceo=("Jemand anderes", "https://irgendwo.de", "Irgendwo")
        )

    body = web.get(f"/client/{client_id}/profil").text
    assert "Verwerfen" in body, "the rows are reachable"
    assert f'name="pid" value="{ids["ceo"]}"' in body, "and the button names them"

    web.post(f"/client/{client_id}/profil/discard", data={"pid": ids["ceo"]},
             follow_redirects=False)

    with factory() as session:
        assert profile_refresh.outstanding(session, client_id) == []


def test_a_contradiction_is_visible_beside_an_ordinary_proposal(factory, web):
    """The two piles are independent. Hanging the contradictions off "there are no
    proposals" hid them on every mandate that also had one ordinary offer — which
    is most of them — and an accepted offer then left the held-back row on file,
    invisible, until the next refresh."""
    with factory() as session:
        client = _client(session)
        profiles.save(session, client, "ceo", "Weiß ich besser")
        client_id = client.id
        _propose(
            session, client_id,
            ceo=("Jemand anderes", "https://irgendwo.de", "Irgendwo"),
            sitz=("Paris", "https://qonto.com", "Qonto"),
        )

    body = web.get(f"/client/{client_id}/profil").text

    assert "Paris" in body, "the offer is drawn"
    assert "das Netz sagt etwas anderes" in body, "and so is the one held back"


def test_the_reason_a_check_broke_reaches_the_page(factory, web):
    """The sweep researches at 06:10 and the page is opened at nine, in a process
    that never saw the failure. Without the stored note the page shows a profile
    that was "checked" with no reason and no way to find one."""
    from newspulse.web.routes import profile as profile_routes

    def _boom(prompt):
        raise RuntimeError("die Suche ist nicht erreichbar")

    with factory() as session:
        client = _client(session)
        client_id = client.id
        with pytest.raises(RuntimeError):
            profile_refresh.refresh(
                session, client, now=dt.datetime(2031, 3, 5, 6, 10, tzinfo=dt.UTC),
                generate=_boom,
            )
        assert client.profile_note
    # Nothing in memory: the sweep runs elsewhere and leaves this dict empty.
    profile_routes._errors.pop(client_id, None)

    body = web.get(f"/client/{client_id}/profil").text

    assert "nicht erreichbar" in body, "the sweep's failure is invisible on the page"


def test_discarding_clears_the_proposals_from_the_page(factory, web):
    """The dict this replaced lost them on a restart; the button has to be the
    only thing that does.

    The row itself stays on file, stamped: it is the record that this value was
    already refused, which is what stops the next refresh offering it again.
    """
    with factory() as session:
        client_id = _client(session).id
        ids = _propose(session, client_id, sitz=("Paris", "https://qonto.com", "Qonto"))

    web.post(f"/client/{client_id}/profil/discard", data={"pid": ids["sitz"]},
             follow_redirects=False)

    with factory() as session:
        assert profile_refresh.outstanding(session, client_id) == []
        assert session.get(ProfileProposal, ids["sitz"]).discarded_at is not None


def test_the_competition_tab_puts_the_four_questions_in_one_place(factory, web):
    """Share of voice, what the others are being written about, the outlets that
    never write about us, and the control to change the set — one question asked
    four ways, previously on four pages."""
    import datetime as dt

    from newspulse.models import Analysis, Article, Category

    with factory() as session:
        client = _client(session)
        rival = Client(name="Trade Republic", aliases=[], industry="Neobank",
                       keywords=[], alert_topics=[], is_competitor=True)
        session.add(rival)
        session.flush()
        client.competitors.append(rival)
        article = Article(
            title="Trade Republic senkt Gebühren", url="https://ex.de/tr",
            source="Handelsblatt", published_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
            fetched_at=dt.datetime.now(dt.UTC), summary_text=None, language="de",
            title_hash="tr000001",
        )
        session.add(article)
        session.flush()
        session.add(Analysis(article_id=article.id, client_id=rival.id, summary="s",
                             category=Category.PRODUKT, relevance_score=6,
                             importance_score=6, is_alert=False))
        session.commit()
        client_id = client.id

    body = web.get(f"/client/{client_id}/wettbewerb").text

    assert "Trade Republic" in body                      # the comparison set
    assert "Trade Republic senkt Gebühren" in body       # what they are written about
    assert "Wettbewerber hinzufügen" in body             # the control
    assert f"/client/{client_id}/map" in body            # the gaps, in full


def test_the_day_tab_keeps_the_workspace_around_it(factory, web):
    """Entered from a mandate, the day is that mandate's day and keeps its tabs;
    reached from the portfolio it is everyone's and the strip would be a lie."""
    with factory() as session:
        client = _client(session)
        client_id = client.id

    scoped = web.get(f"/today?client={client_id}").text
    everyones = web.get("/today").text

    assert f'/client/{client_id}/profil' in scoped, "the workspace strip is present"
    assert "/profil" not in everyones


def test_the_client_day_tab_lands_on_the_filtered_day(factory, web):
    with factory() as session:
        client_id = _client(session).id

    resp = web.get(f"/client/{client_id}/heute", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/today?client={client_id}"
