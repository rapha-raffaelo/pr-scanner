"""A broken tool must not look like a quiet news day.

These come from an independent review of the running product. Each is a case
where the interface stayed calm while something underneath had stopped working —
the failure mode the job's own module docstring says it exists to survive:
"silently stopping in week three".
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from newspulse import job
from newspulse.analyzer import BackendError, _BaseClaudeAnalyzer
from newspulse.db import make_engine
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    RunStatus,
    TopicHit,
)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


class _DeadBackend(_BaseClaudeAnalyzer):
    """The `claude` CLI is gone: not on PATH, or its session expired.

    Not a quota error, so the Gemini fallback never engages — this is the outage
    the deployment is actually exposed to, since the CLI's login lives on a
    volume that cannot be recreated from a laptop.
    """

    def _invoke(self, prompt: str) -> str:
        raise BackendError("claude: command not found")


def _article(session, title="Eine Meldung") -> Article:
    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 10**6}",
        source="Handelsblatt",
        published_at=dt.datetime.now(dt.UTC),
        fetched_at=dt.datetime.now(dt.UTC),
        summary_text=None,
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.commit()
    return article


def test_a_dead_analysis_backend_is_reported_rather_than_swallowed(session):
    """The contract says analyze() must never raise. It was implemented as
    "never tell anyone": every batch was dropped, no error was recorded, and the
    run was written OK — so the dashboard showed a healthy header over an empty
    day, indistinguishable from a quiet one."""
    client = Client(name="Zalando", aliases=[], keywords=[], alert_topics=[])
    session.add(client)
    session.commit()
    article = _article(session)
    errors: list[str] = []

    written = job._analyze_and_persist(
        session, client, [article], _DeadBackend(), errors
    )

    assert written == 0
    assert errors, "a backend that produced nothing must say so"
    assert "command not found" in errors[0]
    # And an error in the list is what stops the run being recorded as OK, which
    # is what the header reads to decide between "Lauf ok" and a fault.
    assert job._final_status(errors) is RunStatus.PARTIAL


def test_a_working_backend_records_no_error(session):
    """The counter must attribute failures to the client that caused them, not
    carry an earlier client's outage into a healthy one."""

    class _Fine(_BaseClaudeAnalyzer):
        def _invoke(self, prompt: str) -> str:
            return json.dumps(
                [
                    {
                        "id": 0,
                        "is_relevant": True,
                        "summary": "Ein Satz.",
                        "category": "produkt",
                        "relevance_score": 6,
                        "importance_score": 4,
                        "is_alert": False,
                        "reasoning": "Direkt über das Unternehmen.",
                    }
                ]
            )

    client = Client(name="Zalando", aliases=[], keywords=[], alert_topics=[])
    session.add(client)
    session.commit()
    errors: list[str] = []

    job._analyze_and_persist(session, client, [_article(session)], _Fine(), errors)

    assert errors == []


def test_a_dismissed_article_leaves_the_outlet_ranking(session):
    """Dismissing takes a story out of "Heute, the archive, the counts, the share
    of voice, the digest, the exports and every prompt" — the outlet and
    journalist rankings on the Marktumfeld page were reading around that gate, so
    a publication kept credit for coverage the user had already rejected."""
    from newspulse.web.routes.client import _nominations

    client = Client(name="Zalando", aliases=[], keywords=[], alert_topics=[])
    session.add(client)
    session.flush()
    article = _article(session, "Eine Verwechslung")
    analysis = Analysis(is_relevant=True, 
        article_id=article.id,
        client_id=client.id,
        summary="s",
        category=Category.SONSTIGES,
        relevance_score=5,
        importance_score=4,
        is_alert=False,
    )
    session.add(analysis)
    session.commit()

    before = _nominations(session, client.id, days=90)
    assert any(o.name == "Handelsblatt" for o in before["own_outlets"])

    analysis.dismissed_at = dt.datetime.now(dt.UTC)
    session.commit()

    after = _nominations(session, client.id, days=90)
    assert not any(o.name == "Handelsblatt" for o in after["own_outlets"])


def test_an_irrelevant_match_never_counts_towards_an_outlet(session):
    """Relevance zero means the analyzer decided the story is not about this
    client. It must not shape who to pitch."""
    from newspulse.web.routes.client import _nominations

    client = Client(name="Zalando", aliases=[], keywords=[], alert_topics=[])
    session.add(client)
    session.flush()
    article = _article(session, "Ein Namensvetter")
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            summary="s",
            category=Category.SONSTIGES,
            relevance_score=0,
            importance_score=1,
            is_alert=False,
        )
    )
    session.commit()

    assert _nominations(session, client.id, days=90)["own_outlets"] == []


def test_the_market_ranking_still_counts_what_the_radar_found(session):
    """The gate belongs on coverage *of* the client only: a radar hit has no
    analysis at all, and filtering it the same way would empty the page it is
    the whole point of."""
    from newspulse.web.routes.client import _nominations

    client = Client(name="Zalando", aliases=[], keywords=["Mode"], alert_topics=[])
    session.add(client)
    session.flush()
    article = _article(session, "Modebranche diskutiert Retouren")
    session.add(
        TopicHit(
            article_id=article.id, client_id=client.id, found_at=dt.datetime.now(dt.UTC)
        )
    )
    session.commit()

    assert any(
        o.name == "Handelsblatt"
        for o in _nominations(session, client.id, days=90)["market_outlets"]
    )
