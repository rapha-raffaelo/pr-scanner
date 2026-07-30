"""The communications guide: extraction, distillation, storage, and its reach.

The guide's whole purpose is to stop three separate prompts from inventing a voice
on every call, so the tests that matter most are the ones asserting it actually
arrives in them — and the ones asserting nothing from an uploaded document is
applied without a person seeing it first.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import guide
from newspulse.models import Base, Client, GuideSource
from newspulse.web.app import create_app, get_db


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as sess:
        yield sess


@pytest.fixture
def client(factory):
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _client(session, **over) -> Client:
    obj = Client(
        name=over.get("name", "Arrakis Finance"),
        aliases=[],
        keywords=[],
        alert_topics=[],
        country="DE",
        comms_guide=over.get("comms_guide", ""),
    )
    session.add(obj)
    session.commit()
    return obj


def _pdf_bytes(text: str) -> bytes:
    """A minimal one-page PDF carrying extractable ``text``.

    Hand-built rather than produced with pypdf: pypdf reads PDFs, it does not draw
    text, and a page without a font resource extracts to nothing — which is the
    *scanned* case this file also tests, so the two must not be confused.
    """
    content = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


# --- Extraction ----------------------------------------------------------------


def test_plain_text_and_markdown_are_read_directly(session):
    text = "Positionierung: Wir machen Wirksamkeit nachweisbar und belegen sie klinisch."
    assert "Wirksamkeit" in guide.extract_text("leitfaden.txt", text.encode())
    assert "Wirksamkeit" in guide.extract_text("leitfaden.md", text.encode())


def test_a_pdf_is_read(session):
    data = _pdf_bytes("Positionierung: Wir machen Wirksamkeit nachweisbar.")

    assert "Wirksamkeit" in guide.extract_text("leitfaden.pdf", data)


def test_a_pdf_without_a_text_layer_is_refused_rather_than_silently_empty(session):
    """The scanned-guideline case. It parses fine and yields nothing, and
    distilling that into an empty guide would look like the feature is broken
    rather than the file."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)

    with pytest.raises(guide.ExtractionError) as exc:
        guide.extract_text("scan.pdf", buf.getvalue())

    assert "Scan" in str(exc.value)


def test_docx_says_it_is_not_supported_instead_of_failing_obscurely(session):
    with pytest.raises(guide.ExtractionError) as exc:
        guide.extract_text("leitfaden.docx", b"PK\x03\x04irgendwas")

    assert "DOCX" in str(exc.value)


def test_an_oversized_upload_is_refused(session):
    too_big = b"x" * (guide.MAX_UPLOAD_BYTES + 1)

    with pytest.raises(guide.ExtractionError):
        guide.extract_text("gross.txt", too_big)


# --- The guide itself ----------------------------------------------------------


def test_saving_trims_to_the_budget_rather_than_rejecting(session):
    """A consultant pasting a long passage should get a saved guide and a visible
    counter, not a lost edit and an error page."""
    client = _client(session)

    stored = guide.save(session, client, "x" * (guide.GUIDE_MAX_CHARS + 500))

    assert len(stored) == guide.GUIDE_MAX_CHARS
    assert len(client.comms_guide) == guide.GUIDE_MAX_CHARS


def test_an_empty_guide_contributes_no_prompt_block(session):
    """No dangling empty section for the common case of a guide nobody wrote."""
    assert guide.for_prompt(_client(session)) == ""


def test_the_prompt_block_states_that_no_gos_are_binding(session):
    """A No-Go handed over as context is something a model may weigh; as a rule it
    is something it must obey."""
    client = _client(session, comms_guide="Nie: Heilversprechen")

    block = guide.for_prompt(client)

    assert "Heilversprechen" in block
    assert "Verbindlich" in block


# --- Distillation ---------------------------------------------------------------


def test_distilling_without_documents_is_a_clear_refusal(session):
    """"No sources" and "the model failed" must stay distinguishable."""
    client = _client(session)

    with pytest.raises(guide.ExtractionError):
        guide.distill(session, client, invoke=lambda *a, **k: "egal")


def test_the_distillation_prompt_carries_the_documents_and_the_budget(session):
    client = _client(session, comms_guide="Bisher: nüchtern bleiben")
    guide.store_source(session, client, "leitfaden.pdf", "Wir erklären, wir bewerben nicht.")
    seen: dict[str, str] = {}

    def _invoke(prompt, **_):
        seen["prompt"] = prompt
        return "Positionierung: erklären statt bewerben."

    guide.distill(session, client, invoke=_invoke)

    assert "Wir erklären, wir bewerben nicht." in seen["prompt"]
    assert "Bisher: nüchtern bleiben" in seen["prompt"]
    assert str(guide.GUIDE_MAX_CHARS) in seen["prompt"]
    assert "Arrakis Finance" in seen["prompt"]


def test_a_proposal_is_returned_but_never_stored(session):
    """The preview step. A document can contradict what is already there, and only
    a person can settle that."""
    client = _client(session, comms_guide="Alt")
    guide.store_source(session, client, "leitfaden.txt", "Neuer Inhalt aus dem Leitfaden.")

    proposed = guide.distill(session, client, invoke=lambda *a, **k: "Positionierung: neu.")

    assert proposed == "Positionierung: neu."
    assert client.comms_guide == "Alt"


def test_a_proposal_is_trimmed_to_the_budget(session):
    client = _client(session)
    guide.store_source(session, client, "leitfaden.txt", "Inhalt, lang genug für einen Test.")

    proposed = guide.distill(
        session, client, invoke=lambda *a, **k: "y" * (guide.GUIDE_MAX_CHARS + 100)
    )

    assert len(proposed) == guide.GUIDE_MAX_CHARS


# --- The page ------------------------------------------------------------------


def test_the_guide_page_shows_the_text_and_its_budget(factory, client):
    with factory() as session:
        obj = _client(session, comms_guide="Positionierung: nachweisbar wirksam.")
        client_id = obj.id

    body = client.get(f"/client/{client_id}/guide").text

    assert "Positionierung: nachweisbar wirksam." in body
    assert str(guide.GUIDE_MAX_CHARS) in body


def test_saving_through_the_page_stores_the_guide(factory, client):
    with factory() as session:
        client_id = _client(session).id

    client.post(
        f"/client/{client_id}/guide",
        data={"comms_guide": "Nie: Heilversprechen"},
        follow_redirects=False,
    )

    with factory() as session:
        assert session.get(Client, client_id).comms_guide == "Nie: Heilversprechen"


def test_uploading_stores_the_text_not_the_file(factory, client):
    """Keeping binaries out of a SQLite file that is copied on every deploy is
    worth more than being able to hand the original back."""
    with factory() as session:
        client_id = _client(session).id

    client.post(
        f"/client/{client_id}/guide/upload",
        files={"file": ("leitfaden.txt", b"Wir erklaeren, wir bewerben nicht. Immer belegt.", "text/plain")},
        follow_redirects=False,
    )

    with factory() as session:
        sources = guide.sources(session, client_id)
        assert len(sources) == 1
        assert sources[0].filename == "leitfaden.txt"
        assert "bewerben nicht" in sources[0].text


def test_an_unreadable_upload_reports_against_the_file(factory, client):
    with factory() as session:
        client_id = _client(session).id

    body = client.post(
        f"/client/{client_id}/guide/upload",
        files={"file": ("leitfaden.docx", b"PK\x03\x04", "application/octet-stream")},
    ).text

    assert "DOCX" in body
    with factory() as session:
        assert guide.sources(session, client_id) == []


def test_removing_a_source_leaves_the_guide_alone(factory, client):
    with factory() as session:
        obj = _client(session, comms_guide="Bleibt stehen")
        guide.store_source(session, obj, "alt.txt", "Inhalt")
        client_id, source_id = obj.id, guide.sources(session, obj.id)[0].id

    client.post(
        f"/client/{client_id}/guide/sources/{source_id}/remove", follow_redirects=False
    )

    with factory() as session:
        assert guide.sources(session, client_id) == []
        assert session.get(Client, client_id).comms_guide == "Bleibt stehen"


def test_an_unknown_client_is_a_404(client):
    assert client.get("/client/9999/guide").status_code == 404


# --- Where it lands -------------------------------------------------------------
#
# The point of the whole feature: three prompts that used to guess a voice.


def test_the_guide_reaches_the_positioning_prompt(session):
    from newspulse import angles
    from newspulse.models import Article

    client = _client(session, comms_guide="Nie: Heilversprechen")
    article = Article(
        title="Markt bewegt sich",
        url="https://ex.de/1",
        source="cash.at",
        published_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        fetched_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        summary_text=None,
        language="de",
        title_hash="markt001",
    )
    session.add(article)
    session.commit()
    seen: dict[str, str] = {}

    def _invoke(prompt, **_):
        seen["prompt"] = prompt
        return '{"worth_sending": false, "subject": "nichts"}'

    angles.suggest(session, client, [(article, "radar")], invoke=_invoke)

    assert "Heilversprechen" in seen["prompt"]


def test_the_guide_reaches_the_advisory_prompt(session):
    import datetime as dt

    from newspulse import advisor
    from newspulse.models import Analysis, Article, Category

    client = _client(session, comms_guide="Nie: Heilversprechen")
    article = Article(
        title="Arrakis meldet Zahlen",
        url="https://ex.de/2",
        source="Handelsblatt",
        published_at=dt.datetime.now(dt.UTC),
        fetched_at=dt.datetime.now(dt.UTC),
        summary_text=None,
        language="de",
        title_hash="zahlen01",
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            summary="s",
            category=Category.FINANZEN,
            relevance_score=6,
            importance_score=6,
            is_alert=False,
        )
    )
    session.commit()
    seen: dict[str, str] = {}

    def _invoke(prompt, timeout=None):
        seen["prompt"] = prompt
        return '{"situation": "ruhig", "suggestions": []}'

    advisor.advise(session, client, invoke=_invoke)

    assert "Heilversprechen" in seen["prompt"]


def test_the_guide_reaches_captain_comms_only_for_the_selected_client(session):
    """A portfolio-wide question has no single voice to obey, and pasting one
    client's No-Gos into an answer about another would be worse than none."""
    from newspulse.web.routes.assistant import _guide_for

    client = _client(session, comms_guide="Nie: Heilversprechen")

    assert "Heilversprechen" in _guide_for(session, client.id)
    assert _guide_for(session, None) == ""


# --- The coach ------------------------------------------------------------------
#
# One fixed question — does the guide hold up against what was actually written —
# answered with typed findings rather than prose, so a Monday morning can scan it.


def _covered(session, client, title, *, days_ago=1):
    import datetime as dt

    from newspulse.models import Analysis, Article, Category

    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 100000}",
        source="cash.at",
        published_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        fetched_at=dt.datetime.now(dt.UTC),
        summary_text=None,
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            summary="s",
            category=Category.PRODUKT,
            relevance_score=6,
            importance_score=6,
            is_alert=False,
        )
    )
    session.commit()
    return article


def _report(**over) -> str:
    import json as _json

    payload = {
        "findings": [
            {
                "kind": "luecke",
                "headline": "„Nachweis vor Neuheit“ kommt nicht vor.",
                "detail": "Die Berichterstattung beschreibt die Technik, nicht den Beleg.",
                "suggestion": "Ein Fachbeitrag zur Prüfmethodik statt der nächsten Produktmeldung.",
                "evidence": [0],
            }
        ]
    }
    payload.update(over)
    return _json.dumps(payload)


def test_without_a_guide_the_coach_refuses_rather_than_reporting_nothing(session):
    """"Nothing to check", "nothing found" and "the call failed" are three
    different answers and must not collapse into one."""
    from newspulse import coach

    client = _client(session)
    _covered(session, client, "Irgendeine Meldung")

    with pytest.raises(coach.GuideMissing):
        coach.review(session, client, invoke=lambda *a, **k: _report())


def test_without_coverage_the_report_is_empty_but_not_an_error(session):
    from newspulse import coach

    client = _client(session, comms_guide="Nachweis vor Neuheit")

    report, coverage = coach.review(session, client, invoke=lambda *a, **k: _report())

    assert report.findings == []
    assert coverage == []


def test_the_coach_prompt_carries_the_guide_and_the_coverage(session):
    from newspulse import coach

    client = _client(session, comms_guide="Nie: Heilversprechen")
    _covered(session, client, "Arrakis meldet Zahlen")
    seen: dict[str, str] = {}

    def _invoke(prompt, **_):
        seen["prompt"] = prompt
        return _report()

    coach.review(session, client, invoke=_invoke)

    assert "Heilversprechen" in seen["prompt"]
    assert "Arrakis meldet Zahlen" in seen["prompt"]
    assert "[0]" in seen["prompt"]


def test_findings_are_typed_and_keep_their_evidence(session):
    from newspulse import coach
    from newspulse.schemas import FindingKind

    client = _client(session, comms_guide="Nachweis vor Neuheit")
    _covered(session, client, "Arrakis meldet Zahlen")

    report, coverage = coach.review(session, client, invoke=lambda *a, **k: _report())

    assert report.findings[0].kind is FindingKind.LUECKE
    assert report.findings[0].evidence == [0]
    assert coverage[0].headline == "Arrakis meldet Zahlen"


def test_invented_evidence_is_dropped(session):
    """A citation pointing at nothing discredits the finding it was meant to
    support."""
    from newspulse import coach

    client = _client(session, comms_guide="Nachweis vor Neuheit")
    _covered(session, client, "Arrakis meldet Zahlen")

    report, _ = coach.review(
        session,
        client,
        invoke=lambda *a, **k: _report(
            findings=[
                {
                    "kind": "konflikt",
                    "headline": "h",
                    "detail": "d",
                    "suggestion": "s",
                    "evidence": [0, 42],
                }
            ]
        ),
    )

    assert report.findings[0].evidence == [0]


def test_a_non_json_reply_is_a_parse_error(session):
    from newspulse import coach

    client = _client(session, comms_guide="Nachweis vor Neuheit")
    _covered(session, client, "Arrakis meldet Zahlen")

    with pytest.raises(coach.ParseError):
        coach.review(session, client, invoke=lambda *a, **k: "Gerne! Hier die Analyse:")


def test_the_page_offers_the_coach_and_says_it_has_not_run(factory, client):
    with factory() as session:
        client_id = _client(session, comms_guide="Nachweis vor Neuheit").id

    body = client.get(f"/client/{client_id}/guide").text

    assert "Strategie-Coach" in body
    assert f'action="/client/{client_id}/guide/coach"' in body
    assert "Noch nicht geprüft" in body


def test_running_the_coach_without_a_guide_says_so_on_the_page(factory, client):
    with factory() as session:
        client_id = _client(session).id

    body = client.post(f"/client/{client_id}/guide/coach").text

    assert "Kein Kommunikations-Guide hinterlegt." in body
