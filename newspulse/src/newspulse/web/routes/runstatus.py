"""The header's run status as a self-polling fragment.

While a dashboard-triggered sweep is in flight the header element polls this
route, so the reader sees a turning wheel instead of a page that looks idle for
the two minutes a run takes. Polling stops without anyone switching it off:

* still running  → the same fragment comes back, carrying the poll trigger again;
* just finished  → the answer is ``HX-Refresh``, the browser reloads the whole
  page, and the reloaded header has no trigger at all.

The full reload is deliberate. When a sweep ends, the header is the *least*
interesting thing that changed — there is new coverage in the feed, possibly new
alerts and a new positioning draft. Swapping only the header would leave a
finished run announced above a stale page.

Without HTMX (no JS) nothing here is reached: the layout still renders the
spinner, it simply does not update itself until the reader reloads. That is the
same progressive-enhancement posture as the date picker.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import runlock
from ..app import get_db, templates
from .today import _fetch_last_run, _local_tz

router = APIRouter()


@router.get("/partials/runmeta", response_class=HTMLResponse)
def runmeta_partial(
    request: Request, session: Session = Depends(get_db)
) -> Response:
    """Re-render the header's date + run status, or ask for a reload when done."""
    if not runlock.is_running():
        # 204 rather than an empty body with 200: there is nothing to swap, and
        # the reload is the whole response. HTMX honours HX-Refresh on any 2xx.
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    return templates.TemplateResponse(
        request,
        "partials/runmeta.html",
        {
            # The header dates every page; this fragment has no viewed day of its
            # own, so it shows today — in the reader's zone, like the layout.
            "header_date": dt.datetime.now(_local_tz()).date(),
            "last_run": _fetch_last_run(session),
        },
    )
