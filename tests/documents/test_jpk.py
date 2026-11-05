"""JPK_V7M (fixture) and JPK_FA (synthetic) importers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kasa.core.documents.ingest import CompanyNipConflict, get_company_nip, ingest_jpk
from kasa.core.documents.jpk import JpkError, parse_jpk

from .conftest import FIXTURES, ODM_NIP, current_documents, ground_truth

JPK_PATH = FIXTURES / "history" / "JPK_V7M_2026-09.xml"


def test_v7m_rows_become_facts_without_lines():
    parsed = parse_jpk(JPK_PATH.read_bytes())
    assert parsed.form == "JPK_V7M"
    assert parsed.subject_nip == ODM_NIP
    assert parsed.period == {"Rok": "2026", "Miesiac": "9"}
    assert parsed.declaration["P_51"] == "8877"
    sales = [f for locator, f in parsed.rows if locator.startswith("sales:")]
    purchases = [f for locator, f in parsed.rows if locator.startswith("purchase:")]
    assert (len(sales), len(purchases)) == (5, 18)
    assert all(not f.lines and f.kind == "vat" and not f.kind_certain for _, f in parsed.rows)
    # the register's VAT totals match its control sums
    assert sum(f.vat_minor for f in sales) == 1_483_500             # SprzedazCtrl/PodatekNalezny
    assert sum(f.vat_minor for f in purchases) == 595_781           # ZakupCtrl/PodatekNaliczony
    fv5 = dict(parsed.rows)["sales:5"]
    assert fv5.number_raw == "FV/5/09/2026" and fv5.number_normalized == "FV592026"
    assert fv5.seller.nip == ODM_NIP and fv5.buyer.nip == "7795524621"
    assert fv5.gross_minor == 307_500 and fv5.sale_date.isoformat() == "2026-09-26"
    kreska = dict(parsed.rows)["purchase:17"]
    assert kreska.direction == "purchase" and kreska.seller.nip == "8970418075"
    assert kreska.extra["received_date"] == "2026-09-26"


def test_history_only_rows_match_ground_truth():
    parsed = dict(parse_jpk(JPK_PATH.read_bytes()).rows)
    for doc in ground_truth()["history_only_documents"]:
        facts = parsed[f"{doc['section']}:{doc['jpk_lp']}"]
        assert facts.number_raw == doc["number"]
        assert facts.issue_date.isoformat() == doc["issue_date"]
        assert facts.counterparty.nip == doc["nip"]
        assert (facts.net_minor, facts.vat_minor) == (int(Decimal(doc["net"]) * 100), int(Decimal(doc["vat"]) * 100))


def test_jpk_of_another_company_is_refused(conn, odm):
    conn.execute("UPDATE company SET nip = '5260001246' WHERE id = %s", (odm.id,))
    with pytest.raises(JpkError, match="dotyczy NIP"):
        ingest_jpk(conn, odm, JPK_PATH.read_bytes(), filename=JPK_PATH.name)


def test_jpk_teaches_the_company_nip(conn, odm):
    assert get_company_nip(conn, odm.id) is None
    report = ingest_jpk(conn, odm, JPK_PATH.read_bytes(), filename=JPK_PATH.name)
    assert get_company_nip(conn, odm.id) == ODM_NIP
    assert report.documents_created == 23 and not report.errors
    assert len(current_documents(conn, odm.id)) == 23


def test_company_nip_never_changes_silently(conn, odm):
    from kasa.core.documents.ingest import ensure_company_nip

    ensure_company_nip(conn, odm.id, ODM_NIP, source="test")
    with pytest.raises(CompanyNipConflict):
        ensure_company_nip(conn, odm.id, "5260001246", source="test")


JPK_FA = """<?xml version="1.0" encoding="UTF-8"?>
<JPK xmlns="http://jpk.mf.gov.pl/wzor/2022/02/17/02171/">
  <Naglowek>
    <KodFormularza kodSystemowy="JPK_FA (4)" wersjaSchemy="1-0">JPK_FA</KodFormularza>
    <WariantFormularza>4</WariantFormularza>
    <DataWytworzeniaJPK>2025-11-03T10:00:00</DataWytworzeniaJPK>
    <DataOd>2025-10-01</DataOd><DataDo>2025-10-31</DataDo>
  </Naglowek>
  <Podmiot1><IdentyfikatorPodmiotu><NIP>5342650778</NIP><PelnaNazwa>ODM Partners sp. z o.o.</PelnaNazwa>
  </IdentyfikatorPodmiotu></Podmiot1>
  <Faktura>
    <KodWaluty>PLN</KodWaluty><P_1>2025-10-05</P_1><P_2A>FV 1/10/2025</P_2A>
    <P_3A>Nordis Retail sp. z o.o.</P_3A><P_3C>ODM Partners sp. z o.o.</P_3C>
    <P_4B>5342650778</P_4B><P_5B>5867422344</P_5B><P_6>2025-10-03</P_6>
    <P_13_1>22030.00</P_13_1><P_14_1>5066.90</P_14_1><P_15>27096.90</P_15>
    <RodzajFaktury>VAT</RodzajFaktury>
  </Faktura>
  <FakturaWiersz><P_2B>FV 1/10/2025</P_2B><P_7>Roadshow - obsługa</P_7><P_8A>usł.</P_8A><P_8B>1</P_8B>
    <P_9A>20000.00</P_9A><P_11>20000.00</P_11><P_12>23</P_12></FakturaWiersz>
  <FakturaWiersz><P_2B>FV 1/10/2025</P_2B><P_7>Transport</P_7><P_8A>usł.</P_8A><P_8B>1</P_8B>
    <P_9A>2030.00</P_9A><P_11>2030.00</P_11><P_12>23</P_12></FakturaWiersz>
</JPK>"""


def test_jpk_fa_gives_sales_invoices_with_lines():
    parsed = parse_jpk(JPK_FA.encode())
    assert parsed.form == "JPK_FA" and parsed.subject_nip == ODM_NIP
    [(locator, facts)] = parsed.rows
    assert locator == "invoice:FV 1/10/2025"
    assert facts.direction == "sales" and facts.buyer.nip == "5867422344"
    assert facts.gross_minor == 2_709_690 and len(facts.lines) == 2 and facts.lines_sum_ok is True


def test_jpk_fa_and_office_csv_are_one_document(conn, odm):
    """The same 2025 invoice from JPK_FA and from the office export: one document, two observations."""
    from kasa.core.documents.csv_import import import_table

    ingest_jpk(conn, odm, JPK_FA.encode(), filename="JPK_FA_2025-10.xml")
    csv_path = FIXTURES / "history" / "biuro_export_2025-10.csv"
    report = import_table(conn, odm, csv_path.read_bytes(), filename=csv_path.name, mapping_key="document")
    assert report.linked == {"nip_kind_number": 1}
    [doc] = [d for d in current_documents(conn, odm.id) if d[1] == "FV 1/10/2025"]
    assert doc[2] == "jpk"      # JPK is more certain than the CSV export; the document keeps its fields
