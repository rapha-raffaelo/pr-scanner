"""The fingerprint on a static URL, and why a page without one ships invisibly.

The static mount answers with an ETag and no ``Cache-Control``, so freshness is
the browser's own guess and it usually guesses "keep it". Measured the day this
was written: a restyled close button was in the commit, on main, and being
served by production, and the consultant looking at the page still had the old
one — "why the heck was the mac os like closing button not merged here: I still
see the old and bad design".

So every static URL carries a hash of the file's own bytes. A changed file gets
a new URL and is fetched; an unchanged one keeps its URL and is not.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Base
from newspulse.web.app import asset_version, create_app, get_db


@pytest.fixture
def web():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_the_stylesheet_url_carries_a_fingerprint(web):
    body = web.get("/today").text

    assert "/static/app.css?v=" in body, "a change to it would ship invisibly"


def test_the_fingerprint_follows_the_file_and_not_the_clock(tmp_path, monkeypatch):
    """A content hash, not a deploy timestamp: a redeploy that changed nothing
    must not force every client to re-download, and a file that did change must
    be re-fetched even if two deploys land in the same second."""
    from newspulse.web import app as web_app

    monkeypatch.setattr(web_app, "_STATIC_DIR", tmp_path)
    (tmp_path / "x.css").write_text("a{}", encoding="utf-8")
    first = web_app.asset_version("x.css")
    again = web_app.asset_version("x.css")
    (tmp_path / "x.css").write_text("b{}", encoding="utf-8")
    changed = web_app.asset_version("x.css")

    assert first == again, "unchanged bytes keep their URL"
    assert changed != first, "changed bytes get a new one"


def test_a_missing_asset_leaves_the_url_alone_rather_than_breaking_the_page():
    """An unreadable file is not a broken page: the URL renders without a
    version and the browser asks for it exactly as it did before."""
    assert asset_version("gibt-es-nicht.css") == ""


def test_the_script_url_carries_one_too(web):
    """htmx ships pinned in the repository, so a version bump has the same
    problem the stylesheet had."""
    body = web.get("/today").text

    assert "/static/htmx.min.js?v=" in body
