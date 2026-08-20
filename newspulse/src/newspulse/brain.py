"""What the house believes, in one place, so the prompts can read it.

The standards were always here. They were just written out ten times, once per
prompt, in ten slightly different wordings: ``angle.txt`` knew the house tone,
``outreach.txt`` knew it too and said it differently, ``crosscheck.txt`` knew how
to spot a breach of it, and no two of them agreed exactly. Deciding that a thesis
now needs two independent pieces of evidence meant finding four places and getting
all four right, and missing one meant the tool held two standards at once and
reported both as correct.

So a block is a named piece of what the house believes, stored as one text file
under ``blocks/``, and a prompt *includes* it rather than restating it::

    #blocks: evidence, refusal

    ...task instructions...

    {{brain:evidence}}

Two markers, deliberately. ``{{brain:key}}`` is the include, and the ``#blocks:``
header is the declaration a person reads at the top of the file to know which
standards govern this prompt without scanning eighty lines for markers. They can
drift apart, so ``tests/test_brain.py`` asserts they never do.

The include syntax is ``{{…}}`` rather than ``$…`` because the prompts are
``string.Template`` and every ``$name`` in them is already spoken for by the
caller's substitution. A brain marker has to survive being read and be gone before
``substitute`` ever sees it, which is what :func:`compose` is for.

Resolution takes its source explicitly. That is what lets a test compose against
six lines of fixture text instead of the shipped files, and it is the seam
DEC-1's override chain hangs on: BRN-02 adds a database layer in front of
:func:`shipped` without any caller learning that it happened.

One rule deliberately stayed out of here. ``prose.plain()`` strips dashes from
generated text in code, and it stays in code. The block :file:`blocks/house_style.txt`
*asks* the model not to use them, because asking is worth something, but a rule
that must hold on the output cannot be a sentence a model is asked to follow: it
complies for two paragraphs and then relapses. The ask and the enforcement are
two different mechanisms and this layer is only the first.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from functools import lru_cache
from importlib import resources
from types import MappingProxyType

#: Where the shipped blocks live, as package data beside ``prompts/``. Named
#: ``blocks`` and not ``brain`` on purpose: a data directory next to
#: ``brain.py`` with the same stem resolves today only because CPython's
#: FileFinder prefers the module over a namespace-package portion, and anything
#: that reverses that preference — an ``__init__.py`` added here by a later
#: story, a packaging tool that walks directories first — would shadow this
#: module and break every prompt render at import.
_BLOCK_DIR = "blocks"
_BLOCK_SUFFIX = ".txt"

#: ``{{brain:key}}``. Deliberately generous about the marker and strict about the
#: key. An earlier version matched only ``[a-z0-9_]+`` with no surrounding space,
#: which meant ``{{brain:Evidence}}`` and ``{{brain: evidence}}`` were not unknown
#: blocks at all: they were not markers, so :func:`compose` returned them verbatim
#: and the literal braces went to the model as prompt text. That is the silent
#: composition-without-the-standard this whole layer exists to prevent, so a typo
#: has to land inside the capture where :func:`block` can raise on it.
_INCLUDE = re.compile(r"\{\{\s*brain\s*:\s*(?P<key>[^{}]*?)\s*\}\}", re.IGNORECASE)

#: The spellings too broken to parse as an include at all: ``{{brain}}``,
#: ``{{ brain evidence }}``, a marker with a stray brace inside it. Checked after
#: expansion, so whatever survives is a mistake rather than prompt text. Broader
#: than ``_INCLUDE`` on purpose: this one only has to *notice*, not resolve.
_SUSPECT_INCLUDE = re.compile(r"\{\{[^}]*brain[^}]*\}\}", re.IGNORECASE)

#: ``#blocks: a, b, c`` on a line of its own. Stripped before the prompt is sent:
#: it addresses the person editing the file, not the model reading it.
_DECLARATION = re.compile(r"(?m)^[ \t]*#blocks:[ \t]*(?P<keys>[^\n]*)\n?")

#: How many expansion rounds :func:`compose` will follow before it calls the text
#: cyclic. A block is one screen a consultant reads, and four levels of block
#: including block is already past the point where anyone can say what a prompt
#: contains; beyond it the likelier explanation is that two blocks include each
#: other, which without a cap expands until the process dies.
_MAX_INCLUDE_DEPTH = 5

#: Enough hex to make a collision between two block sets a non-worry, short
#: enough to read in a log line or a test failure.
_VERSION_DIGITS = 12


class UnknownBlock(LookupError):
    """A prompt asked for a block that does not exist.

    Loud on purpose, and at render rather than at send. The quiet alternative is
    a prompt that composes without the standard it declared and produces text
    that looks fine, which is the failure this whole layer exists to prevent.

    ``LookupError`` rather than ``KeyError`` for the same reason. Prompt
    rendering runs ``Template.substitute``, which raises ``KeyError`` for a
    missing placeholder, so a caller that one day wraps a render in
    ``except KeyError`` would swallow an unresolved block exactly as quietly as
    this class exists to prevent. ``except LookupError`` still catches it for
    anyone who means to.
    """

    def __init__(self, key: str, known: list[str]) -> None:
        self.key = key
        self.known = known
        super().__init__(
            f"unknown brain block {key!r}; the shipped blocks are: "
            f"{', '.join(known) or '(none)'}"
        )


class BlockCycle(RecursionError):
    """Blocks include each other, or nest deeper than composition will follow.

    Unreachable while the blocks are files somebody reviews, and reachable the
    moment BRN-02 makes block text a field a consultant edits: two blocks that
    name each other would otherwise expand until the process runs out of memory,
    in a request that looks like a slow render right up to the point it dies.
    """

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(
            f"brain blocks still expand after {_MAX_INCLUDE_DEPTH} rounds; "
            f"a cycle through: {', '.join(keys) or '(unknown)'}"
        )


@lru_cache(maxsize=1)
def shipped() -> Mapping[str, str]:
    """The blocks as the repository ships them, keyed by filename stem.

    Cached because ten prompts read the same six files and the content only
    changes with a deployment. BRN-02 puts the database overrides in front of
    this; the shipped text stays the default underneath, so a fresh install
    thinks correctly on day one.

    Read-only, because the cache hands out the same object every time. The
    caller BRN-02 is about to write is ``merged = shipped(); merged.update(rows)``
    and one client's overrides would become every client's standards for the life
    of the process. Copy it first: ``dict(shipped())``, or use :func:`blocks`.
    """
    root = resources.files("newspulse").joinpath(_BLOCK_DIR)
    found = {
        entry.name.removesuffix(_BLOCK_SUFFIX): entry.read_text("utf-8").strip()
        for entry in sorted(root.iterdir(), key=lambda item: item.name)
        if entry.name.endswith(_BLOCK_SUFFIX)
    }
    return MappingProxyType(found)


def blocks(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Every block and its text. ``source`` defaults to what the repo ships."""
    return dict(shipped() if source is None else source)


def block(key: str, source: Mapping[str, str] | None = None) -> str:
    """One block's text, or :class:`UnknownBlock` if there is no such block."""
    available = shipped() if source is None else source
    try:
        return available[key]
    except KeyError:
        raise UnknownBlock(key, sorted(available)) from None


def declared(text: str) -> tuple[str, ...]:
    """The keys the prompt's ``#blocks:`` header names, in the order written.

    An empty tuple means the prompt declares no standards, which is a legitimate
    thing for a prompt to say and a different thing from having no header at all;
    :func:`has_declaration` tells those two apart.

    Every header, not just the first, because :func:`compose` strips every one.
    Reading only the first would let a second header be removed from the rendered
    prompt while the keys it names went unreported, and the test that holds the
    declaration to the includes would be comparing against half the declaration.
    """
    keys = [
        key.strip()
        for match in _DECLARATION.finditer(text)
        for key in match.group("keys").split(",")
        if key.strip()
    ]
    return tuple(dict.fromkeys(keys))


def has_declaration(text: str) -> bool:
    """Whether the prompt declares its blocks at all, empty list included."""
    return _DECLARATION.search(text) is not None


def included(text: str) -> tuple[str, ...]:
    """The keys the prompt actually includes, in the order they appear.

    A misspelled marker reports the misspelling rather than nothing, because the
    caller that matters is the test holding a prompt's ``#blocks:`` header to its
    includes: a header that silently matches because the typo below it was
    invisible is worse than no header.
    """
    return tuple(dict.fromkeys(_INCLUDE.findall(text)))


def compose(text: str, source: Mapping[str, str] | None = None) -> str:
    """Expand every ``{{brain:key}}`` and drop the ``#blocks:`` header.

    Pure: the same text and the same source always give the same result, and
    nothing here reads the clock or the database. Raises :class:`UnknownBlock`
    for a key that does not resolve *and* for a marker too malformed to name a
    key at all, rather than leaving either in place: a prompt that ships
    ``{{brain:Evidence}}`` to the model as literal text has quietly composed
    without the standard, which is the one outcome this layer exists to prevent.

    Expansion repeats to a fixed point and the header is stripped afterwards, so
    block text obeys the same two rules as prompt text. A block is a file today
    and an editable field from BRN-02 on, and a single pass would let whatever a
    consultant typed into one leak through unread.

    Block text is escaped for ``string.Template``, which runs after this. A
    ``$`` in the prompt itself is the caller's placeholder and stays untouched; a
    ``$`` inside a block is a character a consultant typed, and once BRN-02 makes
    block text editable someone will type a price. Unescaped it would raise a
    ``KeyError`` from ``substitute`` in a call site that has no idea a block was
    involved. Escaping only what each round inserts is what keeps a nested
    expansion from doubling an earlier round's escapes.
    """
    available = shipped() if source is None else source

    def _expand(match: re.Match[str]) -> str:
        return block(match.group("key"), available).replace("$", "$$")

    composed = text
    for _ in range(_MAX_INCLUDE_DEPTH):
        expanded = _INCLUDE.sub(_expand, composed)
        if expanded == composed:
            break
        composed = expanded
    else:
        raise BlockCycle(sorted(set(_INCLUDE.findall(composed))))

    composed = _DECLARATION.sub("", composed)
    malformed = _SUSPECT_INCLUDE.search(composed)
    if malformed is not None:
        raise UnknownBlock(malformed.group(0), sorted(available))
    return composed.strip() + "\n"


def version(source: Mapping[str, str] | None = None) -> str:
    """A stamp that changes when any block's text changes.

    A digest of the resolved block set rather than a counter, because with the
    blocks in files there is nothing to count: the content *is* the version, and
    two checkouts of the same commit should stamp their texts identically.

    Nothing in ``src/`` calls this yet, and that is deliberate rather than
    forgotten: BRN-03 stamps generated text with the standards it was written
    under, and BRN-02 replaces the digest with an incrementing counter once an
    edit in the tool is an event with an author and a time, which is a thing a
    hash cannot represent. That is a return-type change, and it is the reason
    this returns a string rather than an int today. If both stories are dropped,
    delete this function and its tests with them.
    """
    resolved = shipped() if source is None else source
    digest = hashlib.sha256()
    for key in sorted(resolved):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resolved[key].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:_VERSION_DIGITS]


__all__ = [
    "BlockCycle",
    "UnknownBlock",
    "block",
    "blocks",
    "compose",
    "declared",
    "has_declaration",
    "included",
    "shipped",
    "version",
]
