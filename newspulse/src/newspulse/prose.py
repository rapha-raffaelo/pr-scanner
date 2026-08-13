"""House style for text that leaves the building.

One rule so far, and it is not a matter of taste: **no dashes as punctuation.**

    "bitte speichere dir ab dass du nie '—' benutzt, dann weiss direkt jeder dass
    du KI nutzt um diese Nachrichten zu schreiben"

He is right. The em dash is the single most reliable tell in German business
prose — almost nobody reaches for it twice in a paragraph, and every language
model reaches for it constantly. A pitch that announces itself as machine-written
in its first line has lost the reader before the argument starts, and no amount of
good reasoning behind the text recovers that.

Asking the model not to do it is necessary and not sufficient: it complies for two
paragraphs and then relapses, and nobody is reading the third paragraph closely
enough to catch it before it is sent. So the prompt asks *and* this rewrites what
comes back. The rewrite is deliberately narrow — it changes punctuation and never
words — and it runs only over text meant for a recipient, never over the interface
around it, where a dash is nobody's tell.
"""

from __future__ import annotations

import re

#: Em dash, en dash, and the horizontal bar some models emit instead.
_DASHES = "—–―"

# Horizontal whitespace only, never a newline: a dash at the end of a paragraph
# must not be allowed to swallow the paragraph break and glue two of them into one
# sentence. That is a worse artefact than the dash it removes.
_BETWEEN_WORDS = re.compile(rf"[^\S\n]*[{_DASHES}][^\S\n]*")
# A dash opening or closing a line has nothing to attach a comma to.
_LEADING = re.compile(rf"(?m)^[^\S\n]*[{_DASHES}][^\S\n]*")
_TRAILING = re.compile(rf"(?m)[^\S\n]*[{_DASHES}][^\S\n]*$")
# ", , " and " ,," from a dash that already sat next to punctuation.
_DOUBLED = re.compile(r",\s*([,.;:!?])")
_SPACE_BEFORE = re.compile(r"\s+([,.;:!?])")


def plain(text: str) -> str:
    """The same text with dash punctuation turned into commas.

    Ranges are left alone (``2024–2026``, ``S. 4–7``): a dash between digits is
    not punctuation, it is notation, and nobody reads it as a stylistic choice.
    Hyphens are untouched throughout — ``KI-Hautpflege`` is a word, not a pause.
    """
    if not text:
        return text

    def _swap(match: re.Match[str]) -> str:
        start, end = match.span()
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        # A range: digits on both sides and no spaces around the dash.
        if before.isdigit() and after.isdigit() and not match.group(0).strip(_DASHES):
            return match.group(0)
        return ", "

    out = _LEADING.sub("", text)
    out = _TRAILING.sub("", out)
    out = _BETWEEN_WORDS.sub(_swap, out)
    out = _DOUBLED.sub(r"\1", out)
    out = _SPACE_BEFORE.sub(r"\1", out)
    return out.strip()


def has_dash(text: str) -> bool:
    """Whether any dash punctuation survived. Used by the tests, and by the
    cross-check to report on a draft it did not write."""
    return any(ch in text for ch in _DASHES)


__all__ = ["plain", "has_dash"]
