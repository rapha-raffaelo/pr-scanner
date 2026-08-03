"""Proposing market themes for a client's topic radar — and measuring them.

The radar searches the themes an operator typed. What they type describes the
company: a beauty-tech mandate carried "KI in der Kosmetik", which reads
perfectly and returns nothing, because no journalist writes that phrase. Its
radar found ten items, nine of them about the mandate itself, and the drafting
model refused every time — correctly, since a statement about your own press
release is not a positioning.

So this module does two things, and the second is what makes it worth having:

* :func:`suggest` asks the model for themes of the client's *field* — debates the
  trade press has without naming the client.
* :func:`probe` then puts each proposal through the real Google News query the
  radar would use and counts what comes back, separating hits that mention the
  client from hits that do not. Only the second number matters: a theme that
  returns only the client's own coverage is exactly the failure this replaces.

Nothing is stored until someone clicks. A theme is a search term that shapes
months of archive, so the model proposes, the measurement decides what is worth
offering, and a person picks.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from importlib import resources
from string import Template

from . import config, gnews
from .analyzer import AnalyzerError, ParseError, invoke_with_fallback, strip_code_fence
from .ingest import fetch_feed
from .matching import mentions_client, name_matcher
from .models import Client
from .schemas import ThemeSuggestions

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/themes.txt"

# How far back a probe looks. The same window the impulse draws on, so the number
# shown next to a proposal is the number that theme would actually contribute.
PROBE_DAYS = 90

# Kept small deliberately: the probe fetches one feed per proposed theme, and
# eight sequential Google News requests is already several seconds of waiting.
MAX_PROBED = 8


@dataclass(frozen=True, slots=True)
class ThemeProbe:
    """One proposed theme, with what it actually returns.

    ``external`` is the decisive number: hits that do *not* mention the client.
    Those are the market developments a positioning can be built on. ``own`` are
    hits that name the client — real coverage, but the radar is not where it
    belongs, and a theme that yields only these is the failure mode this whole
    module exists to prevent.
    """

    term: str
    reason: str
    external: int
    own: int
    samples: tuple[str, ...] = ()
    # Whether the count came from the field-scoped query or the widened one. The
    # radar makes the same fallback, so this says which query the number
    # describes rather than leaving the operator to guess.
    widened: bool = False

    @property
    def usable(self) -> bool:
        return self.external > 0


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(text)


def _parse(raw: str) -> ThemeSuggestions:
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"theme suggestions were not valid JSON: {exc}") from exc
    try:
        return ThemeSuggestions.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"theme suggestions did not match the schema: {exc}") from exc


def suggest(client: Client, *, invoke=invoke_with_fallback) -> list:
    """Propose market themes for ``client``. Never stores anything.

    Themes the client already has are dropped rather than shown as new: the point
    of the list is what is missing.
    """
    extra = ""
    if client.website:
        extra = f"Website: {client.website}"
    if client.comms_guide:
        # What the mandate says it stands for is the best available evidence of
        # which debates it can credibly enter.
        extra = f"{extra}\nKommunikations-Guide:\n{client.comms_guide[:600]}".strip()

    prompt = _prompt_template().substitute(
        client_name=client.name,
        industry=client.industry or "—",
        country=client.country or "DE",
        extra=extra or "—",
    )
    result = _parse(invoke(prompt, timeout=config.ANALYZER_TIMEOUT))

    taken = {t.casefold() for t in [*(client.keywords or []), *(client.alert_topics or [])]}
    return [
        theme
        for theme in result.themes
        if theme.term.strip() and theme.term.strip().casefold() not in taken
    ]


def probe(
    client: Client,
    proposals: list,
    *,
    fetch=fetch_feed,
    days: int = PROBE_DAYS,
    now=None,
) -> list[ThemeProbe]:
    """Run each proposed theme as a real radar query and count what it returns.

    This is the whole value of the feature. A proposed theme reads plausible
    whether or not anyone writes about it, and the operator has no way to tell
    the difference — until they have added it, waited for a run, and found the
    radar still empty. One fetch per proposal answers it now.

    A theme whose query fails is reported with zero counts rather than dropped:
    "the search errored" and "nobody writes about this" are different facts, and
    silently discarding the first would present it as the second.
    """
    import datetime as dt

    reference = now() if callable(now) else (now or dt.datetime.now(dt.UTC))
    since = reference - dt.timedelta(days=days)
    lang, country = gnews.edition_for(client)
    field = gnews.context_terms(client)
    matcher = name_matcher(client)

    probes: list[ThemeProbe] = []
    for proposal in proposals[:MAX_PROBED]:
        term = proposal.term.strip()
        scoped = gnews.query_url(
            [term], lang=lang, country=country, context=field, max_terms=1
        )
        try:
            items = fetch(scoped, since, source="Themen-Probe", per_entry_source=True)
            widened = False
            if not items and field:
                # Exactly the fallback the radar makes, so the number shown is the
                # number that theme would really contribute. Measured for a
                # beauty-tech mandate, the field clause AND ("Beauty Tech") sent
                # every proposal to zero — the label is too rare to appear in the
                # press it is meant to select.
                items = fetch(
                    gnews.query_url([term], lang=lang, country=country, max_terms=1),
                    since,
                    source="Themen-Probe",
                    per_entry_source=True,
                )
                widened = True
        except Exception as exc:  # noqa: BLE001 — one bad probe must not lose the rest
            _log.warning("theme probe %r failed: %s", term, exc)
            probes.append(ThemeProbe(term=term, reason=proposal.reason, external=0, own=0))
            continue
        own = [i for i in items if mentions_client(i, matcher)]
        external = [i for i in items if i not in own]
        probes.append(
            ThemeProbe(
                term=term,
                reason=proposal.reason,
                external=len(external),
                own=len(own),
                samples=tuple(i.title for i in external[:2]),
                widened=widened,
            )
        )
    # Best first: the operator should not have to read past the ones that work.
    return sorted(probes, key=lambda p: p.external, reverse=True)


__all__ = [
    "AnalyzerError",
    "MAX_PROBED",
    "PROBE_DAYS",
    "ParseError",
    "ThemeProbe",
    "probe",
    "suggest",
]
