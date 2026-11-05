"""Gate 2 - acquisition, documents part (docs/FAZY.md): every channel of the fixtures imported
once gives the documents of the ground truth; the same invoice from several channels is one
document; KSeF completeness is green; importing everything again adds nothing."""

from __future__ import annotations

from datetime import date

import pytest

from kasa.core.checks import check_ksef_completeness
from kasa.core.companies import Company
from kasa.core.documents.csv_import import import_table
from kasa.core.documents.ingest import capabilities, ingest_jpk, ingest_ksef
from kasa.core.documents.pdf import import_pdfs
from kasa.core.providers.ksef import MockKsef
from kasa.core.providers.llm import MockLlm
from tests.documents.helpers import (
    FIXTURES,
    expected_pdfs,
    ground_truth,
    ksef_index,
    load_nbp_rates,
    observations_of,
    row_counts,
)

WINDOW = (date(2026, 9, 1), date(2026, 10, 31))
JPK = FIXTURES / "history" / "JPK_V7M_2026-09.xml"
OFFICE = FIXTURES / "history" / "biuro_export_2025-10.csv"
PM = FIXTURES / "internal" / "arkusz_pm_2026-10.xlsx"
PDFS = FIXTURES / "email_pdfs"
# The hotel invoice once more, as the office would export it in October: the third channel.
HOTEL_EXPORT = ("numer;data;kontrahent;NIP;netto;VAT;brutto;typ\r\n"
                "FV/2026/10/0412;10.10.2026;Hotel Wawel Grand sp. z o.o.;6768253927;12880,00;1198,40;14078,40;zakup\r\n"
                ).encode("cp1250")


def import_everything(conn, company: Company) -> dict[str, object]:
    reports = {
        "ksef": ingest_ksef(conn, company, MockKsef(), *WINDOW),
        "jpk": ingest_jpk(conn, company, JPK.read_bytes(), filename=JPK.name),
        "office": import_table(conn, company, OFFICE.read_bytes(), filename=OFFICE.name, mapping_key="document"),
        "pm": import_table(conn, company, PM.read_bytes(), filename=PM.name, mapping_key="pm_sheet"),
        "pdf": import_pdfs(conn, company, [PDFS], meta_path=PDFS / "index.json", llm=MockLlm()),
        "hotel_export": import_table(conn, company, HOTEL_EXPORT, filename="biuro_export_2026-10_hotel.csv",
                                     mapping_key="document"),
    }
    return reports


@pytest.fixture
def imported(conn, make):
    company_id = make.company(extension_slug="odm")
    conn.execute("UPDATE company SET name = 'ODM Partners' WHERE id = %s", (company_id,))
    company = Company(id=company_id, slug="odm", name="ODM Partners", extension_slug="odm")
    load_nbp_rates(conn)
    reports = import_everything(conn, company)
    return company, reports


def _current(conn, company_id, where: str = "TRUE", params: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, source, number_raw, ksef_number, direction::text, kind::text,
                   coalesce(CASE direction WHEN 'sales' THEN buyer_nip ELSE seller_nip END,
                            CASE direction WHEN 'sales' THEN buyer_foreign_tax_id ELSE seller_foreign_tax_id END)
                       AS tax_id,
                   evidence
              FROM l1_document WHERE company_id = %s AND superseded_by IS NULL AND {where}
            """,
            (company_id, *params),
        )
        names = [d.name for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def test_gate2_documents(conn, imported):
    company, reports = imported
    for name, report in reports.items():
        assert not report.errors, (name, report.errors)

    # KSeF: 42 documents, exactly the KSeF list
    index = {i["ksef_number"]: i for i in ksef_index()["invoices"]}
    with_ksef = _current(conn, company.id, "ksef_number IS NOT NULL")
    assert {d["ksef_number"] for d in with_ksef} == set(index) and len(with_ksef) == 42
    assert all(d["source"] == "ksef" for d in with_ksef)

    # PDF: 6 files -> 5 new documents, the hotel joins its KSeF invoice
    assert reports["pdf"].documents_created == 5
    assert len(_current(conn, company.id, "source = 'pdf'")) == 5
    hotel_ksef = expected_pdfs()["EM-06"]["same_as_ksef"]
    [hotel] = _current(conn, company.id, "ksef_number = %s", (hotel_ksef,))
    # ... and the office export of the same invoice is the third observation of the same document
    assert sorted(o[:2] for o in observations_of(conn, hotel["id"])) == [
        ("csv", "nip_kind_number"), ("ksef", "created"), ("pdf", "nip_kind_number")]

    # JPK: the September invoices present in KSeF are linked, the rest are history-only documents
    truth = ground_truth()
    assert reports["jpk"].linked == {"nip_kind_number": len(truth["dedup"]["ksef_jpk"])}
    jpk_only = _current(conn, company.id, "source = 'jpk'")
    assert {(d["number_raw"], d["tax_id"]) for d in jpk_only} == {
        (h["number"], h["nip"]) for h in truth["history_only_documents"]}
    for pair in truth["dedup"]["ksef_jpk"]:
        [doc] = _current(conn, company.id, "ksef_number = %s", (pair["ksef_number"],))
        assert ("jpk", "nip_kind_number", f"{pair['jpk_section']}:{pair['jpk_lp']}") in observations_of(conn, doc["id"])

    # Office export 10/2025: 23 documents of their own
    assert reports["office"].documents_created == 23
    assert len(_current(conn, company.id, "source = 'csv'")) == 23

    # 42 + 5 + 19 + 23 current documents; nothing flagged as a possible duplicate
    documents = _current(conn, company.id)
    assert len(documents) == 42 + 5 + len(truth["history_only_documents"]) + 23 == 89
    assert not any("possible_duplicate" in d["evidence"] for d in documents)
    assert conn.execute("SELECT count(*) FROM l2_suggestion WHERE company_id = %s", (company.id,)).fetchone()[0] == 0

    # every document has its counterparty (0006) or at least the counterparty's tax id in its fields
    caps = capabilities(conn)
    without_tax_id = {d["number_raw"] for d in documents if not d["tax_id"]}
    assert without_tax_id == {"4/2026", "5091772410"}       # bill of a person (EM-05), Google in the export
    if caps.counterparties:
        missing = conn.execute(
            "SELECT number_raw FROM l1_document WHERE company_id = %s AND superseded_by IS NULL "
            "AND counterparty_id IS NULL", (company.id,),
        ).fetchall()
        assert missing == []

    # relations: correction -> corrected invoice, settlement -> advance
    relations = conn.execute(
        """
        SELECT d.number_raw, c.number_raw, a.number_raw
          FROM l1_document d
          LEFT JOIN l1_document c ON c.id = d.corrects_document_id
          LEFT JOIN l1_document a ON a.id = d.settles_advance_document_id
         WHERE d.company_id = %s AND d.superseded_by IS NULL AND d.source = 'ksef'
           AND (d.corrects_document_id IS NOT NULL OR d.settles_advance_document_id IS NOT NULL)
        """,
        (company.id,),
    ).fetchall()
    assert sorted(relations) == [("FV 3/10/2026", None, "FV 1/10/2026"), ("KOR 1/10/2026", "FV 2/10/2026", None)]

    # PM sheet: 20 internal records with the whole row and a pseudonym instead of the name
    records = conn.execute(
        "SELECT count(*), count(person_pseudonym), sum(amount_minor) FROM l1_internal_record WHERE company_id = %s",
        (company.id,),
    ).fetchone()
    pm_truth = truth["pm_sheet_rows"]
    assert records == (20, 20, sum(int(r["amount"].replace(".", "")) for r in pm_truth))

    # truth check nr 4
    result = check_ksef_completeness(conn, company, MockKsef(), *WINDOW)
    assert result.ok, result.detail()
    assert result.kasa_count == truth["checks"]["ksef_completeness"]["count"]

    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")   # deferred exclusion constraints hold

    # importing everything again adds nothing
    before = row_counts(conn, company.id)
    again = import_everything(conn, company)
    assert row_counts(conn, company.id) == before
    assert all(r.documents_created == 0 and r.observations_created == 0 and r.versions_created == 0
               for r in again.values())
