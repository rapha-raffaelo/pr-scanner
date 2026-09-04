"""QA probes for RIS-02 — run from newspulse/ with tests/ on the path."""
import datetime as dt
import json

from newspulse import issues
from tests.test_issues import (  # noqa: F401 — fixtures
    _NOW, _cover, _market, _opened, _two_day_repetition, factory, mandate, session,
)


def _yes(prompt, timeout=None):
    return json.dumps({"gehoert_dazu": True, "begruendung": "Derselbe Vorwurf."})


def test_qa_market_signal_candidate_reaches_linking(session, mandate):
    """A fresh dated market signal should be offered to the open issue."""
    opened = issues.accept(
        session, mandate, _two_day_repetition(session, mandate)[1], by="lucas", now=_NOW
    )
    assert opened is not None
    _market(session, mandate, word="frist", published_days_ago=0.5)
    count = issues.link_signals(session, mandate, invoke=_yes, now=_NOW)
    assert count == 1, "the dated market signal never became a candidate"


def test_qa_article_candidate_after_signal_founded_issue(session, mandate):
    """A fresh article should be offered to an issue holding a market signal."""
    lead = _cover(session, mandate, source="FAZ", word="offiziell", days_ago=1)
    _market(session, mandate, word="frist", published_days_ago=5)
    opened = issues.accept(session, mandate, lead, by="lucas", now=_NOW)
    assert opened is not None
    assert any(row.signal_id is not None for row in opened.signals)
    _cover(session, mandate, source="WDR", word="erneut", days_ago=0.2)
    count = issues.link_signals(session, mandate, invoke=_yes, now=_NOW)
    assert count == 1, "no article candidate once a market signal hangs on an issue"
