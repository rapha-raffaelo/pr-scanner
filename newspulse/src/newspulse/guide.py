"""The communications guide: what a client wants to say, and how.

Keywords describe what is written *about* a mandate. They say nothing about what
it wants to say, what it never says, or in what register — and every generated
text here needs exactly that. Without it the angle prompt, the advisory and
Captain Comms each guess a voice, freshly, on every call.

Two ways in, one result. The consultant writes the guide himself, or uploads what
the client already has — a brand guideline, a messaging house, a language policy —
and the document is distilled into a proposal he then edits. Uploaded material is
never applied directly: same posture as the client import, which previews before
it writes, because a document can contradict what is already there and only a
person can settle that.

Why it is short
---------------
The guide is prepended to every prompt, for every mandate, every day. An
eighteen-page guideline in each call would cost a multiple of the analysis it is
supposed to improve, and would bury the actual question. So the distillation
targets a few hundred words, the field shows its budget, and the source documents
stay behind it as the long version.

What is stored
--------------
The extracted text, never the file. The text is what the model reads and what a
re-run needs; the layout is not, and keeping binaries out of a SQLite file that is
copied on every deploy is worth more than being able to hand the original back.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from string import Template

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, gemini
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .models import Client, GuideSource
from .schemas import GuideVerdict, PersonalMessage

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/guide.txt"

_CHECK_RESOURCE = "prompts/guide_check.txt"

#: What :func:`check_guide` returns for a mandate that has no guide at all: no
#: verdict, and no model that gave one. A distinct state on purpose — "nothing to
#: object with" must never arrive at the page looking like "no objections", which
#: is the failure this whole check exists to prevent.
NOT_CHECKED: tuple[GuideVerdict | None, str] = (None, "")

#: Hard ceiling on the stored guide. Every prompt carries it, so this is a cost
#: and an attention budget at once: past a few hundred words it stops being a
#: brief and starts competing with the question the model was asked.
GUIDE_MAX_CHARS = 2000

#: How much of an uploaded document is handed to the distillation. A brand
#: guideline runs to tens of thousands of words, most of them about logo spacing;
#: the voice and messaging sections are near the front, and one call cannot read
#: the whole thing anyway.
_MAX_SOURCE_CHARS = 24_000

#: Upload ceiling. A guideline is a document, not a video.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: What can be read without guessing. ``.docx`` is deliberately absent rather than
#: half-supported: it needs another dependency, and a silently empty guide from an
#: unreadable file is worse than a clear "not supported".
SUPPORTED_SUFFIXES = (".pdf", ".txt", ".md", ".markdown")


class ExtractionError(RuntimeError):
    """The upload could not be turned into text, with a reason worth showing."""


@dataclass(frozen=True, slots=True)
class SourceView:
    """One stored source document, as the guide page lists it."""

    id: int
    filename: str
    characters: int
    uploaded_at: object


def extract_text(filename: str, data: bytes) -> str:
    """Pull readable text out of an uploaded document.

    Raises :class:`ExtractionError` with a sentence the operator can act on. The
    case worth naming is the scanned PDF: it parses fine and yields nothing, and
    silently distilling an empty document into an empty guide would look like the
    feature is broken rather than the file.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ExtractionError(
            f"Die Datei ist größer als {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        text = _extract_pdf(data)
    elif lowered.endswith((".txt", ".md", ".markdown")):
        text = data.decode("utf-8", errors="replace")
    elif lowered.endswith(".docx"):
        raise ExtractionError(
            "DOCX wird noch nicht gelesen. Als PDF exportieren oder den Text "
            "direkt in den Guide schreiben."
        )
    else:
        raise ExtractionError(
            "Nicht lesbar. Unterstützt werden PDF, TXT und Markdown."
        )
    cleaned = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    if len(cleaned.strip()) < 40:
        raise ExtractionError(
            "Aus der Datei ließ sich kein Text lesen — bei einem PDF meist ein "
            "Scan ohne Textebene."
        )
    return cleaned


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 — any parser failure is one message
        raise ExtractionError(f"Das PDF konnte nicht gelesen werden: {exc}") from exc


def store_source(session: Session, client: Client, filename: str, text: str) -> GuideSource:
    """Keep an uploaded document's text as a source for this client's guide."""
    source = GuideSource(
        client_id=client.id,
        filename=filename,
        characters=len(text),
        text=text,
    )
    session.add(source)
    session.commit()
    return source


def sources(session: Session, client_id: int) -> list[GuideSource]:
    """This client's source documents, newest first."""
    return list(
        session.scalars(
            select(GuideSource)
            .where(GuideSource.client_id == client_id)
            .order_by(GuideSource.uploaded_at.desc(), GuideSource.id.desc())
        ).all()
    )


def delete_source(session: Session, client_id: int, source_id: int) -> None:
    """Drop a source. The guide itself is untouched — it is edited text by then."""
    source = session.get(GuideSource, source_id)
    if source is not None and source.client_id == client_id:
        session.delete(source)
        session.commit()


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(text)


def distill(
    session: Session,
    client: Client,
    *,
    invoke=invoke_with_fallback,
) -> str:
    """Propose a guide from this client's uploaded documents.

    Returns the proposed text — it is *not* stored. The caller shows it beside the
    current guide and a person decides, because a new document can contradict what
    is already there and that is not a call a model should make silently.

    Raises :class:`ExtractionError` when there is nothing to distil, so "no sources"
    and "the model failed" stay distinguishable.
    """
    stored = sources(session, client.id)
    if not stored:
        raise ExtractionError("Noch kein Dokument hochgeladen.")

    budget = _MAX_SOURCE_CHARS
    blocks: list[str] = []
    for source in stored:
        if budget <= 0:
            _log.info("guide distillation for %r: source budget spent", client.name)
            break
        chunk = source.text[:budget]
        budget -= len(chunk)
        blocks.append(f"--- {source.filename} ---\n{chunk}")

    prompt = _prompt_template().substitute(
        client_name=client.name,
        industry=client.industry or "—",
        current_guide=(client.comms_guide or "").strip() or "(noch leer)",
        max_chars=GUIDE_MAX_CHARS,
        documents="\n\n".join(blocks),
    )
    proposed = strip_code_fence(invoke(prompt, timeout=config.ANALYZER_TIMEOUT)).strip()
    if not proposed:
        raise ParseError("the distillation returned nothing")
    return proposed[:GUIDE_MAX_CHARS]


def save(session: Session, client: Client, text: str) -> str:
    """Store the guide, trimmed to the budget. Returns what was stored.

    Trimmed rather than rejected: a consultant pasting a long passage should get a
    saved guide and a visible counter, not a lost edit and an error page.
    """
    cleaned = (text or "").strip()[:GUIDE_MAX_CHARS]
    client.comms_guide = cleaned
    session.commit()
    return cleaned


def for_prompt(client: Client) -> str:
    """The guide as a labelled prompt block, or nothing at all.

    A whole block including its heading, so a prompt has no dangling empty section
    for the common case of a mandate whose guide has not been written yet.
    """
    text = (getattr(client, "comms_guide", "") or "").strip()
    if not text:
        return ""
    return (
        "KOMMUNIKATIONS-GUIDE DES MANDANTEN\n"
        "Verbindlich. Was hier als No-Go steht, kommt nicht vor — auch nicht "
        "umschrieben.\n"
        f"{text}\n"
    )


# --- The check: does a finished text obey the rules this client wrote down? ------


def _check_template() -> Template:
    text = resources.files("newspulse").joinpath(_CHECK_RESOURCE).read_text("utf-8")
    return Template(text)


def _second_model() -> Callable[..., str]:
    """The configured second provider, as a ``generate(prompt) -> str``.

    Deliberately not :func:`newspulse.analyzer.invoke_with_fallback`: falling back
    to the model that wrote the letter would turn the check into a self-check, and
    a model does not find its own breach of a rule it read ten seconds ago.

    Raises :class:`RuntimeError` when nothing is configured, for the same reason
    the crosscheck does — a check that silently did not happen is worse than none.
    """
    if not config.review_configured():
        raise RuntimeError(
            "Kein Zweitmodell hinterlegt: GEMINI_API_KEY (oder "
            "NEWSPULSE_GEMINI_API_KEY) in der .env setzen, damit ein anderes "
            "Modell den Text gegen den Guide liest."
        )

    def generate(prompt: str, **kwargs) -> str:
        return gemini.generate(
            prompt,
            model=config.review_model(),
            api_key=config.review_api_key(),
            **kwargs,
        )

    return generate


def _parse_verdict(raw: str) -> GuideVerdict:
    """Validate the reply into a verdict; anything else is a ParseError.

    The same trust boundary as everywhere else here: the reply is text until the
    schema says otherwise, and a half-parsed verdict is discarded rather than
    shown as an approval.
    """
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"the guide check was not valid JSON: {exc}") from exc
    try:
        verdict = GuideVerdict.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"the guide check did not match the schema: {exc}") from exc
    # ``ok`` is recomputed rather than believed. A reply that lists a breach and
    # sets ok anyway would render as a clean bill of health over an objection that
    # is right there underneath it.
    return verdict.model_copy(update={"ok": not verdict.breaches})


def check_guide(
    client: Client,
    message: PersonalMessage,
    *,
    generate: Callable[..., str] | None = None,
) -> tuple[GuideVerdict | None, str]:
    """Read one finished text against this client's stored guide.

    Its own pass, with its own prompt and its own verdict, beside the crosscheck
    rather than inside it: invention and overclaiming are judgements a checker
    weighs, while a No-Go is not a judgement at all — the client wrote it down.
    Folded into one verdict, a written rule would end up averaged against a
    remark about tone.

    The prompt gets the guide verbatim, the subject and the body, and nothing
    else. Not the article, not the profile, not the angle: every extra fact is
    another thing the model can reason its way around when the job is to read a
    rule literally.

    Returns the verdict and the name of the model that gave it, or
    :data:`NOT_CHECKED` for a client with no stored guide — which costs no model
    call, because there is nothing to check against.

    Raises :class:`newspulse.analyzer.ParseError` on an unusable reply and
    whatever the backend raises on a failed call; the caller decides what an
    unchecked letter looks like.
    """
    stored = (getattr(client, "comms_guide", "") or "").strip()
    if not stored:
        _log.info("guide check skipped for %r: no guide stored", client.name)
        return NOT_CHECKED

    if generate is None:
        generate = _second_model()

    prompt = _check_template().substitute(
        comms_guide=stored,
        subject=message.subject or "(kein Betreff)",
        message=message.message,
    )
    return _parse_verdict(generate(prompt)), config.review_model()


__all__ = [
    "ExtractionError",
    "GUIDE_MAX_CHARS",
    "MAX_UPLOAD_BYTES",
    "NOT_CHECKED",
    "SUPPORTED_SUFFIXES",
    "check_guide",
    "delete_source",
    "distill",
    "extract_text",
    "for_prompt",
    "save",
    "sources",
    "store_source",
]
