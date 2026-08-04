"""Proposals computed on a worker thread: themes, and competitors.

Both spend a `claude -p` call — tens of seconds, sometimes a minute or two — and
the theme proposal spends a live search per candidate on top. Held open inside a
request that reads as a page that has stopped working; the operator's words for
the competitor version were "lädt extrem schwer und langsam — das funktioniert im
Grunde gar nicht". So they run in the background and the page polls.

It lives here rather than in a route module because more than one page needs it:
the settings view is where a mandate is configured, but the impulse page is where
the *consequence* of bad themes appears, and a reader who has just been told "the
radar found no market news" is holding the question the theme proposal answers.
Sending them to another screen to act on it is how a remedy goes unused.
"""

from __future__ import annotations

import logging
import threading

from .. import industry, rivals, themes
from ..db import get_session
from ..models import Client

_log = logging.getLogger(__name__)

class Proposal:
    """One kind of background proposal: its lock, its results, its worker.

    One at a time per kind, process-wide: each spends a model call, and two
    clicks would buy the same answer twice. The results live in memory on
    purpose — they describe one click, and forgetting them on restart is right.
    """

    def __init__(self, name: str, produce) -> None:
        self._name = name
        self._produce = produce
        self.lock = threading.Lock()
        self.state: dict[int, dict[str, object]] = {}

    def _work(self, client_id: int) -> None:
        try:
            with get_session() as session:
                client = session.get(Client, client_id)
                if client is None:
                    self.state.pop(client_id, None)
                    return
                result = self._produce(client)
                self.state[client_id] = {
                    "state": "fertig",
                    "client": client.name,
                    **result,
                }
        except Exception as exc:  # noqa: BLE001 — a worker must never die silently
            # A failed proposal must not read as "there is nothing to propose".
            self.state[client_id] = {"state": "fehler", "error": str(exc)}
            _log.exception("%s proposal for client %s failed", self._name, client_id)
        finally:
            self.lock.release()

    def start(self, session, client_id: int) -> bool:
        """Begin the proposal for one client. False if one is already running."""
        client = session.get(Client, client_id)
        if client is None or not self.lock.acquire(blocking=False):
            return False
        self.state[client_id] = {"state": "läuft", "client": client.name}
        threading.Thread(
            target=self._work,
            args=(client_id,),
            daemon=True,
            name=f"newspulse-{self._name}-{client_id}",
        ).start()
        return True

    def running_for(self, client_id: int) -> bool:
        return self.state.get(client_id, {}).get("state") == "läuft"


def _themes_for(client: Client) -> dict[str, object]:
    return {"probes": themes.probe(client, themes.suggest(client))}


def _rivals_for(client: Client) -> dict[str, object]:
    return {"rivals": rivals.suggest(client)}


def _industry_for(client: Client) -> dict[str, object]:
    """Propose and measure industry terms; the caller decides what to store."""
    return {"candidates": industry.measure(client, industry.propose(client))}


themes_job = Proposal("themes", _themes_for)
rivals_job = Proposal("rivals", _rivals_for)
industry_job = Proposal("industry", _industry_for)

# The theme proposal keeps its old module-level names: two route modules and the
# tests reach for them, and the indirection is not worth renaming at every site.
lock = themes_job.lock
state = themes_job.state


def start(session, client_id: int) -> bool:
    return themes_job.start(session, client_id)


def running_for(client_id: int) -> bool:
    return themes_job.running_for(client_id)


__all__ = [
    "Proposal",
    "industry_job",
    "lock",
    "rivals_job",
    "running_for",
    "start",
    "state",
    "themes_job",
]
