"""Points of the phase 2 review fixed in phase 3: NIP provenance, no plain phone numbers in facts."""

from __future__ import annotations

import re
from datetime import date

from kasa.core.documents.ingest import ingest_jpk, ingest_ksef
from kasa.core.providers.ksef import MockKsef

from .helpers import FIXTURES, ground_truth, phones


def test_company_nip_records_where_it_came_from(conn, odm):
    ingest_ksef(conn, odm, MockKsef(), date(2026, 10, 1), date(2026, 10, 31))
    nip, evidence = conn.execute("SELECT nip, nip_evidence FROM company WHERE id = %s", (odm.id,)).fetchone()
    assert nip == "5342650778" and evidence["source"].startswith("KSeF") and evidence["set_at"]
    # a later source does not rewrite the first provenance
    jpk = FIXTURES / "history" / "JPK_V7M_2026-09.xml"
    ingest_jpk(conn, odm, jpk.read_bytes(), filename=jpk.name)
    assert conn.execute("SELECT nip_evidence FROM company WHERE id = %s", (odm.id,)).fetchone()[0] == evidence


def test_jpk_first_records_its_raw_file(conn, make):
    from kasa.core.companies import Company

    company_id = make.company(extension_slug="odm")
    company = Company(company_id, "odm", "ODM Partners", "odm")
    jpk = FIXTURES / "history" / "JPK_V7M_2026-09.xml"
    ingest_jpk(conn, company, jpk.read_bytes(), filename=jpk.name)
    evidence = conn.execute("SELECT nip_evidence FROM company WHERE id = %s", (company_id,)).fetchone()[0]
    raw_id = conn.execute("SELECT id::text FROM l0_raw WHERE company_id = %s", (company_id,)).fetchone()[0]
    assert evidence["raw_id"] == raw_id and "JPK" in evidence["source"]


def test_bank_facts_keep_only_the_phone_pseudonym(conn, loaded):
    rows = conn.execute(
        """
        SELECT entry_reference, extracted, schema_version FROM l1_bank_transaction
         WHERE company_id = %s AND superseded_by IS NULL AND extracted ? 'phones'
        """,
        (loaded.id,),
    ).fetchall()
    assert len(rows) >= 17                                          # BLIK to a phone
    known = phones(ground_truth())
    for ref, extracted, version in rows:
        assert version >= 2
        assert all(set(p) == {"pseudonym"} and p["pseudonym"].startswith("T-") for p in extracted["phones"]), ref
        digits = re.sub(r"\D", "", str(extracted))
        assert not [p for p in known if p in digits], ref
