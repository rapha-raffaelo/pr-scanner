"""Proposing competitors for a client.

Share of voice needs a comparison group, and the picker only offers companies
already marked as competitors — so the first one has to be created by hand, which
is the step nobody does. This proposes them from what the model knows about the
market, and the consultant clicks the ones that are actually competitors.

Advisory in the strict sense: the proposal is never stored. Clicking creates the
company and links it; not clicking leaves no trace. That matters more here than
elsewhere, because a wrong competitor does not merely look odd — it lands in the
share-of-voice arithmetic and quietly changes a number the agency reports.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from string import Template

from . import config
from .analyzer import AnalyzerError, ParseError, invoke_with_fallback, strip_code_fence
from .models import Client
from .schemas import RivalSuggestion, RivalSuggestions

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/rivals.txt"

#: Where a named competitor's name stops and the reason begins beyond doubt. The
#: kick-off asks for "Unternehmen, und in einem Halbsatz warum", so an answer
#: arrives as one line: "Trade Republic – sie umwerben dieselben Kunden". The
#: dashes carry their spaces on purpose — a bare hyphen would cut "Trade-Republic"
#: in half.
_REASON_MARKS = (":", " – ", " — ", " - ")

#: The same cut where the answer runs straight into its reason with no
#: punctuation. Kept in the reason rather than swallowed, because "weil sie
#: billiger sind" is a sentence and "sie billiger sind" is a fragment.
_REASON_WORDS = (" weil ",)

#: The comma and the semicolon are not in ``_REASON_MARKS`` because they do two
#: jobs in the same field: "Trade Republic, weil sie billiger sind" introduces a
#: reason, and "Intuitive Surgical, Medtronic, Stryker" names three companies.
#: The question is one line, so a consultant transcribing a call writes both — and
#: cutting at the first one either way threw two of those three away.
_LIST_MARKS = (",", ";")

#: How many words a fragment may have and still read as a company name. Four
#: covers "Deutsche Bahn Fernverkehr AG"; a half-sentence of reason is longer.
_NAME_MAX_WORDS = 4

#: Lowercase words that appear inside a company name without making the fragment
#: a sentence: "Meyer & Sohn", "Bank of America".
_NAME_JOINERS = frozenset({"&", "und", "and", "of", "for", "de", "van"})


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(text)


def _parse(raw: str) -> RivalSuggestions:
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"rival suggestions were not valid JSON: {exc}") from exc
    try:
        return RivalSuggestions.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"rival suggestions did not match the schema: {exc}") from exc


def _taken(client: Client) -> set[str]:
    """Names that must never be proposed: the mandate itself, and what it has."""
    return {client.name.casefold()} | {c.name.casefold() for c in client.competitors}


def _split_reason(line: str) -> tuple[str, str]:
    """One line, cut where a reason unmistakably begins."""
    line = line.strip()
    # ``width`` is what the separator itself eats: all of it for punctuation,
    # none of it for a word that belongs to the reason it introduces.
    cuts = [(line.find(sep), len(sep)) for sep in _REASON_MARKS if sep in line]
    cuts += [(line.find(sep), 1) for sep in _REASON_WORDS if sep in line]
    if not cuts:
        return line, ""
    at, width = min(cuts)
    return line[:at].strip(), line[at + width:].strip(" ,;:–—-")


def _reads_as_name(fragment: str) -> bool:
    """Whether an enumerated fragment is another company or the start of a reason.

    German capitalises its nouns, so a fragment that runs on in lowercase —
    "weil sie dieselben Kunden umwerben", "die denselben Markt bedienen" — is
    prose about the company before it. Every word has to carry a capital for the
    fragment to count as a name, which errs the safe way: a reason mistaken for a
    company would be created and linked into the share-of-voice arithmetic, while
    a company mistaken for a reason merely stays displayed as prose, which is
    where all of them stood before.
    """
    words = fragment.split()
    if not words or len(words) > _NAME_MAX_WORDS:
        return False
    return all(
        word.casefold() in _NAME_JOINERS or word[:1].isupper() or word[:1].isdigit()
        for word in words
    )


def _named_in(line: str) -> tuple[list[str], str]:
    """One transcribed line, cut into every company it names and their reason.

    The reason is shared: "Intuitive Surgical, Medtronic, weil beide OP-Roboter
    bauen" says the same thing about both, and the consultant reading the
    proposals needs it on each of the rows he is deciding about.
    """
    head, reason = _split_reason(line)
    for mark in _LIST_MARKS[1:]:
        head = head.replace(mark, _LIST_MARKS[0])
    fragments = [f.strip() for f in head.split(_LIST_MARKS[0]) if f.strip()]
    if not fragments:
        return [], reason
    # The first fragment is the company however it is written — a lowercase brand
    # is still what the client answered, and there is nothing before it for it to
    # be a reason about.
    names = fragments[:1]
    rest: list[str] = []
    for fragment in fragments[1:]:
        if not rest and _reads_as_name(fragment):
            names.append(fragment)
        else:
            rest.append(fragment)
    return names, ", ".join(part for part in (*rest, reason) if part)


def from_named(client: Client, lines: list[str]) -> list[RivalSuggestion]:
    """Competitors somebody named, as proposals. Creates nothing, links nothing.

    The kick-off answer is prose — a company and half a sentence of reason — so
    the names are cut out of it here rather than asked for in two fields, which
    would have made the question read like a form. One answer can carry several:
    the question is a single line and "wichtigster Wettbewerber" is routinely
    answered with three, so each of them becomes a proposal of its own. Offering
    only the first left the others as prose nobody could click.

    Everything downstream is the same path the model's own suggestions take: the
    consultant clicks, and only then does a company exist and get linked.

    A competitor the mandate already has is dropped rather than offered again: an
    accept for it would be a no-op, and a proposal nobody can act on is noise on a
    page that is otherwise all decisions.
    """
    seen = _taken(client)
    out: list[RivalSuggestion] = []
    for raw in lines:
        for line in raw.splitlines():
            names, reason = _named_in(line)
            for name in names:
                if not name or name.casefold() in seen:
                    continue
                seen.add(name.casefold())
                out.append(RivalSuggestion(name=name, reason=reason))
    return out


def suggest(client: Client, *, invoke=invoke_with_fallback) -> list:
    """Propose competitors for ``client``. Never stores anything.

    Returns an empty list when the model does not know the company — the normal
    case for a young mandate, and the prompt asks for exactly that rather than a
    plausible-looking guess.
    """
    extra = ""
    if client.website:
        extra = f"Website: {client.website}"
    elif client.keywords:
        extra = f"Themen: {', '.join(client.keywords[:6])}"

    prompt = _prompt_template().substitute(
        client_name=client.name,
        industry=client.industry or "—",
        country=client.country or "DE",
        extra=extra,
    )
    result = _parse(invoke(prompt, timeout=config.ANALYZER_TIMEOUT))

    # Never propose the client itself, and never one it already has.
    taken = _taken(client)
    return [r for r in result.rivals if r.name.strip() and r.name.casefold() not in taken]


__all__ = ["AnalyzerError", "ParseError", "from_named", "suggest"]
