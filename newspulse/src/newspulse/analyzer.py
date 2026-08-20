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
import os
import subprocess
from pathlib import Path
from collections.abc import Sequence
from importlib import resources
from string import Template
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from . import brain, config
from .quota import is_quota_error
from .models import Article, Category, Client
from .schemas import Analysis, ArticleVerdict, BatchVerdict

_log = logging.getLogger(__name__)

# --- Named constants (the "why" lives next to each) ----------------------------

# Backend ids, matched against config.ANALYZER_BACKEND. Kept here so callers name
# a backend by constant, never by a bare string spread across the package.
BACKEND_CLAUDE_CODE = "claude_code"
BACKEND_CLAUDE_API = "claude_api"
BACKEND_GEMINI = "gemini"

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


def _prompt_template() -> Template:
    """Load the prompt template shipped in the package, composed against the brain.

    Deliberately *not* cached, the way the other nine prompt loaders are not.
    This one used to be ``@lru_cache(maxsize=1)``, which was correct while the
    blocks only changed with a deployment. BRN-02 made them a field a consultant
    edits, and runs happen in threads inside the long-lived web process — so a
    cached template meant an edited standard reached every other prompt on the
    next generated text and reached article analysis, the highest-volume path
    there is, only on the next container restart. Re-reading one small file and
    expanding a handful of markers costs microseconds against a model call that
    takes seconds.
    """
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text(encoding="utf-8")
    return Template(brain.compose(text))


def _chunks(items: Sequence[Article], size: int) -> list[Sequence[Article]]:
    """Split ``items`` into consecutive chunks of at most ``size`` (>=1)."""
    step = max(1, size)
    return [items[i : i + step] for i in range(0, len(items), step)]


def _matches_alert_topic(article: Article, alert_topics: Sequence[str]) -> bool:
    """True if any of the client's alert_topics appears in the article text.

    Computed in code (caseless substring over title + feed summary) so the alert
    decision is deterministic and auditable — it does not trust the model's own
    is_alert guess. Only feed-provided text is searched; no body is fetched or
    stored (no-scrape rule). Topics are stripped and casefolded before matching:
    casefold (not lower) folds German casing such as ß correctly, and stripping
    the topic keeps a whitespace-padded config entry (' Rückruf ') matchable —
    the guard and the containment check use the same normalized value."""
    haystack = f"{article.title or ''} {article.summary_text or ''}".casefold()
    for topic in alert_topics:
        needle = topic.strip().casefold()
        if needle and needle in haystack:
            return True
    return False


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
        # Set when the provider reports it is out of capacity. Read by
        # FallbackAnalyzer, which cannot learn this from a raised exception
        # because the protocol requires analyze() to swallow batch failures.
        self.quota_exhausted = False
        # Batches that were dropped because the backend failed, and the last
        # reason. The protocol says analyze() must never raise — a failing batch
        # must not sink the sweep — but "never raise" was implemented as "never
        # tell anyone": a `claude` CLI that is missing, unauthenticated or timing
        # out produced zero analyses, an empty error list, and therefore a run
        # recorded as OK. The dashboard then showed "Feeds ok" over an empty day,
        # which is indistinguishable from a quiet one. The caller reads these to
        # put the failure in the run's errors, where it belongs.
        self.failed_batches = 0
        self.last_error: str | None = None

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
            if self.quota_exhausted:
                # Stop asking. The limit is per account, not per batch, so every
                # remaining chunk would spend a doomed call and its timeout to
                # learn the same thing.
                break
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
                if is_quota_error(exc):
                    # Out of capacity is a run-level condition, not a batch-level
                    # one: every remaining batch will hit the same wall. Record
                    # it and stop immediately — retrying cannot succeed and only
                    # spends another timeout, and the retry loop would do that
                    # once per batch for the rest of the sweep.
                    self.quota_exhausted = True
                    self.failed_batches += 1
                    self.last_error = str(exc)
                    _log.error(
                        "analysis batch for client %r hit the provider's usage limit: %s; "
                        "dropping %d article(s)",
                        label, exc, len(chunk),
                    )
                    return []
                if attempt < _MAX_ATTEMPTS:
                    _log.warning(
                        "analysis batch for client %r failed (attempt %d/%d): %s; retrying",
                        label, attempt, _MAX_ATTEMPTS, exc,
                    )
                    continue
                self.failed_batches += 1
                self.last_error = str(exc)
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
        payload = strip_code_fence(raw_text)
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
                    tonality=verdict.tonality,
                    reasoning=verdict.reasoning,
                )
            )
        return analyses

    def _compute_is_alert(self, article: Article, client: Client, importance_score: int) -> bool:
        """Alert iff the article hits a client alert_topic OR importance clears the
        configured threshold. Recomputed in code from the returned score/topics,
        never copied from the model's own is_alert flag, so it stays tunable.

        Deliberately independent of the model's is_relevant judgment. alert_topics
        are operator-chosen, high-stakes keywords (e.g. "Rückruf", "Insolvenz")
        where a missed alert costs more than a rare name-coincidence false positive,
        so a topic hit fires even when the model marked the article non-relevant.
        This is the AC's definition verbatim (is_alert = OR of the two code-computed
        conditions); gating it on relevance would reintroduce exactly the model
        trust the code path exists to remove.

        Compared against the model's *raw* score, deliberately. Weighting it by
        outlet tier (newspulse.outlets) was measured against a hand-labelled month
        of real coverage and made this decision strictly worse: financial wires
        publish genuine corporate news — job cuts, a regulator's reprimand —
        alongside their ticker filler, so demoting the outlet dropped real stories,
        while promoting the national dailies lifted their routine share-price
        pieces in. Tier belongs in the *ranking* of the feed, where nothing is
        lost, not in a threshold that decides what a human never sees."""
        if importance_score >= self.alert_threshold:
            return True
        return _matches_alert_topic(article, getattr(client, "alert_topics", []) or [])


def claude_env() -> dict[str, str]:
    """The environment for a ``claude`` subprocess, selecting the account.

    The CLI reads its credentials from the directory ``CLAUDE_CONFIG_DIR`` names
    (default ``~/.claude``). Setting it therefore picks *which subscription
    login* to run under — it never introduces an API key, and there is no
    billing difference: an unset value simply inherits whatever account the
    process was started with.
    """
    env = dict(os.environ)
    configured = (config.CLAUDE_CONFIG_DIR or "").strip()
    if configured:
        env["CLAUDE_CONFIG_DIR"] = str(Path(configured).expanduser())
    return env


def invoke_claude_cli(prompt: str, *, timeout: float = _SUBPROCESS_TIMEOUT_SECONDS) -> str:
    """Run one ``claude -p`` call and return the model's text.

    The single place the subscription CLI is invoked, shared by the analyzer and
    the advisor so both inherit the same guarantees: a fixed argv (never a shell
    string), a wall-clock ceiling, and no token or API endpoint touched here —
    the CLI subprocess owns authentication.
    """
    argv = ["claude", "-p", prompt, "--output-format", "json"]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, prompt is an arg
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,  # explicit: the prompt is an argv element, never a shell command
            env=claude_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"claude -p timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise BackendError("claude CLI not found on PATH") from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()[:500]
        raise BackendError(f"claude -p exited {completed.returncode}: {stderr}")
    return _extract_cli_result(completed.stdout)


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
        return invoke_claude_cli(prompt, timeout=self.timeout)


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


def strip_code_fence(text: str) -> str:
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


def invoke_with_fallback(prompt: str, *, timeout: float = _SUBPROCESS_TIMEOUT_SECONDS) -> str:
    """``invoke_claude_cli``, degrading to Gemini when the subscription is spent.

    The one-shot counterpart to :class:`FallbackAnalyzer`, used by the advisor.
    Unlike the analyzer there is no batching here — a brief is a single call — so
    the fallback is a plain retry against the other provider rather than a
    sticky mode.

    Any non-quota failure propagates untouched, so a broken prompt still fails
    loudly instead of being re-run on a metered account.
    """
    try:
        return invoke_claude_cli(prompt, timeout=timeout)
    except AnalyzerError as exc:
        if not (is_quota_error(exc) and config.gemini_configured()):
            raise
        from . import gemini

        _log.warning("subscription is out of quota (%s); generating this one with Gemini", exc)
        return gemini.generate(prompt, timeout=timeout)


class GeminiAnalyzer(_BaseClaudeAnalyzer):
    """Fallback backend: Gemini over REST.

    Inherits every auditable decision from the shared base — batch chunking,
    schema validation, and the alert flag computed in code rather than taken
    from the model. Only the transport differs, which is the point: a sweep that
    fell back must produce rows indistinguishable from a normal one, or the
    archive quietly becomes two datasets.
    """

    def __init__(
        self,
        *,
        alert_threshold: int | None = None,
        batch_size: int | None = None,
        model: str | None = None,
        timeout: float = _SUBPROCESS_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(alert_threshold=alert_threshold, batch_size=batch_size)
        self.model = model
        self.timeout = timeout

    def _invoke(self, prompt: str) -> str:
        from . import gemini

        return gemini.generate(prompt, timeout=self.timeout, model=self.model)


class FallbackAnalyzer:
    """Runs ``primary``, and switches to ``secondary`` only when quota is out.

    A wrapper rather than a branch inside each backend, so the fallback rule
    lives in exactly one readable place and each analyzer stays a plain client of
    its own provider.

    The switch is *sticky* for the life of the object. A sweep is dozens of
    batches; once the subscription says the limit is reached, every subsequent
    batch would hit the same wall, and re-attempting each one would add a failed
    call and its timeout to every batch for the rest of the run.
    """

    def __init__(self, primary: Analyzer, secondary: Analyzer) -> None:
        self.primary = primary
        self.secondary = secondary
        self.switched = False

    @property
    def alert_threshold(self) -> int:
        return self.primary.alert_threshold

    @property
    def batch_size(self) -> int:
        return self.primary.batch_size

    @property
    def failed_batches(self) -> int:
        """Failures of whichever backend is actually answering.

        Not the sum: a run that switched to the fallback and then worked is a
        successful run, and reporting the primary's exhausted quota as a run
        error would mark every fallback day as degraded. What the caller needs to
        know is whether analyses are being produced *now*.
        """
        active = self.secondary if self.switched else self.primary
        return getattr(active, "failed_batches", 0)

    @property
    def last_error(self) -> str | None:
        active = self.secondary if self.switched else self.primary
        return getattr(active, "last_error", None)

    def analyze(self, client: Client, articles: Sequence[Article]) -> list[Analysis]:
        """Analyse with the primary; on exhausted quota, redo it with the fallback.

        The primary is asked via its ``quota_exhausted`` flag rather than by
        catching an exception, because the protocol requires ``analyze`` to
        swallow batch failures and return what it managed — so a quota failure
        arrives as an empty list, not as something raisable.

        A run that trips the limit part-way is re-analysed from the start on the
        fallback, and the primary's partial result is discarded rather than
        merged. Merging would mean one client's coverage was scored by two
        different models on the same day, and the scores are compared against
        each other — in the ranking, in the alert threshold, in share of voice.
        A consistent second-choice reading beats a spliced one.
        """
        if self.switched:
            return self.secondary.analyze(client, articles)

        results = self.primary.analyze(client, articles)
        if not getattr(self.primary, "quota_exhausted", False):
            return results

        _log.warning(
            "primary backend is out of quota; falling back to %s for the rest of this run",
            type(self.secondary).__name__,
        )
        self.switched = True
        return self.secondary.analyze(client, articles)


def get_analyzer(
    backend: str | None = None,
    *,
    alert_threshold: int | None = None,
    batch_size: int | None = None,
) -> Analyzer:
    """Construct the analyzer for the configured (or given) backend.

    Defaults to the subscription ``claude_code`` backend via config; the metered
    API backend is selected only by an explicit config value.

    When a Gemini key is configured and the chosen backend is not itself Gemini,
    the result is wrapped so an exhausted subscription degrades to the fallback
    instead of ending the sweep. Without a key this returns exactly what it
    always did — the fallback cannot engage by accident.
    """
    resolved = backend or config.ANALYZER_BACKEND
    if resolved == BACKEND_CLAUDE_CODE:
        primary: Analyzer = ClaudeCodeAnalyzer(
            alert_threshold=alert_threshold, batch_size=batch_size
        )
    elif resolved == BACKEND_CLAUDE_API:
        primary = ClaudeApiAnalyzer(alert_threshold=alert_threshold, batch_size=batch_size)
    elif resolved == BACKEND_GEMINI:
        return GeminiAnalyzer(alert_threshold=alert_threshold, batch_size=batch_size)
    else:
        raise ValueError(f"unknown analyzer backend {resolved!r}")

    if config.gemini_configured():
        return FallbackAnalyzer(
            primary,
            GeminiAnalyzer(alert_threshold=alert_threshold, batch_size=batch_size),
        )
    return primary


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
    "GeminiAnalyzer",
    "FallbackAnalyzer",
    "get_analyzer",
    "BACKEND_CLAUDE_CODE",
    "BACKEND_CLAUDE_API",
    "BACKEND_GEMINI",
    # Public because every prompt in the app asks for bare JSON and every model
    # occasionally wraps it in a fence anyway; the one-shot callers (advisor,
    # angles) have no retry to absorb that, so they share this rather than each
    # growing their own copy.
    "strip_code_fence",
]
