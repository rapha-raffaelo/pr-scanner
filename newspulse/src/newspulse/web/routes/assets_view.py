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
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import assets, config, pitch, profile
from ...db import get_session
from ...models import Angle, Asset, Client, ClientFact
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

#: The key under which the package as a whole speaks: a refused second click, or a
#: worker that died before it reached any single format. Not a format key, so it
#: can never collide with one.
_PACKAGE = ""

_BUSY = (
    "Es wird gerade ein Text geschrieben. Der Auftrag wurde nicht angenommen: "
    "warten Sie, bis der laufende steht, sonst wird derselbe Aufruf zweimal "
    "bezahlt."
)


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
        note=_notes.get((angle.id, fmt.key), ""),
    )


def progress_for(client_id: int) -> Progress | None:
    """What this mandate's package is doing right now, if anything."""
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
    return _notes.get((angle_id, _PACKAGE), "")


# --- The worker ----------------------------------------------------------------


def _run_write(client_id: int, angle_id: int, kinds: tuple[str, ...]) -> None:
    """Write these formats in sequence on a worker thread; always let go.

    Holds the sweep's guard as well, so the header's wheel covers the wait and a
    sweep cannot start mid-write and race it on the same articles — the same two
    reasons the impulse and the letter hold it.
    """
    try:
        with _run_guard:
            with get_session() as session:
                client = session.get(Client, client_id)
                angle = session.get(Angle, angle_id)
                if client is None or angle is None or angle.client_id != client_id:
                    return
                _notes.pop((angle_id, _PACKAGE), None)
                for done, key in enumerate(kinds):
                    fmt = assets.definition(key)
                    _progress[client_id] = Progress(
                        angle_id=angle_id, label=fmt.name, done=done, total=len(kinds)
                    )
                    _write_one(session, client, angle, fmt)
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        # Nothing may read as "there was nothing to write": the reader would go on
        # believing the package was complete.
        _notes[(angle_id, _PACKAGE)] = (
            f"Das Paket ist mit einem Fehler abgebrochen: {exc}. "
            "Details stehen im Log."
        )
        _log.exception("writing the package failed")
    finally:
        _progress.pop(client_id, None)
        _generating.release()


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
    key = (angle.id, fmt.key)
    _notes.pop(key, None)

    def _note(reason: str) -> None:
        _notes[key] = reason

    try:
        assets.produce(session, fmt, client, angle, note=_note)
    except (assets.RequirementsMissing, assets.Malformed):
        # Both already handed their sentence to ``_note`` — in the consultant's
        # language, naming the field to fill or what the structure was missing.
        _log.info("%s not written for %r", fmt.key, client.name)
    except Exception as exc:  # noqa: BLE001 — one format must not take the rest down
        _notes[key] = (
            f"{fmt.name} nicht geschrieben: {exc}. Details stehen im Log."
        )
        _log.exception("%s failed for %r", fmt.key, client.name)


def _run_recheck(client_id: int, asset_id: int) -> None:
    """Read one stored text again on a worker thread; always let go.

    On the same lock as writing, because it is the same kind of wait: two model
    calls a person is standing in front of.
    """
    angle_id = 0
    try:
        with _run_guard:
            with get_session() as session:
                asset = session.get(Asset, asset_id)
                client = session.get(Client, client_id)
                if asset is None or client is None or asset.client_id != client_id:
                    return
                angle_id = asset.angle_id
                _notes.pop((angle_id, asset.kind), None)
                assets.recheck(session, client, asset)
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        # Never silent: an edited text stays unreleasable until a check runs, so a
        # check that failed without saying so looks like a broken release button.
        _notes[(angle_id, _PACKAGE)] = (
            f"Die Prüfung ist mit einem Fehler abgebrochen: {exc}. "
            "Details stehen im Log."
        )
        _log.exception("re-checking an edited text failed")
    finally:
        _progress.pop(client_id, None)
        _generating.release()


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


def _start(client_id: int, angle_id: int, kinds: tuple[str, ...]) -> None:
    """Hand the formats to a worker, or record that the lock was taken."""
    if not kinds:
        return
    if not _generating.acquire(blocking=False):
        _notes[(angle_id, kinds[0] if len(kinds) == 1 else _PACKAGE)] = _BUSY
        return
    # Recorded here rather than left to the worker, because the redirect renders
    # immediately: a page built before the thread got as far as saying what it was
    # doing would carry no notice, and then nothing would ever ask again.
    _progress[client_id] = Progress(
        angle_id=angle_id,
        label=assets.definition(kinds[0]).name,
        done=0,
        total=len(kinds),
    )
    threading.Thread(
        target=_run_write,
        args=(client_id, angle_id, kinds),
        daemon=True,
        name=f"newspulse-assets-{angle_id}",
    ).start()


@router.post("/client/{client_id}/impulse/{angle_id}/assets")
def write_assets(
    client_id: int,
    angle_id: int,
    kind: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Write one format off this impulse, or every one that is still missing.

    An empty ``kind`` is "Fehlende schreiben": the formats with no text yet whose
    required facts are on file. A format that is blocked stays blocked rather than
    being attempted and refused six seconds later, and the pane already names the
    field it wants.
    """
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    angle = _impulse_or_404(session, client_id, angle_id)
    if kind and kind not in assets.REGISTRY:
        raise HTTPException(status_code=404, detail="Format not found")
    stored = assets.current(session, [angle_id]).get(angle_id, {})
    if kind:
        existing = stored.get(kind)
        if existing is not None and existing.released:
            _notes[(angle_id, kind)] = assets.RELEASED_IS_FINAL
            return _back(client_id, angle_id)
        kinds: tuple[str, ...] = (kind,)
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
    asset = _asset_or_404(session, client_id, angle_id, asset_id)
    if asset.released:
        _notes[(angle_id, asset.kind)] = assets.RELEASED_IS_FINAL
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
    session: Session = Depends(get_db),
) -> Response:
    """Store the consultant's version of this text.

    No structural validation on the way in, deliberately. A consultant who deletes
    the boilerplate because this release is not going out with one has made an
    editorial decision, and a tool that answered it by refusing to save his work
    would be wrong about the text and about who is accountable for it.
    """
    asset = _asset_or_404(session, client_id, angle_id, asset_id)
    if not body.strip():
        # An empty body is a slip, not an edit: saving it would destroy the text
        # and clear the verdicts that were the only evidence it had been read.
        _notes[(angle_id, asset.kind)] = (
            "Der Text wurde nicht gespeichert: das Feld war leer."
        )
        return _back(client_id, angle_id)
    try:
        assets.edit(session, asset, title=title, body=body)
    except assets.Refused as exc:
        _notes[(angle_id, asset.kind)] = str(exc)
        return _back(client_id, angle_id)
    # The saved text answers whatever the last attempt complained about.
    _notes.pop((angle_id, asset.kind), None)
    return _back(client_id, angle_id)


@router.post("/client/{client_id}/impulse/{angle_id}/asset/{asset_id}/recheck")
def recheck_asset(
    client_id: int,
    angle_id: int,
    asset_id: int,
    session: Session = Depends(get_db),
) -> Response:
    """Have the second model read the edited text, so it can be released."""
    asset = _asset_or_404(session, client_id, angle_id, asset_id)
    if asset.released:
        _notes[(angle_id, asset.kind)] = assets.RELEASED_IS_FINAL
        return _back(client_id, angle_id)
    if not _generating.acquire(blocking=False):
        _notes[(angle_id, asset.kind)] = _BUSY
        return _back(client_id, angle_id)
    # Before the thread, for the reason ``_start`` records it before its own.
    _progress[client_id] = Progress(
        angle_id=angle_id,
        label=assets.definition(asset.kind).name,
        done=0,
        total=1,
    )
    threading.Thread(
        target=_run_recheck,
        args=(client_id, asset_id),
        daemon=True,
        name=f"newspulse-recheck-{asset_id}",
    ).start()
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
        _notes[(angle_id, asset.kind)] = str(exc)
    return _back(client_id, angle_id)


__all__ = [
    "Package",
    "Progress",
    "Slot",
    "package",
    "package_note",
    "progress_for",
    "router",
]
