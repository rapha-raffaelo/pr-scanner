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

from ..models import Client


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
