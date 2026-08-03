"""Proposing and measuring themes on a worker thread, shared by two pages.

The work is a model call plus one live search per proposal — far too long to hold
a form submission open, so it runs in the background and the page polls.

It lives here rather than in a route module because two pages need it, and for the
same reason: the settings view is where a mandate is configured, but the impulse
page is where the *consequence* of bad themes appears. A reader who has just been
told "the radar found no market news" is holding the question this answers, and
sending them to another screen to act on it is how a remedy goes unused. Both
views drive the same job and read the same result.
"""

from __future__ import annotations

import logging
import threading

from .. import themes
from ..db import get_session
from ..models import Client

_log = logging.getLogger(__name__)

# One proposal at a time, process-wide: each spends a model call and up to eight
# searches, and two clicks would buy the same answer twice.
lock = threading.Lock()

# client_id -> {"state": "läuft" | "fertig" | "fehler", ...}. In memory on purpose:
# it describes one click, and forgetting it on restart is correct.
state: dict[int, dict[str, object]] = {}


def _work(client_id: int) -> None:
    try:
        with get_session() as session:
            client = session.get(Client, client_id)
            if client is None:
                state.pop(client_id, None)
                return
            proposals = themes.suggest(client)
            state[client_id] = {
                "state": "fertig",
                "client": client.name,
                "probes": themes.probe(client, proposals),
            }
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        # A failed proposal must not read as "this field has no themes".
        state[client_id] = {"state": "fehler", "error": str(exc)}
        _log.exception("theme suggestion for client %s failed", client_id)
    finally:
        lock.release()


def start(session, client_id: int) -> bool:
    """Begin proposing themes for one client. False if one is already running."""
    client = session.get(Client, client_id)
    if client is None or not lock.acquire(blocking=False):
        return False
    state[client_id] = {"state": "läuft", "client": client.name}
    threading.Thread(
        target=_work,
        args=(client_id,),
        daemon=True,
        name=f"newspulse-themes-{client_id}",
    ).start()
    return True


def running_for(client_id: int) -> bool:
    return state.get(client_id, {}).get("state") == "läuft"


__all__ = ["lock", "running_for", "start", "state"]
