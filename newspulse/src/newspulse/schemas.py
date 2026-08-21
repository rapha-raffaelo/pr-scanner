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
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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


class AdvisoryBrief(BaseModel):
    """The model's read of a client's situation plus what it would do about it."""

    situation: str
    suggestions: list[ActionSuggestion] = Field(default_factory=list)


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


#: How many breaches one verdict carries. A cap on what is shown, not on what may
#: be found: it is enforced by truncation in :func:`newspulse.guide._parse_verdict`
#: and deliberately *not* by ``max_length`` on the field below. A sixth breach that
#: voided the whole verdict would invert the feature — the worse the draft, the
#: more breaches it draws, and the letter that draws six is the last one allowed to
#: come back saying "not checked".
MAX_BREACHES = 5

#: A quote that can actually be looked up: whitespace-only is the same as absent,
#: so it is stripped first and then required to have survived. Stripping is what
#: the reader does anyway when they search the letter for the sentence.
_Quote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class GuideBreach(BaseModel):
    """One collision between a sentence in the draft and a line of the guide.

    Both sides are quoted, and that is the whole point of the type: a rule breach
    asserted in the abstract ("zu werblich") has to be taken on faith, while a
    pair of quotes can be judged in a second by the person who is accountable for
    the letter. It is also what keeps a breach checkable against a guide the
    consultant wrote himself and can therefore re-read.

    Both quotes are required *and* non-empty. An empty side is the same failure as
    a missing one: it renders as an accusation with nothing under it, and it would
    still flip ``ok`` to False. Rejecting the verdict costs a real objection and
    yields the honest not-checked state; keeping the breach and dropping the quote
    would show an unanswerable one, and silently dropping the breach could turn an
    objection into an approval, which is the direction that ends a mandate.

    Non-empty is measured *after* stripping, because ``"   "`` and ``"\\n\\t"``
    render exactly like ``""`` — an empty blockquote under a red heading — and a
    bare ``min_length=1`` would wave them through.
    """

    model_config = ConfigDict(extra="ignore")

    #: The sentence from the letter, verbatim.
    draft: _Quote
    #: The line of the stored guide it breaks, verbatim.
    guide: _Quote


class GuideVerdict(BaseModel):
    """A second model's read of a letter against the client's own guide.

    Separate from :class:`MessageReview` on purpose. Invention and overclaiming
    are judgements about the world and a checker weighs them; a No-Go is not a
    judgement, because the client wrote it down. Averaging the two into one
    verdict would let a written rule be diluted into a style note.

    ``ok`` is recomputed in code from ``breaches`` (see
    :func:`newspulse.guide.check_guide`) rather than believed, the way the
    analyzer recomputes ``is_alert``: a reply that lists a breach and calls itself
    fine would otherwise render as an approval. The recompute only ever moves
    ``ok`` toward False; the opposite direction is a ParseError, not a correction.

    ``extra="forbid"`` here, against this module's usual stance (see
    :class:`ArticleVerdict`, where a stray key must not cost a whole batch). The
    prompt is German end to end and its only English tokens are these two keys, so
    a reply that lists its breaches under ``verstoesse`` or ``violations`` is the
    likely miss — and under ``extra="ignore"`` it arrives as an empty list, which
    is byte-identical to a clean letter. Losing one verdict to a stray key costs an
    honest "nicht geprüft"; keeping it costs an approval over an objection.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    #: Empty is the good case and must stay possible — a check that always finds
    #: something is ignored by the third letter and is then worse than none.
    #: Unbounded here on purpose: the list is cut to :data:`MAX_BREACHES` in
    #: :func:`newspulse.guide._parse_verdict`, so a thorough reply is trimmed
    #: rather than thrown away.
    breaches: list[GuideBreach] = Field(default_factory=list)


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
