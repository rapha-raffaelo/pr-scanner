"""The package on an impulse: writing its texts, editing them, releasing them.

DEC-1 locks the surface as "Ein Anlass, ein Paket". A communication occasion is
handled as a package rather than as a folder of separate documents, so the
formats are a strip on the impulse itself: which are written, which are checked,
which are released, which do not exist yet, in one line. The impulse keeps
everything it produced, and "Fehlende schreiben" is one button.

What lives here rather than in :mod:`newspulse.web.routes.advisory` is everything
that acts on a stored text. The impulse page is already the longest route module
in the app, and the four acts below share a lock, a progress record and a set of
refusals that nothing else on that page touches. The page itself still renders
from ``advisory``: it asks :func:`package` for the strip and gets back a view of
the registry crossed with what is stored.

Editing is the reason this is not the letter's surface with six more buttons. A
pitch is read once and copied; a press release is worked on. So an edit is
stored, marked as a human's, and drops both verdicts on the way, and the release
control stays out of reach until a second model has read the text as it now
stands. A text a human changed after the check was cleared is a text nothing has
read, and grade F does not bend for the convenience of one click.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import assets, config, pitch, profile
from ...db import get_session
from ...models import Angle, Asset, CheckState, Client, ClientFact
from ..app import get_db
from ..runlock import guard as _run_guard

router = APIRouter()

_log = logging.getLogger(__name__)
_SEE_OTHER = 303

# One text at a time, process-wide, for the same reason the impulse and the letter
# take one each: writing a format shells out to a model, and six in sequence off
# one button is minutes of paid calls. Non-blocking at the call site, so a second
# request is refused with a sentence rather than queued behind the first — a
# queue here would spend a second round of calls on a package somebody clicked
# twice because nothing had repainted yet.
_generating = threading.Lock()


@dataclass(frozen=True, slots=True)
class Progress:
    """What the worker is doing right now, for the notice on the page.

    In memory and per client, like the impulse's refusal beside it: it describes
    one click rather than the mandate, and going stale on a restart is the correct
    behaviour for a "what is happening" message.
    """

    angle_id: int
    #: The format being written, by name.
    label: str
    #: How many of this run's formats are finished, and how many were asked for.
    done: int
    total: int


_progress: dict[int, Progress] = {}

# What the last attempt on one format had to say, keyed by impulse and format
# key. In memory and deliberately not a schema change, like the impulse's refusal
# next to it: it describes one click rather than the mandate, and going stale on a
# restart is the correct behaviour for a "what just happened" message.
_notes: dict[tuple[int, str], str] = {}

# Both dicts are written from the worker threads and read by request threads. The
# individual operations are atomic under the GIL, but the trim below iterates, and
# an iteration racing an insert raises "dictionary changed size during iteration"
# on the page rather than in the worker. One lock over both, held for a dict
# operation at a time, is cheaper than reasoning about which ones are safe.
_state = threading.Lock()

#: How many *impulses* keep their notes. Counted in impulses rather than in notes
#: because the notes of one impulse are bounded already — one per format plus the
#: package's own — while impulses are made daily for the lifetime of the process.
#: A flat cap on notes trimmed in insertion order, which is what this was, let one
#: busy package of six formats evict a different mandate's fresh refusal off the
#: page it was still being read on.
_MAX_NOTE_ANGLES = 40

#: The key under which the package as a whole speaks: a refused second click, or a
#: worker that died before it reached any single format. Not a format key, so it
#: can never collide with one.
_PACKAGE = ""

_BUSY = (
    "Es wird gerade ein Text geschrieben. Der Auftrag wurde nicht angenommen: "
    "warten Sie, bis der laufende steht, sonst wird derselbe Aufruf zweimal "
    "bezahlt."
)

#: What the package says when the daily sweep holds the run guard. Refused rather
#: than waited out: the guard was taken blocking here and nothing else in the app
#: takes it that way, so a click during a sweep sat on it for the length of a full
#: run with every button out and a progress notice naming a format that nothing
#: was writing. A sentence the reader can act on is the honest version of that.
_SWEEP_RUNNING = (
    "Es läuft gerade ein Sammellauf. Der Auftrag wurde nicht angenommen: "
    "warten Sie, bis er durch ist, und klicken Sie dann noch einmal."
)


def _remember(angle_id: int, key: str, reason: str) -> None:
    """Record what the last attempt on one format had to say, and stay bounded."""
    with _state:
        # Popped before it is set, so a rewritten key moves to the end: dicts keep
        # insertion order, and a key updated in place would otherwise keep the
        # position of its first write and be evicted while it is the freshest
        # thing in here.
        _notes.pop((angle_id, key), None)
        _notes[(angle_id, key)] = reason
        _trim()


def _trim() -> None:
    """Drop the impulses nobody has heard from in a while. Caller holds ``_state``.

    Newest first, keeping the formats of the most recently spoken-for impulses
    together: a package that says six things about itself may cost older impulses
    their notes, never its neighbour's freshest one.
    """
    keep: list[int] = []
    for angle_id, _key in reversed(_notes):
        if angle_id not in keep:
            if len(keep) == _MAX_NOTE_ANGLES:
                break
            keep.append(angle_id)
    for stale in [key for key in _notes if key[0] not in keep]:
        del _notes[stale]


def _forget(angle_id: int, key: str) -> None:
    """Drop what one format had to say, because it has been answered."""
    with _state:
        _notes.pop((angle_id, key), None)


def _said(angle_id: int, key: str) -> str:
    """What one format had to say, if anything."""
    with _state:
        return _notes.get((angle_id, key), "")


# --- What the page reads -------------------------------------------------------


#: What each state is called on the page. Here rather than in the template so the
#: strings are one list a translator can find, and so a new state cannot ship as a
#: raw key like "ungeschrieben" in front of a reader.
_STATE_LABELS = {
    assets.AssetState.UNGESCHRIEBEN: "Nicht geschrieben",
    assets.AssetState.ENTWURF: "Entwurf",
    assets.AssetState.GEPRUEFT: "Geprüft",
    assets.AssetState.FREIGEGEBEN: "Freigegeben",
}


@dataclass(frozen=True, slots=True)
class Remedy:
    """Where to go and fill the field a refusal names.

    A refusal that names a cause has to carry its remedy: the same lesson the
    impulse's "propose themes" button was added for, where the fix sat one page
    away and the report came back three times.
    """

    #: The path under ``/client/{id}/``.
    path: str
    label: str


#: Which page holds a missing input, by where the requirement reads it from. The
#: impulse's own fields and the recipient are not on this list on purpose: neither
#: is something a person fills in, so a link would promise a form that is not there.
_REMEDIES = {
    assets.Source.PROFIL: Remedy("profil", "Profil ergänzen"),
    assets.Source.MANDAT: Remedy("guide", "Guide hinterlegen"),
}


@dataclass(frozen=True, slots=True)
class Slot:
    """One format on one impulse, as the strip and its pane read it."""

    fmt: assets.FormatDef
    #: The text that stands for this format, or ``None`` when none was written.
    asset: Asset | None
    state: assets.AssetState
    #: What this format is still missing before it may be written at all.
    readiness: assets.Readiness
    #: What the last attempt said, whether it refused or only went unchecked.
    note: str = ""

    @property
    def key(self) -> str:
        return self.fmt.key

    @property
    def state_label(self) -> str:
        return _STATE_LABELS[self.state]

    @property
    def hint(self) -> str:
        """What this text still needs before it can be released, if anything.

        The sentence is :mod:`newspulse.assets`'s own, so the page and the route
        that refuses the release cannot come to say different things about the
        same rule.
        """
        return assets.UNREAD_SINCE_EDIT if self.needs_recheck else ""

    @property
    def remedy(self) -> Remedy | None:
        """Where to fill the first missing input, when a page holds it."""
        for req in self.readiness.missing:
            found = _REMEDIES.get(req.source)
            if found is not None:
                return found
        return None

    @property
    def objected(self) -> bool:
        """Whether a checker had something to say. Drawn apart from the state:
        an objected draft is still a draft, but it is not one to copy blind."""
        return self.asset is not None and not (
            self.asset.review_ok and self.asset.guide_review_ok
        )

    @property
    def needs_recheck(self) -> bool:
        return self.asset is not None and assets.needs_recheck(self.asset)

    @property
    def version(self) -> str:
        """Which text the edit panel was opened on, for the form to post back.

        Not the row id: :func:`newspulse.assets.store` reuses the row when it
        replaces a draft, so the id is exactly what a stale form and a fresh one
        have in common.
        """
        return assets.version(self.asset) if self.asset is not None else ""

    @property
    def unchecked(self) -> bool:
        """Whether nothing has read this text, released or not.

        Drawn beside the release control rather than only in the verdict block
        below it: releasing is the act grade F is about, and "no second model
        looked at this" is the one thing the person clicking it has to know at the
        moment of clicking.
        """
        row = self.asset
        return row is not None and row.check_state is CheckState.UNGEPRUEFT

    @property
    def recheckable(self) -> bool:
        """Whether "Gegenlesen lassen" may be offered at all.

        Any unreleased text nothing has read, not only an edited one. The narrower
        rule left a draft whose crosscheck went down — the exact case
        :func:`newspulse.assets.produce`'s fault isolation exists to keep — with
        only "Neu schreiben" as a way back to a checked state, which spends a
        second full model call to throw away a perfectly good text.
        """
        return (
            self.asset is not None
            and not self.asset.released
            and self.asset.check_state is CheckState.UNGEPRUEFT
        )

    @property
    def releasable(self) -> bool:
        return self.asset is not None and assets.releasable(self.asset)

    @property
    def writable(self) -> bool:
        """Whether the button may be offered at all.

        A released text is finished, and a format whose required facts are not on
        file is DEC-2's refusal: the button says why rather than writing a text
        with a name nobody supplied.
        """
        if self.asset is not None and self.asset.released:
            return False
        return self.readiness.ok


@dataclass(frozen=True, slots=True)
class Package:
    """Everything one occasion has produced, and everything it has not."""

    angle_id: int
    slots: tuple[Slot, ...]
    #: The letters written off this impulse. They keep their own table and their
    #: own recipient, and the strip counts them rather than owning them.
    letters: int

    @property
    def written(self) -> int:
        """How many of the seven exist. The letter counts once, however many
        recipients it was written at: the strip is about formats."""
        return sum(1 for slot in self.slots if slot.asset is not None) + bool(
            self.letters
        )

    @property
    def total(self) -> int:
        """Every format on offer, the letter included: what "4 von 7" counts."""
        return len(self.slots) + 1

    @property
    def released(self) -> int:
        return sum(
            1 for slot in self.slots if slot.state is assets.AssetState.FREIGEGEBEN
        )

    @property
    def missing(self) -> tuple[str, ...]:
        """The formats "Fehlende schreiben" would write: unwritten, and writable."""
        return tuple(
            slot.key for slot in self.slots if slot.asset is None and slot.writable
        )


def package(
    session: Session,
    client: Client,
    angles: list[Angle],
    targets: dict[int, list[pitch.PitchTarget]],
    letters: dict[int, list],
) -> dict[int, Package]:
    """The strip for every impulse on the page, keyed by impulse.

    ``targets`` and ``letters`` are what the page already computed, handed in
    rather than fetched again: the recipient lookup behind one briefing's
    readiness is several queries plus a contact lookup per candidate, and the page
    runs it once per impulse already.
    """
    facts = profile.stored(session, client.id)
    stored = assets.current(session, [angle.id for angle in angles])
    packages: dict[int, Package] = {}
    for angle in angles:
        # The impulse's best-evidenced recipient, which is what the one format
        # written *at* somebody needs before it may be written. ``None`` is an
        # answer here, not a missing lookup: the coverage named nobody.
        found = targets.get(angle.id) or []
        packages[angle.id] = Package(
            angle_id=angle.id,
            slots=tuple(
                _slot(
                    session, client, angle, fmt,
                    stored.get(angle.id, {}).get(fmt.key),
                    facts,
                    found[0] if found else None,
                )
                for fmt in assets.FORMATS
            ),
            letters=len(letters.get(angle.id, [])),
        )
    return packages


def _slot(
    session: Session,
    client: Client,
    angle: Angle,
    fmt: assets.FormatDef,
    row: Asset | None,
    facts: dict[str, ClientFact],
    target: pitch.PitchTarget | None,
) -> Slot:
    return Slot(
        fmt=fmt,
        asset=row,
        state=assets.state_of(row),
        readiness=assets.requirements_met(
            session, fmt, client, angle, facts=facts, target=target
        ),
        note=_said(angle.id, fmt.key),
    )


def page_context(
    session: Session,
    client: Client,
    angles: list[Angle],
    targets: dict[int, list[pitch.PitchTarget]],
    letters: dict[int, list],
) -> dict[str, object]:
    """Everything the impulse page needs about the package, as one merge.

    One call rather than four, so ``advisory`` does not have to know that a strip,
    a progress record, a lock and a note are separate things here — and so a fifth
    of them arrives without that route changing.
    """
    return {
        "packages": package(session, client, angles, targets, letters),
        "asset_progress": progress_for(client.id),
        "asset_busy": busy(),
        "asset_notes": {angle.id: package_note(angle.id) for angle in angles},
    }


def progress_for(client_id: int) -> Progress | None:
    """What this mandate's package is doing right now, if anything."""
    with _state:
        return _progress.get(client_id)


def busy() -> bool:
    """Whether a text is being written anywhere in this process.

    Process-wide rather than per mandate because the lock is: a button that stayed
    live while another mandate held it would take the click and refuse it, and the
    two are the same sentence to the reader either way.
    """
    return _generating.locked()


def package_note(angle_id: int) -> str:
    """What the package as a whole has to say, if anything."""
    return _said(angle_id, _PACKAGE)


# --- The worker ----------------------------------------------------------------


def _announce(client_id: int, progress: Progress) -> None:
    """Say what the worker is doing now, for the notice on the page."""
    with _state:
        _progress[client_id] = progress


def _done(client_id: int) -> None:
    """The end of one run, whichever way it ended: the notice goes and the lock
    is let go. One place, so a second worker cannot forget half of it."""
    with _state:
        _progress.pop(client_id, None)
    _generating.release()


@contextmanager
def _sweep_free() -> Iterator[bool]:
    """The sweep's guard, taken without waiting, and let go however this ends.

    Non-blocking like every other caller of that lock. Held blocking, a click
    during a sweep queued behind a job that fetches 40+ feeds and shells out per
    batch, while the page said "Wird geschrieben: Pressemitteilung" and kept every
    button out for the whole of it. The reason for taking the guard at all is
    unchanged: the header's wheel covers the wait, and a sweep must not start
    mid-write and race it on the same articles.
    """
    taken = _run_guard.acquire(blocking=False)
    try:
        yield taken
    finally:
        if taken:
            _run_guard.release()


def _speaks_for(kinds: tuple[str, ...]) -> str:
    """Where a refusal about this run belongs: one format speaks in its own pane,
    a whole package speaks for itself."""
    return kinds[0] if len(kinds) == 1 else _PACKAGE


def _run_write(client_id: int, angle_id: int, kinds: tuple[str, ...]) -> None:
    """Write these formats in sequence on a worker thread; always let go."""
    try:
        with _sweep_free() as free:
            if not free:
                _remember(angle_id, _speaks_for(kinds), _SWEEP_RUNNING)
                return
            with get_session() as session:
                client = session.get(Client, client_id)
                angle = session.get(Angle, angle_id)
                if client is None or angle is None or angle.client_id != client_id:
                    return
                _forget(angle_id, _PACKAGE)
                for done, key in enumerate(kinds):
                    fmt = assets.definition(key)
                    _announce(
                        client_id,
                        Progress(
                            angle_id=angle_id,
                            label=fmt.name,
                            done=done,
                            total=len(kinds),
                        ),
                    )
                    _write_one(session, client, angle, fmt)
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        # Nothing may read as "there was nothing to write": the reader would go on
        # believing the package was complete.
        _remember(
            angle_id,
            _PACKAGE,
            f"Das Paket ist mit einem Fehler abgebrochen: {exc}. "
            "Details stehen im Log.",
        )
        _log.exception("writing the package failed")
    finally:
        _done(client_id)


def _write_one(
    session: Session, client: Client, angle: Angle, fmt: assets.FormatDef
) -> None:
    """One format, with whatever it had to say kept beside it.

    A failure here is contained on purpose: :func:`newspulse.assets.produce`
    stores nothing when the write refused, so the text that was already on file
    for this format stands untouched and the pane says why the new one did not
    arrive. Six formats in sequence must not lose five because the third one had
    no spokesperson.
    """
    _forget(angle.id, fmt.key)
    said = False

    def _tell(reason: str) -> None:
        nonlocal said
        said = True
        _remember(angle.id, fmt.key, reason)

    try:
        assets.produce(session, fmt, client, angle, note=_tell)
    except (assets.RequirementsMissing, assets.Malformed):
        # Both already handed their sentence to ``_tell`` — in the consultant's
        # language, naming the field to fill or what the structure was missing.
        # Neither reaches the database, so the session is still clean.
        _log.info("%s not written for %r", fmt.key, client.name)
    except Exception as exc:  # noqa: BLE001 — one format must not take the rest down
        # A caught exception is not a clean session. ``produce`` ends in
        # ``store`` -> ``commit``, and a commit that fails — the partial unique
        # index, or SQLite's "database is locked" against the cron sweep, which
        # runs in another process and is not covered by the run guard — leaves the
        # transaction in PendingRollbackError. Without this every later format in
        # this run dies on the poisoned session, which is exactly the failure this
        # handler exists to prevent. Same fix as job.py:1329.
        session.rollback()
        _remember(
            angle.id,
            fmt.key,
            f"{fmt.name} nicht geschrieben: {exc}. Details stehen im Log.",
        )
        _log.exception("%s failed for %r", fmt.key, client.name)
    else:
        if not said:
            # The forget at the top of this function is not enough. A click on a
            # format while that same format is being written records the "busy"
            # refusal *after* that opening forget, so without this the refusal is
            # still standing beside the text the run went on to write.
            _forget(angle.id, fmt.key)


def _run_recheck(client_id: int, angle_id: int, asset_id: int) -> None:
    """Read one stored text again on a worker thread; always let go.

    On the same lock as writing, because it is the same kind of wait: two model
    calls a person is standing in front of.

    ``angle_id`` is handed in rather than read off the row, because it is where
    the failure below has to be written and the row is exactly what may not be
    reachable when it fails. It used to start at a sentinel 0 and be filled in
    after the load succeeded, so a database that was down wrote its explanation
    under a key no page ever asks for.
    """
    try:
        with _sweep_free() as free:
            if not free:
                # Under the package's own key: the row that would name the format
                # is exactly what has not been loaded yet.
                _remember(angle_id, _PACKAGE, _SWEEP_RUNNING)
                return
            with get_session() as session:
                asset = session.get(Asset, asset_id)
                client = session.get(Client, client_id)
                if asset is None or client is None or asset.client_id != client_id:
                    return
                _forget(angle_id, asset.kind)
                # ``recheck`` records a check that could not run on the row itself
                # and returns; what reaches here is the session breaking under it.
                assets.recheck(session, client, asset)
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        # Never silent: an edited text stays unreleasable until a check runs, so a
        # check that failed without saying so looks like a broken release button.
        _remember(
            angle_id,
            _PACKAGE,
            f"Die Prüfung ist mit einem Fehler abgebrochen: {exc}. "
            "Details stehen im Log.",
        )
        _log.exception("re-checking an edited text failed")
    finally:
        _done(client_id)


# --- The routes ----------------------------------------------------------------


def _back(client_id: int, angle_id: int) -> RedirectResponse:
    """Back to the impulse the package hangs on, not to the top of the page."""
    return RedirectResponse(
        f"/client/{client_id}/advice#impulse-{angle_id}", status_code=_SEE_OTHER
    )


def _impulse_or_404(session: Session, client_id: int, angle_id: int) -> Angle:
    """The impulse, and that it belongs to this mandate.

    Both ids come off the URL, so without the pair check one mandate's page could
    write a press release onto another's position.
    """
    angle = session.get(Angle, angle_id)
    if angle is None or angle.client_id != client_id:
        raise HTTPException(status_code=404, detail="Impulse not found")
    return angle


def _asset_or_404(
    session: Session, client_id: int, angle_id: int, asset_id: int
) -> Asset:
    """The text, and that it belongs to this mandate and this impulse."""
    asset = session.get(Asset, asset_id)
    if asset is None or asset.client_id != client_id or asset.angle_id != angle_id:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


def _writable_asset_or_404(
    session: Session, client_id: int, angle_id: int, asset_id: int
) -> Asset:
    """The text, and that something still knows how to write and check its format.

    ``Asset.kind`` is a plain string column with no CHECK constraint, so a row can
    outlive the definition it was written against — a format retired from the
    registry, a hand-inserted row. Both acts below go straight to
    ``assets.definition``, which is loud on a kind it does not know, and the same
    404 ``write_assets`` gives for an unknown ``kind`` is the honest answer here.
    """
    asset = _asset_or_404(session, client_id, angle_id, asset_id)
    if asset.kind not in assets.REGISTRY:
        raise HTTPException(status_code=404, detail="Format not found")
    return asset


def _spawn(
    target: Callable[..., None],
    args: tuple[object, ...],
    *,
    client_id: int,
    angle_id: int,
    kinds: tuple[str, ...],
    name: str,
    refuse_under: str,
) -> None:
    """Take the lock, say what is about to happen, and hand it to a worker.

    One function for both acts, because the sequence has three steps that must
    stay in this order and a second hand-written copy of it drifts. ``kinds`` is
    what the run will write, in order: its first format names the notice, and its
    length is what the "3 von 6" counts.
    """
    # Looked up before the lock is taken, and that order is the whole point.
    # ``definition`` is loud on a kind the registry does not know — deliberately,
    # since ``Asset.kind`` is a plain string column with no CHECK constraint — and
    # only the worker releases ``_generating``. A raise between the acquire and
    # the thread therefore wedged the lock for the life of the process: every
    # write, re-write and re-check for every mandate refused until a restart.
    label = assets.definition(kinds[0]).name
    if not _generating.acquire(blocking=False):
        _remember(angle_id, refuse_under, _BUSY)
        return
    # Recorded here rather than left to the worker, because the redirect renders
    # immediately: a page built before the thread got as far as saying what it was
    # doing would carry no notice, and then nothing would ever ask again.
    _announce(
        client_id,
        Progress(angle_id=angle_id, label=label, done=0, total=len(kinds)),
    )
    try:
        threading.Thread(target=target, args=args, daemon=True, name=name).start()
    except BaseException:
        # The worker is the only thing that releases the lock, so a thread that
        # never started would hold it for the life of the process and refuse every
        # write and every re-check until a restart.
        _done(client_id)
        raise


def _start(client_id: int, angle_id: int, kinds: tuple[str, ...]) -> None:
    """Hand the formats to a worker, or record that the lock was taken."""
    if not kinds:
        return
    _spawn(
        _run_write,
        (client_id, angle_id, kinds),
        client_id=client_id,
        angle_id=angle_id,
        kinds=kinds,
        name=f"newspulse-assets-{angle_id}",
        refuse_under=_speaks_for(kinds),
    )


@router.post("/client/{client_id}/impulse/{angle_id}/assets")
def write_assets(
    client_id: int,
    angle_id: int,
    kind: list[str] = Form(default_factory=list),
    session: Session = Depends(get_db),
) -> Response:
    """Write the formats named off this impulse, or every one still missing.

    A list rather than one name, because the page now asks with tick boxes: the
    reader says which of the six he actually wants written and presses once. One
    box ticked posts one ``kind`` and behaves exactly as the old single-format
    button did, so nothing about the per-format action changed.

    An empty list is still "Fehlende schreiben": the formats with no text yet
    whose required facts are on file. A format that is blocked stays blocked
    rather than being attempted and refused six seconds later, and its row names
    the field it wants.

    A released format is dropped from the selection rather than refusing the
    whole click — with six boxes on screen, one stale tick must not cost the five
    beside it, and the note it leaves says which one was skipped.
    """
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    angle = _impulse_or_404(session, client_id, angle_id)
    # A form that posts ``kind=`` means "the missing ones", and did so before this
    # took a list. Unfiltered it now arrives as [""], which is neither a format
    # nor an empty selection.
    kind = [name for name in kind if name.strip()]
    for name in kind:
        if name not in assets.REGISTRY:
            raise HTTPException(status_code=404, detail="Format not found")
    stored = assets.current(session, [angle_id]).get(angle_id, {})
    if kind:
        wanted = []
        for name in kind:
            existing = stored.get(name)
            if existing is not None and existing.released:
                _remember(angle_id, name, assets.RELEASED_IS_FINAL)
                continue
            wanted.append(name)
        if not wanted:
            return _back(client_id, angle_id)
        kinds: tuple[str, ...] = tuple(dict.fromkeys(wanted))
    else:
        kinds = _missing(session, client, angle, stored)
    _start(client_id, angle_id, kinds)
    return _back(client_id, angle_id)


def _missing(
    session: Session, client: Client, angle: Angle, stored: dict[str, Asset]
) -> tuple[str, ...]:
    """The formats "Fehlende schreiben" writes: unwritten, and writable today.

    The profile is read once for all six rather than once per format, which is
    what ``requirements_met`` takes ``facts`` for.
    """
    facts = profile.stored(session, client.id)
    return tuple(
        fmt.key
        for fmt in assets.FORMATS
        if fmt.key not in stored
        and assets.requirements_met(session, fmt, client, angle, facts=facts).ok
    )


@router.post("/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/rewrite")
def rewrite_asset(
    client_id: int,
    angle_id: int,
    asset_id: int,
    session: Session = Depends(get_db),
) -> Response:
    """Write this format again, replacing the draft that stands.

    Refused on a released text, which is the letter's rule arriving here: the
    released text is the record of what went out, and a record that a later click
    can overwrite is not one.
    """
    asset = _writable_asset_or_404(session, client_id, angle_id, asset_id)
    if asset.released:
        _remember(angle_id, asset.kind, assets.RELEASED_IS_FINAL)
        return _back(client_id, angle_id)
    _start(client_id, angle_id, (asset.kind,))
    return _back(client_id, angle_id)


@router.post("/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/edit")
def edit_asset(
    client_id: int,
    angle_id: int,
    asset_id: int,
    title: str = Form(""),
    body: str = Form(""),
    seen: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Store the consultant's version of this text.

    No structural validation on the way in, deliberately. A consultant who deletes
    the boilerplate because this release is not going out with one has made an
    editorial decision, and a tool that answered it by refusing to save his work
    would be wrong about the text and about who is accountable for it.

    ``seen`` is which text the panel was opened on. A re-write replaces the draft
    in place, so an edit panel left open across one posts back to the same row and
    would put the words on that reader's screen over the ones the model has since
    written — and stamp them ``edited_at``, which reads as a human's approval of a
    text no human has seen. The poller already refuses to swap an open panel away;
    this is the other half, for the post it cannot see coming.
    """
    asset = _asset_or_404(session, client_id, angle_id, asset_id)
    if not body.strip():
        # An empty body is a slip, not an edit: saving it would destroy the text
        # and clear the verdicts that were the only evidence it had been read.
        _remember(
            angle_id, asset.kind, "Der Text wurde nicht gespeichert: das Feld war leer."
        )
        return _back(client_id, angle_id)
    try:
        assets.edit(session, asset, title=title, body=body, seen=seen)
    except assets.Refused as exc:
        _remember(angle_id, asset.kind, str(exc))
        return _back(client_id, angle_id)
    # The saved text answers whatever the last attempt complained about.
    _forget(angle_id, asset.kind)
    return _back(client_id, angle_id)


@router.post("/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/recheck")
def recheck_asset(
    client_id: int,
    angle_id: int,
    asset_id: int,
    session: Session = Depends(get_db),
) -> Response:
    """Have the second model read the text again, so it can be released."""
    asset = _writable_asset_or_404(session, client_id, angle_id, asset_id)
    if asset.released:
        _remember(angle_id, asset.kind, assets.RELEASED_IS_FINAL)
        return _back(client_id, angle_id)
    _spawn(
        _run_recheck,
        (client_id, angle_id, asset_id),
        client_id=client_id,
        angle_id=angle_id,
        kinds=(asset.kind,),
        name=f"newspulse-recheck-{asset_id}",
        refuse_under=asset.kind,
    )
    return _back(client_id, angle_id)


@router.post("/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/release")
def release_asset(
    client_id: int,
    angle_id: int,
    asset_id: int,
    session: Session = Depends(get_db),
) -> Response:
    """Freigabe: the human act, recorded the way the outreach ledger records it.

    Refused on a text that was edited after its last check. The control is not
    offered in that state either, and this is the second half of the same rule:
    a form can be posted from a page that was rendered before the edit.
    """
    asset = _asset_or_404(session, client_id, angle_id, asset_id)
    try:
        assets.release(session, asset, by=config.AUTH_USER)
    except assets.Refused as exc:
        _remember(angle_id, asset.kind, str(exc))
    return _back(client_id, angle_id)


__all__ = [
    "Package",
    "Progress",
    "Slot",
    "busy",
    "package",
    "package_note",
    "page_context",
    "progress_for",
    "router",
]
