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
import re
from dataclasses import dataclass
from importlib import resources
from string import Template

from sqlalchemy.orm import Session

from . import advisor, config, guide, matching
from .analyzer import AnalyzerError, ParseError, invoke_with_fallback, strip_code_fence
from .models import Client
from .schemas import CoachReport

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/coach.txt"

#: The window the guide is checked against. A month, like the advisory: shorter and
#: a single quiet fortnight reads as a failing message.
DEFAULT_DAYS = 30

#: The guide's own heading for the messages a text may carry, as ``prompts/guide.txt``
#: asks for it. Matched on the label rather than on position, so a consultant who
#: reorders his guide by hand keeps a readable one.
_KEY_MESSAGE_LABEL = "kernbotschaft"

#: A line that opens a new labelled section of the guide ("No-Gos:", "Tonalität:").
#: The label is short by construction — ``prompts/guide.txt`` asks for five one-word
#: headings — so the bound keeps a sentence containing a colon from being read as a
#: heading and silently ending the section it sits in.
_SECTION_RE = re.compile(r"^\s*([^:]{2,30}):\s*(.*)$")

#: How many words a label may have before it is a sentence rather than a heading.
_MAX_SECTION_LABEL_WORDS = 2

#: The other headings ``prompts/guide.txt`` produces, plus the words a consultant
#: writing his guide by hand reaches for instead. One of these ends the key-message
#: section; anything else with a colon in front of it is treated as a message,
#: because losing a message is the costlier error of the two.
_OTHER_SECTION_LABELS = frozenset(
    {
        "positionierung",
        "no-go",
        "nogo",
        "nie",
        "niemals",
        "tonalität",
        "tonalitaet",
        "ton",
        "sprache",
        "zielpublikum",
        "publikum",
        "zielgruppe",
        "sprecher",
    }
)

#: How the guide separates short points inside one section, per its own prompt.
_POINT_SEPARATORS = re.compile(r"[·•;]|\s\|\s")

#: The guide prompt asks for at most five key messages. Enforced here as well, so a
#: hand-written guide with twenty bullet points cannot turn one report section into
#: a wall of near-identical rows.
_MAX_KEY_MESSAGES = 5

#: The shortest word a message may be recognised by in a headline. Below five
#: characters a German token is a preposition or an initialism and would match half
#: the press; the same floor :func:`newspulse.matching.radar_matcher` uses.
_MIN_PROBE_CHARS = 5

#: How many of a message's own content words a piece must carry before the message
#: counts as landed. One shared noun is not a message: "Buchhaltung wird teurer"
#: and "Buchhaltung ohne Papier" are opposite claims about the same word.
#: :func:`newspulse.matching.radar_matcher` may sit on a single word because Claude
#: reads every candidate it lets through and throws the wrong ones away; this figure
#: has no second reader — it goes into a client's document as a count — so it cannot
#: buy recall with precision the way the pre-filter can.
_MIN_MESSAGE_WORDS = 2


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


# --- The guide's key messages, as something countable ---------------------------
#
# The review above asks a model whether the guide and the press agree. The client
# report asks a narrower question that must not depend on a model at all: how often
# did the mandate's own key messages actually turn up in the coverage. That figure
# goes into a document with the agency's name on it, so the rule that produced it
# has to be one a reader can re-apply by hand.
#
# It lives here rather than in reporting.py because this module is where the guide
# is compared against coverage, and two different readings of "the message landed"
# in one product would be worse than either.


def _section(line: str) -> tuple[str | None, str]:
    """``(label, rest)`` when ``line`` opens a labelled guide section, else ``(None, "")``.

    A label is short by construction, so a sentence that happens to carry a colon
    is not mistaken for a heading.
    """
    heading = _SECTION_RE.match(line)
    if heading is None:
        return None, ""
    label = heading.group(1).strip()
    if len(label.split()) > _MAX_SECTION_LABEL_WORDS:
        return None, ""
    return label, heading.group(2)


def _closes_key_messages(label: str) -> bool:
    """Whether ``label`` is one of the guide's other headings."""
    lowered = label.casefold()
    return any(known in lowered for known in _OTHER_SECTION_LABELS)


def key_messages(client: Client) -> list[str]:
    """The mandate's own key messages, read out of its guide.

    Only the Kernbotschaften section, never the whole guide: a No-Go is a sentence
    the mandate wants *absent* from the press, and counting how often it appeared
    would invert the figure. A guide without that section yields nothing, which is
    an honest answer — the report then says the message check needs a guide rather
    than reporting zero pull-through.

    The section ends at the next *known* guide heading rather than at the next
    colon. A message written as "Wachstum: aus eigener Kraft" is a message, and
    treating it as a heading would drop every message under it without a trace.
    """
    inside = False
    messages: list[str] = []
    for line in (getattr(client, "comms_guide", "") or "").splitlines():
        label, remainder = _section(line)
        opens = label is not None and _KEY_MESSAGE_LABEL in label.casefold()
        if opens:
            inside, body = True, remainder
        elif inside and label is not None and _closes_key_messages(label):
            inside, body = False, ""
        else:
            body = line if inside else ""
        for point in _POINT_SEPARATORS.split(body):
            cleaned = point.strip(" \t-–—·").strip()
            if cleaned and cleaned not in messages:
                messages.append(cleaned)
    return messages[:_MAX_KEY_MESSAGES]


@dataclass(frozen=True, slots=True)
class MessageProbe:
    """What a piece of coverage has to contain to count as carrying one message.

    Two ways in and deliberately not a third: the message as a whole phrase, or at
    least :data:`_MIN_MESSAGE_WORDS` of its own content words.
    """

    phrase: re.Pattern[str]
    words: tuple[re.Pattern[str], ...]


def message_matcher(message: str) -> MessageProbe | None:
    """What it takes to carry ``message``, or ``None`` if it carries nothing to match.

    The phrase alone would count almost nothing — a press report never quotes a key
    message verbatim, it paraphrases. A single word, the way
    :func:`newspulse.matching.radar_matcher` probes a theme, would count almost
    everything, including the piece that uses the word to say the opposite: "die
    Buchhaltung wird teurer" is not "Buchhaltung ohne Papier" landing. That trade
    is right for the pre-filter, which hands its hits to Claude, and wrong here,
    where the hit is the figure.

    So: the phrase, or two of the message's own content words. The cheapest rule
    that still requires the piece to be about what the message is about, and one a
    reader can re-apply by hand — which a client-facing count has to survive. The
    word-boundary lookarounds are what keep "Kosmetik" out of
    "Kosmetikerin-Ausbildung" reading as a hit.
    """
    term = (message or "").strip()
    phrase = matching.terms_matcher([term])
    if phrase is None:
        return None
    found = re.findall(r"\w+", term, re.UNICODE)
    words = dict.fromkeys(w.casefold() for w in found if len(w) >= _MIN_PROBE_CHARS)
    compiled = [matching.terms_matcher([word]) for word in words]
    probes = tuple(pattern for pattern in compiled if pattern is not None)
    # A message with only one content word is carried by its phrase or not at all:
    # a bar of two it could never clear would silence it entirely.
    return MessageProbe(
        phrase=phrase, words=probes if len(probes) >= _MIN_MESSAGE_WORDS else ()
    )


def carries(text: str, probe: MessageProbe | None) -> bool:
    """Whether ``text`` carries the message ``probe`` was built from.

    Case-folded here rather than at every call site, so the ß→ss fold the matchers
    are compiled with is applied on both sides — the same pairing
    :func:`newspulse.matching.match_candidates` makes.
    """
    if probe is None:
        return False
    folded = (text or "").casefold()
    if probe.phrase.search(folded) is not None:
        return True
    hits = sum(1 for word in probe.words if word.search(folded) is not None)
    return hits >= _MIN_MESSAGE_WORDS


__all__ = [
    "AnalyzerError",
    "DEFAULT_DAYS",
    "GuideMissing",
    "MessageProbe",
    "ParseError",
    "carries",
    "key_messages",
    "message_matcher",
    "review",
]
