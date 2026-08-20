"""Pydantic schemas for the analysis layer.

Two kinds of object live here and it is worth keeping them distinct:

* ``ArticleVerdict`` / ``BatchVerdict`` validate exactly what the *model* hands
  back. They are the trust boundary: the analyzer never uses raw parsed JSON,
  only a ``BatchVerdict`` that survived pydantic validation, so an out-of-range
  score or a category outside the closed enum forces a retry rather than
  silently persisting garbage.
* ``Analysis`` is what the analyzer *returns* to its caller (NP-06 maps it onto
  the ORM ``Analysis`` row). Its ``is_alert`` is recomputed in code from the
  returned scores/topics — never copied from the model's own ``is_alert`` — so
  the alert decision stays auditable and tunable.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .models import SCORE_MAX as _SCORE_MAX
from .models import SCORE_MIN as _SCORE_MIN
from .models import Category, Tonality

# Scores share the model's fixed 0..10 scale. Imported straight from
# newspulse.models — the same SCORE_MIN/SCORE_MAX the DB CHECK constraint is built
# from — so the Pydantic Field bounds and the DB range can never silently diverge.


class ArticleVerdict(BaseModel):
    """The model's verdict on a single article, as returned in the batch JSON.

    ``id`` is the 0-based index the prompt assigned to the candidate article, so
    the analyzer can map each verdict back to the article it describes even if
    the model reorders the list. ``extra="forbid"`` would be too strict (models
    love to add stray keys); we ignore unknown keys but validate every known one.
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(ge=0)
    is_relevant: bool
    summary: str
    category: Category
    # Defaulted, not required: a model that omits it should yield an honest
    # "unbekannt" rather than failing the whole batch over one soft field.
    tonality: Tonality = Tonality.UNBEKANNT
    relevance_score: int = Field(ge=_SCORE_MIN, le=_SCORE_MAX)
    importance_score: int = Field(ge=_SCORE_MIN, le=_SCORE_MAX)
    # The model's own alert guess. Deliberately NOT used for the stored flag; the
    # analyzer recomputes is_alert in code (see analyzer._compute_is_alert).
    is_alert: bool
    tonality: Tonality = Tonality.UNBEKANNT
    reasoning: str


class BatchVerdict(BaseModel):
    """A whole batch of verdicts, one per candidate article."""

    verdicts: list[ArticleVerdict]


class Analysis(BaseModel):
    """The analyzer's output for one (article, client) pair.

    This is the Protocol's return element. It carries the code-computed
    ``is_alert`` and the model's ``reasoning`` verbatim so a later "why was this
    flagged?" always has an answer. ``article_id`` / ``client_id`` are copied
    from the input objects when present so NP-06 can persist without re-matching.
    """

    article_id: int | None = None
    client_id: int | None = None
    is_relevant: bool
    summary: str
    category: Category
    # Defaulted, not required: a model that omits it should yield an honest
    # "unbekannt" rather than failing the whole batch over one soft field.
    tonality: Tonality = Tonality.UNBEKANNT
    relevance_score: int = Field(ge=_SCORE_MIN, le=_SCORE_MAX)
    importance_score: int = Field(ge=_SCORE_MIN, le=_SCORE_MAX)
    is_alert: bool
    tonality: Tonality = Tonality.UNBEKANNT
    reasoning: str


__all__ = ["ArticleVerdict", "BatchVerdict", "Analysis", "CoachFinding", "CoachReport"]


# --- Advisory: suggested PR actions --------------------------------------------


class ActionKind(StrEnum):
    """What sort of move a suggestion is."""

    REAKTIV = "reaktiv"      # respond to something that has happened
    PROAKTIV = "proaktiv"    # an opening to push a message
    BEOBACHTEN = "beobachten"  # not yet actionable; watch it


class Urgency(StrEnum):
    """When it needs doing."""

    HEUTE = "heute"
    DIESE_WOCHE = "diese_woche"
    LAUFEND = "laufend"


class ActionSuggestion(BaseModel):
    """One recommendation, as a text that can actually be sent.

    It used to be a briefing line — an imperative plus a rationale — and the
    consultant's verdict on that was that it did not cohere with the other half
    of the page: "für mich sind Empfehlungen Beispiel-Pressemeldungen, die man an
    PR-Berater schicken kann, und unten die Tags der jeweiligen Magazine". The
    positioning drafts already had that shape; this one described work instead of
    doing it, and the reader had to write the text themselves.

    So ``draft`` carries the sendable version and ``rationale`` stays as the
    reason it is worth sending. Defaulted rather than required: an older stored
    brief has no draft, and the page must render it rather than fail on it.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    #: The text itself, ready to go out — prose, no salutation, no bullet points.
    #: Empty for a recommendation whose right answer is to stay silent.
    draft: str = ""
    rationale: str
    kind: ActionKind
    urgency: Urgency
    # Indices into the numbered coverage the prompt supplied. Every suggestion
    # must point at the stories behind it, so a reader can check the reasoning
    # instead of taking it on trust.
    evidence: list[int] = Field(default_factory=list)


# --- Provenance: the one field on these schemas the model does not fill ---------
#
# ``brain_version`` appears on the three schemas a generator stores. It is the
# brain version (:func:`newspulse.brain.version`) the prompt was composed under,
# written onto the validated draft by the generator at the moment it builds that
# prompt. It rides along with the text to whoever stores it rather than being
# re-read at save time: a standard edited between the model answering and the row
# being written must not change what the finished text says it was written under.
#
# It is a field on the reply schema rather than a second return value because a
# stamp a caller has to remember to pass is a stamp that will be missing from
# whichever call site was added last. The cost of putting a system-owned field on
# a schema that parses model output is that the model can now reach it, and
# :func:`without_provenance` below is what takes that back.


def without_provenance(payload: object) -> object:
    """The model's reply with the stamp field taken out of it, before validation.

    Provenance is not the model's to state. A reply that volunteers
    ``"brain_version": 3`` would otherwise be validated straight into the field
    and the row would carry a number nothing recorded; one that volunteers
    ``"brain_version": "v2"`` would fail validation outright and cost the whole
    draft — a text a consultant is waiting for, lost over a field the model had
    no business filling. Dropping the key answers both.

    Applied by each generator's ``_parse`` to the decoded reply, and there alone,
    which is the difference between this and a validator on the field. A
    validator fires on *every* validation, so a draft that ever went through
    ``model_validate(draft.model_dump())`` — a queue payload, a cached draft, an
    API echo — would come back with its stamp silently reset to ``None``, and
    ``None`` is the one value that means "written before the standards were
    recorded". The discard belongs to model output, so it lives on the path model
    output takes.

    Non-mapping payloads pass through untouched: a reply that decoded to a list
    is the schema's failure to report, not this function's to pre-empt.
    """
    if isinstance(payload, dict):
        return {key: value for key, value in payload.items() if key != _STAMP_FIELD}
    return payload


#: The field :func:`without_provenance` takes out. Named once so the three
#: ``_parse`` functions and the three schemas cannot drift apart on the spelling.
_STAMP_FIELD = "brain_version"


class AdvisoryBrief(BaseModel):
    """The model's read of a client's situation plus what it would do about it."""

    situation: str
    suggestions: list[ActionSuggestion] = Field(default_factory=list)
    #: See "Provenance" above. Set by :func:`newspulse.advisor.advise`.
    brain_version: int | None = None


# --- Angle: a positioning message the consultant can send on ---------------------


class AngleDraft(BaseModel):
    """A drafted positioning message for one client, off one market development.

    The field set is taken from how the consultant actually writes these: a
    finished text he could send, the factual basis under it, why *this* mandate
    may credibly say it, the thesis it rests on — and, explicitly, the overclaim
    it must not become. That last field is not decoration. The example this was
    built from named its own trap ("central exchanges are disappearing, DEXes
    win") and rejected it; a draft that cannot name what it is *not* claiming is
    usually claiming too much.

    ``worth_sending`` is the model's own gate. False means "there is no opening
    here", which is the normal answer on most days for most mandates — nothing is
    stored, and the column stays empty rather than filling with manufactured
    urgency.
    """

    model_config = ConfigDict(extra="ignore")

    worth_sending: bool
    # A line for the consultant, not the client: what this is about, so a column
    # of drafts can be scanned without opening each one.
    subject: str = ""
    # The message itself, ready to send: prose, no bullet points, no salutation.
    message: str = ""
    # The developments it rests on, with the specifics (who, when, what exactly).
    context: str = ""
    # Why this mandate can speak to it without overreaching.
    credibility: str = ""
    thesis: str = ""
    overclaim: str = ""
    # The two to four statements derivable from the thesis. Capped: past four they
    # stop being a position and become a list.
    statements: list[str] = Field(default_factory=list, max_length=4)
    # Indices into the numbered developments the prompt supplied, so every draft
    # can be traced back to the coverage that triggered it.
    evidence: list[int] = Field(default_factory=list)
    #: See "Provenance" above. Set by :func:`newspulse.angles.suggest`.
    brain_version: int | None = None


# --- Outreach: the impulse, written at one recipient -----------------------------


class PersonalMessage(BaseModel):
    """A sendable message to one journalist, built from a positioning draft.

    The impulse is the position; this is the letter. It is what the old
    "Empfehlung" panel was reaching for and never quite delivered — that one
    described work ("react to the coverage") where this one does it.

    ``hook`` is for the consultant only and never for the recipient: it says what
    the journalist wrote that this answers, so the pitch can be checked before it
    goes out. Keeping it out of ``message`` is what lets the copy button take the
    text and nothing around it.
    """

    model_config = ConfigDict(extra="ignore")

    subject: str = ""
    message: str
    hook: str = ""
    #: See "Provenance" above. Set by :func:`newspulse.outreach.draft`.
    brain_version: int | None = None


class MessageReview(BaseModel):
    """A second model's read of a letter the first one wrote.

    Two models, one text. The one that wrote it is the worst possible judge of
    whether it oversells: it chose every word for a reason it still believes. So
    the check runs on a different provider entirely (Gemini, configured with its
    own key) and is asked one narrow question — would this embarrass the sender.

    ``send`` is its verdict, and it is advisory like everything else here: a
    consultant who disagrees sends the letter anyway. What the flag buys is that
    disagreeing becomes a decision instead of an oversight.
    """

    model_config = ConfigDict(extra="ignore")

    send: bool = True
    #: One line per concern, in the consultant's language. Empty is the good case
    #: and must stay possible: a checker that always finds something is noise.
    concerns: list[str] = Field(default_factory=list, max_length=5)
    #: The one thing to change first, if anything.
    fix: str = ""
    #: See "Provenance" above. Set by :func:`newspulse.outreach.crosscheck`, and
    #: its own rather than the letter's: the check composes a second brain prompt
    #: after the letter is already written, so the two can land on either side of
    #: an edit.
    brain_version: int | None = None


# --- Coach: does the guide hold up against the actual coverage? ------------------


class FindingKind(StrEnum):
    """What sort of observation the coach made.

    Three, and no more. A coach that returns a paragraph of nuance per point is a
    coach nobody reads on a Monday morning; the value is in saying which of these
    three a thing is.
    """

    LUECKE = "luecke"      # the guide claims it, the coverage does not show it
    KONFLIKT = "konflikt"  # the coverage contradicts the guide, or nears a No-Go
    TRAEGT = "traegt"      # the guide is holding — worth knowing, and rarer


class CoachFinding(BaseModel):
    """One observation, with the evidence that produced it."""

    model_config = ConfigDict(extra="ignore")

    kind: FindingKind
    # One line the consultant can scan; the detail sits underneath.
    headline: str
    detail: str
    # What to do about it. Empty for TRAEGT — "keep going" is not an action, and
    # inventing one there is how a report becomes noise.
    suggestion: str = ""
    # Indices into the numbered coverage the prompt supplied.
    evidence: list[int] = Field(default_factory=list)


class CoachReport(BaseModel):
    """The coach's read of one client's guide against its coverage."""

    model_config = ConfigDict(extra="ignore")

    # Capped: past five the report stops being a briefing and becomes an audit.
    findings: list[CoachFinding] = Field(default_factory=list, max_length=5)


# --- Competitor suggestions -----------------------------------------------------


class RivalSuggestion(BaseModel):
    """One proposed competitor. Nothing is created from it without a click."""

    model_config = ConfigDict(extra="ignore")

    name: str
    reason: str = ""


class RivalSuggestions(BaseModel):
    """The model's proposals for one client.

    An empty list is the expected answer for a small or very young company, and
    the prompt says so: a competitor invented to fill the list would end up in a
    share-of-voice calculation as if it were real.
    """

    model_config = ConfigDict(extra="ignore")

    rivals: list[RivalSuggestion] = Field(default_factory=list, max_length=6)


# --- Industry classification ----------------------------------------------------


class IndustryTerms(BaseModel):
    """Candidate industry terms, ordered from most specific to broadest.

    The order is load-bearing: the caller takes the first that the press actually
    writes, so the narrowest field that exists in print wins over a safe but
    useless umbrella term.
    """

    model_config = ConfigDict(extra="ignore")

    terms: list[str] = Field(default_factory=list, max_length=3)


# --- Theme suggestions ----------------------------------------------------------


class ThemeSuggestion(BaseModel):
    """One proposed market theme for the topic radar."""

    model_config = ConfigDict(extra="ignore")

    term: str
    reason: str = ""


class ThemeSuggestions(BaseModel):
    """The model's proposed themes for one client.

    Proposals only: what makes a theme usable is not that the model likes it but
    that the press actually writes about it without naming the client, and that
    is measured (:func:`newspulse.themes.probe`) before any of these is offered.
    """

    model_config = ConfigDict(extra="ignore")

    themes: list[ThemeSuggestion] = Field(default_factory=list, max_length=8)
