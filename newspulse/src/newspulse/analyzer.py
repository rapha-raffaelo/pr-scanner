"""The analysis layer: a pluggable analyzer with a subscription-first backend.

The whole product turns on this seam. An ``Analyzer`` reads a batch of candidate
articles for one client and decides — per article — whether the story genuinely
concerns the client, what it is, and how much it matters. Two implementations sit
behind the ``Analyzer`` Protocol:

* ``ClaudeCodeAnalyzer`` (default) shells out to the user's installed ``claude``
  CLI in headless mode (``claude -p "<prompt>" --output-format json``). This is
  the *subscription-authorized* path: it runs on the Claude Code subscription the
  operator already pays for. It NEVER reads an on-disk OAuth token, never sets a
  spoofed API header, and never contacts an Anthropic API endpoint itself — it
  only launches the ``claude`` subprocess, which owns its own auth.
* ``ClaudeApiAnalyzer`` (opt-in) hits the metered Anthropic API through the SDK.
  It exists behind the same Protocol for the day this graduates to metered
  billing, and is off by default (see ``config.ANALYZER_BACKEND``).

Failure handling is deliberately forgiving: a single bad batch (parse failure,
schema violation, non-zero CLI exit, timeout) is retried once and then, on a
second failure, logged at ERROR and dropped — the daily run continues rather than
aborting over one flaky call.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Sequence
from functools import lru_cache
from importlib import resources
from string import Template
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from . import config
from .models import Article, Category, Client
from .schemas import Analysis, ArticleVerdict, BatchVerdict

_log = logging.getLogger(__name__)

# --- Named constants (the "why" lives next to each) ----------------------------

# Backend ids, matched against config.ANALYZER_BACKEND. Kept here so callers name
# a backend by constant, never by a bare string spread across the package.
BACKEND_CLAUDE_CODE = "claude_code"
BACKEND_CLAUDE_API = "claude_api"

# One retry, then give up. A batch gets two attempts total: the first, plus one
# retry on a parse/schema/subprocess failure. A second failure is logged and the
# batch yields nothing rather than raising and aborting the daily run.
_MAX_ATTEMPTS = 2

# Wall-clock ceiling for a single `claude -p` call. A batch of ~20 short items is
# comfortably interactive; 120s leaves headroom for a cold CLI start without ever
# letting one hung call stall the whole sweep.
_SUBPROCESS_TIMEOUT_SECONDS = 120

# Metered-API defaults (ClaudeApiAnalyzer only). Sonnet is the cost-sensible tier
# for a bounded classification task; max_tokens caps a batch's JSON response.
_API_MODEL = "claude-sonnet-5"
_API_MAX_TOKENS = 4096

# The prompt template ships alongside the code as an editable asset.
_PROMPT_RESOURCE = "prompts/analysis.txt"


class AnalyzerError(Exception):
    """Base for recoverable batch failures (retry once, then drop the batch)."""


class BackendError(AnalyzerError):
    """The backend could not produce output: non-zero exit, timeout, missing CLI."""


class ParseError(AnalyzerError):
    """The output was produced but could not be parsed or did not match the schema."""


@runtime_checkable
class Analyzer(Protocol):
    """Reads candidate articles for one client and returns one Analysis each.

    Implementations must never raise on a bad batch: they log and return the
    analyses they could produce (possibly none), so the daily job is fault
    isolated per batch.
    """

    def analyze(self, client: Client, articles: Sequence[Article]) -> list[Analysis]: ...


@lru_cache(maxsize=1)
def _prompt_template() -> Template:
    """Load and cache the prompt template shipped in the package."""
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text(encoding="utf-8")
    return Template(text)


def _chunks(items: Sequence[Article], size: int) -> list[Sequence[Article]]:
    """Split ``items`` into consecutive chunks of at most ``size`` (>=1)."""
    step = max(1, size)
    return [items[i : i + step] for i in range(0, len(items), step)]


def _matches_alert_topic(article: Article, alert_topics: Sequence[str]) -> bool:
    """True if any of the client's alert_topics appears in the article text.

    Computed in code (case-insensitive substring over title + feed summary) so
    the alert decision is deterministic and auditable — it does not trust the
    model's own is_alert guess. Only feed-provided text is searched; no body is
    fetched or stored (no-scrape rule)."""
    haystack = f"{article.title or ''} {article.summary_text or ''}".lower()
    return any(topic.strip() and topic.lower() in haystack for topic in alert_topics)


class _BaseClaudeAnalyzer:
    """Shared prompt-building, parsing, alert logic, and retry loop.

    Subclasses differ only in ``_invoke``: how a rendered prompt becomes the
    model's raw JSON text. Everything auditable — the batch chunking, the schema
    validation trust boundary, and the code-computed alert flag — lives here so
    both backends behave identically once the text comes back.
    """

    def __init__(self, *, alert_threshold: int | None = None, batch_size: int | None = None) -> None:
        self.alert_threshold = (
            alert_threshold if alert_threshold is not None else config.ALERT_THRESHOLD
        )
        self.batch_size = batch_size if batch_size is not None else config.BATCH_SIZE

    # -- Subclass hook ----------------------------------------------------------

    def _invoke(self, prompt: str) -> str:  # pragma: no cover - abstract
        """Run ``prompt`` and return the model's raw response text (the JSON we
        asked for). Raise BackendError on a backend failure, ParseError if the
        transport envelope itself is unreadable."""
        raise NotImplementedError

    # -- Orchestration ----------------------------------------------------------

    def analyze(self, client: Client, articles: Sequence[Article]) -> list[Analysis]:
        """Analyze every candidate article for ``client``, batching by size.

        More than ``batch_size`` articles are split across multiple calls; a
        failing batch is isolated (logged, dropped) and never sinks the others.
        """
        if not articles:
            return []
        results: list[Analysis] = []
        for chunk in _chunks(articles, self.batch_size):
            results.extend(self._analyze_batch(client, chunk))
        return results

    def _analyze_batch(self, client: Client, chunk: Sequence[Article]) -> list[Analysis]:
        """One batched call with a single retry, then give up (return [])."""
        prompt = self._render_prompt(client, chunk)
        label = getattr(client, "name", "?")
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                raw_text = self._invoke(prompt)
                batch = self._parse_batch(raw_text)
                return self._to_analyses(client, chunk, batch)
            except AnalyzerError as exc:
                if attempt < _MAX_ATTEMPTS:
                    _log.warning(
                        "analysis batch for client %r failed (attempt %d/%d): %s; retrying",
                        label, attempt, _MAX_ATTEMPTS, exc,
                    )
                    continue
                _log.error(
                    "analysis batch for client %r gave up after %d attempts: %s; "
                    "dropping %d article(s), run continues",
                    label, _MAX_ATTEMPTS, exc, len(chunk),
                )
                return []
        return []  # unreachable, but keeps the type checker honest

    # -- Prompt rendering -------------------------------------------------------

    def _render_prompt(self, client: Client, chunk: Sequence[Article]) -> str:
        return _prompt_template().safe_substitute(
            client_profile=_build_client_profile(client),
            articles=_build_articles_block(chunk),
            categories=", ".join(c.value for c in Category),
            max_articles=self.batch_size,
        )

    # -- Parsing (the trust boundary) -------------------------------------------

    def _parse_batch(self, raw_text: str) -> BatchVerdict:
        """Parse and schema-validate the model's JSON. Any failure -> ParseError,
        which triggers the single retry and, on a second failure, the drop."""
        payload = _strip_code_fence(raw_text)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParseError(f"response was not valid JSON: {exc}") from exc
        verdicts = _coerce_verdict_list(data)
        try:
            return BatchVerdict(verdicts=verdicts)
        except ValidationError as exc:
            raise ParseError(f"response failed schema validation: {exc}") from exc

    def _to_analyses(
        self, client: Client, chunk: Sequence[Article], batch: BatchVerdict
    ) -> list[Analysis]:
        """Map each verdict back to its article and compute the alert flag in code.

        Requires exactly one verdict per candidate id; a mismatch is a schema
        failure (ParseError) so the batch retries rather than persisting a partial
        or misaligned result."""
        by_id: dict[int, ArticleVerdict] = {}
        for verdict in batch.verdicts:
            if verdict.id in by_id:
                raise ParseError(f"duplicate verdict id {verdict.id}")
            by_id[verdict.id] = verdict
        expected = set(range(len(chunk)))
        if set(by_id) != expected:
            raise ParseError(
                f"expected one verdict per article {sorted(expected)}, got {sorted(by_id)}"
            )

        client_id = getattr(client, "id", None)
        analyses: list[Analysis] = []
        for idx, article in enumerate(chunk):
            verdict = by_id[idx]
            is_alert = self._compute_is_alert(article, client, verdict.importance_score)
            analyses.append(
                Analysis(
                    article_id=getattr(article, "id", None),
                    client_id=client_id,
                    is_relevant=verdict.is_relevant,
                    summary=verdict.summary,
                    category=verdict.category,
                    relevance_score=verdict.relevance_score,
                    importance_score=verdict.importance_score,
                    is_alert=is_alert,
                    reasoning=verdict.reasoning,
                )
            )
        return analyses

    def _compute_is_alert(self, article: Article, client: Client, importance_score: int) -> bool:
        """Alert iff the article hits a client alert_topic OR importance clears the
        configured threshold. Recomputed in code from the returned score/topics,
        never copied from the model's own is_alert flag, so it stays tunable."""
        if importance_score >= self.alert_threshold:
            return True
        return _matches_alert_topic(article, getattr(client, "alert_topics", []) or [])


class ClaudeCodeAnalyzer(_BaseClaudeAnalyzer):
    """Default backend: shell out to the subscription-authorized ``claude`` CLI.

    Invocation is exactly ``claude -p "<prompt>" --output-format json`` under a
    timeout. No token is read from disk, no API header is set, and no Anthropic
    endpoint is contacted here — the CLI subprocess owns authentication.
    """

    def __init__(
        self,
        *,
        alert_threshold: int | None = None,
        batch_size: int | None = None,
        timeout: float = _SUBPROCESS_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(alert_threshold=alert_threshold, batch_size=batch_size)
        self.timeout = timeout

    def _invoke(self, prompt: str) -> str:
        argv = ["claude", "-p", prompt, "--output-format", "json"]
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, prompt is an arg
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"claude -p timed out after {self.timeout}s") from exc
        except FileNotFoundError as exc:
            raise BackendError("claude CLI not found on PATH") from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:500]
            raise BackendError(f"claude -p exited {completed.returncode}: {stderr}")
        return _extract_cli_result(completed.stdout)


class ClaudeApiAnalyzer(_BaseClaudeAnalyzer):
    """Opt-in metered backend: the Anthropic API via the SDK.

    Off by default. The ``anthropic`` SDK is imported lazily so the subscription
    path (and its tests) never require it to be installed.
    """

    def __init__(
        self,
        *,
        alert_threshold: int | None = None,
        batch_size: int | None = None,
        model: str = _API_MODEL,
        max_tokens: int = _API_MAX_TOKENS,
    ) -> None:
        super().__init__(alert_threshold=alert_threshold, batch_size=batch_size)
        self.model = model
        self.max_tokens = max_tokens

    def _invoke(self, prompt: str) -> str:
        try:
            import anthropic  # noqa: PLC0415 - lazy: subscription path must not require it
        except ImportError as exc:
            raise BackendError(
                "ClaudeApiAnalyzer requires the 'anthropic' package (metered backend)"
            ) from exc
        try:
            client = anthropic.Anthropic()
            message = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AnthropicError as exc:
            raise BackendError(f"Anthropic API call failed: {exc}") from exc
        parts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
        if not parts:
            raise ParseError("Anthropic API returned no text content")
        return "".join(parts)


# --- Module-level parsing helpers ----------------------------------------------


def _build_client_profile(client: Client) -> str:
    """Render the client profile block passed into the prompt."""
    aliases = ", ".join(getattr(client, "aliases", []) or []) or "—"
    alert_topics = ", ".join(getattr(client, "alert_topics", []) or []) or "—"
    return (
        f"Name: {getattr(client, 'name', '')}\n"
        f"Branche: {getattr(client, 'industry', None) or '—'}\n"
        f"Aliasse: {aliases}\n"
        f"Alarm-Themen: {alert_topics}"
    )


def _build_articles_block(chunk: Sequence[Article]) -> str:
    """Render the numbered candidate-article block. ids are 0-based list indices,
    so verdicts map straight back onto ``chunk``."""
    entries = []
    for idx, article in enumerate(chunk):
        summary = getattr(article, "summary_text", None) or "—"
        entries.append(
            f"[id {idx}]\n"
            f"Titel: {getattr(article, 'title', '')}\n"
            f"Quelle: {getattr(article, 'source', '')}\n"
            f"Feed-Zusammenfassung: {summary}"
        )
    return "\n\n".join(entries)


def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing markdown code fence if the model wrapped its JSON
    in one (```json ... ```), a common and harmless formatting habit."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # Drop the opening fence line (``` or ```json) and a trailing fence if present.
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _coerce_verdict_list(data: object) -> object:
    """Accept either a bare JSON array or a one-key object wrapping the array, and
    return the list for pydantic to validate. Anything else -> ParseError."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("verdicts", "analyses", "articles", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        # A single-key object whose only value is a list (any key name).
        list_values = [v for v in data.values() if isinstance(v, list)]
        if len(list_values) == 1:
            return list_values[0]
    raise ParseError("expected a JSON array of verdicts")


def get_analyzer(
    backend: str | None = None,
    *,
    alert_threshold: int | None = None,
    batch_size: int | None = None,
) -> Analyzer:
    """Construct the analyzer for the configured (or given) backend.

    Defaults to the subscription ``claude_code`` backend via config; the metered
    API backend is selected only by an explicit config value."""
    resolved = backend or config.ANALYZER_BACKEND
    if resolved == BACKEND_CLAUDE_CODE:
        return ClaudeCodeAnalyzer(alert_threshold=alert_threshold, batch_size=batch_size)
    if resolved == BACKEND_CLAUDE_API:
        return ClaudeApiAnalyzer(alert_threshold=alert_threshold, batch_size=batch_size)
    raise ValueError(f"unknown analyzer backend {resolved!r}")


def _extract_cli_result(stdout: str) -> str:
    """Pull the assistant's text out of the ``--output-format json`` envelope.

    ``claude -p ... --output-format json`` wraps the reply in a result object
    (``{"type":"result","result":"<text>", ...}``); the inner ``result`` is the
    JSON we asked the model for. A malformed envelope or an error result is a
    recoverable failure (retry once)."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ParseError(f"claude -p did not return JSON on stdout: {exc}") from exc
    if isinstance(envelope, dict) and "result" in envelope:
        if envelope.get("is_error"):
            raise BackendError(f"claude -p reported an error: {envelope.get('result')!r}")
        result = envelope["result"]
        if not isinstance(result, str):
            raise ParseError("claude -p envelope 'result' was not a string")
        return result
    raise ParseError("unexpected claude -p output envelope shape")


__all__ = [
    "Analyzer",
    "ClaudeCodeAnalyzer",
    "ClaudeApiAnalyzer",
    "AnalyzerError",
    "BackendError",
    "ParseError",
    "get_analyzer",
    "BACKEND_CLAUDE_CODE",
    "BACKEND_CLAUDE_API",
]
