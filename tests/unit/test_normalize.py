from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

from kasa.core.normalize import (
    from_minor,
    is_valid_iban,
    normalize_iban,
    normalize_invoice_number,
    normalize_nip,
    parse_amount,
    to_minor,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("FV 7/10/2026", "FV7102026"),           # example from the briefing
        ("fv 7/10/2026", "FV7102026"),
        ("FV-7-10-2026", "FV7102026"),
        ("FV 07/10/2026", "FV7102026"),          # different zeros, same number
        ("FV/0007/10/2026", "FV7102026"),
        ("  FV  7 / 10 / 2026 ", "FV7102026"),
        ("FV7102026", "FV7102026"),              # idempotent
        ("FV 7∕10∕2026", "FV7102026"),  # NBSP and division slash
        ("ＦＶ ７/１０/２０２６", "FV7102026"),   # full-width characters
        ("F/2026/10/0012", "F20261012"),
        ("ZAL 3/2026", "ZAL32026"),
        ("KOR/0/2026", "KOR02026"),              # a zero segment stays a zero
        ("INV-2026-00042", "INV202642"),
    ],
)
def test_normalize_invoice_number(raw, expected):
    assert normalize_invoice_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "/-/", "."])
def test_normalize_invoice_number_rejects_empty(raw):
    with pytest.raises(ValueError):
        normalize_invoice_number(raw)


def test_normalize_invoice_number_rejects_non_text():
    with pytest.raises(TypeError):
        normalize_invoice_number(7102026)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("123.45"), 12345),
        (Decimal("12300"), 1230000),
        (Decimal("12300.00"), 1230000),
        (Decimal("-0.01"), -1),
        (Decimal("0"), 0),
        (5, 500),
        ("12 300,00", 1230000),
        ("12 300,50 zł", 1230050),
        ("1.234,56", 123456),
        ("1,234.56", 123456),
        ("1 234 567,89", 123456789),
        ("0,5", 50),
        ("-450", -45000),
        ("12,30-", -1230),
        ("+99.00", 9900),
        ("2300 PLN", 230000),
    ],
)
def test_to_minor(amount, expected):
    assert to_minor(amount) == expected
    assert isinstance(to_minor(amount), int)


def test_to_minor_never_rounds_silently():
    with pytest.raises(ValueError):
        to_minor(Decimal("1.005"))
    with pytest.raises(ValueError):
        to_minor("1.234")  # a single dot is a decimal point -> three decimals
    assert to_minor(Decimal("1.005"), rounding=ROUND_HALF_UP) == 101


@pytest.mark.parametrize("amount", [12.3, True])
def test_to_minor_refuses_floats_and_bools(amount):
    with pytest.raises(TypeError):
        to_minor(amount)


@pytest.mark.parametrize("text", ["", "abc", "12,345,67", "1.2.3,4", "--5", "12 zł 30", "1,23,456"])
def test_parse_amount_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_to_minor_currency_exponents():
    assert to_minor("1500", "JPY") == 1500
    assert to_minor(Decimal("1.234"), "KWD") == 1234
    assert to_minor(Decimal("99"), "EUR") == 9900
    with pytest.raises(ValueError):
        to_minor(Decimal("1.5"), "JPY")
    with pytest.raises(ValueError):
        to_minor(1, "pln")


def test_from_minor_round_trip():
    assert from_minor(12345) == Decimal("123.45")
    assert from_minor(-1) == Decimal("-0.01")
    assert from_minor(1500, "JPY") == Decimal("1500")
    for minor in (0, 1, -99, 1230000, 123456789):
        assert to_minor(from_minor(minor)) == minor


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PL61 1090 1014 0000 0712 1981 2874", "PL61109010140000071219812874"),
        ("pl61109010140000071219812874", "PL61109010140000071219812874"),
        ("61 1090 1014 0000 0712 1981 2874", "PL61109010140000071219812874"),  # bare NRB
        ("IBAN PL61-1090-1014-0000-0712-1981-2874", "PL61109010140000071219812874"),
        ("DE89 3704 0044 0532 0130 00", "DE89370400440532013000"),
        ("GB82 WEST 1234 5698 7654 32", "GB82WEST12345698765432"),
        ("LT12 1000 0111 0100 1000", "LT121000011101001000"),
    ],
)
def test_normalize_iban(raw, expected):
    assert normalize_iban(raw) == expected
    assert is_valid_iban(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "PL62109010140000071219812874",  # checksum
        "PL6110901014000007121981287",   # length
        "DE8937040044053201300",         # length for DE
        "XX",
        "",
    ],
)
def test_normalize_iban_rejects_invalid(raw):
    with pytest.raises(ValueError):
        normalize_iban(raw)
    assert not is_valid_iban(raw)


def test_normalize_iban_without_validation_only_cleans():
    assert normalize_iban("pl62 1090", validate=False) == "PL621090"


def test_normalize_nip():
    assert normalize_nip("PL 526-000-12-46") == "5260001246"
    assert normalize_nip("526 000 12 46") == "5260001246"
    with pytest.raises(ValueError):
        normalize_nip("5260001247")
    with pytest.raises(ValueError):
        normalize_nip("12345")
