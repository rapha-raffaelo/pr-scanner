"""The Gemini fallback: when it engages, and — more importantly — when it must not.

The value of this feature is a sweep that survives an exhausted subscription. The
risk of it is a second, metered provider silently absorbing work that failed for
some other reason. So most of what is asserted here is restraint: a parse bug, a
missing CLI, a broken login and a network fault must all still fail, loudly, on
the primary.
"""

from __future__ import annotations

import datetime as dt

import pytest

from newspulse import analyzer as analyzer_mod
from newspulse import config
from newspulse.models import Article, Client
from newspulse.quota import is_quota_error


# --- Classification: the whole safety mechanism -------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Claude usage limit reached. Your limit will reset at 3pm.",
        "API error 429: too many requests",
        "rate_limit_error: please slow down",
        "RESOURCE_EXHAUSTED: quota exceeded for model",
        "Your credit balance is too low to access the API",
        "insufficient_quota",
    ],
)
def test_capacity_refusals_are_recognised(message):
    assert is_quota_error(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "claude CLI not found on PATH",
        "Not logged in · Please run /login",
        "authentication_failed",
        "claude -p did not return JSON on stdout",
        "expected a JSON array of verdicts",
        "connection refused",
        "Overloaded",
        "claude -p timed out after 120s",
        "",
        "   ",
    ],
)
def test_other_failures_are_not_treated_as_quota(message):
    """These must keep failing on the primary.

    Each is a bug, a misconfiguration or a transient fault. Falling back on any
    of them would convert a visible failure into a silent charge — and the login
    and CLI-missing cases would mask exactly the deployment faults that are
    hardest to diagnose from a dashboard that merely looks empty.
    """
    assert is_quota_error(message) is False


def test_a_number_that_merely_contains_429_is_not_a_quota_error():
    """Guards the digit match against article ids, ports and session strings."""
    assert is_quota_error("processed article 14293 in run 4291") is False


def test_accepts_an_exception_directly():
    assert is_quota_error(analyzer_mod.BackendError("usage limit reached")) is True


# --- The analyzer fallback ----------------------------------------------------


def _client() -> Client:
    return Client(id=1, name="Zalando", aliases=[], keywords=[], alert_topics=[])


def _articles(n: int) -> list[Article]:
    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=dt.UTC)
    return [
        Article(
            id=i,
            title=f"Zalando meldet Quartalszahlen {i}",
            url=f"https://example.de/{i}",
            source="Handelsblatt",
            published_at=now,
            fetched_at=now,
            summary_text="Kurz.",
            language="de",
            title_hash=f"h{i}",
        )
        for i in range(1, n + 1)
    ]


def _verdicts(count: int) -> str:
    """A well-formed batch response for ``count`` articles.

    The parser requires one verdict per candidate, so a stub returning "[]" would
    fail validation and trip the retry loop — measuring the harness rather than
    the fallback.
    """
    import json

    return json.dumps([
        {
            "id": i,
            "is_relevant": True,
            "summary": "Zusammenfassung.",
            "category": "finanzen",
            "relevance_score": 7,
            "importance_score": 6,
            "is_alert": False,
            "reasoning": "Direkt über das Kerngeschäft.",
        }
        for i in range(count)
    ])


class _StubAnalyzer(analyzer_mod._BaseClaudeAnalyzer):
    """A base-class analyzer whose provider call is scripted per attempt."""

    def __init__(self, responses=(), **kw):
        super().__init__(**kw)
        self.responses = list(responses)
        self.calls = 0
        self.last_batch_size = 0

    def _render_prompt(self, client, chunk):
        self.last_batch_size = len(chunk)
        return super()._render_prompt(client, chunk)

    def _invoke(self, prompt: str) -> str:
        self.calls += 1
        item = self.responses.pop(0) if self.responses else _verdicts(self.last_batch_size)
        if isinstance(item, Exception):
            raise item
        return item


def test_quota_failure_sets_the_flag_and_stops_calling():
    """One refusal ends the attempt loop and the remaining batches.

    Without this, a 30-batch sweep spends 30 doomed calls plus 30 timeouts to
    rediscover a limit that is per account, and the retry loop doubles it.
    """
    limit = analyzer_mod.BackendError("Claude usage limit reached")
    stub = _StubAnalyzer([limit, limit, limit], batch_size=1)

    result = stub.analyze(_client(), _articles(3))

    assert result == []
    assert stub.quota_exhausted is True
    assert stub.calls == 1, "must not retry a quota error, nor try the next batch"


def test_non_quota_failure_still_retries_once_and_keeps_going():
    """The pre-existing contract is untouched for ordinary failures."""
    boom = analyzer_mod.ParseError("expected a JSON array of verdicts")
    stub = _StubAnalyzer([boom, boom], batch_size=1)

    stub.analyze(_client(), _articles(2))

    assert stub.quota_exhausted is False
    assert stub.calls == 3, "two attempts on the first batch, then the second batch"


def test_fallback_reanalyses_with_the_secondary_when_quota_is_gone():
    primary = _StubAnalyzer([analyzer_mod.BackendError("usage limit reached")], batch_size=10)
    secondary = _StubAnalyzer(batch_size=10)
    wrapper = analyzer_mod.FallbackAnalyzer(primary, secondary)

    wrapper.analyze(_client(), _articles(2))

    assert wrapper.switched is True
    assert secondary.calls == 1


def test_fallback_stays_switched_for_the_rest_of_the_run():
    """Sticky, so batch two does not re-test a limit that just refused batch one."""
    primary = _StubAnalyzer([analyzer_mod.BackendError("quota exceeded")], batch_size=10)
    secondary = _StubAnalyzer(batch_size=10)
    wrapper = analyzer_mod.FallbackAnalyzer(primary, secondary)

    wrapper.analyze(_client(), _articles(1))
    calls_after_first = primary.calls
    wrapper.analyze(_client(), _articles(1))

    assert primary.calls == calls_after_first, "primary must not be asked again"
    assert secondary.calls == 2


def test_fallback_does_not_engage_on_an_ordinary_failure():
    """A parse bug must not become spend on the metered provider."""
    bad = analyzer_mod.ParseError("expected a JSON array of verdicts")
    primary = _StubAnalyzer([bad, bad], batch_size=10)
    secondary = _StubAnalyzer(batch_size=10)
    wrapper = analyzer_mod.FallbackAnalyzer(primary, secondary)

    wrapper.analyze(_client(), _articles(1))

    assert wrapper.switched is False
    assert secondary.calls == 0, "the fallback must never see a non-quota failure"


# --- Wiring: the fallback cannot arm itself -----------------------------------


def test_no_key_means_no_wrapper(monkeypatch):
    monkeypatch.delenv("NEWSPULSE_GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")

    assert not isinstance(analyzer_mod.get_analyzer(), analyzer_mod.FallbackAnalyzer)


def test_a_key_arms_the_fallback(monkeypatch):
    monkeypatch.setenv("NEWSPULSE_GEMINI_API_KEY", "test-key")

    built = analyzer_mod.get_analyzer()

    assert isinstance(built, analyzer_mod.FallbackAnalyzer)
    assert isinstance(built.secondary, analyzer_mod.GeminiAnalyzer)


def test_choosing_gemini_outright_is_not_wrapped_in_itself(monkeypatch):
    monkeypatch.setenv("NEWSPULSE_GEMINI_API_KEY", "test-key")

    built = analyzer_mod.get_analyzer(analyzer_mod.BACKEND_GEMINI)

    assert isinstance(built, analyzer_mod.GeminiAnalyzer)
    assert not isinstance(built, analyzer_mod.FallbackAnalyzer)


def test_gemini_refuses_to_run_without_a_key(monkeypatch):
    """Better a clear BackendError than an HTTP 400 the operator has to decode."""
    from newspulse import gemini

    monkeypatch.delenv("NEWSPULSE_GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")

    with pytest.raises(analyzer_mod.BackendError, match="not configured"):
        gemini.generate("hallo")


def test_the_api_key_is_sent_as_a_header_not_in_the_url(monkeypatch):
    """A key in a query string leaks into proxy logs and error reports."""
    from newspulse import gemini

    monkeypatch.setenv("NEWSPULSE_GEMINI_API_KEY", "secret-key")
    request = gemini._request(f"{gemini._ENDPOINT}/m:generateContent", "hallo")

    assert "secret-key" not in request.full_url
    assert request.get_header(gemini._KEY_HEADER.capitalize()) == "secret-key"


# --- Captain Comms: the drawer ------------------------------------------------
#
# Streaming has a constraint the batch paths do not: output is already on the
# reader's screen. A provider swap is only honest before the first word.


def _events(kinds_and_data):
    from newspulse.streaming import StreamEvent

    return [StreamEvent(k, d) for k, d in kinds_and_data]


def test_drawer_falls_back_when_the_limit_hits_before_any_text(monkeypatch):
    from newspulse import streaming

    monkeypatch.setenv("NEWSPULSE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        streaming, "stream_claude",
        lambda p, **kw: iter(_events([("status", "verbunden"),
                                      ("error", "Claude usage limit reached")])),
    )
    monkeypatch.setattr(
        streaming, "stream_gemini",
        lambda p, **kw: iter(_events([("text", "Die Lage ist stabil."), ("done", "")])),
    )

    out = list(streaming.stream_assistant("Was ist los?"))
    kinds = [e.kind for e in out]

    assert "error" not in kinds, "a recovered failure must never reach the reader"
    assert "".join(e.data for e in out if e.kind == "text") == "Die Lage ist stabil."
    assert kinds[-1] == "done"


def test_drawer_does_not_switch_once_words_are_on_screen(monkeypatch):
    """No swapping mid-sentence.

    Half an answer from one model continued by another reads as one voice
    contradicting itself. The error stands and the reader can ask again.
    """
    from newspulse import streaming

    monkeypatch.setenv("NEWSPULSE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        streaming, "stream_claude",
        lambda p, **kw: iter(_events([("text", "Zalando steht "),
                                      ("error", "Claude usage limit reached")])),
    )
    monkeypatch.setattr(
        streaming, "stream_gemini",
        lambda p, **kw: iter(_events([("text", "SHOULD NOT APPEAR"), ("done", "")])),
    )

    out = list(streaming.stream_assistant("Was ist los?"))

    assert "SHOULD NOT APPEAR" not in "".join(e.data for e in out)
    assert out[-1].kind == "error"


def test_drawer_surfaces_a_non_quota_error_untouched(monkeypatch):
    """A broken login must stay visible, not be papered over by the fallback."""
    from newspulse import streaming

    monkeypatch.setenv("NEWSPULSE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        streaming, "stream_claude",
        lambda p, **kw: iter(_events([("error", "Not logged in · Please run /login")])),
    )
    monkeypatch.setattr(
        streaming, "stream_gemini",
        lambda p, **kw: iter(_events([("text", "SHOULD NOT APPEAR"), ("done", "")])),
    )

    out = list(streaming.stream_assistant("Was ist los?"))

    assert len(out) == 1
    assert out[0].kind == "error"
    assert "Not logged in" in out[0].data


def test_drawer_without_a_key_is_exactly_the_old_behaviour(monkeypatch):
    from newspulse import streaming

    monkeypatch.delenv("NEWSPULSE_GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(
        streaming, "stream_claude",
        lambda p, **kw: iter(_events([("error", "Claude usage limit reached")])),
    )

    out = list(streaming.stream_assistant("Was ist los?"))

    assert [e.kind for e in out] == ["error"]


# --- The advisor's one-shot path ----------------------------------------------


def test_advisor_invoke_falls_back_on_quota(monkeypatch):
    from newspulse import gemini

    monkeypatch.setenv("NEWSPULSE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        analyzer_mod, "invoke_claude_cli",
        lambda p, **kw: (_ for _ in ()).throw(analyzer_mod.BackendError("usage limit reached")),
    )
    monkeypatch.setattr(gemini, "generate", lambda p, **kw: '{"situation": "ok"}')

    assert analyzer_mod.invoke_with_fallback("prompt") == '{"situation": "ok"}'


def test_advisor_invoke_reraises_anything_else(monkeypatch):
    """A brief that fails for a real reason must say so, not cost money elsewhere."""
    from newspulse import gemini

    monkeypatch.setenv("NEWSPULSE_GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        analyzer_mod, "invoke_claude_cli",
        lambda p, **kw: (_ for _ in ()).throw(analyzer_mod.BackendError("claude CLI not found")),
    )
    called = []
    monkeypatch.setattr(gemini, "generate", lambda p, **kw: called.append(1) or "x")

    with pytest.raises(analyzer_mod.BackendError, match="not found"):
        analyzer_mod.invoke_with_fallback("prompt")
    assert called == []


def test_advisor_invoke_without_a_key_reraises_even_on_quota(monkeypatch):
    monkeypatch.delenv("NEWSPULSE_GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(
        analyzer_mod, "invoke_claude_cli",
        lambda p, **kw: (_ for _ in ()).throw(analyzer_mod.BackendError("usage limit reached")),
    )

    with pytest.raises(analyzer_mod.BackendError):
        analyzer_mod.invoke_with_fallback("prompt")
