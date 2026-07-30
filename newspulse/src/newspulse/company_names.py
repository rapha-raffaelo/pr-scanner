"""Reading a company name the way a headline does.

A register entry and a headline are not the same string, and the difference is
almost always the legal form: the Handelsregister says "IB-7 Beauty Tech GmbH",
cash.at writes "IB-7 Beauty Tech: Weltweit erste KI-Hautpflege". The matcher
searches a client's terms as whole phrases (see :mod:`newspulse.matching`), so a
name entered with its legal form matched nothing at all — measured against the
live Google News results for that client, **0 of 7** items survived the filter,
and all seven were genuine coverage of it.

So every name yields two terms: as entered, and with the legal form removed.
That is a recall move, consistent with the matcher's stated bias — it only ever
adds a *shorter* form of a name the operator typed themselves, and Claude still
makes the relevance call downstream. In the Google News query the stripped form
*replaces* the full one (:mod:`newspulse.gnews`): a phrase search for "… GmbH"
can only ever exclude the coverage worth finding, because no headline writes it.

Deliberately conservative. Only forms that are unambiguous as a trailing token
are stripped; the ones that collide with ordinary words or brand tails — Nordic
``AS``/``AB``/``OY``, ``SA`` — are left in place. For a German-language portfolio
they buy nothing, and stripping one would shorten a name to something that pulls
in noise.
"""

from __future__ import annotations

import re

# Legal forms as they appear at the end of a company name, normalized (see
# _normalize: case-folded, punctuation dropped, so "GmbH", "gmbh" and "G.m.b.H."
# all arrive here as "gmbh").
_LEGAL_FORMS = frozenset(
    {
        # German, Austrian and Swiss
        "gmbh",
        "mbh",
        "ggmbh",
        "gesmbh",
        "ag",
        "se",
        "kg",
        "kgaa",
        "ohg",
        "gbr",
        "ug",
        "eg",
        "ek",
        "ev",
        # Common international, unambiguous as a trailing token
        "ltd",
        "limited",
        "plc",
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "llc",
        "llp",
        "sarl",
        "srl",
        "bv",
        "nv",
    }
)

# Tokens that only ever glue two legal forms together ("GmbH & Co. KG",
# "Miele & Cie. KG"). Stripped along with the forms, but — because the scan runs
# right to left and stops at the first token that is neither — they can never
# eat into a real name: "Marks & Spencer" stops at "Spencer" immediately.
_FORM_CONNECTORS = frozenset({"&", "und", "co", "cie", "haftungsbeschraenkt"})

# Folded so the qualifier of a German UG — "UG (haftungsbeschränkt)" — is
# recognized however the operator spelled the umlaut.
_UMLAUT_FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

_NON_ALNUM_RE = re.compile(r"[\W_]+", re.UNICODE)

# Below this, a stripped name is too short to search or match usefully ("K GmbH"
# would become "K", which matches every headline containing a lone K), so the
# name is left as entered.
_MIN_STRIPPED_CHARS = 2


def _normalize(token: str) -> str:
    """A token reduced to comparable word content: folded, letters and digits only."""
    return _NON_ALNUM_RE.sub("", token.casefold().translate(_UMLAUT_FOLD))


def strip_legal_form(name: str) -> str:
    """``name`` without its trailing legal form, or unchanged if it has none.

    Scans right to left and drops legal forms and the connectors between them,
    stopping at the first token that is neither — so only the tail is ever
    touched. Returns the name as given when stripping would leave nothing (a name
    that *is* a legal form) or too little to be a useful term.
    """
    tokens = name.split()
    kept = len(tokens)
    while kept:
        normalized = _normalize(tokens[kept - 1])
        # A token that normalizes to nothing (a stray "-", "&") is dropped with
        # the forms rather than treated as the start of the real name.
        if normalized and normalized not in _LEGAL_FORMS and normalized not in _FORM_CONNECTORS:
            break
        kept -= 1
    if kept == len(tokens):
        return name.strip()
    stripped = " ".join(tokens[:kept]).strip(" ,-–—&")
    if len(stripped) < _MIN_STRIPPED_CHARS:
        return name.strip()
    return stripped


def variants(name: str) -> list[str]:
    """``name`` as entered, plus its legal-form-free form when that differs.

    Order is load-bearing where the caller has a term budget: the form the
    operator typed comes first.
    """
    entered = (name or "").strip()
    if not entered:
        return []
    stripped = strip_legal_form(entered)
    if stripped.casefold() == entered.casefold():
        return [entered]
    return [entered, stripped]


__all__ = ["strip_legal_form", "variants"]
