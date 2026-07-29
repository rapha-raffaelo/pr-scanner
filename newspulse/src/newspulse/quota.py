"""Deciding whether a backend failure means "out of quota" or something else.

This is the whole safety mechanism of the fallback, so it is deliberately narrow.
Falling back on *any* failure would be simpler and much worse: a malformed prompt,
a parsing bug or a network blip would quietly re-run every article against a
metered second provider, every night, and the bill would be the first symptom.
A quota error is the one failure where switching providers is the correct
response, because retrying the same one cannot succeed.

The signals are matched on text because that is what the surfaces actually give
us: the ``claude`` CLI reports usage exhaustion as a human-readable message on a
non-zero exit or in the ``result`` field of its JSON envelope, not as a typed
error. Matching text is fragile, so the rule is one-directional — an unrecognised
message is *not* a quota error, and the run fails loudly rather than spending
money on a guess.
"""

from __future__ import annotations

import re

# Matched case-insensitively against the backend's message.
#
# Each entry is a phrase that only appears when the provider is refusing work for
# capacity or entitlement reasons. Deliberately absent: "overloaded" (HTTP 529)
# and bare "unavailable", which are transient server-side conditions where the
# right response is to retry the same provider, not to change vendor; and
# "authentication", which means the login is broken — falling back there would
# mask exactly the misconfiguration we spent so long diagnosing on the server.
_QUOTA_SIGNALS: tuple[str, ...] = (
    "usage limit",
    "usage_limit",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota",
    "resource_exhausted",
    "resource exhausted",
    "insufficient_quota",
    "too many requests",
    "out of credit",
    "credit balance",
    "billing",
)

# HTTP 429 is the unambiguous status for this, but it arrives embedded in prose
# ("API error 429", "status code: 429"). Bounded so a 429 inside an article title
# or a session id cannot trip it.
_STATUS_429 = re.compile(r"(?<!\d)429(?!\d)")


def is_quota_error(message: str | BaseException | None) -> bool:
    """True when ``message`` says the provider is out of capacity for us.

    Accepts an exception so callers can pass what they caught without unwrapping
    it first; the exception's ``str()`` is what gets matched.

    Returns False for anything unrecognised. That default is the point: an
    unknown failure surfaces as a failure, instead of silently becoming spend on
    the fallback provider.
    """
    if message is None:
        return False
    text = str(message).lower()
    if not text.strip():
        return False
    if any(signal in text for signal in _QUOTA_SIGNALS):
        return True
    return bool(_STATUS_429.search(text))


__all__ = ["is_quota_error"]
