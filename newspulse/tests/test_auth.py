"""Authentication, and the guard that makes it impossible to forget.

The dashboard's whole security model used to be the loopback bind. These tests
pin the replacement — and above all the boot refusal, because the failure mode
being prevented is publishing a client portfolio, which no amount of noticing
afterwards undoes.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config
from newspulse.models import Base
from newspulse.web import auth
from newspulse.web.app import create_app, get_db


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --- The boot guard ------------------------------------------------------------


def test_a_public_bind_without_credentials_refuses_to_start(monkeypatch):
    """The one mistake that cannot be undone by noticing it later."""
    monkeypatch.setattr(config, "AUTH_USER", "")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    with pytest.raises(SystemExit):
        auth.require_auth_for_public_bind("0.0.0.0")


def test_a_public_bind_with_credentials_starts(monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    auth.require_auth_for_public_bind("0.0.0.0")  # must not raise


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_never_requires_credentials(monkeypatch, host):
    """The local workflow that has always existed keeps working unconfigured."""
    monkeypatch.setattr(config, "AUTH_USER", "")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    auth.require_auth_for_public_bind(host)  # must not raise


# --- The middleware ------------------------------------------------------------


def test_unconfigured_means_open(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    assert client.get("/today").status_code == 200


def test_configured_challenges_an_anonymous_request(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    resp = client.get("/today")
    assert resp.status_code == 401
    assert "Basic" in resp.headers["WWW-Authenticate"]


def test_correct_credentials_pass(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    assert client.get("/", headers=_basic("lucas", "geheim")).status_code == 200


@pytest.mark.parametrize(
    "user,password",
    [("lucas", "falsch"), ("wer", "geheim"), ("", ""), ("lucas", "")],
)
def test_wrong_credentials_are_refused(client, monkeypatch, user, password):
    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    assert client.get("/", headers=_basic(user, password)).status_code == 401


def test_a_malformed_header_is_refused_not_crashed(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    for header in ("Basic !!!notbase64", "Bearer abc", "Basic", "garbage"):
        assert client.get("/", headers={"Authorization": header}).status_code == 401


def test_every_data_route_is_behind_the_challenge(client, monkeypatch):
    """A route added later must not be reachable just because it was forgotten."""
    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    for path in ("/", "/clients", "/archive", "/settings", "/client/1",
                 "/api/assistant/clients", "/api/assistant/stream?q=x"):
        assert client.get(path).status_code == 401, path


def test_static_assets_stay_public(client, monkeypatch):
    """They carry no client data, and challenging every file makes the browser
    re-prompt on each one."""
    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    assert client.get("/static/app.css").status_code == 200


# --- The digest link -----------------------------------------------------------


def test_the_digest_links_to_the_configured_address(monkeypatch):
    """Hardcoded, it emailed every recipient a link to their own machine."""
    monkeypatch.setattr(config, "BASE_URL", "https://newspulse.example.com")
    assert config.base_url() == "https://newspulse.example.com"


def test_the_base_url_falls_back_to_the_bind(monkeypatch):
    monkeypatch.setattr(config, "BASE_URL", "")
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_PORT", 8000)
    assert config.base_url() == "http://127.0.0.1:8000"


# --- PaaS port binding ---------------------------------------------------------


def test_the_platform_injected_port_is_respected(monkeypatch):
    """Railway, Render and Heroku inject PORT and route to exactly it. Binding
    something else means the platform sends traffic to a closed socket."""
    import importlib

    monkeypatch.delenv("NEWSPULSE_WEB_PORT", raising=False)
    monkeypatch.setenv("PORT", "4242")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.WEB_PORT == 4242
    finally:
        monkeypatch.delenv("PORT", raising=False)
        importlib.reload(config)


def test_an_explicit_port_still_wins_over_the_platform(monkeypatch):
    """A local override must never be silently ignored."""
    import importlib

    monkeypatch.setenv("PORT", "4242")
    monkeypatch.setenv("NEWSPULSE_WEB_PORT", "9000")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.WEB_PORT == 9000
    finally:
        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.delenv("NEWSPULSE_WEB_PORT", raising=False)
        importlib.reload(config)


# --- Reverse-proxy awareness -----------------------------------------------
#
# Regression: served behind Railway's TLS-terminating router the dashboard came
# up unstyled and inert. `url_for` builds an *absolute* URL, uvicorn trusts
# X-Forwarded-Proto only from 127.0.0.1, so every asset link was emitted as
# http:// on an https page and the browser blocked the stylesheet and htmx as
# mixed content. The HTML still rendered, which is why it looked like a broken
# design rather than a broken deploy.


def test_asset_urls_are_root_relative_not_absolute():
    """Assets on this same app must not carry a scheme or host.

    An absolute URL can be wrong (behind a proxy, or a rename of the public
    hostname), and when it is wrong the browser does not degrade — it blocks the
    asset outright. A root-relative path cannot be wrong.
    """
    from pathlib import Path

    import newspulse.web.app as web_app

    base = (Path(web_app.__file__).parent / "templates" / "base.html").read_text()

    assert "url_for(" not in base, (
        "url_for() yields an absolute URL including the scheme; use /static/... "
        "so the asset cannot be blocked as mixed content behind a TLS proxy"
    )
    for asset in ("app.css", "htmx.min.js", "captain.svg"):
        assert f"/static/{asset}" in base, f"{asset} link went missing"
    assert "http://" not in base, "no asset may be requested over plain http"


def test_forwarded_headers_trusted_only_when_a_proxy_is_in_front():
    """Loopback keeps uvicorn's strict default; a public bind trusts the proxy.

    A non-loopback bind implies something is in front of it, because binding
    publicly without credentials is refused outright.
    """
    from newspulse.web.app import forwarded_allow_ips

    for local in ("127.0.0.1", "localhost", "::1"):
        assert forwarded_allow_ips(local) is None, (
            f"{local} needs no proxy trust, and widening it there would let a "
            "local process spoof the scheme"
        )
    for public in ("0.0.0.0", "::"):
        assert forwarded_allow_ips(public) == "*"


def test_a_non_ascii_credential_is_refused_rather_than_crashing(monkeypatch):
    """`hmac.compare_digest` raises TypeError on a str above U+007F, and the
    middleware does not catch it. An unauthenticated request carrying an umlaut
    in the header was a 500, and an umlaut in the configured password broke
    every request instead of one."""
    import base64

    from newspulse import config
    from newspulse.web import auth

    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    header = "Basic " + base64.b64encode("ü:x".encode()).decode()

    assert auth._credentials_match(header) is False

    monkeypatch.setattr(config, "AUTH_PASSWORD", "gehäim")
    right = "Basic " + base64.b64encode("lucas:gehäim".encode()).decode()
    assert auth._credentials_match(right) is True
