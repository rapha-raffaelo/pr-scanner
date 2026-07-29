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
    assert client.get("/").status_code == 200


def test_configured_challenges_an_anonymous_request(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "lucas")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    resp = client.get("/")
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
