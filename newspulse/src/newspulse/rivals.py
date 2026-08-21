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

#: Where a named competitor's name stops and the reason begins. The kick-off asks
#: for "Unternehmen, und in einem Halbsatz warum", so an answer arrives as one
#: line: "Trade Republic, weil sie dieselben Kunden umwerben". The dashes carry
#: their spaces on purpose — a bare hyphen would cut "Trade-Republic" in half.
_REASON_SEPARATORS = (",", ";", ":", " weil ", " da ", " – ", " — ", " - ")


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
    """One named competitor, cut into the company and why it was named."""
    line = line.strip()
    cuts = [(line.find(sep), len(sep)) for sep in _REASON_SEPARATORS if sep in line]
    if not cuts:
        return line, ""
    at, width = min(cuts)
    return line[:at].strip(), line[at + width:].strip(" ,;:–—-")


def from_named(client: Client, lines: list[str]) -> list[RivalSuggestion]:
    """Competitors somebody named, as proposals. Creates nothing, links nothing.

    The kick-off answer is prose — a company and half a sentence of reason — so
    the name is cut out of it here rather than asked for in two fields, which
    would have made the question read like a form. Everything downstream is the
    same path the model's own suggestions take: the consultant clicks, and only
    then does a company exist and get linked.

    A competitor the mandate already has is dropped rather than offered again: an
    accept for it would be a no-op, and a proposal nobody can act on is noise on a
    page that is otherwise all decisions.
    """
    seen = _taken(client)
    out: list[RivalSuggestion] = []
    for raw in lines:
        for line in raw.splitlines():
            name, reason = _split_reason(line)
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
