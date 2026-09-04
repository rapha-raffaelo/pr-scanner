"""Who a workspace page is for.

A yardstick is tracked to measure a mandate against; nobody reports to it. The
sweep has always known that — it skips ``is_competitor`` companies for the theme
radar, the impulses, the profile refresh, the monthly report and the theme
settling — but the pages that render those things did not. A competitor was
offered the whole workspace and every generate button on it, so pressing one
spent a model call writing a document for a company that will never receive one.

The visibility page got this right when it was built. This is the same guard, in
one place, for the five that did not.

What a benchmark keeps is its coverage: the archive page and the charts that
compare it are exactly why it is on file, and they read rather than generate.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Client, Crisis


def mandate_or_404(session: Session, client_id: int) -> Client:
    """The mandate, or 404 — and a benchmark is a 404 here.

    404 rather than a redirect or a disabled button: a benchmark has no
    workspace, so there is no page to send the reader to and nothing to grey
    out. It is not in the sidebar and not in the portfolio either, so the only
    way to arrive here is a hand-typed URL or a stale tab.
    """
    client = session.get(Client, client_id)
    if client is None or client.is_competitor:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def crisis_or_none(session: Session, crisis_id: int) -> Crisis | None:
    """The crisis, or ``None`` for a stale id — and a benchmark's is ``None``.

    The button half of the guard above, in the same place as the page half. A
    crisis id arrives on POST endpoints in two route modules, and both of them
    hang a model call off it: without this, a hand-typed POST still spends one
    writing for a company that will never receive what it writes. ``None``
    rather than a 404, because these are one-click actions inside a list that
    may have been open since before a sweep — a stale id costs nothing rather
    than the page.
    """
    standing = session.get(Crisis, crisis_id)
    if standing is None or standing.client is None or standing.client.is_competitor:
        return None
    return standing
