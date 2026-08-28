"""Whether a sweep is running right now, for the one process that started it.

Two things need this and they must not learn it from each other: the run trigger,
which refuses a second concurrent sweep, and the layout, which shows the header
spinner. Keeping the lock in its own module lets both import it — the routes
import ``web.app``, so ``web.app`` cannot import a route to reach it.

Why a lock rather than the database
-----------------------------------
The ``runs`` row is written when a sweep *finishes* (``job._finalize_run``), so
"a row with no finished_at" — what the header used to test — never exists, and
the "Lauf läuft…" branch had never once rendered. The lock is the only live
signal there is.

Its limit, stated rather than hidden: it lives in one process. A sweep started by
the dashboard button spins in that dashboard. A sweep started by the cron service
is a different process and stays invisible here, which is the right trade for
now — nobody is watching the screen at 06:10, and the alternative (a marker row,
plus a heartbeat to recognise the marker a crashed run left behind) is a lot of
machinery for a spinner.
"""

from __future__ import annotations

import threading

# One sweep at a time. A run fetches 40+ feeds and shells out to `claude` per
# batch; two concurrent runs would double-fetch and race on the same articles.
# Non-blocking acquire at the call site, so a second click is refused immediately
# rather than queueing another full sweep behind the first.
guard = threading.Lock()


#: What a button says when it reached for the guard and found the daily sweep
#: holding it. Here rather than in one of the routes because the impulse's
#: package and the plan's recompute both say it, and a second copy of a German
#: sentence is a second key for a
#: translator to find — which is exactly how the same string once shipped twice
#: in ``i18n._EN``, the later copy silently overriding the earlier one's English.
SWEEP_RUNNING = (
    "Es läuft gerade ein Sammellauf. Der Auftrag wurde nicht angenommen: "
    "warten Sie, bis er durch ist, und klicken Sie dann noch einmal."
)


def is_running() -> bool:
    """True while a dashboard-triggered sweep holds the guard.

    Read-only and cheap enough for the layout to call on every render.
    """
    return guard.locked()


__all__ = ["SWEEP_RUNNING", "guard", "is_running"]
