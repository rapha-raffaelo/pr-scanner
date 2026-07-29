"""Visual identity for a client: a logo when there is one, a monogram when not.

A portfolio of text-only cards reads as a spreadsheet. Logos fix that, but
requiring one per client would leave a half-configured portfolio looking broken —
so the fallback is a generated monogram: the client's initials over a colour
derived from its own name.

Deterministic, so a client keeps the same colour forever and becomes recognisable
by it. Computed locally with no network call and no image files, which keeps the
offline guarantee (DEC-3) intact for the common case; a ``logo_url`` is only
fetched by the browser when the operator supplies one.
"""

from __future__ import annotations

import hashlib
import re

# A spread of hues that stay legible with white text and do not read as status
# colours — nothing here should be mistaken for the red an alert uses.
_PALETTE = (
    "#4f46e5", "#0369a1", "#15803d", "#a16207", "#7e22ce",
    "#0f766e", "#b45309", "#1d4ed8", "#6d28d9", "#047857",
)

_WORD_RE = re.compile(r"[0-9A-Za-zÀ-ÿ]+")

# Legal-form suffixes carry no identity: "Zalando SE" and "Zalando" should give
# the same monogram, and "SE" must never become the initial.
_LEGAL_FORMS = frozenset(
    {"ag", "se", "gmbh", "kg", "kgaa", "mbh", "co", "ohg", "ug", "eg",
     "group", "holding", "inc", "ltd", "plc", "corp", "nv", "bv", "sa"}
)


def _significant_words(name: str) -> list[str]:
    words = _WORD_RE.findall(name or "")
    kept = [w for w in words if w.casefold() not in _LEGAL_FORMS]
    return kept or words


def monogram(name: str) -> str:
    """One or two initials for ``name``.

    Two words give two letters ("Deutsche Bahn" -> DB); a single word gives its
    first two characters ("Zalando" -> ZA), which is more distinguishable than a
    lone letter across a portfolio.
    """
    words = _significant_words(name)
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][:1] + words[1][:1]).upper()


def colour(name: str) -> str:
    """A stable colour for ``name``.

    Hashed rather than assigned by index so a client's colour never shifts when
    another is added or removed — the whole value of a colour identity is that it
    stays put.
    """
    digest = hashlib.sha256((name or "").casefold().encode("utf-8")).digest()
    return _PALETTE[digest[0] % len(_PALETTE)]


__all__ = ["colour", "monogram"]
