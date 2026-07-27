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
from .models import Category

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
    relevance_score: int = Field(ge=_SCORE_MIN, le=_SCORE_MAX)
    importance_score: int = Field(ge=_SCORE_MIN, le=_SCORE_MAX)
    # The model's own alert guess. Deliberately NOT used for the stored flag; the
    # analyzer recomputes is_alert in code (see analyzer._compute_is_alert).
    is_alert: bool
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
    relevance_score: int = Field(ge=_SCORE_MIN, le=_SCORE_MAX)
    importance_score: int = Field(ge=_SCORE_MIN, le=_SCORE_MAX)
    is_alert: bool
    reasoning: str


__all__ = ["ArticleVerdict", "BatchVerdict", "Analysis"]


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
    """One suggested PR action, tied to the coverage that prompted it."""

    title: str
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
