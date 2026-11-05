"""FA(3) parser on every KSeF fixture and on the traps described in fixtures/odm/SCENARIUSZ.md."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import pytest

from kasa.core.documents.fa3 import Fa3Error, parse_fa3
from kasa.core.documents.facts import ksef_number_date, ksef_number_valid
from kasa.core.normalize import normalize_invoice_number, to_minor

from .conftest import FIXTURES, ODM_NIP, ground_truth, ksef_index

INDEX = ksef_index()["invoices"]


def _parse(entry: dict, **kwargs):
    xml = (FIXTURES / "ksef" / entry["file"]).read_bytes()
    return parse_fa3(xml, company_nip=ODM_NIP, ksef_number=entry["ksef_number"], **kwargs)


def _by_number(number: str, direction: str) -> dict:
    return next(e for e in INDEX if e["invoice_number"] == number and e["direction"] == direction)


@pytest.mark.parametrize("entry", INDEX, ids=[e["ksef_number"] for e in INDEX])
def test_every_invoice_parses_with_totals_and_lines_adding_up(entry):
    facts = _parse(entry)
    currency = entry["currency"]
    assert facts.direction == entry["direction"]
    assert facts.number_raw == entry["invoice_number"]
    assert facts.number_normalized == normalize_invoice_number(entry["invoice_number"])
    assert facts.issue_date.isoformat() == entry["issue_date"]
    assert facts.currency == currency
    assert (facts.net_minor, facts.vat_minor, facts.gross_minor) == (
        to_minor(Decimal(entry["net"]), currency), to_minor(Decimal(entry["vat"]), currency),
        to_minor(Decimal(entry["gross"]), currency),
    )
    # sums per VAT rate (P_13_x + P_14_x) = P_15
    assert sum(s.net_minor + (s.vat_minor or 0) for s in facts.vat_summary) == facts.gross_minor
    assert facts.checks["per_rate_sum_equals_P_15"] is True
    # lines: net per rate equals P_13_x (correction: after - before; advance: order lines = order value)
    if facts.kind == "advance":
        assert facts.lines_sum_ok is True
        order = [l for l in facts.lines if l.line_kind == "order"]
        assert order and sum(l.net_minor + l.vat_minor for l in order) == facts.checks["lines"]["order_value"]
    elif facts.kind == "settlement":
        assert facts.lines_sum_ok is None   # needs the advance invoice: checked in the next test
    else:
        assert facts.lines_sum_ok is True, facts.checks
        per_rate: dict[str, int] = defaultdict(int)
        for line in facts.lines:
            per_rate[line.vat_rate] += line.net_minor
        assert sorted(per_rate.values()) == sorted(s.net_minor for s in facts.vat_summary)
        # line VAT is the rate's P_14 shared out exactly
        assert sum(l.vat_minor for l in facts.lines) == facts.vat_minor
        assert sum(l.gross_minor for l in facts.lines) == facts.gross_minor
    assert facts.file_hash == entry["sha256"]
    assert ksef_number_valid(entry["ksef_number"])
    assert facts.counterparty.tax_key == (entry["buyer_id"] if entry["direction"] == "sales" else entry["seller_nip"])


def test_settlement_lines_are_the_whole_order_minus_the_advance():
    advance = _parse(_by_number("FV 1/10/2026", "sales"))
    settlement = _parse(_by_number("FV 3/10/2026", "sales"))
    assert advance.kind == "advance" and settlement.kind == "settlement"
    assert settlement.advance_refs == [{"ksef_number": "5342650778-20261002-BE973AA732BF-1B"}]
    assert settlement.checks["lines"]["advance_net_deducted"] == advance.net_minor == 2_400_000
    whole_order = sum(l.net_minor for l in settlement.lines)
    assert whole_order == 8_000_000 == settlement.net_minor + advance.net_minor
    assert advance.sale_date.isoformat() == "2026-10-01"          # P_6 = date of the payment
    assert advance.payment["paid"] is True


def test_correction_has_reference_and_before_after_lines():
    facts = _parse(_by_number("KOR 1/10/2026", "sales"))
    assert facts.kind == "correction"
    assert facts.corrects_ref["ksef_number"] == "5342650778-20261005-F3887A61DE66-1E"
    assert facts.corrects_ref["number"] == "FV 2/10/2026"
    assert facts.corrects_ref["issue_date"] == "2026-10-05"
    assert "Rabat" in facts.corrects_ref["reason"]
    assert facts.sale_date is None                                 # no P_6: follows the corrected invoice
    [line] = facts.lines
    assert line.line_kind == "correction"
    assert line.before["net_minor"] == 813008 and line.after["net_minor"] == 650406
    assert line.net_minor == -162602 == facts.net_minor
    assert facts.gross_minor == -200000


def test_dates_period_missing_p6_and_missing_due_date():
    period = _parse(_by_number("FV 4/10/2026", "sales"))
    assert period.sale_date is None
    assert (period.service_period_start.isoformat(), period.service_period_end.isoformat()) == (
        "2026-09-15", "2026-10-14")
    no_p6 = _parse(_by_number("LP/118/2026", "purchase"))
    assert no_p6.sale_date == no_p6.issue_date                     # P_6 omitted = sale on the issue date
    no_due = _parse(_by_number("FV 9/10/2026", "sales"))
    assert no_due.due_date is None                                 # optional in FA(3), not "payable now"
    assert _parse(_by_number("FV 4/09/2026", "sales")).due_date.isoformat() == "2026-09-28"


def test_offline_invoice_ksef_date_differs_from_issue_date():
    entry = _by_number("FV 14/2026", "purchase")
    facts = _parse(entry)
    assert entry["offline"] is True
    assert facts.issue_date.isoformat() == "2026-10-05"
    assert ksef_number_date(entry["ksef_number"]).isoformat() == "2026-10-06"


def test_euro_invoice_keeps_euro_and_derives_pln():
    facts = _parse(_by_number("FV 5/10/2026", "sales"))
    assert facts.currency == "EUR" and facts.gross_minor == 1_250_000
    assert facts.fx_rate == Decimal("4.2468") and facts.fx_rate_source == "document"
    assert facts.gross_pln_minor == 5_308_500                      # ground truth: 53 085,00 PLN
    assert facts.reverse_charge is True and facts.mpp_required is False
    assert facts.buyer.foreign_tax_id == "DE686148513" and facts.buyer.nip is None
    assert facts.vat_summary[0].rate == "np II" and facts.vat_summary[0].field == "P_13_9"
    assert facts.payment_iban == "PL40114020040000350277812345"
    assert facts.payment["accounts"][0]["swift"] == "BREXPLPWMBK"


def test_hotel_two_rates_and_period():
    facts = _parse(_by_number("FV/2026/10/0412", "purchase"))
    rates = {s.rate: (s.net_minor, s.vat_minor) for s in facts.vat_summary}
    assert rates == {"23": (112000, 25760), "8": (1176000, 94080)}
    assert [l.vat_rate for l in facts.lines] == ["8", "23"]
    assert facts.payment_iban == "PL58102100283340938176291318"     # 26-digit NRB -> IBAN


def test_ground_truth_dates_and_flags_match():
    truth = {i["ksef_number"]: i for i in ground_truth()["invoices"]}
    for entry in INDEX:
        facts = _parse(entry)
        expected = truth[entry["ksef_number"]]
        assert (facts.sale_date.isoformat() if facts.sale_date else None) == expected["sale_date"]
        assert (facts.due_date.isoformat() if facts.due_date else None) == expected["due_date"]
        assert facts.mpp_required == expected["mpp_flag"]
        assert facts.reverse_charge == expected["reverse_charge"]
        if expected["period"]:
            assert facts.service_period_start.isoformat() == expected["period"]["from"]
        assert to_minor(Decimal(expected["gross_pln"])) == facts.gross_pln_minor


def test_unknown_elements_are_kept_in_extra():
    facts = _parse(_by_number("FV 7/10/2026", "sales"))
    assert facts.extra["Podmiot2/JST"] == "2"
    assert facts.extra["Fa/Adnotacje/Zwolnienie/P_19N"] == "1"
    assert facts.extra["form"]["code"] == "FA (3)"
    assert facts.issuer_software


def _patched(entry: dict, replace: list[tuple[str, str]]) -> bytes:
    xml = (FIXTURES / "ksef" / entry["file"]).read_text(encoding="utf-8")
    for old, new in replace:
        assert old in xml, old
        xml = xml.replace(old, new, 1)
    return xml.encode()


def test_third_parties_order_contract_split_payment_and_new_elements():
    entry = _by_number("FV 7/10/2026", "sales")
    factor = ("</Podmiot2><Podmiot3><DaneIdentyfikacyjne><NIP>5260001246</NIP>"
              "<Nazwa>Faktor SA</Nazwa></DaneIdentyfikacyjne><Rola>1</Rola></Podmiot3>")
    description = ("<RodzajFaktury>VAT</RodzajFaktury><DodatkowyOpis><Klucz>Projekt</Klucz>"
                   "<Wartosc>Gala</Wartosc></DodatkowyOpis>")
    conditions = ("</Platnosc><WarunkiTransakcji><Umowy><DataUmowy>2026-05-01</DataUmowy>"
                  "<NrUmowy>14/2026</NrUmowy></Umowy><Zamowienia><DataZamowienia>2026-05-02</DataZamowienia>"
                  "<NrZamowienia>ZAM/77</NrZamowienia></Zamowienia></WarunkiTransakcji>")
    xml = _patched(entry, [
        ("</Podmiot2>", factor),
        ("<P_16>2</P_16>", "<P_16>1</P_16>"),
        ("<RodzajFaktury>VAT</RodzajFaktury>", description),
        ("</Platnosc>", conditions),
    ])
    facts = parse_fa3(xml, company_nip=ODM_NIP, ksef_number=entry["ksef_number"])
    [factor] = facts.third_parties
    assert (factor.nip, factor.role, factor.role_label) == ("5260001246", "1", "faktor")
    assert facts.mpp_required is True                              # P_18A=1 in this fixture already
    assert facts.cash_method is True
    assert facts.order_number == "ZAM/77" and facts.contract_ref == "14/2026"
    assert facts.extra["Fa/DodatkowyOpis/Klucz"] == "Projekt"


def test_direction_needs_the_company():
    entry = _by_number("FV 7/10/2026", "sales")
    xml = (FIXTURES / "ksef" / entry["file"]).read_bytes()
    with pytest.raises(Fa3Error, match="nie dotyczy firmy"):
        parse_fa3(xml, company_nip="5260001246")
    assert parse_fa3(xml, company_nip=None, direction_hint="sales").direction == "sales"
    with pytest.raises(Fa3Error, match="kierunku"):
        parse_fa3(xml, company_nip=None)
    with pytest.raises(Fa3Error):
        parse_fa3(b"<not-xml", company_nip=ODM_NIP)
