"""Deduplication (briefing, step 2): KSeF number -> file digest -> (NIP, kind, number) with the same
date and amount -> otherwise a new document flagged for the queue. Plus relations and versions."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from kasa.core.documents.csv_import import import_table
from kasa.core.documents.fa3 import parse_fa3
from kasa.core.documents.ingest import (
    Capabilities,
    Observation,
    ingest_jpk,
    ingest_ksef,
    relink_pending,
    upsert_document,
)
from kasa.core.documents.pdf import import_pdf
from kasa.core.providers.ksef import MockKsef
from kasa.core.providers.llm import MockLlm
from kasa.core.raw_store import store_raw

from .conftest import (
    FIXTURES,
    ODM_NIP,
    current_documents,
    expected_pdfs,
    ground_truth,
    ksef_index,
    observations_of,
    row_counts,
)

WINDOW = (date(2026, 9, 1), date(2026, 10, 31))
JPK = FIXTURES / "history" / "JPK_V7M_2026-09.xml"


class OnlyKsef(MockKsef):
    """Mock KSeF serving a chosen subset of invoices, in the given order."""

    def __init__(self, numbers: list[str]):
        super().__init__()
        self.numbers = numbers

    def fetch_invoices(self, company, date_from, date_to, direction=None):
        found = {i.ksef_number: i for i in super().fetch_invoices(company, date_from, date_to, direction)}
        return [found[n] for n in self.numbers if n in found]


def _doc_by_ksef(conn, company_id, ksef_number):
    return conn.execute(
        "SELECT id, source, corrects_document_id, settles_advance_document_id FROM l1_document "
        "WHERE company_id = %s AND ksef_number = %s AND superseded_by IS NULL",
        (company_id, ksef_number),
    ).fetchone()


def _ksef_number(number: str, direction: str) -> str:
    return next(i["ksef_number"] for i in ksef_index()["invoices"]
                if i["invoice_number"] == number and i["direction"] == direction)


def test_pdf_first_then_ksef_gives_one_document_with_ksef_fields(conn, odm):
    hotel = expected_pdfs()["EM-06"]
    conn.execute("UPDATE company SET nip = %s WHERE id = %s", (ODM_NIP, odm.id))
    pdf_report = import_pdf(conn, odm, (FIXTURES / "email_pdfs" / hotel["file"]).read_bytes(),
                            filename=hotel["file"], llm=MockLlm())
    pdf_doc = pdf_report.outcomes[0].document_id
    report = ingest_ksef(conn, odm, OnlyKsef([hotel["same_as_ksef"]]), *WINDOW)
    assert report.versions_created == 1 and report.documents_created == 0
    assert report.linked == {"nip_kind_number": 1}
    doc_id, source, _, _ = _doc_by_ksef(conn, odm.id, hotel["same_as_ksef"])
    assert source == "ksef"                                         # the most certain channel's fields
    superseded = conn.execute("SELECT superseded_by FROM l1_document WHERE id = %s", (pdf_doc,)).fetchone()[0]
    assert superseded == doc_id
    assert len(current_documents(conn, odm.id)) == 1
    assert sorted(observations_of(conn, doc_id)) == [("ksef", "nip_kind_number", None), ("pdf", "created", None)]
    # repeating both imports changes nothing
    before = row_counts(conn, odm.id)
    ingest_ksef(conn, odm, OnlyKsef([hotel["same_as_ksef"]]), *WINDOW)
    import_pdf(conn, odm, (FIXTURES / "email_pdfs" / hotel["file"]).read_bytes(), filename=hotel["file"],
               llm=MockLlm())
    assert row_counts(conn, odm.id) == before


def test_september_invoices_from_jpk_and_ksef_are_one_document(conn, odm):
    ingest_ksef(conn, odm, MockKsef(), *WINDOW)
    report = ingest_jpk(conn, odm, JPK.read_bytes(), filename=JPK.name)
    pairs = ground_truth()["dedup"]["ksef_jpk"]
    assert report.linked == {"nip_kind_number": len(pairs)} == {"nip_kind_number": 4}
    assert report.documents_created == 23 - 4
    for pair in pairs:
        doc_id, source, _, _ = _doc_by_ksef(conn, odm.id, pair["ksef_number"])
        observations = observations_of(conn, doc_id)
        assert ("jpk", "nip_kind_number", f"{pair['jpk_section']}:{pair['jpk_lp']}") in observations
        assert source == "ksef" and len(observations) == 2


def test_same_number_from_another_nip_is_never_a_duplicate(conn, odm):
    ingest_ksef(conn, odm, MockKsef(), *WINDOW)
    ingest_jpk(conn, odm, JPK.read_bytes(), filename=JPK.name)
    for pair in ground_truth()["dedup"]["not_duplicates"]:
        rows = conn.execute(
            """
            SELECT direction::text, seller_nip, buyer_nip, evidence ? 'possible_duplicate'
              FROM l1_document WHERE company_id = %s AND superseded_by IS NULL AND number_raw = %s
            """,
            (odm.id, pair["a"]["number"]),
        ).fetchall()
        assert len(rows) == 2, pair
        assert not any(r[3] for r in rows)
    assert conn.execute("SELECT count(*) FROM l2_suggestion WHERE company_id = %s", (odm.id,)).fetchone()[0] == 0


def test_correction_points_to_the_corrected_invoice(conn, odm):
    ingest_ksef(conn, odm, MockKsef(), *WINDOW)
    original = _doc_by_ksef(conn, odm.id, _ksef_number("FV 2/10/2026", "sales"))
    correction = _doc_by_ksef(conn, odm.id, _ksef_number("KOR 1/10/2026", "sales"))
    assert correction[2] == original[0]


def test_correction_imported_before_the_original_is_linked_later(conn, odm, with_0006):
    kor, fv = _ksef_number("KOR 1/10/2026", "sales"), _ksef_number("FV 2/10/2026", "sales")
    ingest_ksef(conn, odm, OnlyKsef([kor]), *WINDOW)
    first = _doc_by_ksef(conn, odm.id, kor)
    assert first[2] is None
    report = ingest_ksef(conn, odm, OnlyKsef([fv]), *WINDOW)
    assert report.versions_created == 1                             # the correction got a linked version
    linked = _doc_by_ksef(conn, odm.id, kor)
    assert linked[2] == _doc_by_ksef(conn, odm.id, fv)[0] and linked[0] != first[0]
    assert conn.execute("SELECT superseded_by FROM l1_document WHERE id = %s", (first[0],)).fetchone()[0] == linked[0]
    assert conn.execute("SELECT count(*) FROM l1_document_line WHERE document_id = %s", (linked[0],)).fetchone()[0] == 1
    same = conn.execute(
        "SELECT a.gross_minor = b.gross_minor AND a.extra = b.extra AND a.number_raw = b.number_raw"
        + (" AND a.counterparty_id = b.counterparty_id" if with_0006 else "")
        + " FROM l1_document a, l1_document b WHERE a.id = %s AND b.id = %s", (first[0], linked[0]),
    ).fetchone()[0]
    assert same                                                      # the new version is a full copy
    assert relink_pending(conn, odm.id) == 0


def test_settlement_points_to_the_advance(conn, odm):
    ingest_ksef(conn, odm, MockKsef(), *WINDOW)
    advance = _doc_by_ksef(conn, odm.id, _ksef_number("FV 1/10/2026", "sales"))
    settlement = _doc_by_ksef(conn, odm.id, _ksef_number("FV 3/10/2026", "sales"))
    assert settlement[3] == advance[0]
    order_lines = conn.execute(
        "SELECT line_kind, net_minor FROM l1_document_line WHERE document_id = %s", (advance[0],)
    ).fetchall()
    assert order_lines == [("order", 8_000_000)]


def test_same_nip_and_number_with_another_amount_goes_to_the_queue(conn, odm):
    ingest_ksef(conn, odm, OnlyKsef([_ksef_number("FV 4/09/2026", "sales")]), *WINDOW)
    csv = ("numer;data;kontrahent;NIP;netto;VAT;brutto;typ\r\n"
           "FV 4/09/2026;14.09.2026;Agencja Fala sp. z o.o.;5253143376;7500,00;1725,00;9000,00;sprzedaż\r\n")
    report = import_table(conn, odm, csv.encode("cp1250"), filename="fala.csv", mapping_key="document")
    assert report.documents_created == 1 and report.possible_duplicates == 1
    docs = conn.execute(
        "SELECT id, source, evidence FROM l1_document WHERE company_id = %s AND superseded_by IS NULL ORDER BY source",
        (odm.id,),
    ).fetchall()
    assert [d[1] for d in docs] == ["csv", "ksef"]
    csv_doc, ksef_doc = docs[0], docs[1]
    assert csv_doc[2]["possible_duplicate"]["candidates"] == [str(ksef_doc[0])]
    [suggestion] = conn.execute(
        "SELECT kind::text, subject_id, proposal, status::text, evidence FROM l2_suggestion WHERE company_id = %s",
        (odm.id,),
    ).fetchall()
    assert suggestion[:2] == ("duplicate", csv_doc[0]) and suggestion[3] == "open"
    assert suggestion[2]["duplicate_of"] == str(ksef_doc[0]) and "gross" in suggestion[4]["differences"]
    # re-import: the row now finds its own document, nothing new
    before = row_counts(conn, odm.id)
    import_table(conn, odm, csv.encode("cp1250"), filename="fala.csv", mapping_key="document")
    assert row_counts(conn, odm.id) == before


def test_new_parser_version_supersedes_the_old_row(conn, odm):
    number = _ksef_number("FV 7/10/2026", "sales")
    ingest_ksef(conn, odm, OnlyKsef([number]), *WINDOW)
    old_id = _doc_by_ksef(conn, odm.id, number)[0]
    [invoice] = OnlyKsef([number]).fetch_invoices(odm, *WINDOW)
    raw = store_raw(conn, odm.id, "ksef", invoice.xml, content_type="application/xml")
    assert raw.created is False
    facts = parse_fa3(invoice.xml, company_nip=ODM_NIP, ksef_number=number, parser_version=2)
    observation = Observation(raw_id=raw.id, source="ksef", created_by="pytest")
    result = upsert_document(conn, odm.id, facts, observation)
    assert result.action == "new_version"
    rows = conn.execute(
        "SELECT id, superseded_by FROM l1_document WHERE company_id = %s AND ksef_number = %s",
        (odm.id, number),
    ).fetchall()
    assert set(rows) == {(old_id, result.document_id), (result.document_id, None)}
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")                  # one current row per KSeF number
    lines = "SELECT count(*) FROM l1_document_line WHERE document_id = %s"
    assert conn.execute(lines, (result.document_id,)).fetchone()[0] == conn.execute(lines, (old_id,)).fetchone()[0] == 2
    assert upsert_document(conn, odm.id, facts, observation).action == "unchanged"
    # the old parser version reading the same file again does not go back
    old_facts = parse_fa3(invoice.xml, company_nip=ODM_NIP, ksef_number=number)
    assert upsert_document(conn, odm.id, old_facts, observation).action == "unchanged"


def test_older_parser_never_replaces_a_newer_reading(conn, odm):
    number = _ksef_number("FV 8/10/2026", "sales")
    [invoice] = OnlyKsef([number]).fetch_invoices(odm, *WINDOW)
    raw = store_raw(conn, odm.id, "ksef", invoice.xml, content_type="application/xml")
    observation = Observation(raw_id=raw.id, source="ksef", created_by="pytest")
    newer = parse_fa3(invoice.xml, company_nip=ODM_NIP, ksef_number=number, parser_version=3)
    assert upsert_document(conn, odm.id, newer, observation).action == "created"
    older = parse_fa3(invoice.xml, company_nip=ODM_NIP, ksef_number=number, parser_version=2)
    assert upsert_document(conn, odm.id, older, observation).action == "unchanged"
    assert len(current_documents(conn, odm.id)) == 1


def test_counterparty_from_nip(conn, odm, with_0006):
    if not with_0006:
        pytest.skip("brak migracji 0006 (l1_counterparty, kasa_upsert_counterparty)")
    ingest_ksef(conn, odm, MockKsef(), *WINDOW)
    rows = conn.execute(
        """
        SELECT d.direction::text, coalesce(d.buyer_nip, d.buyer_foreign_tax_id),
               coalesce(d.seller_nip, d.seller_foreign_tax_id), c.nip, c.foreign_tax_id
          FROM l1_document d JOIN l1_counterparty c ON c.id = d.counterparty_id
         WHERE d.company_id = %s AND d.superseded_by IS NULL
        """,
        (odm.id,),
    ).fetchall()
    assert len(rows) == 42
    for direction, buyer, seller, nip, foreign in rows:
        assert (nip or foreign) == (buyer if direction == "sales" else seller)
    # Orange: 12 invoices, one counterparty
    assert conn.execute(
        "SELECT count(DISTINCT counterparty_id) FROM l1_document WHERE company_id = %s AND seller_nip = '5263451926'",
        (odm.id,),
    ).fetchone()[0] == 1
    # our NIP reached the company's own counterparty
    assert conn.execute("SELECT nip FROM l1_counterparty WHERE company_id = %s AND kind = 'own'",
                        (odm.id,)).fetchone() == (ODM_NIP,)


def test_without_counterparty_function_documents_keep_nip_in_fields(conn, odm, monkeypatch):
    from kasa.core.documents import ingest

    real = ingest.capabilities

    def without_0006(c):
        return replace(real(c), counterparty_function=False, own_counterparty_function=False)

    monkeypatch.setattr(ingest, "capabilities", without_0006)
    report = ingest_ksef(conn, odm, OnlyKsef([_ksef_number("FV 7/10/2026", "sales")]), *WINDOW)
    assert report.documents_created == 1 and not report.errors
    row = conn.execute("SELECT buyer_nip FROM l1_document WHERE company_id = %s", (odm.id,)).fetchone()
    assert row == ("5213775822",)
    assert isinstance(without_0006(conn), Capabilities)
