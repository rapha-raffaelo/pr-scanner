"""Start a background worker, or undo the bookkeeping that assumed it started.

Five routes hand work to a thread, and all five take a lock in the *request*
thread first and rely on the worker's ``finally`` to give it back. That is the
right shape and it has one hole: if ``Thread.start()`` raises, there is no
worker, so nothing ever releases. The lock is module-level and the process is
long-lived, which means every later impulse, message, research run, sweep and
theme proposal is refused until somebody restarts the app — with the interface
saying, correctly and uselessly, that one is already running.

One of the five had the guard and its comment named the consequence exactly.
This is that guard, once, so the next route to spawn something inherits it
instead of rediscovering it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence

_log = logging.getLogger(__name__)


def start_or_release(
    target: Callable[..., object],
    *,
    args: Sequence[object] = (),
    name: str,
    release: Callable[[], None],
) -> None:
    """Start ``target`` on a daemon thread; on failure run ``release`` and re-raise.

    ``release`` is the caller's own undo, not a bare ``lock.release()``: a route
    that also recorded "a run is in progress" has to take that back too, or the
    page keeps announcing work that no longer exists.

    ``BaseException`` deliberately. ``RuntimeError: can't start new thread`` is
    the expected one, but a ``MemoryError`` at the same moment leaves the same
    stuck lock, and there is nothing here that a narrower catch would protect.
    """
    try:
        threading.Thread(target=target, args=tuple(args), daemon=True, name=name).start()
    except BaseException:
        _log.exception("could not start %s; releasing what it would have held", name)
        release()
        raise
