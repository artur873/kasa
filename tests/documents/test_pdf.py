"""PDF from e-mails: mock model on the six fixture PDFs, the lines check, one reading per file,
and the pseudonymization gate in front of every model that leaves the machine."""

from __future__ import annotations

import hashlib
import re
import types
from datetime import date
from decimal import Decimal

import pytest

from kasa.core.documents import privacy
from kasa.core.documents.ingest import ingest_ksef
from kasa.core.documents.pdf import EXTRACTION_SCHEMA, import_pdf, import_pdfs, load_email_meta, pdf_text
from kasa.core.normalize import to_minor
from kasa.core.providers.ksef import MockKsef, ProviderNotConfigured
from kasa.core.providers.llm import MockLlm, VertexLlm, get_llm_provider
from kasa.core.pseudonym import pseudonym_for

from .conftest import FIXTURES, expected_pdfs, observations_of

PDF_DIR = FIXTURES / "email_pdfs"
META = PDF_DIR / "index.json"
KIND = {"EM-01": "foreign", "EM-02": "foreign", "EM-03": "foreign", "EM-04": "receipt", "EM-05": "bill",
        "EM-06": "vat"}


class RecordingLlm:
    """A model that 'leaves the machine': records exactly what it was sent, answers like the mock."""

    name = "recording"
    model = "recording-1"
    leaves_machine = True

    def __init__(self):
        self.calls: list[str | bytes] = []
        self._mock = MockLlm()

    def extract_invoice(self, content, schema, *, source_name=None, source_sha256=None):
        self.calls.append(content)
        data, _, prompt = self._mock.extract_invoice(content, schema, source_name=source_name,
                                                     source_sha256=source_sha256)
        return data, self.model, prompt

    def complete(self, prompt, schema=None):  # pragma: no cover - not used here
        raise AssertionError


class CountingLlm(MockLlm):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def extract_invoice(self, *args, **kwargs):
        self.calls += 1
        return super().extract_invoice(*args, **kwargs)


def _pdf(doc_id: str) -> bytes:
    return (PDF_DIR / expected_pdfs()[doc_id]["file"]).read_bytes()


def test_pdf_text_layer_and_email_meta():
    text = pdf_text(_pdf("EM-06"))
    assert "FV/2026/10/0412" in text and "14 078,40" in text
    meta = load_email_meta(META)
    entry = meta[expected_pdfs()["EM-05"]["file"]]
    assert entry["id"] == "EM-05" and entry["subject"] == "Rachunek za prowadzenie Gali"
    assert meta[hashlib.sha256(_pdf("EM-05")).hexdigest()] is entry


def test_mock_reads_six_pdfs_as_expected(conn, odm, nbp):
    report = import_pdfs(conn, odm, [PDF_DIR], meta_path=META, llm=MockLlm())
    assert not report.errors and report.documents_created == 6 and report.needs_review == 0
    expected = expected_pdfs()
    for outcome in report.outcomes:
        doc_id = outcome.filename[:5]
        exp = expected[doc_id]
        row = conn.execute(
            """
            SELECT kind::text, number_raw, issue_date, currency, gross_minor, net_minor, method, prompt_version,
                   gross_pln_minor, evidence, service_period_start, sale_date, reverse_charge
              FROM l1_document WHERE id = %s
            """,
            (outcome.document_id,),
        ).fetchone()
        kind, number, issue, currency, gross, net, method, prompt, gross_pln, evidence, period, _sale, reverse = row
        assert kind == KIND[doc_id] and number == exp["number"]
        assert issue.isoformat() == exp["issue_date"] and currency == exp["currency"]
        assert gross == to_minor(Decimal(exp["totals"]["gross"]), currency)
        assert net == to_minor(Decimal(exp["totals"]["net"]), currency)
        assert (method, prompt) == ("model", "pdf-invoice-v1")
        assert evidence["lines_sum_ok"] is True and not evidence.get("needs_review")
        if exp["pln"]:
            assert gross_pln == to_minor(Decimal(exp["pln"]["gross_pln"]))   # NBP of the previous day
        assert (period.isoformat() if period else None) == exp["period_from"]
        assert reverse == (exp["vat_mode"] == "reverse")
        lines = conn.execute(
            "SELECT name, gross_minor FROM l1_document_line WHERE document_id = %s ORDER BY line_no",
            (outcome.document_id,),
        ).fetchall()
        assert [l[0] for l in lines] == [l["name"] for l in exp["lines"]]
        assert sum(l[1] for l in lines) == gross
    extractions = conn.execute(
        "SELECT count(*), bool_and(lines_sum_ok), count(document_id) FROM l1_pdf_extraction WHERE company_id = %s",
        (odm.id,),
    ).fetchone()
    assert extractions == (6, True, 6)


def test_hotel_pdf_and_ksef_are_one_document_two_observations(conn, odm):
    ingest_ksef(conn, odm, MockKsef(), date(2026, 10, 1), date(2026, 10, 31))
    report = import_pdf(conn, odm, _pdf("EM-06"), filename=expected_pdfs()["EM-06"]["file"], llm=MockLlm())
    [outcome] = report.outcomes
    ksef_doc = conn.execute(
        "SELECT id FROM l1_document WHERE ksef_number = %s AND superseded_by IS NULL AND company_id = %s",
        (expected_pdfs()["EM-06"]["same_as_ksef"], odm.id),
    ).fetchone()[0]
    assert outcome.document_id == ksef_doc and report.documents_created == 0
    assert sorted(observations_of(conn, ksef_doc)) == [("ksef", "created", None), ("pdf", "nip_kind_number", None)]


def test_model_reads_a_file_once(conn, odm):
    llm = CountingLlm()
    for _ in range(3):
        import_pdf(conn, odm, _pdf("EM-01"), filename="EM-01.pdf", llm=llm)
    assert llm.calls == 1
    assert conn.execute("SELECT count(*) FROM l1_pdf_extraction WHERE company_id = %s", (odm.id,)).fetchone()[0] == 1


def test_lines_not_adding_up_keep_the_observation_and_go_to_review(conn, odm):
    class WrongLines(MockLlm):
        def extract_invoice(self, content, schema, **kwargs):
            data, model, prompt = super().extract_invoice(content, schema, **kwargs)
            data["lines"][0]["gross"] = "1700.00"       # the line does not add up to 1 800,00
            return data, model, prompt

    report = import_pdf(conn, odm, _pdf("EM-05"), filename=expected_pdfs()["EM-05"]["file"], llm=WrongLines())
    [outcome] = report.outcomes
    assert outcome.status == "document" and outcome.needs_review and report.needs_review == 1
    evidence = conn.execute("SELECT evidence FROM l1_document WHERE id = %s", (outcome.document_id,)).fetchone()[0]
    assert evidence["needs_review"] is True and evidence["lines_sum_ok"] is False
    assert observations_of(conn, outcome.document_id) == [("pdf", "created", None)]
    row = conn.execute("SELECT lines_sum_ok, needs_review FROM l1_pdf_extraction WHERE document_id = %s",
                       (outcome.document_id,)).fetchone()
    assert row == (False, True)


def test_unknown_pdf_gets_a_regex_reading_and_review():
    text = pdf_text(_pdf("EM-06"))
    data, model, prompt = MockLlm().extract_invoice(text, EXTRACTION_SCHEMA, source_name="nieznany.pdf")
    assert model == "mock-regex" and prompt == "pdf-invoice-v1"
    assert data["number"] == "FV/2026/10/0412" and data["issue_date"] == "2026-10-10"
    assert data["seller"]["tax_id"] == "6768253927" and data["buyer"]["tax_id"] == "5342650778"
    assert data["totals"]["gross"] == "14078.40" and data["lines"] == []


def test_unknown_pdf_without_lines_is_queued(conn, odm):
    report = import_pdf(conn, odm, _pdf("EM-06"), filename="skan-hotel.pdf", llm=_RegexOnly())
    [outcome] = report.outcomes
    assert outcome.status == "document" and outcome.needs_review


class _RegexOnly(MockLlm):
    def known(self, source_name, source_sha256):
        return None


# ---------------------------------------------------------------------------
# Pseudonymization gate
# ---------------------------------------------------------------------------


def test_no_gate_no_call_for_a_model_that_leaves_the_machine(conn, odm, monkeypatch):
    monkeypatch.setattr(privacy, "load_pseudonym_module", lambda: None)
    llm = RecordingLlm()
    report = import_pdf(conn, odm, _pdf("EM-05"), filename="EM-05.pdf", llm=llm)
    assert llm.calls == []                                           # the call never happened
    [outcome] = report.outcomes
    assert outcome.status == "blocked" and "pseudonimizacji" in outcome.detail
    # the raw file is kept (L0), nothing was read from it
    assert conn.execute("SELECT count(*) FROM l0_raw WHERE company_id = %s", (odm.id,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM l1_document WHERE company_id = %s", (odm.id,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM l1_pdf_extraction WHERE company_id = %s", (odm.id,)).fetchone()[0] == 0


def test_failing_gate_blocks_the_call(conn, odm, monkeypatch):
    def broken(conn, company_id, text):
        raise RuntimeError("klucz pseudonimizacji niedostępny")

    monkeypatch.setattr(privacy, "load_pseudonym_module",
                        lambda: types.SimpleNamespace(pseudonymize_text=broken))
    llm = RecordingLlm()
    report = import_pdf(conn, odm, _pdf("EM-05"), filename="EM-05.pdf", llm=llm)
    assert llm.calls == [] and report.outcomes[0].status == "blocked"


def test_without_the_gate_even_the_local_mock_is_not_called(conn, odm, monkeypatch):
    # phase 3: every provider goes through kasa.core.semantics.gate - no gate, no call (mock included)
    monkeypatch.setattr(privacy, "load_pseudonym_module", lambda: None)
    llm = CountingLlm()
    report = import_pdf(conn, odm, _pdf("EM-05"), filename="EM-05.pdf", llm=llm)
    assert llm.calls == 0 and report.outcomes[0].status == "blocked"
    assert conn.execute("SELECT count(*) FROM l1_pdf_extraction WHERE company_id = %s", (odm.id,)).fetchone() == (0,)
    assert conn.execute(
        "SELECT status::text, blocked_reason, input_hash FROM llm_call_log WHERE company_id = %s", (odm.id,),
    ).fetchall() == [("blocked", "gate_unavailable", None)]


def test_regex_reading_is_code_and_codes_are_reversed_only_when_sent(conn, odm):
    class Hallucinating(_RegexOnly):
        def extract_invoice(self, content, schema, **kwargs):
            data, model, prompt = super().extract_invoice(content, schema, **kwargs)
            sent = set(re.findall(r"\b[PITENA]-[0-9a-f]{4,16}\b", content))
            data["seller"]["name"] = f"{sorted(sent)[0]} / {stranger}"       # a code it never saw
            return data, model, prompt

    stranger = pseudonym_for(conn, odm.id, "person", "Zenobia Obca")
    report = import_pdf(conn, odm, _pdf("EM-05"), filename="skan.pdf", llm=Hallucinating())
    [outcome] = report.outcomes
    seller = conn.execute("SELECT seller_name FROM l1_document WHERE id = %s", (outcome.document_id,)).fetchone()
    assert stranger in seller[0] and "Obca" not in seller[0]          # the unseen code stays a code
    assert not re.search(r"\b[PITENA]-[0-9a-f]{4,16}\b", seller[0].split(" / ")[0])   # the seen code: reversed
    assert conn.execute("SELECT method::text, model FROM l1_pdf_extraction WHERE company_id = %s",
                        (odm.id,)).fetchone() == ("code", "mock-regex")
    assert observations_of(conn, outcome.document_id)[0][0] == "pdf"
    assert conn.execute("SELECT method::text FROM l1_document_observation WHERE document_id = %s",
                        (outcome.document_id,)).fetchone() == ("code",)


def test_model_sees_only_pseudonymized_text(conn, odm):
    if privacy.load_pseudonym_module() is None or not conn.execute(
            "SELECT to_regclass('pseudonym_key') IS NOT NULL").fetchone()[0]:
        pytest.skip("bramka pseudonimizacji (kasa.core.pseudonym + migracja 0006) niedostępna")
    llm = RecordingLlm()
    report = import_pdf(conn, odm, _pdf("EM-05"), filename=expected_pdfs()["EM-05"]["file"], llm=llm)
    [sent] = llm.calls
    assert isinstance(sent, str)                                     # text, never the raw PDF bytes
    for secret in ("Marek", "Zawadzki", "500 038 158", "2492 4115 2270 9072 8598 4411", "Lipowa 12"):
        assert secret not in sent, secret
    assert "P-" in sent and "ODM Partners" in sent and "1 800,00" in sent
    [outcome] = report.outcomes
    seller, = conn.execute("SELECT seller_name FROM l1_document WHERE id = %s", (outcome.document_id,)).fetchone()
    assert seller == "Marek Zawadzki"                                # facts keep the real value
    row = conn.execute("SELECT pseudonymized, input_sha256 FROM l1_pdf_extraction WHERE company_id = %s",
                       (odm.id,)).fetchone()
    assert row == (True, hashlib.sha256(sent.encode()).hexdigest())


def test_vertex_needs_configuration_and_never_takes_raw_pdf(monkeypatch, tmp_path):
    for name in ("KASA_VERTEX_PROJECT", "KASA_VERTEX_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ProviderNotConfigured):
        VertexLlm.from_env()
    monkeypatch.setenv("KASA_LLM_PROVIDER", "vertex")
    assert isinstance(get_llm_provider(), MockLlm)                   # missing keys -> mock, no exception
    credentials = tmp_path / "sa.json"
    credentials.write_text("{}")
    with pytest.raises(ProviderNotConfigured, match="UE"):
        VertexLlm(project="p", location="us-central1", credentials_path=str(credentials), model="gemini-2.5-flash")
    vertex = VertexLlm(project="p", location="europe-west1", credentials_path=str(credentials), model="m")
    assert vertex.leaves_machine is True
    with pytest.raises(TypeError, match="pseudonimizacji"):
        vertex.extract_invoice(b"%PDF-1.4", EXTRACTION_SCHEMA)
