"""Interface language (newspulse.i18n) and the toggle.

The risk with a two-language UI is a *mixed* one: half the chrome switches and
half does not, which looks broken in a way a fully-German UI does not. These
tests check both the lookup and that real pages actually change.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import i18n
from newspulse.models import Base, Client
from newspulse.web.app import create_app, get_db


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(factory):
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


# --- The lookup ----------------------------------------------------------------


def test_german_is_the_default_and_returns_the_source_string():
    assert i18n.translate("Heute") == "Heute"
    assert i18n.translate("Heute", "de") == "Heute"


def test_english_translates():
    assert i18n.translate("Heute", "en") == "Today"
    assert i18n.translate("Mandanten", "en") == "Clients"


def test_an_untranslated_string_falls_back_to_german_not_to_a_key():
    """A key-based scheme would show `settings.clients.add` to the operator,
    which is worse than the wrong language."""
    assert i18n.translate("Ein völlig neuer Satz", "en") == "Ein völlig neuer Satz"


def test_an_unknown_language_falls_back_to_german():
    assert i18n.normalize("fr") == "de"
    assert i18n.normalize(None) == "de"
    assert i18n.normalize("") == "de"
    assert i18n.translate("Heute", "fr") == "Heute"


def test_a_locale_style_code_is_accepted():
    """A browser or a hand-edited cookie may carry en-GB."""
    assert i18n.normalize("en-GB") == "en"
    assert i18n.normalize("DE") == "de"


# --- The toggle ----------------------------------------------------------------


def test_the_toggle_sets_a_cookie_and_returns_to_the_page(client):
    resp = client.post("/language/en?next=/archive", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/archive"
    assert client.cookies.get(i18n.COOKIE_NAME) == "en"


def test_an_offsite_redirect_is_refused(client):
    """`next` comes from the query string, so it must never leave the app."""
    resp = client.post("/language/en?next=https://evil.example/", follow_redirects=False)
    assert resp.headers["location"] == "/"


def test_an_unknown_language_code_stores_the_default(client):
    client.post("/language/klingon?next=/", follow_redirects=False)
    assert client.cookies.get(i18n.COOKIE_NAME) == "de"


# --- The rendered pages --------------------------------------------------------


@pytest.mark.parametrize(
    "path,german,english",
    [
        ("/", "Heute", "Today"),
        ("/clients", "Mandanten", "Clients"),
        ("/archive", "Archiv", "Archive"),
        ("/settings", "Einstellungen", "Settings"),
    ],
)
def test_pages_render_in_the_selected_language(client, path, german, english):
    assert german in client.get(path).text

    client.cookies.set(i18n.COOKIE_NAME, "en")
    body = client.get(path).text
    assert english in body


def test_the_html_lang_attribute_follows_the_choice(client):
    assert 'lang="de"' in client.get("/").text
    client.cookies.set(i18n.COOKIE_NAME, "en")
    assert 'lang="en"' in client.get("/").text


def test_both_flags_are_offered_on_every_page(client):
    for path in ("/", "/clients", "/archive", "/settings"):
        body = client.get(path).text
        assert "/language/de" in body, path
        assert "/language/en" in body, path


def test_no_german_chrome_survives_on_an_english_page(client, factory):
    """The failure this guards is a *mixed* UI: a page that switches its nav but
    keeps its buttons German reads as broken."""
    with factory() as s:
        s.add(Client(name="Alpha AG"))
        s.commit()

    client.cookies.set(i18n.COOKIE_NAME, "en")
    body = client.get("/").text
    for leftover in ("Einstellungen", "Aktualisieren", "Warnungen", "Gesamter Tag"):
        assert leftover not in body, leftover


def test_captain_comms_is_reachable_from_every_page(client):
    for path in ("/", "/clients", "/archive", "/settings"):
        body = client.get(path).text
        assert "Captain Comms" in body, path
        assert "captain.svg" in body, path


def test_category_labels_are_translated_but_the_stored_value_is_not(client, factory):
    """Categories are German enum members in the database. The interface must
    show them translated while the option value stays the stored one — a
    translated value would filter for a category that does not exist."""
    import datetime as dt

    from newspulse.models import Analysis, Article, Category

    with factory() as s:
        c = Client(name="Alpha AG")
        s.add(c)
        s.flush()
        when = dt.datetime.now().astimezone()
        art = Article(
            title="Eine Meldung", url="https://ex.de/1", source="FAZ",
            published_at=when, fetched_at=when, summary_text="s",
            language="de", title_hash="h1",
        )
        s.add(art)
        s.flush()
        s.add(Analysis(
            article_id=art.id, client_id=c.id, summary="Zusammenfassung.",
            category=Category.KRISE, relevance_score=5, importance_score=8,
            is_alert=False,
        ))
        s.commit()

    de = client.get("/").text
    assert ">Krise</span>" in de

    client.cookies.set(i18n.COOKIE_NAME, "en")
    en = client.get("/").text
    assert ">Crisis</span>" in en
    assert ">Krise</span>" not in en
    # The filter still submits the stored German value.
    assert 'value="krise"' in client.get("/archive").text


def test_every_category_has_an_english_label():
    """A new Category member must not silently render as its German value."""
    from newspulse.models import Category

    for member in Category:
        assert i18n.translate(member.value, "en") != member.value, member.value
