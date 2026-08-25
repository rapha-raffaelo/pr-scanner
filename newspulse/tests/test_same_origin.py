"""A write submitted from another site's page is refused, on every route.

Two routes carried this check and about fifty did not. The two were the ones
that mint the audit record, which was the right place to start and the wrong
place to stop: deactivating a mandate, committing an import and overwriting a
communications guide are all writes an open web page could auto-submit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from newspulse.web.app import create_app
from newspulse.web.origin import REFUSAL


@pytest.fixture
def web():
    return TestClient(create_app(), follow_redirects=False)


# One per family of writes, named by what an attacker would get out of it.
WRITES = [
    ("/settings/run", {}),
    ("/language/en", {}),
    ("/settings/clients/1/deactivate", {}),
    ("/client/1/outreach/1/release", {}),
    ("/logout", {}),
]


@pytest.mark.parametrize(("path", "body"), WRITES)
def test_a_post_from_another_site_is_refused(web, path, body):
    answer = web.post(path, data=body, headers={"Origin": "https://evil.example"})

    assert answer.status_code == 403
    assert REFUSAL in answer.text


def test_the_same_form_from_our_own_page_is_not(web):
    """The check must cost the real workflow nothing. Driven through the one
    write that needs neither a database nor a sweep, so what is being measured
    is the middleware and not the handler behind it."""
    answer = web.post("/language/en", headers={"Origin": "http://testserver"})

    assert answer.status_code != 403


@pytest.mark.parametrize(("path", "body"), WRITES)
def test_the_check_runs_before_the_handler(path, body):
    """Which is why the refusals above need no database: a cross-site write is
    turned away without the app doing any of the work behind it."""
    from starlette.datastructures import Headers
    from starlette.requests import Request

    from newspulse.web.origin import is_foreign

    scope = {
        "type": "http", "method": "POST", "path": path, "query_string": b"",
        "headers": Headers({"origin": "https://evil.example", "host": "testserver"}).raw,
        "server": ("testserver", 80), "scheme": "http",
    }
    assert is_foreign(Request(scope)) is True


def test_a_referer_is_read_when_there_is_no_origin(web):
    """Older browsers and some privacy settings send one and not the other."""
    answer = web.post("/settings/run", headers={"Referer": "https://evil.example/x"})

    assert answer.status_code == 403


def test_a_request_that_names_no_page_passes(web):
    """Not a browser: curl, the scheduler, this test client. A non-browser
    carries no ambient credentials for a foreign page to ride on, and refusing
    it would break every script without closing anything."""
    assert web.post("/language/en").status_code != 403


def test_reading_is_never_refused(web):
    """A same-origin rule on GET would break every bookmark and every link
    somebody pastes into a chat."""
    answer = web.get("/login", headers={"Referer": "https://news.example/article"})

    assert answer.status_code == 200
