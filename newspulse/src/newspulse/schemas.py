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

from pydantic import BaseModel, ConfigDict, Field

from .models import Category

# Scores share the model's fixed 0..10 scale (see models.SCORE_MIN/SCORE_MAX).
# Duplicated as literals here only inside the Field bounds; kept in sync with the
# DB CHECK constraint by the round-trip validation tests.
_SCORE_MIN = 0
_SCORE_MAX = 10


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
