"""Das Entscheidungspapier (RIS-05).

Nothing here reaches a model and nothing reaches the network: the one model call
in :mod:`newspulse.decision` is exercised with an injected ``invoke`` returning
canned JSON, and the tests that must prove a call never happened inject one that
fails the test if it fires.

The disciplines under test, in order:

* **A sentence is led as belegt only if its Kennung resolves.** A citation
  pointing at nothing — or at another mandate's row — moves the sentence under
  *unbestätigt* rather than making a claim out of it.
* **Die Quellenordnung is fixed and printed.** Four ranks in one order, and a
  sentence carries the rank of the strongest line under it.
* **A contradiction needs both sides.** One side, or the same row twice, and it
  is not reported at all.
* **Nothing is numbered that the stored lines do not carry**, and no figure this
  tool may not produce reaches the paper.
* **A gap is a named line with a link**, and a missing decider or deadline
  stands at the *top* of the paper.
* **The paper is a record.** A new one stands beside the old, and a decided one
  refuses every edit.

The golden-file test at the bottom is the acceptance criterion in file form: the
rendered paper of a seeded issue is byte-compared against
``fixtures/decision/entscheidungspapier.html``, so every wording change shows up
as a reviewable diff. Regenerate deliberately with
``NEWSPULSE_UPDATE_GOLDEN=1 pytest tests/test_decision.py``.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, decision, i18n
from newspulse.matching import title_hash
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Asset,
    AssetKind,
    Base,
    Category,
    Client,
    ClientFact,
    Crisis,
    DecisionPacket,
    EvidenceKind,
    GapKind,
    Issue,
    IssueSignal,
    IssueStatus,
    MarketSignal,
    EscalationPotential,
    Outreach,
    OutreachReply,
    PacketSection,
    ResponseOption,
    ResponseSpeed,
    SignalKind,
    SourceRank,
)

_NOW = dt.datetime(2026, 9, 4, 9, 0, tzinfo=dt.UTC)

GOLDEN = Path(__file__).parent / "fixtures" / "decision" / "entscheidungspapier.html"


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


#: One counter for every article this file stores. ``articles.url`` is UNIQUE,
#: and a URL derived from the title alone made the second row of any test that
#: seeds two default issues an ``IntegrityError`` rather than a test failure.
_urls = itertools.count(1)


def _article(
    session, title: str = "Vorwurf im Werk", *, source: str = "Rheinische Post"
) -> Article:
    article = Article(
        title=title,
        url=f"https://example.de/{next(_urls)}",
        source=source,
        published_at=_NOW - dt.timedelta(days=2),
        fetched_at=_NOW - dt.timedelta(days=2),
        summary_text="Der Betriebsrat wirft dem Werk unklare Klauseln vor.",
        language="de",
        title_hash=title_hash(title, source),
    )
    session.add(article)
    session.commit()
    return article


def _issue(session, client: Client, *, articles: list[Article] | None = None) -> Issue:
    """One open issue with its founding signals, the way the register opens it."""
    rows = articles if articles is not None else [_article(session)]
    issue = Issue(
        client_id=client.id,
        title="Vorwurf Vertragsklauseln",
        description="Seit drei Wochen im Gespräch.",
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


def _fact(session, client: Client, key: str, value: str) -> ClientFact:
    row = ClientFact(
        client_id=client.id,
        key=key,
        value=value,
        filled_by="mensch",
        updated_at=_NOW - dt.timedelta(days=10),
    )
    session.add(row)
    session.commit()
    return row


def _released_text(session, client: Client, body: str) -> Asset:
    angle = Angle(
        client_id=client.id,
        generated_at=_NOW - dt.timedelta(days=9),
        subject="Klauseln",
        message="Text",
        context="",
        thesis="",
        overclaim="",
        statements=[],
    )
    session.add(angle)
    session.flush()
    row = Asset(
        client_id=client.id,
        angle_id=angle.id,
        kind=AssetKind.STATEMENT.value,
        generated_at=_NOW - dt.timedelta(days=9),
        title=body,
        body=body,
        released_at=_NOW - dt.timedelta(days=8),
        released_by="lucas",
    )
    session.add(row)
    session.commit()
    return row


def _market_signal(session, client: Client) -> MarketSignal:
    row = MarketSignal(
        client_id=client.id,
        kind=SignalKind.REGULIERUNG,
        title="Konsultation zu Vertragsklauseln",
        publisher="Bundesnetzagentur",
        url="https://behoerde.example/konsultation",
        found_at=_NOW - dt.timedelta(days=6),
        effective_at=_NOW + dt.timedelta(days=30),
    )
    session.add(row)
    session.commit()
    return row


def _reply(session, client: Client, body: str) -> OutreachReply:
    angle = Angle(
        client_id=client.id,
        generated_at=_NOW - dt.timedelta(days=4),
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
        client_id=client.id,
        generated_at=_NOW - dt.timedelta(days=4),
        journalist="Mara Wolf",
        outlet="WDR",
        subject="Anfrage",
        message="Sehr geehrte Frau Wolf,",
    )
    session.add(letter)
    session.flush()
    row = OutreachReply(
        outreach_id=letter.id,
        gmail_message_id=f"m-{abs(hash(body))}",
        from_name="Mara Wolf",
        from_email="mara@wdr.de",
        received_at=_NOW - dt.timedelta(hours=5),
        body=body,
    )
    session.add(row)
    session.commit()
    return row


def _answer(**over) -> str:
    payload = {
        "was_passiert_ist": "Der Betriebsrat wirft dem Werk unklare Klauseln vor.",
        "belegt": [],
        "unbestaetigt": [],
        "offen": [],
        "widersprueche": [],
        "zu_entscheiden": "Ob wir uns öffentlich äußern.",
    }
    payload.update(over)
    return json.dumps(payload)


def _reply_with(**over):
    """An injected ``invoke`` returning one canned answer."""
    return lambda *a, **k: _answer(**over)


def _never_called(*args, **kwargs):
    raise AssertionError("the model was asked, and it must not have been")


def _build(session, client, issue, **over) -> DecisionPacket:
    row = decision.build(
        session, client, issue=issue, by="lucas", invoke=_reply_with(**over), now=_NOW
    )
    assert row is not None
    return row


def _texts(packet: DecisionPacket, section: PacketSection) -> list[str]:
    return [row.text for row in decision.sections(packet)[section]]


# --- The paper carries its parts ---------------------------------------------------


def test_the_paper_carries_what_happened_and_what_is_to_be_decided(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    assert packet.situation == "Der Betriebsrat wirft dem Werk unklare Klauseln vor."
    assert packet.question == "Ob wir uns öffentlich äußern."
    assert packet.issue_id == issue.id and packet.crisis_id is None
    assert packet.created_by == "lucas"


def test_the_three_parts_are_kept_apart(session, mandate):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[
            {
                "satz": "Der Vorwurf steht in der Presse.",
                "beleg": [f"beitrag:{article.id}"],
            }
        ],
        unbestaetigt=[{"satz": "Eine Kündigung sei im Gespräch."}],
        offen=[{"satz": "Wer hat die Klauseln formuliert?"}],
    )
    assert _texts(packet, PacketSection.BELEGT) == ["Der Vorwurf steht in der Presse."]
    assert _texts(packet, PacketSection.UNBESTAETIGT) == [
        "Eine Kündigung sei im Gespräch."
    ]
    assert _texts(packet, PacketSection.OFFEN) == ["Wer hat die Klauseln formuliert?"]


def test_an_answer_without_a_situation_stores_no_paper(session, mandate):
    """A paper that cannot open with what happened is not a paper: storing one
    would head a page with nothing in front of somebody about to decide."""
    issue = _issue(session, mandate)
    assert (
        decision.build(
            session,
            mandate,
            issue=issue,
            by="lucas",
            invoke=_reply_with(was_passiert_ist="   "),
            now=_NOW,
        )
        is None
    )
    assert session.scalar(select(func.count()).select_from(DecisionPacket)) == 0


def test_a_paper_hangs_on_exactly_one_occasion(session, mandate):
    """Two anchors or none is a caller error, refused before a call is spent."""
    issue = _issue(session, mandate)
    article = _article(session, "Werk stoppt Produktion")
    standing = _crisis(session, mandate, article)
    with pytest.raises(ValueError):
        decision.build(
            session, mandate, issue=issue, crisis=standing, by="x", invoke=_never_called
        )
    with pytest.raises(ValueError):
        decision.build(session, mandate, by="x", invoke=_never_called)
    with pytest.raises(ValueError):
        decision.packets_for(session)


# --- Every belegt sentence carries the identifier of its row -----------------------


def test_a_supported_sentence_carries_the_kennung_of_the_row_it_came_from(
    session, mandate
):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[
            {"satz": "Der Vorwurf steht in der Presse.", "beleg": [f"beitrag:{article.id}"]}
        ],
    )
    row = decision.sections(packet)[PacketSection.BELEGT][0]
    assert [(e.kind, e.ref_id) for e in row.evidence] == [
        (EvidenceKind.BEITRAG, article.id)
    ]
    assert row.evidence[0].label == article.title
    assert row.evidence[0].source == article.source


def test_a_kennung_that_resolves_to_nothing_moves_the_sentence_to_unbestaetigt(
    session, mandate
):
    """The acceptance, with an injected generation: a sentence whose evidence
    cannot be followed is not a claim. It is said out loud under unbestätigt
    rather than dropped, because "we have heard this" is worth reading."""
    issue = _issue(session, mandate)
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[{"satz": "Vierzig Stellen fielen weg.", "beleg": ["beitrag:99999"]}],
    )
    assert _texts(packet, PacketSection.BELEGT) == []
    assert _texts(packet, PacketSection.UNBESTAETIGT) == ["Vierzig Stellen fielen weg."]
    assert decision.sections(packet)[PacketSection.UNBESTAETIGT][0].source_rank is None


def test_a_sentence_citing_no_kennung_at_all_stands_under_unbestaetigt(
    session, mandate
):
    issue = _issue(session, mandate)
    packet = _build(
        session, mandate, issue, belegt=[{"satz": "Man munkelt etwas.", "beleg": []}]
    )
    assert _texts(packet, PacketSection.UNBESTAETIGT) == ["Man munkelt etwas."]


def test_a_kennung_naming_another_mandates_row_does_not_resolve(session, mandate):
    """Resolution is against what the prompt offered, so client scoping is a
    property of the resolver rather than a rule every caller has to keep."""
    other = Client(name="Andere AG", aliases=[])
    session.add(other)
    session.commit()
    theirs = _article(session, "Ganz andere Sache", source="FAZ")
    _issue(session, other, articles=[theirs])
    issue = _issue(session, mandate)
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[{"satz": "Etwas steht anderswo.", "beleg": [f"beitrag:{theirs.id}"]}],
    )
    assert _texts(packet, PacketSection.BELEGT) == []
    assert _texts(packet, PacketSection.UNBESTAETIGT) == ["Etwas steht anderswo."]


def test_a_kennung_dressed_up_in_prose_resolves_to_nothing(session, mandate):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[
            {"satz": "Der Vorwurf steht da.", "beleg": [f"Beitrag {article.id} (RP)"]}
        ],
    )
    assert _texts(packet, PacketSection.BELEGT) == []


def test_the_same_kennung_named_twice_is_one_piece_of_evidence(session, mandate):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[
            {
                "satz": "Der Vorwurf steht da.",
                "beleg": [f"beitrag:{article.id}", f"beitrag:{article.id}"],
            }
        ],
    )
    assert len(decision.sections(packet)[PacketSection.BELEGT][0].evidence) == 1


# --- Die Quellenordnung ------------------------------------------------------------


def test_the_source_order_is_fixed_and_reads_from_the_strongest_down():
    assert list(SourceRank) == [
        SourceRank.INTERN,
        SourceRank.BEHOERDE,
        SourceRank.MEDIEN,
        SourceRank.UEBRIGES,
    ]


def test_every_evidence_kind_has_a_place_in_the_order():
    """A kind added without a rank would be filed under a guess, or crash the
    page a reader is holding in a crisis."""
    for kind in EvidenceKind:
        assert kind in decision.RANK_BY_KIND, kind


def test_a_sentence_carries_the_rank_of_its_strongest_line(session, mandate):
    """A sentence resting on a confirmed internal line *and* on coverage rests
    on the internal one; showing it under the weaker rank would understate what
    the reader is standing on."""
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    fact = _fact(session, mandate, "sprecher", "Anna Berger, Unternehmenssprecherin")
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[
            {
                "satz": "Die Sprecherin steht bereit.",
                "beleg": [f"beitrag:{article.id}", f"profil:{fact.id}"],
            }
        ],
    )
    assert (
        decision.sections(packet)[PacketSection.BELEGT][0].source_rank
        is SourceRank.INTERN
    )


def test_coverage_alone_ranks_as_a_verified_media_report(session, mandate):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[{"satz": "Der Vorwurf steht da.", "beleg": [f"beitrag:{article.id}"]}],
    )
    assert (
        decision.sections(packet)[PacketSection.BELEGT][0].source_rank
        is SourceRank.MEDIEN
    )


def test_a_mail_in_the_mailbox_ranks_last(session, mandate):
    issue = _issue(session, mandate)
    reply = _reply(session, mandate, "Stimmt es, dass gekündigt wurde?")
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[{"satz": "Eine Redaktion fragt nach.", "beleg": [f"mail:{reply.id}"]}],
    )
    assert (
        decision.sections(packet)[PacketSection.BELEGT][0].source_rank
        is SourceRank.UEBRIGES
    )


# --- A contradiction needs both sides ----------------------------------------------


def test_a_contradiction_with_both_sides_named_is_reported(session, mandate):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    text = _released_text(session, mandate, "Alle Klauseln sind abgestimmt.")
    packet = _build(
        session,
        mandate,
        issue,
        widersprueche=[
            {
                "worin": "Unser freigegebener Text sagt das Gegenteil der Meldung.",
                "seite_a": f"text:{text.id}",
                "seite_b": f"beitrag:{article.id}",
            }
        ],
    )
    assert len(packet.contradictions) == 1
    row = packet.contradictions[0]
    assert (row.left_kind, row.left_ref_id) == (EvidenceKind.TEXT, text.id)
    assert (row.right_kind, row.right_ref_id) == (EvidenceKind.BEITRAG, article.id)
    assert row.left_label and row.right_label


def test_a_contradiction_with_only_one_side_is_not_reported(session, mandate):
    """A reported contradiction whose second side nobody can name is worse than
    none at all, because in a crisis it is believed."""
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    packet = _build(
        session,
        mandate,
        issue,
        widersprueche=[
            {
                "worin": "Das passt nicht zusammen.",
                "seite_a": f"beitrag:{article.id}",
                "seite_b": "",
            }
        ],
    )
    assert packet.contradictions == []


def test_a_contradiction_whose_second_side_resolves_to_nothing_is_not_reported(
    session, mandate
):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    packet = _build(
        session,
        mandate,
        issue,
        widersprueche=[
            {
                "worin": "Das passt nicht zusammen.",
                "seite_a": f"beitrag:{article.id}",
                "seite_b": "profil:99999",
            }
        ],
    )
    assert packet.contradictions == []


def test_a_contradiction_naming_the_same_row_twice_is_not_reported(session, mandate):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    packet = _build(
        session,
        mandate,
        issue,
        widersprueche=[
            {
                "worin": "Der Beitrag widerspricht sich.",
                "seite_a": f"beitrag:{article.id}",
                "seite_b": f"beitrag:{article.id}",
            }
        ],
    )
    assert packet.contradictions == []


def test_a_contradiction_without_a_sentence_is_not_reported(session, mandate):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    text = _released_text(session, mandate, "Alle Klauseln sind abgestimmt.")
    packet = _build(
        session,
        mandate,
        issue,
        widersprueche=[
            {"worin": "", "seite_a": f"text:{text.id}", "seite_b": f"beitrag:{article.id}"}
        ],
    )
    assert packet.contradictions == []


# --- Nothing is numbered that the lines do not carry -------------------------------


def test_a_figure_that_stands_in_no_named_line_drops_the_sentence(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(
        session,
        mandate,
        issue,
        unbestaetigt=[{"satz": "Es seien 412 Beschäftigte betroffen."}],
    )
    assert _texts(packet, PacketSection.UNBESTAETIGT) == []


def test_a_figure_the_material_carries_survives(session, mandate):
    article = _article(session, "412 Klauseln geprüft")
    issue = _issue(session, mandate, articles=[article])
    packet = _build(
        session, mandate, issue, offen=[{"satz": "Wurden alle 412 Klauseln geprüft?"}]
    )
    assert _texts(packet, PacketSection.OFFEN) == ["Wurden alle 412 Klauseln geprüft?"]


def test_a_figure_this_tool_may_not_produce_drops_the_sentence(session, mandate):
    """Reach, impressions and advertising value are not derivable from an
    archive of headlines, and a decision paper is exactly where such a number
    would be believed. The same guard the report keeps."""
    issue = _issue(session, mandate)
    packet = _build(
        session,
        mandate,
        issue,
        unbestaetigt=[{"satz": "Die Reichweite der Meldung ist erheblich."}],
    )
    assert _texts(packet, PacketSection.UNBESTAETIGT) == []


def test_an_invented_figure_in_the_opening_stores_no_paper(session, mandate):
    issue = _issue(session, mandate)
    assert (
        decision.build(
            session,
            mandate,
            issue=issue,
            by="lucas",
            invoke=_reply_with(was_passiert_ist="Seit 77 Tagen läuft die Sache."),
            now=_NOW,
        )
        is None
    )


def test_an_invented_figure_in_the_question_leaves_it_empty(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(
        session, mandate, issue, zu_entscheiden="Ob wir bis zum 31. antworten."
    )
    assert packet.question == ""


def test_a_forbidden_figure_in_the_opening_stores_no_paper(session, mandate):
    """The same two rules everywhere on the paper. The opening is the sentence a
    reader takes into the room, so a figure this tool may not produce is refused
    there at least as firmly as in a bullet under it."""
    issue = _issue(session, mandate)
    assert (
        decision.build(
            session,
            mandate,
            issue=issue,
            by="lucas",
            invoke=_reply_with(
                was_passiert_ist="Die Reichweite der Meldung ist erheblich."
            ),
            now=_NOW,
        )
        is None
    )


def test_a_forbidden_figure_in_the_question_leaves_it_empty(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(
        session,
        mandate,
        issue,
        zu_entscheiden="Ob wir den Werbewert der Berichterstattung ausweisen.",
    )
    assert packet.question == ""


def test_a_forbidden_figure_in_a_contradiction_drops_it(session, mandate):
    first = _article(session, "Werk bestätigt die Klauseln")
    second = _article(session, "Werk bestreitet die Klauseln")
    issue = _issue(session, mandate, articles=[first, second])
    packet = _build(
        session,
        mandate,
        issue,
        widersprueche=[
            {
                "worin": "Die Impressionen der beiden Meldungen gehen auseinander.",
                "seite_a": f"beitrag:{first.id}",
                "seite_b": f"beitrag:{second.id}",
            }
        ],
    )
    assert packet.contradictions == []


# --- The named gaps ----------------------------------------------------------------


def test_a_profile_without_a_spokesperson_or_crisis_contact_names_both_gaps(
    session, mandate
):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    kinds = {gap.kind for gap in packet.stored_gaps}
    assert GapKind.SPRECHER in kinds
    assert GapKind.KRISENKONTAKT in kinds


def test_a_filled_profile_field_closes_its_gap(session, mandate):
    _fact(session, mandate, "sprecher", "Anna Berger")
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    kinds = {gap.kind for gap in packet.stored_gaps}
    assert GapKind.SPRECHER not in kinds
    assert GapKind.KRISENKONTAKT in kinds


def test_a_paper_without_a_confirmed_internal_figure_says_so(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    assert GapKind.BETROFFENENZAHL in {gap.kind for gap in packet.stored_gaps}


def test_a_confirmed_internal_figure_closes_that_gap(session, mandate):
    fact = _fact(session, mandate, "mitarbeiter", "412 Beschäftigte, Stand 2026")
    issue = _issue(session, mandate)
    packet = _build(
        session,
        mandate,
        issue,
        belegt=[
            {"satz": "Das Werk hat 412 Beschäftigte.", "beleg": [f"profil:{fact.id}"]}
        ],
    )
    assert GapKind.BETROFFENENZAHL not in {gap.kind for gap in packet.stored_gaps}


def test_every_gap_kind_has_a_sentence(session):
    for kind in GapKind:
        assert decision.GAP_LABELS[kind]


def test_a_missing_decider_and_deadline_stand_at_the_top(session, mandate):
    """The acceptance: "fehlen Entscheider oder Frist, steht das oben auf dem
    Papier und nicht als Leerstelle"."""
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    leading = [gap.kind for gap in decision.gaps(session, packet) if gap.leading]
    assert leading == [GapKind.ENTSCHEIDER, GapKind.FRIST]


def test_every_gap_carries_a_link_to_where_it_is_closed(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    assert all(gap.link for gap in decision.gaps(session, packet))


def test_naming_the_decider_takes_the_leading_gap_off(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    assert decision.set_decider(
        session,
        packet,
        decision_maker="Anna Berger",
        deadline=_NOW + dt.timedelta(days=1),
    )
    assert [gap.kind for gap in decision.gaps(session, packet) if gap.leading] == []


def test_a_material_gap_stays_on_the_paper_after_the_profile_is_filled(
    session, mandate
):
    """The paper is the record of what was known then: a profile filled on
    Thursday must not quietly remove Monday's gap from Monday's paper."""
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    _fact(session, mandate, "sprecher", "Anna Berger")
    assert GapKind.SPRECHER in {
        gap.kind for gap in decision.gaps(session, packet) if not gap.leading
    }


# --- A new paper stands beside the old ---------------------------------------------


def test_a_second_paper_does_not_replace_the_first(session, mandate):
    issue = _issue(session, mandate)
    first = _build(session, mandate, issue)
    second = decision.build(
        session,
        mandate,
        issue=issue,
        by="lucas",
        invoke=_reply_with(was_passiert_ist="Die Lage hat sich verschoben."),
        now=_NOW + dt.timedelta(days=1),
    )
    assert second is not None and second.id != first.id
    stored = decision.packets_for(session, issue=issue)
    assert [row.id for row in stored] == [second.id, first.id]
    assert stored[1].situation == first.situation


# --- The decision, and who took it -------------------------------------------------


def test_the_decision_and_the_person_are_recorded_and_readable(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    assert decision.record_decision(
        session, packet, decision="Wir schweigen und beobachten.", by="lucas", now=_NOW
    )
    reread = decision.packet(session, mandate, packet.id)
    assert reread.decision == "Wir schweigen und beobachten."
    assert reread.decided_by == "lucas"
    assert reread.decided_at == _NOW
    assert reread.is_decided


def test_an_empty_decision_is_not_recorded(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    assert not decision.record_decision(session, packet, decision="  ", by="lucas")
    assert not packet.is_decided


def test_a_decided_paper_refuses_a_second_decision(session, mandate):
    """A second decision written over the first would erase the answer rather
    than add to it. A changed mind is a new paper."""
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    decision.record_decision(session, packet, decision="Wir schweigen.", by="lucas")
    assert not decision.record_decision(
        session, packet, decision="Doch nicht.", by="mara"
    )
    assert packet.decision == "Wir schweigen."
    assert packet.decided_by == "lucas"


def test_a_decided_paper_refuses_an_edit_of_the_decider(session, mandate):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    decision.record_decision(session, packet, decision="Wir schweigen.", by="lucas")
    assert not decision.set_decider(session, packet, decision_maker="Jemand anders")
    assert packet.decision_maker == ""


def test_a_paper_of_another_mandate_is_not_returned(session, mandate):
    other = Client(name="Andere AG", aliases=[])
    session.add(other)
    session.commit()
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    assert decision.packet(session, other, packet.id) is None


# --- The material ------------------------------------------------------------------


def test_the_material_offers_every_kind_of_stored_line(session, mandate):
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    session.add(
        Analysis(
            article_id=article.id,
            client_id=mandate.id,
            summary="Der Vorwurf betrifft die Vertragsgestaltung.",
            category=Category.KRISE,
            relevance_score=7,
            importance_score=8,
            is_alert=True,
        )
    )
    session.commit()
    signal = _market_signal(session, mandate)
    issue.signals.append(
        IssueSignal(
            signal_id=signal.id,
            reason="Dieselbe Sache, datiert.",
            attached_by="mensch",
            attached_at=_NOW,
            happened_at=_NOW - dt.timedelta(days=6),
        )
    )
    session.commit()
    _fact(session, mandate, "sprecher", "Anna Berger")
    _released_text(session, mandate, "Alle Klauseln sind abgestimmt.")
    _reply(session, mandate, "Stimmt es, dass gekündigt wurde?")

    kinds = {line.kind for line in decision.material_lines(session, mandate, issue=issue)}
    assert kinds == set(EvidenceKind)


def test_the_material_never_offers_a_kennung_as_a_supported_figure(session, mandate):
    """An id is a number this tool printed, not a fact anybody stated: counting
    it as material would let a model quote it back as one."""
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    line = decision.material_lines(session, mandate, issue=issue)[0]
    assert line.token not in line.stated


# --- A paper to a crisis -----------------------------------------------------------


def _crisis(session, client: Client, article: Article) -> Crisis:
    row = Crisis(
        client_id=client.id,
        article_id=article.id,
        declared_by="lucas",
        declared_at=_NOW - dt.timedelta(days=1),
        level=3,
    )
    session.add(row)
    session.commit()
    return row


def test_a_paper_to_a_crisis_takes_the_escalated_issues_coverage(session, mandate):
    """The crisis's chronology begins where the issue's did, so the paper's
    material does too."""
    article = _article(session)
    issue = _issue(session, mandate, articles=[article])
    standing = _crisis(session, mandate, _article(session, "Werk stoppt Produktion"))
    issue.crisis_id = standing.id
    session.commit()

    packet = decision.build(
        session,
        mandate,
        crisis=standing,
        by="lucas",
        invoke=_reply_with(
            belegt=[
                {
                    "satz": "Der Vorwurf steht in der Presse.",
                    "beleg": [f"beitrag:{article.id}"],
                }
            ]
        ),
        now=_NOW,
    )
    assert packet is not None
    assert packet.crisis_id == standing.id and packet.issue_id is None
    assert _texts(packet, PacketSection.BELEGT) == ["Der Vorwurf steht in der Presse."]
    assert decision.packets_for(session, crisis=standing) == [packet]
    assert decision.anchor_issue(session, packet).id == issue.id


def test_a_crisis_declared_cold_still_gets_its_own_coverage(session, mandate):
    article = _article(session, "Werk stoppt Produktion")
    standing = _crisis(session, mandate, article)
    packet = decision.build(
        session,
        mandate,
        crisis=standing,
        by="lucas",
        invoke=_reply_with(
            belegt=[{"satz": "Die Produktion steht.", "beleg": [f"beitrag:{article.id}"]}]
        ),
        now=_NOW,
    )
    assert packet is not None
    assert _texts(packet, PacketSection.BELEGT) == ["Die Produktion steht."]


# --- Every visible string is in the table ------------------------------------------


_LITERAL_T = re.compile(r"""\bt\(\s*["'](.+?)["']\s*\)""", re.DOTALL)


def test_every_visible_string_of_the_paper_is_translated():
    """Checked against the templates themselves and against the module
    constants, rather than against a list somebody has to remember to extend."""
    from newspulse.web import app as web_app
    from newspulse.web.routes import issues_view

    known = set(i18n.known_keys())
    literals: set[str] = set()
    for name in ("decision_packet.html", "partials/decision_packets.html"):
        literals |= set(
            _LITERAL_T.findall((web_app._TEMPLATES_DIR / name).read_text("utf-8"))
        )
    assert literals, "the scan found nothing — the pattern rotted"
    words = [
        *decision.GAP_LABELS.values(),
        *decision.SECTION_LABELS.values(),
        *decision.EVIDENCE_LABELS.values(),
        *(member.value for member in SourceRank),
        *issues_view.PACKET_NOTES,
    ]
    missing = sorted(s for s in (literals | set(words)) if s not in known)
    assert not missing, f"untranslated strings: {missing}"


# --- The page, and the file that leaves the tool -----------------------------------


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


def _seeded_paper(session, client: Client) -> DecisionPacket:
    """One issue with a line of every kind, and the paper written from it.

    Every value is pinned — no clock, no hash-derived URL — because the golden
    file below is compared byte for byte and a fixture that varied between runs
    would be testing the host rather than the wording.
    """
    article = Article(
        title="Betriebsrat wirft Solaris unklare Klauseln vor",
        url="https://rp-online.example/solaris-klauseln",
        source="Rheinische Post",
        published_at=_NOW - dt.timedelta(days=2),
        fetched_at=_NOW - dt.timedelta(days=2),
        summary_text="Der Betriebsrat wirft dem Werk unklare Klauseln vor.",
        language="de",
        title_hash="golden-1",
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            summary="Der Vorwurf betrifft die Vertragsgestaltung im Werk.",
            category=Category.KRISE,
            relevance_score=7,
            importance_score=8,
            is_alert=True,
        )
    )
    signal = MarketSignal(
        client_id=client.id,
        kind=SignalKind.REGULIERUNG,
        title="Konsultation zu Vertragsklauseln",
        publisher="Bundesnetzagentur",
        url="https://behoerde.example/konsultation",
        found_at=_NOW - dt.timedelta(days=6),
        effective_at=_NOW + dt.timedelta(days=30),
    )
    session.add(signal)
    session.flush()
    issue = Issue(
        client_id=client.id,
        title="Vorwurf Vertragsklauseln",
        description="Seit drei Wochen im Gespräch.",
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
    issue.signals.append(
        IssueSignal(
            signal_id=signal.id,
            reason="Dieselbe Sache, datiert.",
            attached_by="mensch",
            attached_at=_NOW - dt.timedelta(days=1),
            happened_at=_NOW - dt.timedelta(days=6),
        )
    )
    session.add(issue)
    _fact(session, client, "sprecher", "Anna Berger, Unternehmenssprecherin")
    text = _released_text(
        session, client, "Alle Klauseln sind mit dem Betriebsrat abgestimmt."
    )
    session.add(
        ResponseOption(
            issue_id=issue.id,
            label="Nicht reagieren",
            benefit="Der Sache wird keine Öffentlichkeit verschafft.",
            risk="Ein Vorwurf bleibt unwidersprochen stehen.",
            escalation=EscalationPotential.NIEDRIG,
            no_response=True,
            recommended=True,
            speed=ResponseSpeed.VORBEREITEN,
            position=1,
            created_at=_NOW - dt.timedelta(days=1),
        )
    )
    session.commit()

    packet = decision.build(
        session,
        client,
        issue=issue,
        by="lucas",
        invoke=_reply_with(
            belegt=[
                {
                    "satz": "Der Vorwurf steht in der Berichterstattung.",
                    "beleg": [f"beitrag:{article.id}"],
                },
                {
                    "satz": "Als Sprecherin ist Anna Berger hinterlegt.",
                    "beleg": [f"profil:{session.scalar(select(ClientFact.id))}"],
                },
            ],
            unbestaetigt=[{"satz": "Eine Kündigungswelle sei im Gespräch."}],
            offen=[{"satz": "Wer hat die Klauseln formuliert?"}],
            widersprueche=[
                {
                    "worin": "Unser freigegebener Text sagt das Gegenteil der Meldung.",
                    "seite_a": f"text:{text.id}",
                    "seite_b": f"beitrag:{article.id}",
                }
            ],
        ),
        now=_NOW,
    )
    assert packet is not None
    decision.set_decider(session, packet, decision_maker="Anna Berger", deadline=None)
    decision.record_decision(
        session,
        packet,
        decision="Wir reagieren nicht und beobachten weiter.",
        by="lucas",
        now=_NOW,
    )
    return packet


def test_the_page_renders_the_paper_with_its_parts(web, session, mandate):
    packet = _seeded_paper(session, mandate)
    page = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}")
    assert page.status_code == 200
    for part in ("Was passiert ist", "Belegt", "Unbestätigt", "Offen", "Widersprüche"):
        assert part in page.text, part
    assert "Der Vorwurf steht in der Berichterstattung." in page.text
    assert "Die Quellenordnung" in page.text


def test_the_page_prints_the_kennung_of_every_supported_sentence(web, session, mandate):
    packet = _seeded_paper(session, mandate)
    article_id = session.scalar(select(Article.id))
    page = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}")
    assert f"beitrag:{article_id}" in page.text


def test_the_page_prints_the_source_order_in_its_fixed_order(web, session, mandate):
    packet = _seeded_paper(session, mandate)
    body = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}").text
    # Measured inside the legend block: the ranks also stand above it, on the
    # supported sentences themselves, and the claim here is about the printed
    # *order*, not about where a rank first appears on the page.
    legend = body.split("Die Quellenordnung", 1)[1]
    places = [legend.index(rank.value) for rank in SourceRank]
    assert places == sorted(places)


def test_a_missing_decider_and_deadline_head_the_page(web, session, mandate):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    body = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}").text
    assert "Dieses Papier trägt nicht, was eine Entscheidung braucht:" in body
    assert decision.GAP_LABELS[GapKind.ENTSCHEIDER] in body
    assert decision.GAP_LABELS[GapKind.FRIST] in body


def test_the_download_is_a_file_and_carries_no_link_back_into_the_app(
    web, session, mandate
):
    """The acceptance, mechanically: the file leaves the tool, and a path into
    an application the reader may not have is worse than no link at all."""
    packet = _seeded_paper(session, mandate)
    resp = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}/dokument.html")
    assert resp.status_code == 200
    assert "attachment; filename=" in resp.headers["content-disposition"]
    assert 'href="/' not in resp.text
    assert 'action="/' not in resp.text
    assert "/client/" not in resp.text


def test_the_download_says_what_the_screen_says(web, session, mandate):
    """One template for both, so the file cannot carry a sentence the page did
    not — or leave one out."""
    packet = _seeded_paper(session, mandate)
    screen = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}").text
    export = web.get(
        f"/client/{mandate.id}/entscheidungspapier/{packet.id}/dokument.html"
    ).text
    for sentence in (
        packet.situation,
        packet.decision,
        "Der Vorwurf steht in der Berichterstattung.",
        "Eine Kündigungswelle sei im Gespräch.",
        "Unser freigegebener Text sagt das Gegenteil der Meldung.",
    ):
        assert sentence in screen and sentence in export, sentence


def test_the_page_shows_the_decision_and_the_person(web, session, mandate):
    packet = _seeded_paper(session, mandate)
    body = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}").text
    assert "Wir reagieren nicht und beobachten weiter." in body
    assert "lucas" in body


def test_another_mandates_paper_is_a_404(web, session, mandate):
    other = Client(name="Andere AG", aliases=[])
    session.add(other)
    session.commit()
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    assert (
        web.get(f"/client/{other.id}/entscheidungspapier/{packet.id}").status_code == 404
    )
    assert web.get(f"/client/{mandate.id}/entscheidungspapier/9999").status_code == 404


def test_the_register_offers_the_button_and_lists_every_paper(web, session, mandate):
    issue = _issue(session, mandate)
    first = _build(session, mandate, issue)
    second = decision.build(
        session,
        mandate,
        issue=issue,
        by="lucas",
        invoke=_reply_with(was_passiert_ist="Die Lage hat sich verschoben."),
        now=_NOW + dt.timedelta(days=1),
    )
    body = web.get(f"/client/{mandate.id}/issues").text
    assert f"/issues/{issue.id}/entscheidungspapier" in body
    assert f"/client/{mandate.id}/entscheidungspapier/{first.id}" in body
    assert f"/client/{mandate.id}/entscheidungspapier/{second.id}" in body


def test_a_packet_answer_is_read_under_the_row_that_was_pressed(
    web, session, mandate
):
    """The note channel is shared with the Stakeholder-Karte's buttons — one
    channel is what stops two clicks paying for two calls — but the sentence is
    read where the button was pressed. Once, under that row, and not inside the
    Karte, which answers a click nobody made there."""
    from newspulse.web.routes import issues_view, stakeholder_ui

    pressed = _issue(session, mandate)
    _issue(session, mandate)  # a second row, whose block must stay silent
    issues_view._running_packet_click[mandate.id] = f"dpk-issue-{pressed.id}"
    stakeholder_ui.note(mandate.id, issues_view.NO_PACKET)

    body = web.get(f"/client/{mandate.id}/issues").text
    assert body.count(issues_view.NO_PACKET) == 1
    assert (
        body.index(f'id="dpk-issue-{pressed.id}"')
        < body.index(issues_view.NO_PACKET)
        < body.index('<section class="smap"')
    )


def test_the_crisis_page_offers_the_button_and_lists_its_papers(
    web, session, mandate
):
    """The second occasion the acceptance names. A crisis declared straight off
    an article has no register row, so without this button the most urgent
    matter the tool knows has no path to a decision paper at all."""
    article = _article(session, "Werk stoppt Produktion")
    standing = _crisis(session, mandate, article)
    packet = decision.build(
        session,
        mandate,
        crisis=standing,
        by="lucas",
        invoke=_reply_with(),
        now=_NOW,
    )
    assert packet is not None
    body = web.get(f"/client/{mandate.id}/krise").text
    assert f"/crisis/{standing.id}/entscheidungspapier" in body
    assert f"/client/{mandate.id}/entscheidungspapier/{packet.id}" in body


def test_the_crisis_button_writes_a_paper_that_hangs_on_the_crisis(
    web, session, mandate, monkeypatch
):
    """The anchor is the crisis itself. A paper written here must not quietly
    hang on an issue: the crisis is what the matter is called from the
    declaration onwards, and a cold-declared one has no issue at all."""
    from newspulse.web.routes import issues_view

    article = _article(session, "Werk stoppt Produktion")
    standing = _crisis(session, mandate, article)

    # The route spends its call on a worker thread with a session of its own.
    # Run the job here against this test's session instead, with the generation
    # injected: what is under test is which anchor the route hands `build`.
    written = decision.build
    monkeypatch.setattr(
        issues_view.stakeholder_ui,
        "spend",
        lambda job, *, client_id, name, failed: job(session),
    )
    monkeypatch.setattr(
        issues_view.decision,
        "build",
        lambda worker, client, *, issue=None, crisis=None, by="": written(
            worker, client, issue=issue, crisis=crisis, by=by,
            invoke=_reply_with(), now=_NOW,
        ),
    )
    resp = web.post(
        f"/crisis/{standing.id}/entscheidungspapier",
        data={"redirect_to": f"/client/{mandate.id}/krise"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    stored = decision.packets_for(session, crisis=standing)
    assert len(stored) == 1
    assert stored[0].crisis_id == standing.id and stored[0].issue_id is None


def test_a_crisis_that_is_gone_redirects_rather_than_raising(web, session, mandate):
    resp = web.post(
        "/crisis/9999/entscheidungspapier",
        data={"redirect_to": f"/client/{mandate.id}/krise"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_recording_the_decision_through_the_form_lands_back_on_the_paper(
    web, session, mandate
):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    resp = web.post(
        f"/client/{mandate.id}/entscheidungspapier/{packet.id}/entscheidung",
        data={"decision": "Wir schweigen und beobachten."},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(
        f"/client/{mandate.id}/entscheidungspapier/{packet.id}"
    )
    session.expire_all()
    assert session.get(DecisionPacket, packet.id).is_decided


def test_the_decider_form_stores_the_name_and_the_deadline(web, session, mandate):
    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    web.post(
        f"/client/{mandate.id}/entscheidungspapier/{packet.id}/entscheider",
        data={"decision_maker": "Anna Berger", "deadline": "2026-09-06"},
        follow_redirects=False,
    )
    session.expire_all()
    stored = session.get(DecisionPacket, packet.id)
    assert stored.decision_maker == "Anna Berger"
    assert stored.deadline is not None


def test_an_unreadable_deadline_keeps_the_old_one_and_stores_the_decider(
    web, session, mandate
):
    """A hand-edited date is not a deadline. Stored as "none" it would read as a
    deadline nobody set, so the Frist stays as it stood and the page says why —
    but the name typed in the same form is not thrown away with it."""
    from newspulse.web.routes import issues_view

    issue = _issue(session, mandate)
    packet = _build(session, mandate, issue)
    decision.set_decider(
        session,
        packet,
        decision_maker="Anna Berger",
        deadline=_NOW + dt.timedelta(days=2),
    )
    # Read back rather than held from the write: SQLite hands the column back
    # in its own shape, and the comparison below must be of two stored values.
    session.expire_all()
    kept = session.get(DecisionPacket, packet.id).deadline
    assert kept is not None
    web.post(
        f"/client/{mandate.id}/entscheidungspapier/{packet.id}/entscheider",
        data={"decision_maker": "Jemand", "deadline": "übermorgen"},
        follow_redirects=False,
    )
    session.expire_all()
    stored = session.get(DecisionPacket, packet.id)
    assert stored.decision_maker == "Jemand"
    assert stored.deadline == kept
    body = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}").text
    assert issues_view.DEADLINE_UNREADABLE in body


def test_a_decided_paper_shows_no_forms_and_says_why_an_edit_was_refused(
    web, session, mandate
):
    from newspulse.web.routes import issues_view

    packet = _seeded_paper(session, mandate)
    body = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}").text
    assert f"/entscheidungspapier/{packet.id}/entscheidung" not in body
    assert f"/entscheidungspapier/{packet.id}/entscheider" not in body
    web.post(
        f"/client/{mandate.id}/entscheidungspapier/{packet.id}/entscheidung",
        data={"decision": "Doch etwas anderes."},
        follow_redirects=False,
    )
    assert issues_view.PACKET_DECIDED in web.get(
        f"/client/{mandate.id}/entscheidungspapier/{packet.id}"
    ).text
    session.expire_all()
    assert session.get(DecisionPacket, packet.id).decision == (
        "Wir reagieren nicht und beobachten weiter."
    )


def test_the_page_renders_in_english_when_asked(web, session, mandate):
    packet = _seeded_paper(session, mandate)
    web.post("/language/en?next=/", follow_redirects=False)
    body = web.get(f"/client/{mandate.id}/entscheidungspapier/{packet.id}").text
    assert "Decision packet" in body
    assert "The source order" in body
    assert "verified media report" in body
    assert "Was passiert ist" not in body


# --- The golden file ---------------------------------------------------------------


def test_the_seeded_paper_renders_exactly_the_golden_file(
    web, session, mandate, monkeypatch
):
    """Every wording change on the paper becomes a diff a reviewer can read.

    The download rather than the screen, because it is the artefact that leaves
    the tool: it carries no nav, no forms and no link back into the app, so the
    bytes are the document itself. The zone is pinned because the golden holds
    rendered local dates, and a suite that produced different bytes on a
    differently-configured host would be testing the host. Deliberate changes
    regenerate the fixture with ``NEWSPULSE_UPDATE_GOLDEN=1`` — and the diff
    goes into review with them.
    """
    monkeypatch.setattr(config, "LOCAL_ZONE", ZoneInfo("Europe/Berlin"))
    packet = _seeded_paper(session, mandate)

    body = web.get(
        f"/client/{mandate.id}/entscheidungspapier/{packet.id}/dokument.html"
    ).text
    # The ids the seed cannot pin: they are the rows' own primary keys, and the
    # paper prints them as Kennungen. Normalised rather than dropped — the
    # golden has to show that a Kennung *is* printed, without freezing which
    # number an in-memory database happened to hand out. The alternation comes
    # off the enum rather than off the three kinds this seed happens to cite:
    # a seed extended to cite a fourth would otherwise start freezing keys.
    kinds = "|".join(kind.value for kind in EvidenceKind)
    body = re.sub(rf"\b({kinds}):\d+\b", r"\1:N", body)
    if os.environ.get("NEWSPULSE_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(body, "utf-8")
    assert GOLDEN.exists(), (
        "no golden file; run NEWSPULSE_UPDATE_GOLDEN=1 pytest tests/test_decision.py"
    )
    assert body == GOLDEN.read_text("utf-8")


# --- The second anchor, reached from the one page that offers the button -----------


def test_an_escalated_issues_paper_hangs_on_the_crisis(session, mandate):
    """The acceptance asks for a paper "zu einem Issue oder einer Krise". The
    register row keeps its ``crisis_id`` from the handover, so the button on an
    escalated row reaches the second anchor without a second button."""
    from newspulse.web.routes import issues_view

    issue = _issue(session, mandate)
    standing = _crisis(session, mandate, _article(session, "Werk stoppt Produktion"))
    issue.crisis_id = standing.id
    session.commit()
    anchor, crisis = issues_view._packet_occasion(session, issue)
    assert anchor is None and crisis is standing


def test_an_open_issues_paper_hangs_on_the_issue(session, mandate):
    from newspulse.web.routes import issues_view

    issue = _issue(session, mandate)
    anchor, crisis = issues_view._packet_occasion(session, issue)
    assert anchor is issue and crisis is None


def test_the_register_lists_a_crisis_paper_on_the_row_it_was_written_from(
    web, session, mandate
):
    """An escalated row's papers hang on the crisis; hiding them from the row
    would make them unreachable from the one page that offers the button."""
    issue = _issue(session, mandate)
    standing = _crisis(session, mandate, _article(session, "Werk stoppt Produktion"))
    issue.crisis_id = standing.id
    issue.status = IssueStatus.ESKALIERT
    session.commit()
    packet = decision.build(
        session, mandate, crisis=standing, by="lucas", invoke=_reply_with(), now=_NOW
    )
    assert packet is not None
    body = web.get(f"/client/{mandate.id}/issues").text
    assert f"/client/{mandate.id}/entscheidungspapier/{packet.id}" in body
