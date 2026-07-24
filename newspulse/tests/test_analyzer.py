"""Unit tests for the pluggable analysis layer (NP-05).

These test the analyzer directly — no daily job, no database, no real network or
CLI. The ``claude -p`` subprocess is mocked with canned JSON so the tests are
fast and deterministic. The three failure/success paths the story calls out
(happy, retry-then-succeed, give-up) each get a focused test, plus the alert
computation, the invocation shape, and backend selection.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from newspulse import analyzer as az
from newspulse.analyzer import (
    BACKEND_CLAUDE_API,
    BACKEND_CLAUDE_CODE,
    ClaudeApiAnalyzer,
    ClaudeCodeAnalyzer,
    get_analyzer,
)
from newspulse.models import Article, Category, Client

# --- Fixtures / builders -------------------------------------------------------


def make_client(**over) -> Client:
    defaults = dict(
        id=1,
        name="Beispiel AG",
        industry="Automotive",
        aliases=["Beispiel", "Beispiel Aktiengesellschaft"],
        alert_topics=["Rückruf", "Insolvenz"],
    )
    defaults.update(over)
    return Client(**defaults)


def make_article(idx: int = 0, **over) -> Article:
    defaults = dict(
        id=100 + idx,
        title=f"Beispiel AG meldet Neuigkeit {idx}",
        url=f"https://example.de/story-{idx}",
        source="Handelsblatt",
        summary_text="Kurze Feed-Zusammenfassung.",
        title_hash=f"h{idx}",
    )
    defaults.update(over)
    return Article(**defaults)


def _verdict(idx: int, **over) -> dict:
    v = dict(
        id=idx,
        is_relevant=True,
        summary="Ein Satz Zusammenfassung.",
        category="produkt",
        relevance_score=6,
        importance_score=4,
        is_alert=False,
        reasoning="Direkt über das Unternehmen.",
    )
    v.update(over)
    return v


def _cli_stdout(verdicts: list[dict], *, is_error: bool = False) -> str:
    """Build a `claude -p ... --output-format json` envelope whose inner result is
    the JSON array of verdicts the model was asked to produce."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "result": json.dumps(verdicts),
        }
    )


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


# --- Invocation shape / no-API-endpoint ----------------------------------------


def test_claude_code_invocation_shape_is_subprocess_only():
    """The subscription backend invokes `claude -p "<prompt>" --output-format json`
    as a subprocess under a timeout — nothing else."""
    article = make_article(0)
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout([_verdict(0)]))
        ClaudeCodeAnalyzer().analyze(make_client(), [article])

    run.assert_called_once()
    argv = run.call_args.args[0]
    assert argv[0] == "claude"
    assert argv[1] == "-p"
    assert argv[3:] == ["--output-format", "json"]
    assert isinstance(argv[2], str) and "Beispiel AG" in argv[2]
    # A timeout is always set so one hung call can never stall the sweep.
    assert run.call_args.kwargs.get("timeout")
    # No shell, so the prompt can never be interpreted as a command.
    assert run.call_args.kwargs.get("shell") in (None, False)


def test_claude_code_never_contacts_an_anthropic_endpoint():
    """The subscription backend contacts no API endpoint and never imports the
    metered SDK: it passes no api.anthropic.com URL and touches no HTTP client."""
    sys.modules.pop("anthropic", None)
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout([_verdict(0)]))
        ClaudeCodeAnalyzer().analyze(make_client(), [make_article(0)])

    argv = run.call_args.args[0]
    assert not any("anthropic.com" in str(part) for part in argv)
    # The env passed to the subprocess is not doctored with spoofed API headers.
    env = run.call_args.kwargs.get("env")
    if env is not None:
        assert "ANTHROPIC_API_KEY" not in env
    # The subscription path never pulls in the metered API SDK.
    assert "anthropic" not in sys.modules


# --- Happy path ----------------------------------------------------------------


def test_happy_path_returns_one_analysis_per_article():
    articles = [make_article(0), make_article(1)]
    verdicts = [
        _verdict(0, category="krise", relevance_score=9, importance_score=3),
        _verdict(1, category="finanzen", relevance_score=7, importance_score=2),
    ]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout(verdicts))
        results = ClaudeCodeAnalyzer(alert_threshold=7).analyze(make_client(alert_topics=[]), articles)

    assert len(results) == 2
    first, second = results
    assert first.article_id == 100 and first.client_id == 1
    assert first.category == Category.KRISE
    assert first.relevance_score == 9
    assert first.reasoning == "Direkt über das Unternehmen."
    assert second.category == Category.FINANZEN
    # Neither cleared the threshold nor hit a topic, so neither is an alert.
    assert first.is_alert is False and second.is_alert is False
    run.assert_called_once()


def test_reasoning_is_stored_on_every_analysis():
    articles = [make_article(0), make_article(1)]
    verdicts = [
        _verdict(0, reasoning="Weil A."),
        _verdict(1, reasoning="Weil B."),
    ]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout(verdicts))
        results = ClaudeCodeAnalyzer().analyze(make_client(alert_topics=[]), articles)

    assert [a.reasoning for a in results] == ["Weil A.", "Weil B."]


# --- Retry-then-succeed --------------------------------------------------------


def test_retry_then_succeed_calls_cli_twice_and_returns_analyses(caplog):
    """A first parse failure is retried once; the second, valid response wins."""
    articles = [make_article(0)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.side_effect = [
            _completed("this is not json at all"),
            _completed(_cli_stdout([_verdict(0)])),
        ]
        with caplog.at_level(logging.WARNING):
            results = ClaudeCodeAnalyzer().analyze(make_client(), articles)

    assert len(results) == 1
    assert run.call_count == 2
    assert any("retrying" in r.message for r in caplog.records)


# --- Give up -------------------------------------------------------------------


def test_give_up_after_second_failure_logs_error_and_yields_nothing(caplog):
    """Two parse failures -> ERROR logged, empty result, no exception (run continues)."""
    articles = [make_article(0), make_article(1)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.side_effect = [
            _completed("garbage one"),
            _completed("garbage two"),
        ]
        with caplog.at_level(logging.ERROR):
            results = ClaudeCodeAnalyzer().analyze(make_client(), articles)

    assert results == []
    assert run.call_count == 2
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "gave up" in errors[0].message


def test_schema_violation_is_treated_as_a_parse_failure():
    """An out-of-range score fails pydantic validation -> retry, then give up."""
    articles = [make_article(0)]
    bad = [_verdict(0, importance_score=99)]  # outside 0..10
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout(bad))
        results = ClaudeCodeAnalyzer().analyze(make_client(), articles)

    assert results == []
    assert run.call_count == 2  # first attempt + one retry


def test_missing_verdict_for_an_article_is_a_parse_failure():
    """One verdict for two articles is a schema mismatch -> no analyses."""
    articles = [make_article(0), make_article(1)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout([_verdict(0)]))
        results = ClaudeCodeAnalyzer().analyze(make_client(), articles)

    assert results == []
    assert run.call_count == 2


# --- Subprocess failure modes --------------------------------------------------


def test_non_zero_exit_is_a_batch_failure(caplog):
    """A non-zero `claude` exit is logged and drops the batch; run continues."""
    articles = [make_article(0)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(stdout="", returncode=1, stderr="not logged in")
        with caplog.at_level(logging.ERROR):
            results = ClaudeCodeAnalyzer().analyze(make_client(), articles)

    assert results == []
    assert run.call_count == 2
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_timeout_is_a_batch_failure():
    """A subprocess timeout is a batch failure, not a crash."""
    articles = [make_article(0)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.side_effect = subprocess.TimeoutExpired(cmd=["claude"], timeout=120)
        results = ClaudeCodeAnalyzer(timeout=120).analyze(make_client(), articles)

    assert results == []
    assert run.call_count == 2


def test_missing_claude_cli_is_a_batch_failure():
    articles = [make_article(0)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.side_effect = FileNotFoundError("claude")
        results = ClaudeCodeAnalyzer().analyze(make_client(), articles)

    assert results == []


# --- Alert computed in code ----------------------------------------------------


def test_is_alert_true_when_importance_meets_threshold_even_if_model_says_false():
    """The alert flag is computed in code from the score, never copied from the
    model's own is_alert (which here is deliberately False)."""
    articles = [make_article(0)]
    verdicts = [_verdict(0, importance_score=8, is_alert=False)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout(verdicts))
        results = ClaudeCodeAnalyzer(alert_threshold=7).analyze(make_client(alert_topics=[]), articles)

    assert results[0].is_alert is True


def test_is_alert_true_when_article_hits_an_alert_topic_below_threshold():
    """A low importance score still alerts if the article matches an alert_topic."""
    articles = [make_article(0, title="Beispiel AG kündigt großen Rückruf an", summary_text="")]
    verdicts = [_verdict(0, importance_score=2, is_alert=False)]
    client = make_client(alert_topics=["Rückruf"])
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout(verdicts))
        results = ClaudeCodeAnalyzer(alert_threshold=7).analyze(client, articles)

    assert results[0].is_alert is True


def test_model_is_alert_true_is_not_trusted_when_code_says_no():
    """Model claims alert, but importance is low and no topic matches -> not an alert."""
    articles = [make_article(0, title="Unaufgeregte Meldung", summary_text="nichts Dringendes")]
    verdicts = [_verdict(0, importance_score=1, is_alert=True)]
    client = make_client(alert_topics=["Rückruf"])
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout(verdicts))
        results = ClaudeCodeAnalyzer(alert_threshold=7).analyze(client, articles)

    assert results[0].is_alert is False


# --- Batching ------------------------------------------------------------------


def test_articles_are_split_into_batches_of_the_configured_size():
    """More than batch_size articles => multiple CLI calls, one per chunk."""
    articles = [make_article(i) for i in range(3)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.side_effect = [
            _completed(_cli_stdout([_verdict(0), _verdict(1)])),  # chunk of 2
            _completed(_cli_stdout([_verdict(0)])),  # chunk of 1
        ]
        results = ClaudeCodeAnalyzer(batch_size=2).analyze(make_client(alert_topics=[]), articles)

    assert run.call_count == 2
    assert len(results) == 3


def test_empty_article_list_makes_no_call():
    with patch("newspulse.analyzer.subprocess.run") as run:
        results = ClaudeCodeAnalyzer().analyze(make_client(), [])
    assert results == []
    run.assert_not_called()


def test_a_failing_batch_does_not_sink_the_other_batches():
    """One bad chunk drops only its own articles; the good chunk still returns."""
    articles = [make_article(i) for i in range(3)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.side_effect = [
            _completed(_cli_stdout([_verdict(0), _verdict(1)])),  # chunk 1 ok
            _completed("garbage"),  # chunk 2 attempt 1
            _completed("garbage"),  # chunk 2 retry -> give up
        ]
        results = ClaudeCodeAnalyzer(batch_size=2).analyze(make_client(alert_topics=[]), articles)

    assert len(results) == 2  # only the surviving chunk


# --- Backend selection ---------------------------------------------------------


def test_get_analyzer_defaults_to_the_subscription_backend():
    assert isinstance(get_analyzer(), ClaudeCodeAnalyzer)
    assert isinstance(get_analyzer(BACKEND_CLAUDE_CODE), ClaudeCodeAnalyzer)


def test_get_analyzer_selects_api_backend_by_config_value():
    assert isinstance(get_analyzer(BACKEND_CLAUDE_API), ClaudeApiAnalyzer)


def test_get_analyzer_rejects_unknown_backend():
    with pytest.raises(ValueError, match="unknown analyzer backend"):
        get_analyzer("nonsense")


def test_default_backend_id_matches_config():
    """The default config backend id resolves to the subscription analyzer."""
    from newspulse import config

    assert config.ANALYZER_BACKEND == BACKEND_CLAUDE_CODE


# --- Envelope / fence parsing --------------------------------------------------


def test_markdown_fenced_json_is_parsed():
    """Models often wrap JSON in a ```json fence; it is stripped before parsing."""
    articles = [make_article(0)]
    inner = "```json\n" + json.dumps([_verdict(0)]) + "\n```"
    stdout = json.dumps({"type": "result", "is_error": False, "result": inner})
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(stdout)
        results = ClaudeCodeAnalyzer().analyze(make_client(alert_topics=[]), articles)

    assert len(results) == 1


def test_cli_error_envelope_is_a_batch_failure():
    """An is_error envelope from the CLI is a backend failure (retry, then drop)."""
    articles = [make_article(0)]
    with patch("newspulse.analyzer.subprocess.run") as run:
        run.return_value = _completed(_cli_stdout([_verdict(0)], is_error=True))
        results = ClaudeCodeAnalyzer().analyze(make_client(), articles)

    assert results == []
    assert run.call_count == 2
