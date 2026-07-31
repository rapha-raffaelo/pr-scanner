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
from .schemas import RivalSuggestions

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/rivals.txt"


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
    taken = {client.name.casefold()} | {c.name.casefold() for c in client.competitors}
    return [r for r in result.rivals if r.name.strip() and r.name.casefold() not in taken]


__all__ = ["AnalyzerError", "ParseError", "suggest"]
