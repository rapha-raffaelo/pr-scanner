"""What the house believes, in one place, so the prompts can read it.

The standards were always here. They were just written out ten times, once per
prompt, in ten slightly different wordings: ``angle.txt`` knew the house tone,
``outreach.txt`` knew it too and said it differently, ``crosscheck.txt`` knew how
to spot a breach of it, and no two of them agreed exactly. Deciding that a thesis
now needs two independent pieces of evidence meant finding four places and getting
all four right, and missing one meant the tool held two standards at once and
reported both as correct.

So a block is a named piece of what the house believes, stored as one text file
under ``brain/``, and a prompt *includes* it rather than restating it::

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
generated text in code, and it stays in code. The block :file:`house_style.txt`
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

#: Where the shipped blocks live, as package data beside ``prompts/``.
_BLOCK_DIR = "brain"
_BLOCK_SUFFIX = ".txt"

#: ``{{brain:key}}``. Keys are lowercase snake_case so a typo is a miss rather
#: than an accidental hit on a different block.
_INCLUDE = re.compile(r"\{\{brain:([a-z0-9_]+)\}\}")

#: ``#blocks: a, b, c`` on a line of its own. Stripped before the prompt is sent:
#: it addresses the person editing the file, not the model reading it.
_DECLARATION = re.compile(r"(?m)^[ \t]*#blocks:[ \t]*(?P<keys>[^\n]*)\n?")

#: Enough hex to make a collision between two block sets a non-worry, short
#: enough to read in a log line or a test failure.
_VERSION_DIGITS = 12


class UnknownBlock(KeyError):
    """A prompt asked for a block that does not exist.

    Loud on purpose, and at render rather than at send. The quiet alternative is
    a prompt that composes without the standard it declared and produces text
    that looks fine, which is the failure this whole layer exists to prevent.
    """

    def __init__(self, key: str, known: list[str]) -> None:
        self.key = key
        self.known = known
        super().__init__(
            f"unknown brain block {key!r}; the shipped blocks are: "
            f"{', '.join(known) or '(none)'}"
        )


@lru_cache(maxsize=1)
def shipped() -> Mapping[str, str]:
    """The blocks as the repository ships them, keyed by filename stem.

    Cached because ten prompts read the same six files and the content only
    changes with a deployment. BRN-02 puts the database overrides in front of
    this; the shipped text stays the default underneath, so a fresh install
    thinks correctly on day one.
    """
    root = resources.files("newspulse").joinpath(_BLOCK_DIR)
    found = {
        entry.name.removesuffix(_BLOCK_SUFFIX): entry.read_text("utf-8").strip()
        for entry in sorted(root.iterdir(), key=lambda item: item.name)
        if entry.name.endswith(_BLOCK_SUFFIX)
    }
    return dict(found)


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
    """
    match = _DECLARATION.search(text)
    if match is None:
        return ()
    return tuple(key.strip() for key in match.group("keys").split(",") if key.strip())


def has_declaration(text: str) -> bool:
    """Whether the prompt declares its blocks at all, empty list included."""
    return _DECLARATION.search(text) is not None


def included(text: str) -> tuple[str, ...]:
    """The keys the prompt actually includes, in the order they appear."""
    return tuple(dict.fromkeys(_INCLUDE.findall(text)))


def compose(text: str, source: Mapping[str, str] | None = None) -> str:
    """Expand every ``{{brain:key}}`` and drop the ``#blocks:`` header.

    Pure: the same text and the same source always give the same result, and
    nothing here reads the clock or the database. Raises :class:`UnknownBlock`
    for a key that does not resolve, rather than leaving the marker in place or
    quietly dropping it.
    """
    available = shipped() if source is None else source

    def _expand(match: re.Match[str]) -> str:
        return block(match.group(1), available)

    return _INCLUDE.sub(_expand, _DECLARATION.sub("", text)).strip() + "\n"


def version(source: Mapping[str, str] | None = None) -> str:
    """A stamp that changes when any block's text changes.

    A digest of the resolved block set rather than a counter, because with the
    blocks in files there is nothing to count: the content *is* the version, and
    two checkouts of the same commit should stamp their texts identically. BRN-02
    introduces the incrementing counter once an edit in the tool is an event with
    an author and a time, which is a thing a hash cannot represent.
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
