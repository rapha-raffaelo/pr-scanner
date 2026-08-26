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

from . import brain, config, gemini
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .models import Client, GuideSource
from .schemas import GuideVerdict

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/guide.txt"

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


def replace_source(
    session: Session, client: Client, filename: str, text: str
) -> GuideSource:
    """Store a source that has one current version, replacing the last of its name.

    An uploaded file is a document with a date, and two versions of a brand book
    are two documents. The kick-off questionnaire is not: it is a snapshot of
    something that keeps being answered, and a fresh copy on every regeneration
    would push the real documents out of the distillation's character budget with
    six near-identical versions of itself.

    Replacing keeps the original ``uploaded_at`` for the same reason. ``sources``
    is ordered newest first and :func:`distill` spends its budget in that order,
    so a bumped timestamp would put the questionnaire in front of a brand book
    uploaded since — on every later distillation, including the plain document
    one the kick-off had nothing to do with. A new version of the same source is
    not a newer source.
    """
    existing = session.scalars(
        select(GuideSource).where(
            GuideSource.client_id == client.id, GuideSource.filename == filename
        )
    ).first()
    # A new row takes its date from the column default; an existing one keeps the
    # date it already had.
    source = existing or GuideSource(client_id=client.id, filename=filename)
    source.text = text
    source.characters = len(text)
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
    return Template(brain.compose(text))


#: The source name a record-derived draft is filed under, so its provenance
#: reads as what it is rather than looking like something somebody uploaded.
RECORD_SOURCE_NAME = "Profil und Berichterstattung"

#: How much coverage goes into that source. "Alles lesen" bounded by what a model
#: call can hold: newest first, so the budget is spent on what is current rather
#: than on the first month this mandate was ever tracked.
_COVERAGE_CHARS = 16_000


def _record_text(session: Session, client: Client) -> str:
    """The mandate as the tool already holds it: the profile, then the coverage.

    Two blocks and they are different kinds of evidence, so they are labelled
    rather than run together. The profile is what somebody recorded about this
    company. The coverage is what the press actually wrote — which is the half
    that says how this mandate is talked about, and therefore the half a guide
    built without a brand book is really made of.
    """
    from . import profile as profiles  # local: profile imports nothing from here
    from .models import Analysis, Article

    facts = profiles.stored(session, client.id)
    lines = [f"UNTERNEHMEN: {client.name}"]
    if client.industry:
        lines.append(f"BRANCHE: {client.industry}")
    lines.append("")
    lines.append("PROFIL")
    recorded = 0
    for field in profiles.FIELDS:
        fact = facts.get(field.key)
        if fact and fact.value.strip():
            recorded += 1
            lines.append(f"- {field.label}: {fact.value.strip()}")
    if not recorded:
        # Said rather than left blank: an empty block reads as a block the
        # builder forgot, and the model should know it is working from coverage
        # alone rather than quietly filling the gap.
        lines.append("- (keine Angaben hinterlegt)")

    rows = session.execute(
        select(Article.published_at, Article.source, Article.title, Analysis.summary)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(Analysis.client_id == client.id)
        .order_by(Article.published_at.desc())
    ).all()

    lines.append("")
    lines.append(f"BERICHTERSTATTUNG ({len(rows)} Beiträge, neueste zuerst)")
    budget = _COVERAGE_CHARS
    used = 0
    for published_at, source, title, summary in rows:
        entry = f"- {published_at:%Y-%m-%d} {source}: {title}"
        if summary:
            entry += f"\n  {summary}"
        if budget - len(entry) <= 0:
            break
        budget -= len(entry)
        used += 1
        lines.append(entry)
    if not rows:
        lines.append("- (noch keine Berichterstattung erfasst)")
    elif used < len(rows):
        lines.append(f"- (… {len(rows) - used} ältere Beiträge nicht mitgelesen)")
    return "\n".join(lines)


def from_record(
    session: Session,
    client: Client,
    *,
    invoke: Callable[..., str] = invoke_with_fallback,
) -> str:
    """Propose a guide from what the tool already holds: profile and coverage.

    The third way in, after an uploaded brand book and the kick-off answers, and
    the one for a mandate that has neither. Through the same door as those two —
    the material is filed as a :class:`GuideSource` and distilled — so there is
    one path to a guide rather than three, and the rule that dictated material is
    never applied directly holds here as well: this returns a proposal, and a
    person decides.

    Worth being plain about, because the three are not equal evidence. A brand
    book is what the client decided. The kick-off is what the client said. This
    is what the press has written and what somebody recorded in the profile —
    the weakest of the three, and the only one available to a mandate that
    arrived with nothing.
    """
    replace_source(session, client, RECORD_SOURCE_NAME, _record_text(session, client))
    return distill(session, client, invoke=invoke)


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


def _lf(text: str) -> str:
    """Form newlines counted the way the rest of the tool counts them.

    A browser posts every newline inside a ``<textarea>`` as CRLF, whatever the
    page put in it. Unnormalised, a draft built to sit exactly on
    :data:`GUIDE_MAX_CHARS` comes back one character per line too long, and the
    trim below takes that overshoot off the *end* — which is where the client's
    own no-gos are, verbatim, and where the note naming the unanswered sections
    is. A rule cut mid-clause is a rule nobody wrote.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def save(session: Session, client: Client, text: str) -> str:
    """Store the guide, trimmed to the budget. Returns what was stored.

    Trimmed rather than rejected: a consultant pasting a long passage should get a
    saved guide and a visible counter, not a lost edit and an error page.
    """
    cleaned = _lf(text or "").strip()[:GUIDE_MAX_CHARS]
    client.comms_guide = cleaned
    session.commit()
    return cleaned


_CHECK_RESOURCE = "prompts/guide_check.txt"

#: What a client with no guide gets told, instead of a clean bill of health.
#: "Nothing objected" and "nothing to object with" must not read alike.
NO_GUIDE = "Kein Guide hinterlegt, gegen den geprüft werden könnte."

#: What is stored when the check was attempted and broke. A third sentence rather
#: than either of the other two: the guide exists, the check ran, and it produced
#: nothing, which is not the same as a mandate that has no guide to check against.
CHECK_FAILED = "Die Guide-Prüfung ist fehlgeschlagen. Der Text ist ungeprüft."


def check_guide(
    client: Client,
    *,
    title: str,
    body: str,
    kind: str = "Text",
    generate: Callable[..., str] | None = None,
) -> tuple[GuideVerdict, str] | None:
    """Read a finished text against this client's own rules.

    Returns the verdict and the model that gave it, or ``None`` when the mandate
    has no guide on file. ``None`` is not a pass: the caller stores
    :data:`NO_GUIDE` so the page can say the check could not run, which is a
    different sentence from "nothing to object to".

    A separate pass rather than five more lines in the crosscheck prompt, for
    the reason a No-Go is a different kind of thing: invention and overclaiming
    are judgements about the world, and a model weighs them. A No-Go is not a
    judgement. The client wrote it down, and a written rule that gets averaged
    against a tone remark has been diluted rather than checked.

    Runs on the second provider, like every other check here, so the model that
    wrote the text is never the one that clears it.
    """
    rules = (client.comms_guide or "").strip()
    if not rules:
        return None
    if generate is None:
        generate = gemini.reviewer()

    template = Template(
        resources.files("newspulse").joinpath(_CHECK_RESOURCE).read_text("utf-8")
    )
    prompt = template.substitute(
        client=client.name,
        guide=rules,
        kind=kind,
        title=title or "(ohne Titel)",
        body=body,
    )
    raw = generate(prompt)
    try:
        payload = json.loads(strip_code_fence(raw))
        verdict = GuideVerdict.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic and json raise their own
        raise ParseError(f"guide check did not match the schema: {exc}") from exc
    # The model's own flag is not trusted against its own findings: a verdict
    # that lists a breach and still says ok would clear a text nobody cleared.
    if verdict.breaches:
        verdict = verdict.model_copy(update={"ok": False})
    return verdict, config.review_model()


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


__all__ = [
    "CHECK_FAILED",
    "ExtractionError",
    "GUIDE_MAX_CHARS",
    "MAX_UPLOAD_BYTES",
    "NO_GUIDE",
    "SUPPORTED_SUFFIXES",
    "check_guide",
    "delete_source",
    "distill",
    "extract_text",
    "for_prompt",
    "replace_source",
    "save",
    "sources",
    "store_source",
]
