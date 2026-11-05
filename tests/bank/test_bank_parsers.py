"""Parsers: EnableBanking JSON, camt.053 and MT940 give the same intermediate model."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kasa.core.bank import camt053, mt940
from bank_paths import DATA, FIXTURES
from kasa.core.bank import enablebanking as eb

EB = FIXTURES / "bank" / "enablebanking"
MBANK = "PL40114020040000350277812345"


def test_enablebanking_transactions_are_signed_and_complete():
    entries = eb.parse_transactions((EB / "mbank_biezacy" / "transactions.json").read_bytes(), MBANK)
    assert len(entries) == 66
    by_ref = {e.entry_reference: e for e in entries}

    incoming = by_ref["202610017411739420"]
    assert incoming.amount_minor == 2952000 and incoming.status == "booked"
    assert incoming.counterparty_name == "ACME Polska sp. z o.o."
    assert incoming.counterparty_iban == "PL19102602838510658312528272"
    assert incoming.counterparty_bic == "BPKOPLPW"
    assert incoming.balance_after_minor == 17190652
    assert incoming.bank_tx_code == "RCDT/DMCT"

    card = by_ref["202610166224040945"]
    assert card.amount_minor == -43712
    assert (card.original_amount_minor, card.original_currency) == (-9900, "EUR")
    assert card.fx_rate == Decimal("4.4154")
    assert card.transaction_date.isoformat() == "2026-10-15" and card.booking_date.isoformat() == "2026-10-16"
    assert card.merchant_category_code == "7372"
    assert card.title == "GOOGLE *Workspace odmpar G.CO/HELPPAY# IE | Kwota oryg. 99,00 EUR, kurs 4,4154 | " \
                         "Karta 5375 **** **** 4417"

    fee = by_ref["202610281026795668"]
    assert fee.counterparty_name is None and fee.amount_minor == -2500


def test_enablebanking_pending_and_balances():
    pending = eb.parse_transactions((EB / "mbank_biezacy" / "transactions_pending.json").read_bytes(), MBANK)
    assert [p.status for p in pending] == ["pending"] * 3
    assert all(p.booking_date is None and p.transaction_date for p in pending)
    balances = {b.balance_type: b for b in eb.parse_balances((EB / "mbank_biezacy" / "balances.json").read_bytes(),
                                                             MBANK)}
    assert balances["OPBD"].amount_minor == 14238652 and balances["OPBD"].reference_date.isoformat() == "2026-10-01"
    assert balances["CLBD"].amount_minor == 13882035
    assert balances["ITAV"].amount_minor == 13718455


def test_camt053_is_the_same_month_as_enablebanking():
    [statement] = camt053.parse((FIXTURES / "bank" / "camt053" / "mbank_biezacy_2026-10.xml").read_bytes())
    assert statement.account_iban == MBANK and statement.currency == "PLN"
    assert statement.owner_name == "ODM Partners sp. z o.o." and statement.bic == "BREXPLPWMBK"
    balances = {b.balance_type: b.amount_minor for b in statement.balances}
    assert balances == {"OPBD": 14238652, "CLBD": 13882035}
    assert balances["OPBD"] + sum(e.amount_minor for e in statement.entries) == balances["CLBD"]

    eb_entries = {e.entry_reference: e for e in
                  eb.parse_transactions((EB / "mbank_biezacy" / "transactions.json").read_bytes(), MBANK)}
    assert {e.entry_reference for e in statement.entries} == set(eb_entries)
    for entry in statement.entries:
        twin = eb_entries[entry.entry_reference]
        assert (entry.amount_minor, entry.booking_date, entry.value_date, entry.title, entry.counterparty_iban,
                entry.bank_tx_code) == (twin.amount_minor, twin.booking_date, twin.value_date, twin.title,
                                        twin.counterparty_iban, twin.bank_tx_code)
        assert entry.balance_after_minor is None          # camt.053 has no balance after each entry
    usd = next(e for e in statement.entries if e.entry_reference == "202610021670611934")
    assert (usd.original_amount_minor, usd.original_currency, usd.fx_rate) == (-15000, "USD", Decimal("3.9090"))


def test_camt053_rejects_other_xml():
    with pytest.raises(camt053.CamtError):
        camt053.parse(b"<Document><Other/></Document>")
    with pytest.raises(camt053.CamtError):
        camt053.parse(b"not xml")


def test_mt940_structured_86_and_balances():
    [statement] = mt940.parse((DATA / "millennium_2611.mt940").read_bytes())
    assert statement.account_iban == "PL65116022020000000555123456"
    assert statement.statement_id == "MT940-2611"
    balances = {b.balance_type: b.amount_minor for b in statement.balances}
    # :60F: of 31.10 = closing of the previous statement (PRCD); the statement starts on 1.11
    assert balances == {"PRCD": 1000000, "CLBD": 1017000, "CLAV": 1017000}
    assert statement.period_from.isoformat() == "2026-11-01" and statement.period_to.isoformat() == "2026-11-30"
    assert balances["PRCD"] + sum(e.amount_minor for e in statement.entries) == balances["CLBD"]
    first, fee, mpp, blik, no_ref = statement.entries
    assert first.entry_reference == "MT2611020001" and first.amount_minor == 123000
    assert first.counterparty_name == "ACME Polska sp. z o.o."
    assert first.counterparty_iban == "PL19102602838510658312528272"
    assert first.title == "Zapłata za FV 12/11/2026 i FV 13/11/2026"      # cp1250 decoded, pieces glued
    assert fee.title == "Prowizja za przelew Express Elixir" and fee.bank_tx_code == "SWIFT/NCHG"
    assert mpp.title == "/VAT/115,00/IDC/9516127975/INV/SP/2026/11/004/TXT/technika"
    assert blik.bank_tx_description == "PRZELEW NA TELEFON BLIK"
    assert no_ref.entry_reference is None and no_ref.title == "Zwrot kosztów - kwiaty"


def test_mt940_rejects_garbage():
    with pytest.raises(mt940.Mt940Error):
        mt940.parse(b"hello")


def test_fixture_accounts_have_the_vat_kind():
    accounts = json.loads((EB / "accounts.json").read_text(encoding="utf-8"))["accounts"]
    assert [a["fixture"]["kind"] for a in accounts] == ["current", "vat", "current"]
