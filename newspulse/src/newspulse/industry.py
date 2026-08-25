"""Working out a client's industry — as a search term, not as a label.

The industry field stopped being decoration when the radar started using it. It
is now the clause that says "in this field", and it decides two things: which
market news the search returns, and which stored articles are linked as material
a mandate can position on.

That makes a wrong word expensive in a way nobody would guess from the form. A
beauty-tech mandate carried "Beauty Tech" — accurate, and almost absent from
German press text, so ``AND ("Beauty Tech")`` intersected every query to nothing
and the mandate sat for months with no market material. An empty field is no
better: without it the themes are matched against everything, which returns
Canada's GDP for a fashion retailer.

So the term is proposed by the model and then **measured**: a candidate is only
usable if the press actually writes it. Same discipline as the theme proposals,
for the same reason — a plausible-sounding term and a working one look identical
until something searches for it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from importlib import resources
from string import Template

from . import brain, config, gnews
from .analyzer import AnalyzerError, ParseError, invoke_with_fallback, strip_code_fence
from .ingest import fetch_feed
from .models import Client
from .schemas import IndustryTerms

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/industry.txt"

# How far back a candidate is measured. Wide enough that a quiet fortnight in one
# trade press does not condemn an otherwise good term.
PROBE_DAYS = 90

# Below this a term is not a field, it is a coincidence. Two is deliberately low:
# the term only has to be *written*, and a niche trade vocabulary is exactly what
# a good filter looks like.
MIN_HITS = 2


@dataclass(frozen=True, slots=True)
class Candidate:
    """One proposed industry term and how much press actually uses it."""

    term: str
    hits: int
    #: Whether the probe actually ran. A search that could not be reached is not
    #: a measurement of zero, and the two must not look alike: "nobody writes
    #: this word" sends an operator off to change a term, and doing that on the
    #: strength of one rate-limited morning is worse than saying nothing.
    measured: bool = True

    @property
    def usable(self) -> bool:
        return self.hits >= MIN_HITS


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(brain.compose(text))


def _parse(raw: str) -> IndustryTerms:
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"industry terms were not valid JSON: {exc}") from exc
    try:
        return IndustryTerms.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"industry terms did not match the schema: {exc}") from exc


def propose(client: Client, *, invoke=invoke_with_fallback) -> list[str]:
    """Ask for candidate industry terms, nearest first. Stores nothing."""
    parts = []
    if client.website:
        parts.append(f"Website: {client.website}")
    if client.aliases:
        parts.append(f"Aliasse: {', '.join(client.aliases[:4])}")
    if client.keywords:
        parts.append(f"Bisherige Suchbegriffe: {', '.join(client.keywords[:6])}")
    if client.industry:
        parts.append(f"Bisherige Branchenangabe: {client.industry}")

    prompt = _prompt_template().substitute(
        client_name=client.name,
        country=client.country or "DE",
        extra="\n".join(parts) or "—",
    )
    terms = [t.strip() for t in _parse(invoke(prompt, timeout=config.ANALYZER_TIMEOUT)).terms]
    return [t for t in terms if t]


def measure(
    client: Client, terms: list[str], *, fetch=fetch_feed, days: int = PROBE_DAYS, now=None
) -> list[Candidate]:
    """Search each candidate on its own and count what the press wrote.

    A term used as a filter has to appear in ordinary coverage; searching it
    alone is exactly that question. Terms are measured in the client's own news
    edition, because a word can be common in one and absent in another.
    """
    reference = now() if callable(now) else (now or dt.datetime.now(dt.UTC))
    since = reference - dt.timedelta(days=days)
    lang, country = gnews.edition_for(client)

    measured: list[Candidate] = []
    for term in terms:
        try:
            items = fetch(
                gnews.query_url([term], lang=lang, country=country, max_terms=1),
                since,
                source="Branchen-Probe",
                per_entry_source=True,
            )
        except Exception as exc:  # noqa: BLE001 — one bad probe must not lose the rest
            _log.warning("industry probe %r failed: %s", term, exc)
            measured.append(Candidate(term=term, hits=0, measured=False))
            continue
        measured.append(Candidate(term=term, hits=len(items)))
    return measured


def classify(
    client: Client, *, invoke=invoke_with_fallback, fetch=fetch_feed, now=None
) -> Candidate | None:
    """The best usable industry term for ``client``, or ``None``.

    "Best" is the nearest proposal that clears :data:`MIN_HITS`, not the one with
    the most hits: the list comes back ordered from most specific to broadest, and
    a broader term is a weaker filter. Taking the first that works keeps the
    narrowest field that actually exists in print.
    """
    candidates = measure(client, propose(client, invoke=invoke), fetch=fetch, now=now)
    for candidate in candidates:
        if candidate.usable:
            return candidate
    if candidates:
        _log.info(
            "no usable industry term for %r; measured %s",
            client.name,
            ", ".join(f"{c.term}={c.hits}" for c in candidates),
        )
    return None


def field_is_usable(client: Client, *, fetch=fetch_feed, now=None) -> bool | None:
    """Whether the client's stored industry works as a filter at all.

    The question asked before telling an operator that their perfectly accurate
    industry is the reason the mandate has no market material.

    Three answers, not two. ``None`` means the question could not be answered:
    every probe failed, so nothing was measured. It is kept apart from ``False``
    because :func:`measure` records an unreachable search as zero hits, and a
    caller that read that as an answer would tell an operator to change a term
    over an outage. ``False`` is only returned when the press was actually asked
    and did not write the word.
    """
    terms = gnews.context_terms(client)
    if not terms:
        return False
    candidates = measure(client, terms, fetch=fetch, now=now)
    if any(c.usable for c in candidates):
        return True
    return False if any(c.measured for c in candidates) else None


__all__ = [
    "AnalyzerError",
    "Candidate",
    "MIN_HITS",
    "ParseError",
    "classify",
    "field_is_usable",
    "measure",
    "propose",
]
