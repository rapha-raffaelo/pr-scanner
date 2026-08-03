"""What the setup form has to explain, and what it must accept.

From a PR consultant's walk through the tool. Her verdict on the biggest
obstacle was not a missing feature: "die Reise scheitert an der Stelle, an der
ich als Nicht-Entwicklerin raten muss, statt es erklärt zu bekommen". She had to
read the source to learn that alert topics do not search the press — they only
escalate what the search terms already found — and concluded that anyone entering
"Rückruf" as an alert topic alone would wait for an alarm that can never fire.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Base
from newspulse.web.app import create_app, get_db
from newspulse.web.routes.settings import _clean_country


@pytest.fixture
def factory():
    """A sessionmaker on one shared in-memory connection, so a POST's write is
    visible to the follow-up GET."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(factory):
    app = create_app()

    def _override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("DE", "DE"),
        ("de", "DE"),
        ("Deutschland", "DE"),
        ("deutschland", "DE"),
        ("Germany", "DE"),
        ("Österreich", "AT"),
        ("Schweiz", "CH"),
        ("United Kingdom", "GB"),
        ("", "DE"),
    ],
)
def test_the_country_field_takes_the_name_as_well_as_the_code(typed, expected):
    """Typing "Deutschland" into a field labelled "Land" is the obvious thing to
    do. Being told after submitting that it is "kein 2-Buchstaben-ISO-Code" is a
    rule the form never stated, and the person supplied the fact correctly —
    only the encoding differed."""
    assert _clean_country(typed) == expected


def test_a_genuinely_unusable_country_is_still_refused():
    """The column is String(2): a value that is neither a code nor a name it
    knows must not be silently truncated into a different country."""
    with pytest.raises(ValueError):
        _clean_country("Irgendwo")


def test_the_form_says_what_each_search_field_does(client):
    """The two fields do different things and the difference was invisible."""
    body = " ".join(client.get("/settings").text.split())

    assert "entscheiden, was gefunden wird" in body
    assert "stufen hoch, was schon gefunden wurde" in body
    # And specifically that alert topics do not run their own press scan, which
    # is the misunderstanding that costs an alarm.
    assert "Keine eigene Presseschau" in body


def test_one_name_per_concept_across_the_app(factory, client):
    """The same two fields were "Keywords"/"Alert-Themen" on the client page and
    "Suchbegriffe"/"Alarm-Themen" in settings — three names for two concepts,
    which reads as three concepts to anyone who did not write the code."""
    from newspulse.models import Client

    with factory() as session:
        subject = Client(
            name="Zalando",
            aliases=[],
            keywords=["Retouren"],
            alert_topics=["Rückruf"],
        )
        session.add(subject)
        session.commit()
        subject_id = subject.id

    detail = client.get(f"/client/{subject_id}").text
    settings = client.get("/settings").text

    assert "Suchbegriffe" in detail and "Keywords" not in detail
    assert "Alarm-Themen" in detail and "Alert-Themen" not in detail
    assert "Suchbegriffe" in settings and "Alarm-Themen" in settings
