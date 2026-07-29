"""Streaming a ``claude -p`` answer, event by event.

The advisor's briefs take 30–90 seconds. Rendered as a page that is a wait; in a
drawer it would be a hang, because the reader cannot even navigate away from
something that has not painted. Streaming turns the same 60 seconds into
something that visibly works.

``--output-format stream-json --verbose`` emits NDJSON. What arrives is *not*
token deltas: the CLI re-emits the assistant message as each content block
completes, so text lands in chunks rather than characters. This module turns that
into the small set of events a UI actually needs — status, text, done, error —
and computes the delta itself, since the transport repeats the whole message
every time.

Nothing here parses the *content* as structure. A streamed answer is prose for a
human to read; the schema-validated path (``advisor.advise``) stays the one that
produces stored, machine-read briefs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass

from . import config
from . import config
from .analyzer import claude_env
from .quota import is_quota_error

_log = logging.getLogger(__name__)

# Long enough for a considered answer over a month of coverage, short enough that
# a wedged subprocess cannot hold a connection open indefinitely.
_STREAM_TIMEOUT = 180

# How much of the model's reply we are willing to relay. A runaway generation
# should not be able to fill the browser's memory through an open socket.
_MAX_CHARS = 60_000


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One thing worth telling the UI about."""

    kind: str  # "status" | "text" | "done" | "error"
    data: str = ""

    def to_sse(self) -> str:
        """This event as a Server-Sent Events frame.

        The payload is JSON-encoded even for plain text: an answer containing a
        newline would otherwise split into two SSE data lines and arrive
        mangled.
        """
        return f"event: {self.kind}\ndata: {json.dumps(self.data)}\n\n"


def _assistant_text(payload: dict) -> str:
    """The full assistant text carried by one event, ignoring thinking blocks."""
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)


def stream_gemini(
    prompt: str, *, timeout: int = _STREAM_TIMEOUT, t=lambda text: text
) -> Iterator[StreamEvent]:
    """The fallback drawer: the same event contract, backed by Gemini.

    Gemini's SSE frames are already deltas, so unlike the CLI path there is no
    diffing against what was emitted before — treating them as cumulative would
    repeat the whole answer on every frame.
    """
    from . import gemini

    emitted = 0
    try:
        yield StreamEvent("status", t("verbunden"))
        for delta in gemini.stream(prompt, timeout=timeout):
            emitted += len(delta)
            if emitted > _MAX_CHARS:
                yield StreamEvent("error", t("Antwort zu lang, abgebrochen."))
                return
            yield StreamEvent("text", delta)
        if not emitted:
            yield StreamEvent("error", t("Keine Antwort erhalten."))
            return
        yield StreamEvent("done", "")
    except Exception as exc:  # noqa: BLE001 — an open socket must never see a traceback
        _log.exception("gemini streaming failed")
        yield StreamEvent("error", f'{t("Unerwarteter Fehler")}: {exc}')


def stream_assistant(
    prompt: str, *, timeout: int = _STREAM_TIMEOUT, t=lambda text: text
) -> Iterator[StreamEvent]:
    """Stream from the subscription, switching to Gemini if its quota is spent.

    The switch is only attempted while nothing has been shown yet. Once a partial
    answer is on screen there is no honest way to swap providers mid-sentence:
    the reader would watch one model's half-finished thought be continued by
    another. In that case the error stands, and they can ask again.

    Status events do not count as output — they are replaced, not appended — so a
    fallback that happens after "connected" is still invisible to the reader.
    """
    if not config.gemini_configured():
        yield from stream_claude(prompt, timeout=timeout, t=t)
        return

    shown = False
    pending_error: str | None = None
    for event in stream_claude(prompt, timeout=timeout, t=t):
        if event.kind == "text":
            shown = True
        if event.kind == "error" and not shown:
            # Hold it back: if this turns out to be a quota error we will answer
            # from the other provider instead, and the reader must not see an
            # error that was recovered from.
            pending_error = event.data
            break
        yield event
        if event.kind in ("done", "error"):
            return

    if pending_error is None:
        return
    if not is_quota_error(pending_error):
        yield StreamEvent("error", pending_error)
        return

    _log.warning("subscription out of quota mid-drawer (%s); answering with Gemini", pending_error)
    yield StreamEvent("status", t("Ausweichmodell"))
    yield from stream_gemini(prompt, timeout=timeout, t=t)


def stream_claude(
    prompt: str, *, timeout: int = _STREAM_TIMEOUT, t=lambda text: text
) -> Iterator[StreamEvent]:
    """Run ``claude -p`` and yield events as the answer forms.

    Never raises: a failure is an ``error`` event, because the caller is an open
    HTTP connection and an exception there would drop the socket with nothing
    said. Always terminates with exactly one ``done`` or ``error``, so the
    browser can close the stream rather than waiting on a socket that will never
    speak again.

    ``t`` translates the status and error text. The strings this yields are read
    inside the drawer, so they follow the reader's language like the rest of the
    interface; a German "Zeitüberschreitung" in an English drawer is exactly the
    half-translated result the language work set out to avoid.
    """
    argv = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        # stream-json requires it; without it the CLI refuses to start.
        "--verbose",
    ]
    process = None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line buffered: the whole point is not to wait for EOF
            env=claude_env(),
            shell=False,
        )
    except FileNotFoundError:
        yield StreamEvent("error", t("claude CLI nicht gefunden."))
        return
    except OSError as exc:
        yield StreamEvent("error", f'{t("Start fehlgeschlagen")}: {exc}')
        return

    yield StreamEvent("status", t("verbunden"))
    emitted = ""
    try:
        for line in process.stdout or ():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # The CLI is entitled to print something that is not an event;
                # relaying it as an error would turn noise into a failure.
                continue

            kind = payload.get("type")
            if kind == "system" and payload.get("subtype") == "init":
                yield StreamEvent("status", t("denkt nach"))
            elif kind == "assistant":
                full = _assistant_text(payload)
                # The transport repeats the whole message; send only what is new.
                if full.startswith(emitted) and len(full) > len(emitted):
                    delta = full[len(emitted):]
                    emitted = full
                    yield StreamEvent("text", delta)
                elif full and not full.startswith(emitted):
                    # A rewrite rather than an append — restart cleanly instead
                    # of splicing two versions of the answer together.
                    emitted = full
                    yield StreamEvent("text", "\n" + full)
                if len(emitted) > _MAX_CHARS:
                    yield StreamEvent("error", t("Antwort zu lang, abgebrochen."))
                    return
            elif kind == "result" or "is_error" in payload:
                if payload.get("is_error"):
                    yield StreamEvent("error", str(payload.get("result") or t("Fehler")))
                    return

        code = process.wait(timeout=timeout)
        if code != 0 and not emitted:
            stderr = (process.stderr.read() if process.stderr else "")[:300]
            yield StreamEvent("error", f'{t("claude beendet mit")} {code}: {stderr}'.strip())
            return
        if not emitted:
            yield StreamEvent("error", t("Keine Antwort erhalten."))
            return
        yield StreamEvent("done", "")
    except subprocess.TimeoutExpired:
        yield StreamEvent("error", f'{t("Zeitüberschreitung nach")} {timeout}s.')
    except Exception as exc:  # noqa: BLE001 — an open socket must never see a traceback
        _log.exception("streaming failed")
        yield StreamEvent("error", f'{t("Unerwarteter Fehler")}: {exc}')
    finally:
        # The reader may disconnect mid-answer (closed drawer, navigation). Kill
        # the subprocess rather than leaving a `claude` process running for the
        # rest of the server's life.
        if process is not None and process.poll() is None:
            process.kill()


__all__ = ["StreamEvent", "stream_assistant", "stream_claude", "stream_gemini"]
