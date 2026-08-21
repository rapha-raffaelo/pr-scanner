"""The deep-dive mandate profile, and the button that fills it from the web.

A PR consultant carries about twenty facts per mandate in his head: who speaks for
it, what it actually sells, how big it is, what it must never say. Until now the
tool held five of them (name, aliases, industry, keywords, alert topics) and every
generated text had to work from those five. A pitch that knows the CEO's name and
the size of the company is a different pitch.

Two rules shape this module.

**Every fact says where it came from.** A fact a machine read on a company's
website and a fact the consultant knows from the last kick-off must never look
alike, so each carries its source and its author, and the page shows both. A
consultant who overrules the machine leaves no source behind — his own knowledge
is the strongest provenance in the building and needs no link.

**Filling is proposing.** The button reads the web and offers what it found; it
writes nothing until someone accepts. That is the same posture the theme
suggestions and the competitor suggestions already take, and for the same reason:
these values shape months of monitoring and every generated text downstream.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, gemini
from .analyzer import ParseError, strip_code_fence
from .models import Client, ClientFact

_log = logging.getLogger(__name__)

#: What a value the consultant typed himself is credited to. His own knowledge is
#: the strongest provenance in the building and carries no source, so this is also
#: what the page reads to decide whether to print a citation at all.
FILLED_BY_HAND = "mensch"


@dataclass(frozen=True, slots=True)
class Field:
    """One line of the profile: what it is called and what a good answer looks
    like. The hint travels into the prompt, so "Mitarbeiter" comes back as a
    number and not as a paragraph about company culture."""

    key: str
    label: str
    hint: str
    #: Long answers get a text area and a full-width row; the rest sit two to a row.
    long: bool = False
    #: Whether the web research may fill this field. False for the ones only the
    #: kick-off can answer: a search-grounded model asked for an after-hours
    #: crisis number will return a switchboard, and the consultant would dial it
    #: during the one hour of the year it matters. Filtered out of the prompt
    #: *and* out of what comes back, because a model that was not asked can still
    #: volunteer.
    researched: bool = True


#: The profile, in the order it is read. Chosen from what the drafting prompts
#: actually need rather than from what a company database would hold: an outreach
#: letter needs a spokesperson and a claim it can stand on, not a VAT number.
FIELDS: tuple[Field, ...] = (
    Field("geschaeftsfeld", "Geschäftsfeld", "Was das Unternehmen verkauft, in einem Satz.", long=True),
    Field("ceo", "Geschäftsführung", "Namen und Rollen der Geschäftsführung."),
    Field("pressekontakt", "Pressekontakt", "Name und Rolle der Kommunikationsverantwortlichen."),
    Field("gruendung", "Gegründet", "Jahreszahl."),
    Field("sitz", "Sitz", "Stadt und Land des Hauptsitzes."),
    Field("mitarbeiter", "Mitarbeitende", "Ungefähre Zahl, mit Stand."),
    Field("umsatz", "Umsatz / Finanzierung", "Letzte öffentlich genannte Zahl, mit Jahr."),
    Field("eigentuemer", "Eigentümer", "Konzernmutter, Investoren oder Streubesitz."),
    Field("zielgruppe", "Zielgruppe", "Wem das Unternehmen verkauft.", long=True),
    Field("produkte", "Produkte", "Die wichtigsten Produkte oder Marken.", long=True),
    Field("positionierung", "Positionierung", "Womit es sich vom Wettbewerb abgrenzt.", long=True),
    Field("wettbewerber", "Wettbewerber", "Die meistgenannten Vergleichsunternehmen.", long=True),
    Field("themen", "Öffentliche Themen", "Debatten, in denen das Unternehmen vorkommt.", long=True),
    Field("risiken", "Reputationsrisiken", "Was in der Berichterstattung gegen es verwendet wird.", long=True),
    # The three the kick-off questionnaire feeds and the web cannot. A company's
    # site never says who may be quoted on what, which trade title its buyers
    # actually read, or who picks up the phone at seven in the evening — those
    # answers exist in the kick-off call, and ONB-01 already asks for them. A
    # question naming a profile slot the profile does not have would be a promise
    # the tool cannot keep, so the slots exist here.
    #
    # ``researched=False`` is that same sentence enforced rather than commented:
    # asked for these, the model would answer, plausibly and wrongly, and a
    # guessed spokesperson goes into every outreach letter while a guessed crisis
    # number is dialled the one evening it counts.
    Field("sprecher", "Sprecher", "Wer zitiert werden darf, und wozu.", long=True, researched=False),
    Field("zielmedien", "Zielmedien", "Die Titel, in denen dieses Unternehmen vorkommen muss.", long=True, researched=False),
    Field("krisenkontakt", "Krisenkontakt", "Wer abends erreichbar ist, mit Nummer.", researched=False),
)

FIELDS_BY_KEY = {f.key: f for f in FIELDS}

#: The fields the web may be asked about. The rest are the kick-off's, and a
#: research run neither asks for them nor accepts them back.
RESEARCHED: tuple[Field, ...] = tuple(f for f in FIELDS if f.researched)

#: What a filled profile looks like, for the progress line on the page. All of
#: them, including the three only the kick-off can answer: the line says how much
#: of this mandate's file exists, and a denominator that quietly left out the
#: hardest three would read as fuller than the mandate is.
FILLABLE = len(FIELDS)


@dataclass(frozen=True, slots=True)
class Proposal:
    """One proposed value, with the page it was read from.

    Or with no page at all: a kick-off answer names the questionnaire as its
    source, and there is no URL to link because nobody published it. That is the
    strongest provenance a value can have here, not a missing one.
    """

    key: str
    value: str
    source_url: str = ""
    source_title: str = ""
    #: Who to record as the author when this is accepted. Empty means the research
    #: model, which is what proposes everything else on this page.
    filled_by: str = ""
    #: Whether accepting this may overrule a value already on file, keeping the
    #: old one visible beside it (DEC-2). True only for what a person said: the
    #: web research proposes into empty fields and corrects itself, and never
    #: overrules the consultant.
    supersedes: bool = False


def stored(session: Session, client_id: int) -> dict[str, ClientFact]:
    """Everything on file for this mandate, keyed by field."""
    rows = session.scalars(
        select(ClientFact).where(ClientFact.client_id == client_id)
    ).all()
    return {row.key: row for row in rows}


def _supersede(row: ClientFact, value: str, filled_by: str) -> None:
    """Move what this field says now into the slot behind it (DEC-2 option A).

    Only where the two actually disagree. Accepting an answer that says what the
    field already said is not a contradiction, and recording it as one would put a
    "die Recherche sagte" line under a value nothing ever contradicted.

    And only between two different authors. One profile slot can be fed by two
    kick-off questions — who may be quoted, and on what — so accepting that field
    a second time hands it the questionnaire's own earlier text. That is the same
    source restating itself, not a contradiction; treating it as one would move
    the client's own words into the slot behind the field and overwrite the
    researched value that was genuinely superseded, which nothing else can
    restore.
    """
    if not row.value.strip() or row.value.strip() == value:
        return
    if row.filled_by == filled_by:
        return
    row.superseded_value = row.value
    row.superseded_source_url = row.source_url
    row.superseded_source_title = row.source_title
    row.superseded_filled_by = row.filled_by
    row.superseded_at = dt.datetime.now(dt.UTC)


def _forget(row: ClientFact) -> None:
    """Clear the older value standing beside this field. The disagreement is over."""
    row.superseded_value = ""
    row.superseded_source_url = ""
    row.superseded_source_title = ""
    row.superseded_filled_by = ""
    row.superseded_at = None


def save(
    session: Session,
    client: Client,
    key: str,
    value: str,
    *,
    source_url: str = "",
    source_title: str = "",
    filled_by: str = FILLED_BY_HAND,
    supersede: bool = False,
) -> ClientFact | None:
    """Write one field. An empty value clears it rather than storing a blank.

    Clearing matters: a consultant who deletes a wrong machine-filled answer means
    "this is not known", and a row holding an empty string would keep claiming the
    field had been dealt with.

    ``supersede`` keeps the replaced value visible instead of overwriting it, for
    the one case DEC-2 is about: a kick-off answer that contradicts what the web
    said. The answer wins, and what the web said stays on the page with its own
    provenance until somebody drops it.

    A hand edit ends such a disagreement rather than joining it: the consultant
    has seen both values and written a third, so the older one stops being a
    contradiction worth showing — including where he typed it back in himself,
    which would otherwise leave "Vorher: X" standing under a current value of X.
    """
    if key not in FIELDS_BY_KEY:
        return None
    existing = session.scalars(
        select(ClientFact).where(
            ClientFact.client_id == client.id, ClientFact.key == key
        )
    ).first()
    value = (value or "").strip()
    if not value:
        if existing is not None:
            session.delete(existing)
            session.commit()
        return None
    row = existing or ClientFact(client_id=client.id, key=key)
    if existing is not None:
        if supersede:
            _supersede(row, value, filled_by)
        elif filled_by == FILLED_BY_HAND and existing.value.strip() != value:
            _forget(row)
    row.value = value
    row.source_url = source_url.strip()
    row.source_title = source_title.strip()
    row.filled_by = filled_by
    row.updated_at = dt.datetime.now(dt.UTC)
    session.add(row)
    session.commit()
    return row


def forget_superseded(session: Session, client_id: int, key: str) -> ClientFact | None:
    """Drop the old value standing beside this field, ending the disagreement.

    The way out of a permanent second line: once the consultant has seen that the
    web said something else and decided it no longer matters, keeping it on the
    page forever would turn provenance into clutter.
    """
    row = session.scalars(
        select(ClientFact).where(
            ClientFact.client_id == client_id, ClientFact.key == key
        )
    ).first()
    if row is None:
        return None
    _forget(row)
    session.commit()
    return row


_PROMPT = """Du recherchierst ein Unternehmen für eine PR-Agentur. Nutze die Suche.

UNTERNEHMEN
Name: $name
{extra}

Fülle die folgenden Felder aus, ausschließlich mit dem, was du in den Quellen
tatsächlich findest. Regeln, die wichtiger sind als Vollständigkeit:

- Kein Feld raten. Wenn du etwas nicht belegen kannst, lass es weg. Eine leere
  Zeile ist brauchbar, eine erfundene Zahl richtet Schaden an, weil ein Berater
  sie in ein Anschreiben übernimmt.
- Zahlen mit Stand: "rund 1.400 (2025)" statt "1.400".
- Deutsch, knapp, keine Werbesprache. Du beschreibst, du verkaufst nicht.
- Keine Gedankenstriche.

FELDER
$fields

Gib AUSSCHLIESSLICH ein JSON-Objekt zurück, ohne Markdown:

{"felder": {"schluessel": "Wert", ...}}

Nur Schlüssel aus der Liste, nur belegte Werte."""


def _prompt_for(client: Client) -> str:
    extra = []
    if client.website:
        extra.append(f"Website: {client.website}")
    if client.industry:
        extra.append(f"Branche laut Akte: {client.industry}")
    if client.aliases:
        extra.append(f"Auch bekannt als: {', '.join(client.aliases[:4])}")
    if client.country:
        extra.append(f"Land: {client.country}")
    fields = "\n".join(f"- {f.key}: {f.label}. {f.hint}" for f in RESEARCHED)
    return (
        _PROMPT.replace("{extra}", "\n".join(extra) or "Keine weiteren Angaben.")
        .replace("$name", client.name)
        .replace("$fields", fields)
    )


def research(client: Client, *, generate=None) -> list[Proposal]:
    """Read the web for this mandate and propose values. Stores nothing.

    Runs on the grounded provider rather than on the drafting model: the question
    is "what does the internet say about this company", and a model answering it
    from memory would produce exactly the plausible, unsourced, two-years-stale
    profile this feature exists to avoid. The sources come back with the answer
    and are stored beside each value.
    """
    if generate is None:
        if not config.review_configured():
            raise RuntimeError(
                "Für die Recherche fehlt der Zugang: GEMINI_API_KEY in der .env "
                "setzen, dann liest das Modell die Quellen selbst."
            )
        generate = gemini.search

    raw, sources = generate(_prompt_for(client))
    try:
        payload = json.loads(strip_code_fence(raw))
        found = payload.get("felder") or {}
    except Exception as exc:  # noqa: BLE001 — json raises its own
        raise ParseError(f"profile research was not valid JSON: {exc}") from exc

    # One source list for the whole answer is what the grounding API returns, so
    # the first source is attached to every field rather than pretending to a
    # per-field precision the provider does not give us.
    first = sources[0] if sources else ("", "")
    out: list[Proposal] = []
    for key, value in found.items():
        # ``researched`` again on the way back: the prompt does not list these
        # fields, and a model that answers a question it was not asked must not
        # be the one that decides whether the answer lands on the profile.
        field = FIELDS_BY_KEY.get(key)
        if field is not None and field.researched and isinstance(value, str) and value.strip():
            out.append(
                Proposal(
                    key=key,
                    value=value.strip(),
                    source_url=first[0],
                    source_title=first[1],
                )
            )
    return out


__all__ = ["FIELDS", "FIELDS_BY_KEY", "FILLABLE", "FILLED_BY_HAND", "RESEARCHED",
           "Field", "Proposal", "forget_superseded", "research", "save", "stored"]
