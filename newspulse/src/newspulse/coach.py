"""The strategy coach: does the guide hold up against what is actually written?

The guide says what a mandate wants to stand for. The archive says what the press
made of it. Nobody compares the two — it is slow, it needs the whole month in one
head, and it is exactly the work that gets postponed until a quarterly review.

Not a second assistant. Captain Comms answers questions; this answers one fixed
question, unasked, and its findings are typed rather than prose so a Monday
morning can scan them: a claim the coverage does not carry, a quote drifting
towards a No-Go, or a message that is visibly landing.

Advisory like everything else here. It changes no guide and sends nothing; it
says where it stumbles and leaves the decision with the person who is accountable
for it.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from string import Template

from sqlalchemy.orm import Session

from . import advisor, config, guide
from .analyzer import AnalyzerError, ParseError, invoke_with_fallback, strip_code_fence
from .models import Client
from .schemas import CoachReport

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/coach.txt"

#: The window the guide is checked against. A month, like the advisory: shorter and
#: a single quiet fortnight reads as a failing message.
DEFAULT_DAYS = 30


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(text)


def _parse(raw: str) -> CoachReport:
    """Validate the reply; anything else is a ParseError.

    Same trust boundary as everywhere else: the reply is text until the schema
    says otherwise, and a half-parsed report is discarded rather than shown.
    """
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"coach report was not valid JSON: {exc}") from exc
    try:
        return CoachReport.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"coach report did not match the schema: {exc}") from exc


def review(
    session: Session,
    client: Client,
    *,
    days: int = DEFAULT_DAYS,
    invoke=invoke_with_fallback,
) -> tuple[CoachReport, list[advisor.CoverageRef]]:
    """Check this client's guide against its coverage.

    Returns the report and the numbered coverage it was based on, so every finding
    can be resolved back to the stories behind it — a judgement you cannot trace is
    one you cannot check.

    Raises :class:`GuideMissing` when there is no guide to check and
    :class:`AnalyzerError` on a backend failure, so "nothing to check", "nothing
    found" and "the call failed" stay three distinguishable outcomes.
    """
    if not (client.comms_guide or "").strip():
        raise GuideMissing("Kein Kommunikations-Guide hinterlegt.")

    coverage = advisor.recent_coverage(session, client.id, days=days)
    if not coverage:
        return CoachReport(), []

    prompt = _prompt_template().substitute(
        client_profile=f"Name: {client.name}",
        comms_guide=guide.for_prompt(client),
        days=days,
        coverage=advisor._render_coverage(coverage),
    )
    report = _parse(invoke(prompt, timeout=config.ANALYZER_TIMEOUT))

    # Drop invented evidence ids, as the advisor and the angle do: a citation
    # pointing at nothing discredits the finding it was meant to support.
    valid = range(len(coverage))
    cleaned = [
        finding.model_copy(
            update={"evidence": [i for i in finding.evidence if i in valid]}
        )
        for finding in report.findings
    ]
    return report.model_copy(update={"findings": cleaned}), coverage


class GuideMissing(RuntimeError):
    """There is no guide to check, which is not a failure of the coach."""


__all__ = ["AnalyzerError", "DEFAULT_DAYS", "GuideMissing", "ParseError", "review"]
