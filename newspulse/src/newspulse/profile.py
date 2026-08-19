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
)

FIELDS_BY_KEY = {f.key: f for f in FIELDS}

#: What a filled profile looks like, for the progress line on the page.
FILLABLE = len(FIELDS)

#: What :attr:`~newspulse.models.ClientFact.filled_by` holds for a value a person
#: typed in, as opposed to the model that proposed it. Named because it is an
#: authority level and not a label: a fact carrying this may be contradicted by
#: the web and may never be overwritten by it.
BY_HAND = "mensch"


@dataclass(frozen=True, slots=True)
class Proposal:
    """One proposed value, with the page it was read from."""

    key: str
    value: str
    source_url: str = ""
    source_title: str = ""


def stored(session: Session, client_id: int) -> dict[str, ClientFact]:
    """Everything on file for this mandate, keyed by field."""
    rows = session.scalars(
        select(ClientFact).where(ClientFact.client_id == client_id)
    ).all()
    return {row.key: row for row in rows}


def save(
    session: Session,
    client: Client,
    key: str,
    value: str,
    *,
    source_url: str = "",
    source_title: str = "",
    filled_by: str = BY_HAND,
) -> ClientFact | None:
    """Write one field. An empty value clears it rather than storing a blank.

    Clearing matters: a consultant who deletes a wrong machine-filled answer means
    "this is not known", and a row holding an empty string would keep claiming the
    field had been dealt with.
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
    row.value = value
    row.source_url = source_url.strip()
    row.source_title = source_title.strip()
    row.filled_by = filled_by
    row.updated_at = dt.datetime.now(dt.UTC)
    session.add(row)
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
    fields = "\n".join(f"- {f.key}: {f.label}. {f.hint}" for f in FIELDS)
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
        if key in FIELDS_BY_KEY and isinstance(value, str) and value.strip():
            out.append(
                Proposal(
                    key=key,
                    value=value.strip(),
                    source_url=first[0],
                    source_title=first[1],
                )
            )
    return out


__all__ = ["FIELDS", "FIELDS_BY_KEY", "FILLABLE", "Field", "Proposal",
           "research", "save", "stored"]
