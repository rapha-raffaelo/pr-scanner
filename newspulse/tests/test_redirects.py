"""Where a redirect may send somebody, in one place.

Four routes took a destination from the reader's own request and each had
written its own check; three were wrong in a different way. This is the shared
rule and the cases each of those checks used to miss.
"""

from __future__ import annotations

import pytest

from newspulse.web.redirects import local_target


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/steal",
        "http://evil.example",
        "//evil.example",            # scheme-relative; two routes let this through
        "/\\evil.example",           # the browser normalises the backslash
        "\\\\evil.example",
        "javascript:alert(1)",
        "",
        "   ",
        None,
    ],
)
def test_nothing_that_can_leave_the_site_is_honoured(hostile):
    """A reflected absolute URL is a link on the domain the two of them trust
    that lands somewhere else, which is worth real money to whoever is phishing
    them."""
    assert local_target(hostile, "/zurueck") == "/zurueck"


@pytest.mark.parametrize(
    "path",
    ["/", "/settings", "/client/3/advice", "/archive?client=2&source=Welt", "/today#top"],
)
def test_a_path_on_this_site_is_kept(path):
    assert local_target(path, "/zurueck") == path


def test_the_fallback_is_the_callers_own():
    """Each route lands somewhere that makes sense for it, not on a shared root."""
    assert local_target("http://evil.example", "/contacts") == "/contacts"
