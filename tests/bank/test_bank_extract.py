"""Extraction from titles by code, on every title of the fixtures, and the operation type."""

from __future__ import annotations

import re
from datetime import date

import pytest

from bank_paths import FIXTURES
from kasa.core.bank import enablebanking as eb
from kasa.core.bank.extract import classify_op_type, extract_title, ksef_checksum_ok
from kasa.core.normalize import normalize_invoice_number

OWN = {"PL40114020040000350277812345", "PL67114020040000310277812346", "PL80116022020000000412906733"}
KINDS = {"mbank_biezacy": "current", "mbank_vat": "vat", "millennium_biezacy": "current"}
IBANS = {"mbank_biezacy": "PL40114020040000350277812345", "mbank_vat": "PL67114020040000310277812346",
         "millennium_biezacy": "PL80116022020000000412906733"}


def _entries():
    out = {}
    for slug, iban in IBANS.items():
        for name in ("transactions.json", "transactions_pending.json"):
            path = FIXTURES / "bank" / "enablebanking" / slug / name
            for entry in eb.parse_transactions(path.read_bytes(), iban):
                out[entry.entry_reference] = (slug, entry)
    return out


ENTRIES = _entries()


def _numbers(extracted):
    found = set()
    for item in extracted.get("invoice_numbers", []):
        found.add(item["normalized"])
        found.update(item["alt"])
    return found


def test_every_invoice_number_written_in_a_title_is_extracted(ground_truth):
    checked = 0
    for tx in ground_truth["transactions"]:
        _slug, entry = ENTRIES[tx["entry_reference"]]
        extracted = extract_title(entry.title_lines, reference_date=entry.booking_date)
        for doc in tx["matched_documents"]:
            number = doc.get("invoice_number")
            if not number or number.casefold() not in entry.title.casefold():
                continue
            assert normalize_invoice_number(number) in _numbers(extracted), (tx["title"], number, extracted)
            checked += 1
    assert checked >= 35


@pytest.mark.parametrize(("title", "expected"), [
    ("Zaplata za FV SP/2026/10/081, SP/2026/10/093, SP/2026/10/101",
     ["SP/2026/10/081", "SP/2026/10/093", "SP/2026/10/101"]),
    ("FS/2026/10/0007 i F-2026-10-7", ["FS/2026/10/0007", "F-2026-10-7"]),
    ("Faktura 214/09/2026", ["214/09/2026"]),
    ("F/40020041/10/26", ["F/40020041/10/26"]),
    ("Polecenie zaplaty L/10/2026/0815 rata 10/36", ["L/10/2026/0815"]),
    ("INV FV 5/10/2026 HELIOS PARTNER SUMMIT 2026", ["FV 5/10/2026"]),
    ("FV/2026/10/0412", ["FV/2026/10/0412"]),
    ("Rachunek 4/2026 - konferansjerka", ["4/2026"]),
    ("/VAT/2300,00/IDC/5342650778/INV/FV 7/10/2026/TXT/atrakcje", ["FV 7/10/2026"]),
])
def test_invoice_number_patterns(title, expected):
    extracted = extract_title([title], reference_date=date(2026, 10, 20))
    assert {normalize_invoice_number(n) for n in expected} <= _numbers(extracted), extracted
    assert len(extracted["invoice_numbers"]) == len(expected)


@pytest.mark.parametrize("title", [
    "Wynagrodzenie 09/2026",
    "Skladki ZUS za 09/2026",
    "Zaliczka 30% - Gala ACME 2026 - umowa 14/2026",
    "Umowa o dzielo 07/2026 - projekt scenografii",
    "Komornik KM 1187/26 - zajecie wynagrodzenia za 09/2026",
    "Oplata za prowadzenie rachunku 10/2026",
    "Kapitalizacja odsetek 10/2026",
    "Helios Summit hostessa 15-16.10",
    "Zaliczka na poczet faktury - rezerwacja, termin 14.11.2026",
    "Przeksiegowanie z rachunku VAT - postanowienie NUS 1471-SPV.4012.37.2026 z 21.10.2026",
])
def test_periods_contracts_dates_and_cases_are_not_invoices(title):
    assert "invoice_numbers" not in extract_title([title], reference_date=date(2026, 10, 20))


def test_periods_contracts_cases_installments_dates():
    ref = date(2026, 10, 9)
    assert extract_title(["Czynsz pazdziernik 2026 FV 7/10/2026"], reference_date=ref)["periods"] == [
        {"raw": "pazdziernik 2026", "month": "2026-10", "kind": "month_name"}]
    assert extract_title(["czynsz listopad"], reference_date=ref)["periods"][0]["month"] == "2026-11"
    assert extract_title(["Skladki ZUS za 09/2026"], reference_date=ref)["periods"][0]["month"] == "2026-09"
    assert extract_title(["umowa 14/2026"])["contract_refs"] == ["14/2026"]
    assert extract_title(["Umowa o dzielo 07/2026"])["contract_refs"] == ["07/2026"]
    assert extract_title(["Komornik KM 1187/26 - zajecie"])["case_refs"] == ["KM 1187/26"]
    assert extract_title(["rata 10/36"])["installment"] == {"no": 10, "of": 36}
    assert extract_title(["Helios Summit hostessa 15-16.10"], reference_date=ref)["dates"] == [
        "2026-10-15", "2026-10-16"]
    assert extract_title(["wplata z 14.10.2026"])["dates"] == ["2026-10-14"]


def test_split_payment_and_tax_blocks(ground_truth):
    for tx in ground_truth["transactions"]:
        _slug, entry = ENTRIES[tx["entry_reference"]]
        extracted = extract_title(entry.title_lines, reference_date=entry.booking_date)
        block = tx["mpp_vat_block"]
        if block:
            mpp = extracted["mpp"]
            assert (mpp["vat"], mpp["idc"], mpp["inv"]) == (block["vat"], block["idc"], block["inv"])
            assert mpp["vat_minor"] == int(block["vat"].replace(".", "")) and mpp["idc_nip"] == block["idc"]
        else:
            assert "mpp" not in extracted
    tax = extract_title(["/TI/N5342650778/OKR/26M09/SFP/VAT7/TXT/VAT za 09/2026"])
    assert tax["tax"] == {"id_type": "N", "id": "5342650778", "period": "26M09", "form": "VAT7",
                          "txt": "VAT za 09/2026"}
    assert {"raw": "26M09", "month": "2026-09", "kind": "tax"} in tax["periods"]


def test_ksef_number_phone_card_operator_and_original_amount():
    number = "5342650778-20261021-1D7921E70E4A-35"
    assert ksef_checksum_ok(number) and not ksef_checksum_ok(number[:-2] + "00")
    out = extract_title([f"Zaplata {number} FV 7/10/2026"])
    assert out["ksef_numbers"] == [{"number": number, "checksum_ok": True}]
    assert "FV7102026" in _numbers(out)
    blik = extract_title(["Nordis Gdansk hostessa 03.10", "Przelew na telefon BLIK +48 500 085 853"])
    assert blik["phones"] == ["500085853"]
    card = extract_title(["GOOGLE *Workspace", "Kwota oryg. 99,00 EUR, kurs 4,4154", "Karta 5375 **** **** 4417"])
    assert card["card"] == {"masked": "5375********4417"}
    assert card["original_amount"] == {"amount": "99.00", "currency": "EUR", "rate": "4.4154"}
    revolut = extract_title(["KWADRAT DEVELOPMENT SP. Z O.O. - wynajem naglosnienia"],
                            counterparty_name="Revolut Bank UAB")
    assert revolut["operator"] == {"name": "revolut", "ref": None, "via": "counterparty"}
    assert extract_title(["Zaplata PayU nr 12345678 sklep"])["operator"]["ref"] == "12345678"
    assert extract_title(["STRIPE PAYOUT po_1NxAbCdEfGhIjK"])["operator"]["ref"] == "po_1NxAbCdEfGhIjK"


def test_every_fixture_title_extracts_without_error():
    for _slug, entry in ENTRIES.values():
        out = extract_title(entry.title_lines, reference_date=entry.effective_date,
                            counterparty_name=entry.counterparty_name)
        assert out["extractor_version"] >= 1
        for item in out.get("invoice_numbers", []):
            assert re.fullmatch(r"[A-Z0-9]+", item["normalized"])


def test_op_type_matches_the_ground_truth(ground_truth):
    for tx in [*ground_truth["transactions"], *ground_truth["pending_transactions"]]:
        slug, entry = ENTRIES[tx["entry_reference"]]
        extracted = extract_title(entry.title_lines, reference_date=entry.effective_date)
        op_type, reason = classify_op_type(entry, extracted, own_ibans=OWN, account_kind=KINDS[slug])
        assert op_type == tx["op_type"], (tx["entry_reference"], tx["title"], op_type, reason)
