"""Ingestion: L0 -> L1, idempotency, parser versions, pending holds, counterparties, MPP, statement files."""

from __future__ import annotations

from datetime import date

import pytest

from bank_paths import DATA, FIXTURES, OCTOBER
from kasa.core.bank import ingest as ingest_module
from kasa.core.bank.ingest import (
    AccountRow,
    IngestResult,
    glue_mpp,
    import_statement_file,
    ingest_company,
    store_entries,
    upsert_account,
)
from kasa.core.bank.model import BankBalance, BankEntry
from kasa.core.checks import check_balance_continuity, check_mpp, run_truth_checks
from kasa.core.companies import ensure_company
from kasa.core.extension import get_extension
from kasa.core.providers.bank import AccountInfo

CAMT = FIXTURES / "bank" / "camt053" / "mbank_biezacy_2026-10.xml"


def _counts(conn, company_id):
    return conn.execute(
        """
        SELECT count(*) FILTER (WHERE superseded_by IS NULL AND status = 'booked'),
               count(*) FILTER (WHERE superseded_by IS NULL AND status = 'pending'),
               count(*)
          FROM l1_bank_transaction WHERE company_id = %s
        """,
        (company_id,),
    ).fetchone()


def test_raw_payloads_are_stored_before_parsing(conn, ingested):
    company, results = ingested
    assert sum(r.raw_created for r in results) == 9            # 3 accounts x (balances, BOOK, PDNG)
    rows = conn.execute(
        "SELECT channel::text, meta->>'endpoint', meta->'params'->>'date_from' FROM l0_raw WHERE company_id = %s",
        (company.id,)).fetchall()
    assert {r[0] for r in rows} == {"bank"}
    assert sum(1 for r in rows if r[1].endswith("/transactions") and r[2] == "2026-10-01") == 6
    unlinked = conn.execute(
        "SELECT count(*) FROM l1_bank_transaction WHERE company_id = %s AND raw_id IS NULL", (company.id,)).fetchone()
    assert unlinked == (0,)


def test_refetching_the_same_range_creates_no_duplicates(conn, ingested, provider):
    company, _ = ingested
    before = _counts(conn, company.id)
    raw_before = conn.execute("SELECT count(*) FROM l0_raw WHERE company_id = %s", (company.id,)).fetchone()
    results = ingest_company(conn, company, provider, *OCTOBER)
    assert sum(r.inserted + r.superseded for r in results) == 0
    assert sum(r.unchanged for r in results) == 87
    assert sum(r.raw_existing for r in results) == 9 and sum(r.raw_created for r in results) == 0
    assert _counts(conn, company.id) == before == (83, 4, 87)
    assert conn.execute("SELECT count(*) FROM l0_raw WHERE company_id = %s", (company.id,)).fetchone() == raw_before
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")              # exclusion constraints hold


def test_a_new_parser_version_supersedes_every_fact(conn, ingested, provider, monkeypatch, rows_of):
    company, _ = ingested
    old = {r["entry_reference"]: r for r in rows_of(company.id)}
    groups_before = {r["entry_reference"]: r["mpp_group_id"] for r in old.values() if r["mpp_group_id"]}
    monkeypatch.setattr(ingest_module, "PARSER_SCHEMA_VERSION", 2)
    results = ingest_company(conn, company, provider, *OCTOBER)
    assert sum(r.superseded for r in results) == 87 and sum(r.inserted for r in results) == 0
    assert _counts(conn, company.id) == (83, 4, 174)
    new = {r["entry_reference"]: r for r in rows_of(company.id)}
    assert {r["schema_version"] for r in new.values()} == {2}
    pairs = conn.execute(
        """
        SELECT o.entry_reference, o.schema_version, n.id, n.schema_version FROM l1_bank_transaction o
          JOIN l1_bank_transaction n ON n.id = o.superseded_by
         WHERE o.company_id = %s
        """,
        (company.id,)).fetchall()
    assert len(pairs) == 87 and all(p[1] == 1 and p[3] == 2 and new[p[0]]["id"] == p[2] for p in pairs)
    assert {r["entry_reference"]: r["mpp_group_id"] for r in new.values() if r["mpp_group_id"]} == groups_before
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    # the version that is current now is not rewritten again
    assert sum(r.unchanged for r in ingest_company(conn, company, provider, *OCTOBER)) == 87


def test_booked_operation_supersedes_its_card_hold(conn, company):
    account = upsert_account(conn, company.id, AccountInfo(
        uid="t", iban="PL65116022020000000555123456", currency="PLN", kind="current", provider="test",
        bank_name="Bank Test"))
    hold = BankEntry(source="test", account_iban=account.iban, locator="p0", entry_reference="P1", status="pending",
                     transaction_date=date(2026, 10, 30), amount_minor=-120000, currency="PLN",
                     counterparty_name="HOTEL VISTULA WARSZAWA", title_lines=["HOTEL VISTULA - preautoryzacja"],
                     bank_tx_code="CCRD/POSD")
    result = IngestResult(account.iban, "test")
    store_entries(conn, company, account, [hold], {}, result)
    booked = hold.model_copy(update={"entry_reference": "B1", "status": "booked", "booking_date": date(2026, 11, 2),
                                     "locator": "b0", "title_lines": ["HOTEL VISTULA WARSZAWA PL"]})
    result = IngestResult(account.iban, "test")
    store_entries(conn, company, account, [booked], {}, result)
    assert result.pending_resolved == 1
    rows = conn.execute(
        """
        SELECT p.entry_reference, b.entry_reference FROM l1_bank_transaction p
          JOIN l1_bank_transaction b ON b.id = p.superseded_by WHERE p.company_id = %s
        """, (company.id,)).fetchall()
    assert rows == [("P1", "B1")]
    # the hold fetched again does not come back
    again = IngestResult(account.iban, "test")
    store_entries(conn, company, account, [hold], {}, again)
    assert again.unchanged == 1 and _counts(conn, company.id)[:2] == (1, 0)


def test_counterparties_own_transfers_and_nip(conn, ingested, rows_of, ground_truth):
    company, _ = ingested
    own = conn.execute(
        "SELECT id, nip, ibans FROM l1_counterparty WHERE company_id = %s AND kind = 'own'", (company.id,)).fetchone()
    assert own[1] == ground_truth["meta"]["company"]["nip"] and len(own[2]) == 3
    rows = {r["entry_reference"]: r for r in rows_of(company.id)}
    own_transfers = {t["entry_reference"] for t in ground_truth["transactions"] if t["op_type"] == "own"}
    assert {k for k, r in rows.items() if r["op_type"] == "own"} == own_transfers
    assert all(rows[k]["counterparty_id"] == own[0] for k in own_transfers)
    # outgoing split payment: the supplier's NIP comes from the /IDC/ of the block
    scena = conn.execute(
        "SELECT nip, kind::text, ibans FROM l1_counterparty WHERE id = %s",
        (rows["202610143186203462"]["counterparty_id"],)).fetchone()
    assert scena == ("9516127975", "company", ["PL69114818653705756556558562"])
    # the same IBAN is the same counterparty (three Scena Pro transfers, two Orbita ones)
    assert rows["202610289276403991"]["counterparty_id"] == rows["202610143186203462"]["counterparty_id"]
    assert rows["202610136613771164"]["counterparty_id"] == rows["202610219260141951"]["counterparty_id"]
    kinds = dict(conn.execute(
        "SELECT kind::text, count(*) FROM l1_counterparty WHERE company_id = %s GROUP BY 1", (company.id,)).fetchall())
    assert kinds["institution"] == 3 and kinds["bank"] == 2 and kinds["person"] >= 20
    fee = rows["ML2610196097579"]
    assert conn.execute("SELECT name, kind::text FROM l1_counterparty WHERE id = %s",
                        (fee["counterparty_id"],)).fetchone() == ("Bank Millennium S.A.", "bank")
    low = conn.execute("SELECT confidence FROM l1_counterparty WHERE id = %s",
                       (rows["202610166224040945"]["counterparty_id"],)).fetchone()[0]
    assert float(low) < 0.5                                    # card merchant known only by name


def test_vat_account_points_at_its_current_account(conn, ingested):
    company, _ = ingested
    rows = conn.execute(
        """
        SELECT a.iban, a.kind::text, p.iban FROM l1_bank_account a LEFT JOIN l1_bank_account p
            ON p.id = a.parent_account_id WHERE a.company_id = %s ORDER BY a.iban
        """, (company.id,)).fetchall()
    assert rows == [("PL40114020040000350277812345", "current", None),
                    ("PL67114020040000310277812346", "vat", "PL40114020040000350277812345"),
                    ("PL80116022020000000412906733", "current", None)]


def test_split_payment_legs_share_one_group(conn, ingested, rows_of, ground_truth):
    company, _ = ingested
    rows = {r["entry_reference"]: r for r in rows_of(company.id)}
    for _name, refs in ground_truth["checks"]["mpp"]["groups"].items():
        groups = {rows[ref]["mpp_group_id"] for ref in refs}
        assert len(groups) == 1 and None not in groups
        assert sorted(rows[ref]["mpp_role"] for ref in refs) == ["main", "vat_leg"]
        main = next(rows[ref] for ref in refs if rows[ref]["mpp_role"] == "main")
        assert main["account_kind"] == "current"
    grouped = {k for k, r in rows.items() if r["mpp_group_id"]}
    assert grouped == {ref for refs in ground_truth["checks"]["mpp"]["groups"].values() for ref in refs}
    # the rebooking by the tax office's decision is an own transfer, not part of a split payment
    assert rows["202610288152441704"]["mpp_group_id"] is None
    assert glue_mpp(conn, company.id, *OCTOBER) == 0           # gluing again changes nothing


def test_split_payment_with_a_rebooking_into_the_vat_account(conn, company):
    current = upsert_account(conn, company.id, AccountInfo(
        uid="c", iban="PL65116022020000000555123456", currency="PLN", kind="current", provider="test", bic="BIGBPLPW"))
    vat = upsert_account(conn, company.id, AccountInfo(
        uid="v", iban="PL80116022020000000412906733", currency="PLN", kind="vat", provider="test", bic="BIGBPLPW"))
    title = ["/VAT/230,00/IDC/9516127975/INV/SP/2026/11/004/TXT/technika"]
    day = date(2026, 11, 5)

    def entry(iban, ref, amount, lines, party="Scena Pro sp. z o.o.", party_iban="PL69114818653705756556558562",
              code="ICDT/DMCT"):
        return BankEntry(source="test", account_iban=iban, locator=ref, entry_reference=ref, status="booked",
                         booking_date=day, amount_minor=amount, currency="PLN", counterparty_name=party,
                         counterparty_iban=party_iban, title_lines=lines, bank_tx_code=code)

    store_entries(conn, company, current, [
        entry(current.iban, "C1", -100000, title),
        entry(current.iban, "C2", -23000, ["Przeksięgowanie na rachunek VAT"], party="Firma", party_iban=vat.iban),
    ], {}, IngestResult(current.iban, "test"))
    store_entries(conn, company, vat, [
        entry(vat.iban, "V1", 23000, ["Przeksięgowanie na rachunek VAT"], party="Firma", party_iban=current.iban),
        entry(vat.iban, "V2", -23000, title),
    ], {}, IngestResult(vat.iban, "test"))
    assert glue_mpp(conn, company.id) == 4
    roles = dict(conn.execute(
        "SELECT entry_reference, mpp_role FROM l1_bank_transaction WHERE company_id = %s", (company.id,)).fetchall())
    assert roles == {"C1": "main", "V2": "vat_leg", "C2": "vat_rebooking", "V1": "vat_rebooking"}
    assert conn.execute("SELECT count(DISTINCT mpp_group_id) FROM l1_bank_transaction WHERE company_id = %s",
                        (company.id,)).fetchone() == (1,)


def test_camt_after_enablebanking_is_the_same_facts(conn, ingested):
    company, _ = ingested
    [result] = import_statement_file(conn, company, CAMT.read_bytes(), fmt="camt053", filename=CAMT.name)
    assert (result.inserted, result.unchanged, result.booked) == (0, 66, 66)
    assert _counts(conn, company.id) == (83, 4, 87)
    raw = conn.execute("SELECT channel::text, meta->>'format' FROM l0_raw WHERE company_id = %s AND external_ref = %s",
                       (company.id, CAMT.name)).fetchone()
    assert raw == ("file", "camt053")


def test_camt_alone_closes_the_balance_with_computed_balances(conn):
    company, _ = ensure_company(conn, get_extension("rentease"))
    [result] = import_statement_file(conn, company, CAMT.read_bytes(), fmt="camt053", filename=CAMT.name)
    assert result.inserted == 66
    computed = conn.execute(
        "SELECT bool_and(balance_after_computed) FROM l1_bank_transaction WHERE company_id = %s",
        (company.id,)).fetchone()
    assert computed == (True,)
    [balance] = check_balance_continuity(conn, company.id, *OCTOBER, record=False)
    assert balance.ok and balance.detail["closing_minor"] == 13882035


def test_mt940_import(conn, company):
    payload = (DATA / "millennium_2611.mt940").read_bytes()
    [result] = import_statement_file(conn, company, payload, fmt="mt940", filename="millennium_2611.mt940")
    assert (result.booked, result.inserted) == (5, 5)
    rows = {r[0]: r[1:] for r in conn.execute(
        """
        SELECT coalesce(entry_reference, '-'), op_type::text, extracted, title_pseudonymized, counterparty_pseudonym
          FROM l1_bank_transaction WHERE company_id = %s
        """, (company.id,)).fetchall()}
    assert rows["MT2611030002"][0] == "fee"
    assert rows["MT2611060004"][0] == "blik"               # MT940 code NTRF, the ~00 description says BLIK
    assert rows["MT2611050003"][1]["mpp"]["inv"] == "SP/2026/11/004"
    assert {i["raw"] for i in rows["MT2611020001"][1]["invoice_numbers"]} == {"FV 12/11/2026", "FV 13/11/2026"}
    assert "500 08" not in rows["MT2611060004"][2] and rows["MT2611060004"][3].startswith("P-")
    checks = run_truth_checks(conn, company.id, date(2026, 11, 1), date(2026, 11, 30), record=False)
    assert [c.ok for c in checks] == [True, True]
    # importing the same file again: nothing new
    [again] = import_statement_file(conn, company, payload, fmt="mt940", filename="millennium_2611.mt940")
    assert (again.inserted, again.unchanged, again.raw_existing) == (0, 5, 1)


def test_balance_check_turns_red_when_an_operation_is_missing(conn, company):
    account = upsert_account(conn, company.id, AccountInfo(
        uid="t", iban="PL65116022020000000555123456", currency="PLN", kind="current", provider="test"))
    day = date(2026, 11, 2)
    entries = [
        BankEntry(source="test", account_iban=account.iban, locator=str(i), entry_reference=f"R{i}", status="booked",
                  booking_date=day, amount_minor=amount, currency="PLN", balance_after_minor=after,
                  title_lines=[f"op {i}"], bank_tx_code="RCDT/DMCT")
        for i, (amount, after) in enumerate([(1000, 11000), (500, 12500)])   # 1000 missing between them
    ]
    balances = [BankBalance(source="test", account_iban=account.iban, balance_type="OPBD", reference_date=day,
                            amount_minor=10000, currency="PLN")]
    result = IngestResult(account.iban, "test")
    store_entries(conn, company, AccountRow(account.id, account.iban, "PLN", "current", None), entries, {}, result,
                  balances=balances)
    assert result.warnings                                      # the parser already noticed
    conn.execute(
        """
        INSERT INTO l1_bank_balance (company_id, account_id, balance_type, reference_date, amount_minor, currency,
                                     source, method, created_by)
        VALUES (%s, %s, 'OPBD', %s, 10000, 'PLN', 'test', 'code', 'pytest')
        """, (company.id, account.id, day))
    [balance] = check_balance_continuity(conn, company.id, day, day)
    assert not balance.ok and balance.detail["mismatches"][0]["kind"] == "chain_break"
    stored = conn.execute(
        "SELECT ok, scope, detail->>'iban' FROM truth_check_result WHERE company_id = %s AND check_name = %s",
        (company.id, "balance_continuity")).fetchone()
    assert stored == (False, account.iban, account.iban)


def test_mpp_check_turns_red_without_the_vat_leg(conn, company):
    account = upsert_account(conn, company.id, AccountInfo(
        uid="t", iban="PL65116022020000000555123456", currency="PLN", kind="current", provider="test"))
    entry = BankEntry(source="test", account_iban=account.iban, locator="0", entry_reference="M1", status="booked",
                      booking_date=date(2026, 11, 3), amount_minor=100000, currency="PLN",
                      title_lines=["/VAT/230,00/IDC/5342650778/INV/FV 1/11/2026/TXT/x"], bank_tx_code="RCDT/DMCT")
    store_entries(conn, company, account, [entry], {}, IngestResult(account.iban, "test"))
    result = check_mpp(conn, company.id, date(2026, 11, 1), date(2026, 11, 30), record=False)
    assert not result.ok and result.detail["difference_minor"] == 23000
    assert result.detail["legs_without_group"] == ["M1"]


@pytest.mark.parametrize("fmt", ["camt053", "mt940"])
def test_statement_parsers_reject_the_other_format(conn, company, fmt):
    with pytest.raises(ValueError):
        import_statement_file(conn, company, b"garbage", fmt=fmt)
