"""Where a redirect is allowed to send somebody.

Four routes take a destination from a query string or a form field and redirect
to it, and each had written its own check. Three of them were wrong in a
different way: two tested only for a leading slash, one tested for ``//`` but
not for the backslash, and the fourth — the one this file is copied from — got
it right. A rule that every caller reimplements is a rule three callers get
wrong, so there is one now.

The check is deliberately about *shape* and not about hosts. An allow-list of
our own domains sounds stricter and is worse: the deployment's hostname is a
setting, the reader may be on a preview URL, and a check that has to be kept in
step with configuration is a check that quietly stops matching.
"""

from __future__ import annotations


def local_target(redirect_to: str | None, fallback: str = "/") -> str:
    """A redirect target that cannot leave this site.

    One leading slash, no second slash anywhere — and no backslash either, which
    is the part a plain ``//`` check misses: browsers normalise the separator, so
    ``/\\evil.example`` is protocol-relative by the time it is followed and takes
    the reader off-site. The two characters have to be treated as one.

    A reflected absolute URL is worth real money to whoever is phishing the two
    people with access: it is a link on the domain they trust that lands
    somewhere else.
    """
    candidate = (redirect_to or "").strip()
    if not candidate.startswith("/") or "//" in candidate or "\\" in candidate:
        return fallback
    return candidate
