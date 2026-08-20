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
DEC-1's override chain hangs on: the database layer BRN-02 put in front of
:func:`shipped` arrived without any caller learning that it happened.

That chain is the second half of this module. The repository ships the blocks so
a fresh install thinks correctly on day one and git keeps the lineage; any block
can be overridden in the tool, because what good PR looks like is the agency's
judgement and not the developer's. :func:`current` is the resolved set — shipped,
with the stored overrides on top — and it is what a prompt composes against when
no source is named, so an edit takes effect on the next generated text rather
than on the next deployment.

One rule deliberately stayed out of here. ``prose.plain()`` strips dashes from
generated text in code, and it stays in code. The block :file:`blocks/house_style.txt`
*asks* the model not to use them, because asking is worth something, but a rule
that must hold on the output cannot be a sentence a model is asked to follow: it
complies for two paragraphs and then relapses. The ask and the enforcement are
two different mechanisms and this layer is only the first.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable, Mapping
from functools import lru_cache
from importlib import resources
from types import MappingProxyType

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import config
from .db import get_session
from .models import BrainOverride, Setting

_log = logging.getLogger(__name__)

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

#: The settings-table key holding the portfolio-wide brain version: how many
#: recorded changes the standards have been through, across every block. One
#: number for the whole house rather than one per block, so a generated text can
#: name the standards it was written under with a single integer.
_VERSION_KEY = "brain_version"

#: What a change is recorded as when the installation has no named user. The
#: dashboard has one shared Basic-auth credential and no user table, so the
#: honest answer is that a person here made the change — the same word
#: ``ClientFact.filled_by`` uses for a fact a consultant typed himself. Inventing
#: a name nobody supplied would be worse than admitting there is none.
#:
#: The same literal is written out in two other places, and changing it here
#: alone would leave the three disagreeing: ``BrainOverride.edited_by``'s ORM
#: default in ``models.py`` (which cannot import this constant — ``brain``
#: imports ``models``), and the ``server_default`` in
#: ``migrations/versions/0018_brain_overrides.py``, which is frozen history and
#: must keep saying "mensch" whatever this becomes.
_ANONYMOUS_EDITOR = "mensch"

#: Why an empty block is refused, in the words the panel shows. A module
#: constant rather than a literal inside :func:`edit` because it is interface
#: text: the route hands it to the template, so it goes through the same
#: translation lookup as the chrome around it and has an ``i18n._EN`` entry like
#: any other German string the tool renders.
EMPTY_BLOCK_MESSAGE = (
    "Ein Block darf nicht leer sein: ein Prompt, der einen leeren "
    "Maßstab einsetzt, lässt ihn stillschweigend weg."
)


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
    changes with a deployment. The database overrides sit in front of this (see
    :func:`current`); the shipped text stays the default underneath, so a fresh
    install thinks correctly on day one.

    Read-only, because the cache hands out the same object every time. The
    tempting caller is ``merged = shipped(); merged.update(rows)``, and one
    edit would become every reader's standards for the life of the process. Copy
    it first: ``dict(shipped())``, or use :func:`resolved`.
    """
    root = resources.files("newspulse").joinpath(_BLOCK_DIR)
    found = {
        entry.name.removesuffix(_BLOCK_SUFFIX): entry.read_text("utf-8").strip()
        for entry in sorted(root.iterdir(), key=lambda item: item.name)
        if entry.name.endswith(_BLOCK_SUFFIX)
    }
    return MappingProxyType(found)


def blocks(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Every block and its text. ``source`` defaults to :func:`current`."""
    return dict(current() if source is None else source)


def block(key: str, source: Mapping[str, str] | None = None) -> str:
    """One block's text, or :class:`UnknownBlock` if there is no such block."""
    available = current() if source is None else source
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
    block text obeys the same two rules as prompt text. A block is a file the
    repository ships *and* a field a consultant edits, and a single pass would
    let whatever was typed into one leak through unread.

    Block text is escaped for ``string.Template``, which runs after this. A
    ``$`` in the prompt itself is the caller's placeholder and stays untouched; a
    ``$`` inside a block is a character a consultant typed, and sooner or later
    somebody types a price. Unescaped it would raise a ``KeyError`` from
    ``substitute`` in a call site that has no idea a block was involved. Escaping
    only what each round inserts is what keeps a nested expansion from doubling
    an earlier round's escapes.
    """
    available = current() if source is None else source

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


# ---------------------------------------------------------------------------
# The override chain (DEC-1 option C).
#
# Everything above this line is pure and takes its source explicitly. Everything
# below reads a database, because the blocks belong to the people who own the
# judgement rather than to whoever can deploy: files are the default, the tool
# holds the disagreement, and a revert brings the shipped text back exactly.
# ---------------------------------------------------------------------------


def resolved(overrides: Mapping[str, str]) -> dict[str, str]:
    """The shipped blocks with ``overrides`` on top. Pure, and a fresh dict.

    An override for a key that no longer ships stays in the result rather than
    being dropped: it is live text that some prompt may still include, and the
    settings panel shows it as orphaned. Silently discarding it would make a
    renamed block file look like a block that was never overridden.
    """
    return dict(shipped()) | dict(overrides)


def orphaned(overrides: Mapping[str, str]) -> tuple[str, ...]:
    """Override keys with no shipped default behind them any more. Pure.

    A block renamed in the repository leaves its override behind, still resolving
    and no longer visible beside a default. That is the one state in which an
    edit nobody can find stays in force, so it is named rather than hidden.
    """
    return tuple(sorted(set(overrides) - set(shipped())))


def _stored_overrides() -> Mapping[str, str]:
    """The overrides as the database has them, through a short-lived session.

    Read per composition rather than cached. A prompt render happens a handful of
    times per generated text, each of which is a multi-second model call, so the
    cost is a few microseconds of SQLite against a table with one row per edit
    ever made; a cache would buy nothing and would have to be invalidated from
    the one place that must never get it wrong, which is the edit that has just
    been saved and is expected to hold for the next text.

    Fails soft to the shipped set. The reachable case is a database that has not
    been migrated yet, and the shipped blocks are a correct answer there by
    construction — refusing to compose would take the tool down over a table that
    is empty on every fresh install anyway.
    """
    try:
        with get_session() as session:
            return stored(session)
    except SQLAlchemyError as exc:
        _log.warning("brain overrides unreadable, composing the shipped text: %s", exc)
        return {}


#: Where :func:`current` reads the overrides. A seam rather than a direct call so
#: a test can compose against a known set without a database, and so the suite
#: cannot silently start writing a SQLite file into whatever directory it ran in
#: (see ``tests/conftest.py``).
_override_source: Callable[[], Mapping[str, str]] = _stored_overrides


def use_overrides(source: Callable[[], Mapping[str, str]] | None) -> None:
    """Install where :func:`current` reads overrides; ``None`` restores the database."""
    global _override_source
    _override_source = _stored_overrides if source is None else source


def current() -> dict[str, str]:
    """Every block as it stands right now: shipped, with the overrides on top.

    What a prompt composes against when no source is named, which is what makes
    an edit take effect on the next generated text instead of on the next
    deployment.
    """
    return resolved(_override_source())


def latest(session: Session) -> dict[str, BrainOverride]:
    """The newest recorded change per block, whether it set an override or undid one.

    Reverts included, because "this block is the shipped default, and it is that
    because somebody put it back on 3 September" is what the panel has to be able
    to say. :func:`stored` is the same rows with the reverts filtered out.
    """
    newest = (
        select(
            BrainOverride.key.label("key"),
            func.max(BrainOverride.version).label("version"),
        )
        .group_by(BrainOverride.key)
        .subquery()
    )
    rows = session.scalars(
        select(BrainOverride).join(
            newest,
            (BrainOverride.key == newest.c.key)
            & (BrainOverride.version == newest.c.version),
        )
    ).all()
    return {row.key: row for row in rows}


def stored(session: Session) -> dict[str, str]:
    """The override text per block, for the blocks that currently have one."""
    return {
        key: row.text for key, row in latest(session).items() if row.text is not None
    }


def history(session: Session, key: str) -> list[BrainOverride]:
    """Every recorded change to one block, newest first, with its text.

    The texts and not only the dates: "version 6, 3 September, Lucas" says a
    change happened and nothing about what the house believed between then and
    version 7, which is the only question anyone opens a history to answer.
    """
    return list(
        session.scalars(
            select(BrainOverride)
            .where(BrainOverride.key == key)
            .order_by(BrainOverride.version.desc())
        ).all()
    )


def version(session: Session) -> int:
    """The portfolio-wide brain version: how many changes the standards have had.

    Zero on a fresh install, which is a true statement — nothing has been changed
    — and distinguishable from "unknown", which is what BRN-03 renders for a text
    stored before there was anything to stamp.

    Read as the larger of the counter and the highest recorded row. The counter
    is authoritative and both are written in one commit, so they can only differ
    if the table was restored from a dump without the settings row; taking the
    maximum means the next change gets a fresh number instead of colliding with a
    version that already has a text attached to it.
    """
    return max(_counter(session), _highest_version(session))


def editor() -> str:
    """Who a change made through the dashboard is recorded as."""
    return (config.AUTH_USER or "").strip() or _ANONYMOUS_EDITOR


def edit(
    session: Session,
    key: str,
    text: str,
    *,
    edited_by: str = "",
    now: dt.datetime | None = None,
) -> BrainOverride | None:
    """Store an override for one block and bump the brain version once.

    Returns the recorded change, or ``None`` when the text is already what the
    block says — that is not a change, and a version number nobody can point at a
    difference for is worse than no version number.

    Raises ``ValueError`` for an empty or whitespace-only text. A prompt
    composing an empty standard drops it in silence and goes on producing text
    that looks fine, which is the failure this whole layer exists to prevent, so
    the refusal is at the point of the edit rather than at the point of the
    render. Raises :class:`UnknownBlock` for a key that neither ships nor has an
    override *in force*, so a mistyped URL cannot conjure a block no prompt reads.

    "In force" and not "has ever had a row", which is the same question
    :func:`stored` answers and the same one the settings routes' 404 asks. An
    orphan that was reverted still has rows — a revert is recorded, not deleted —
    and admitting it here would let a POST from a tab held open across that revert
    resurrect an override for a block the repository no longer ships, while the
    GET for the same URL answers 404.
    """
    cleaned = _normalise(text)
    if not cleaned:
        raise ValueError(EMPTY_BLOCK_MESSAGE)
    row = latest(session).get(key)
    override = row.text if row is not None else None
    if key not in shipped() and override is None:
        raise UnknownBlock(key, sorted(shipped()))
    if cleaned == (override if override is not None else shipped().get(key)):
        return None
    return _record(session, key, cleaned, edited_by=edited_by, now=now)


def revert(
    session: Session,
    key: str,
    *,
    edited_by: str = "",
    now: dt.datetime | None = None,
) -> BrainOverride | None:
    """Drop a block's override and bump the version again.

    The shipped text comes back exactly, because it was never edited — the file
    is still the file, and the override simply stops being read.

    Recorded as a row of its own rather than by deleting the override, so a
    revert is a change with a date and an author like any other. Returns ``None``
    when there is nothing to revert: a second click on a page held open in
    another tab must not spend a version on a no-op.
    """
    if key not in stored(session):
        return None
    return _record(session, key, None, edited_by=edited_by, now=now)


def _record(
    session: Session,
    key: str,
    text: str | None,
    *,
    edited_by: str,
    now: dt.datetime | None,
) -> BrainOverride:
    """Write one change and its version number in a single commit.

    One commit for both, so the counter and the row it belongs to cannot disagree
    — a bumped counter with no row would leave a version nobody can read the
    standards for.
    """
    change = BrainOverride(
        key=key,
        text=text,
        edited_at=now or dt.datetime.now(dt.UTC),
        edited_by=(edited_by or "").strip() or editor(),
        version=version(session) + 1,
    )
    session.add(change)
    _store_counter(session, change.version)
    session.commit()
    return change


def _normalise(text: str) -> str:
    """A textarea's submission as a block file would hold it.

    Browsers submit ``\\r\\n`` from a textarea and the shipped blocks use ``\\n``.
    Without this every edited block would carry Windows line endings into the
    prompts, where they are invisible in the interface and show up as a diff in
    every golden file the block touches.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _counter(session: Session) -> int:
    """The stored brain version, or 0 if it was never written or is unreadable."""
    setting = session.get(Setting, _VERSION_KEY)
    if setting is None or setting.value is None:
        return 0
    try:
        return int(setting.value)
    except ValueError:
        _log.warning("brain version %r is not a number; treating it as 0", setting.value)
        return 0


def _highest_version(session: Session) -> int:
    """The highest version any recorded change carries, or 0 if there are none."""
    return session.scalar(select(func.max(BrainOverride.version))) or 0


def _store_counter(session: Session, value: int) -> None:
    """Write the brain version without committing; the caller owns the commit."""
    setting = session.get(Setting, _VERSION_KEY)
    if setting is None:
        session.add(Setting(key=_VERSION_KEY, value=str(value)))
    else:
        setting.value = str(value)


__all__ = [
    "BlockCycle",
    "UnknownBlock",
    "block",
    "blocks",
    "compose",
    "current",
    "declared",
    "edit",
    "editor",
    "has_declaration",
    "history",
    "included",
    "latest",
    "orphaned",
    "resolved",
    "revert",
    "shipped",
    "stored",
    "use_overrides",
    "version",
]
