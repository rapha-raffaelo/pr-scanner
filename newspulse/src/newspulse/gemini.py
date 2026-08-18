"""The Gemini fallback provider, spoken to over plain REST.

No SDK. ``ingest`` and ``logos`` already reach the network with ``urllib``, and
two HTTP calls do not justify a dependency that has to be pinned, audited,
carried into the image and kept in step with a lockfile. The request bodies
below are the whole integration surface.

The two shapes mirror the Claude paths exactly, so callers swap providers
without learning a second vocabulary:

* :func:`generate` — one prompt in, the model's text out (analyzer, advisor)
* :func:`stream` — the same, yielding text deltas as they arrive (Captain Comms)

Errors are raised as :class:`newspulse.analyzer.BackendError` so the existing
retry and reporting logic treats a Gemini failure exactly like a Claude one.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterator

from . import config

_log = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
_TIMEOUT = 180.0

# The key travels in a header, never in the query string: URLs end up in proxy
# logs, error messages and crash reports, and a leaked key here is a metered
# account someone else can spend.
_KEY_HEADER = "x-goog-api-key"


def _body(prompt: str) -> bytes:
    return json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")


def _request(url: str, prompt: str, api_key: str | None = None) -> urllib.request.Request:
    from .analyzer import BackendError

    # A caller may bring its own key. The cross-check does: it is allowed to run
    # on a bare GEMINI_API_KEY, while the coverage fallback insists on the
    # namespaced one — two different things to consent to (see config).
    key = api_key or config.gemini_api_key()
    if not key:
        raise BackendError("Gemini is not configured (NEWSPULSE_GEMINI_API_KEY unset)")
    return urllib.request.Request(  # noqa: S310 - fixed https endpoint, not caller-supplied
        url,
        data=_body(prompt),
        headers={"Content-Type": "application/json", _KEY_HEADER: key},
        method="POST",
    )


def _raise_for_http(exc: urllib.error.HTTPError) -> None:
    """Re-raise an HTTP failure with the provider's own message attached.

    The body matters: Gemini puts "RESOURCE_EXHAUSTED" and quota detail there,
    and :func:`newspulse.quota.is_quota_error` reads the message text. Dropping
    the body would make the fallback's own exhaustion indistinguishable from any
    other 4xx — which is how a fallback ends up silently doing nothing.
    """
    from .analyzer import BackendError

    try:
        detail = exc.read().decode("utf-8", "replace")[:500]
    except Exception:  # noqa: BLE001 — the original error is what matters
        detail = ""
    raise BackendError(f"Gemini HTTP {exc.code}: {detail or exc.reason}") from exc


def _text_from(payload: dict) -> str:
    """Pull the assistant text out of a generateContent response.

    Shape: ``{"candidates":[{"content":{"parts":[{"text": ...}]}}]}``. Parts are
    joined because the API is free to split one answer across several.
    """
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def generate(
    prompt: str,
    *,
    timeout: float = _TIMEOUT,
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    """One prompt in, the model's text out. Raises ``BackendError`` on failure."""
    from .analyzer import BackendError

    name = model or config.GEMINI_MODEL
    url = f"{_ENDPOINT}/{name}:generateContent"
    try:
        with urllib.request.urlopen(
            _request(url, prompt, api_key), timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        _raise_for_http(exc)
    except urllib.error.URLError as exc:
        raise BackendError(f"Gemini unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise BackendError(f"Gemini timed out after {timeout}s") from exc
    except json.JSONDecodeError as exc:
        raise BackendError(f"Gemini did not return JSON: {exc}") from exc

    text = _text_from(payload)
    if not text:
        # An empty candidate list usually means the safety filter blocked it;
        # say so rather than returning "" and letting the parser blame itself.
        raise BackendError(f"Gemini returned no text (finish: {payload.get('promptFeedback')})")
    return text


def stream(
    prompt: str, *, timeout: float = _TIMEOUT, model: str | None = None
) -> Iterator[str]:
    """Yield text deltas as they arrive. Raises ``BackendError`` before the first.

    Uses ``alt=sse``, so the response is ``data: {...}`` lines carrying the same
    envelope as :func:`generate`. Unlike the Claude CLI, each chunk is already a
    delta rather than the whole message so far, so callers must not diff it.
    """
    from .analyzer import BackendError

    name = model or config.GEMINI_MODEL
    url = f"{_ENDPOINT}/{name}:streamGenerateContent?alt=sse"
    try:
        response = urllib.request.urlopen(_request(url, prompt), timeout=timeout)
    except urllib.error.HTTPError as exc:
        _raise_for_http(exc)
    except urllib.error.URLError as exc:
        raise BackendError(f"Gemini unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise BackendError(f"Gemini timed out after {timeout}s") from exc

    with response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[len("data:") :].strip()
            if not body or body == "[DONE]":
                continue
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                # A malformed frame is noise, not a failure: relaying it as an
                # error would end a working answer over one bad line.
                _log.debug("gemini: skipping unparsable sse frame")
                continue
            delta = _text_from(payload)
            if delta:
                yield delta


def search(
    prompt: str, *, timeout: float = _TIMEOUT, model: str | None = None
) -> tuple[str, list[tuple[str, str]]]:
    """Answer with the web, and hand back the pages it used.

    The same call as :func:`generate` with Google's grounding tool switched on.
    Returns ``(text, [(url, title), ...])`` — the sources are not decoration: a
    profile field filled from the internet is only worth having if the reader can
    open the page it came from, and this is the only provider in the stack that
    returns them.
    """
    from .analyzer import BackendError

    name = model or config.review_model()
    url = f"{_ENDPOINT}/{name}:generateContent"
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            _KEY_HEADER: config.review_api_key(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        _raise_for_http(exc)
    except urllib.error.URLError as exc:
        raise BackendError(f"Gemini unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise BackendError(f"Gemini timed out after {timeout}s") from exc
    except json.JSONDecodeError as exc:
        raise BackendError(f"Gemini did not return JSON: {exc}") from exc

    text = _text_from(payload)
    if not text:
        raise BackendError(
            f"Gemini returned no text (finish: {payload.get('promptFeedback')})"
        )
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in (
        payload.get("candidates", [{}])[0]
        .get("groundingMetadata", {})
        .get("groundingChunks", [])
    ):
        web = chunk.get("web") or {}
        uri, title = web.get("uri", ""), web.get("title", "")
        if uri and uri not in seen:
            seen.add(uri)
            sources.append((uri, title))
    return text, sources


__all__ = ["generate", "stream"]
