"""Normalization of identifiers and amounts shared by every parser.

* invoice numbers: ``"FV 7/10/2026"`` -> ``"FV7102026"``
* amounts: ``Decimal("123.45")`` -> ``12345`` minor units (never floats, never silent rounding)
* IBAN: ``"pl61 1090 1014 0000 0712 1981 2874"`` -> ``"PL61109010140000071219812874"``
* NIP: ``"PL 526-000-12-46"`` -> ``"5260001246"``
"""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

# ---------------------------------------------------------------------------
# Invoice numbers
# ---------------------------------------------------------------------------

_NUMBER_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+")


def normalize_invoice_number(raw: str) -> str:
    """Normalize an invoice number so it can be found in a transfer title.

    Upper case, no spaces or separators, and leading zeros dropped from every
    numeric segment ("FV 07/10/2026" and "FV 7/10/2026" are the same number).
    The result is a search key, not an identity: the same key from another
    NIP is never a duplicate, and dedup also requires matching date and amount.
    """
    if not isinstance(raw, str):
        raise TypeError(f"invoice number must be str, got {type(raw).__name__}")
    text = unicodedata.normalize("NFKC", raw).upper()
    tokens = _NUMBER_TOKEN_RE.findall(text)
    if not tokens:
        raise ValueError(f"invoice number has no letters or digits: {raw!r}")
    return "".join(token.lstrip("0") or "0" if token.isdigit() else token for token in tokens)


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------

# ISO 4217 minor-unit exponents that differ from the default of 2.
CURRENCY_EXPONENTS: dict[str, int] = {
    "BHD": 3, "CLP": 0, "ISK": 0, "JOD": 3, "JPY": 0, "KRW": 0, "KWD": 3, "OMR": 3, "TND": 3, "VND": 0,
}
_SPACES = {" ", " ", " ", " ", "'", "\t"}
_MINUS_SIGNS = {"-", "−", "–"}


def minor_exponent(currency: str) -> int:
    """Number of decimal places of the currency's minor unit."""
    if not re.fullmatch(r"[A-Z]{3}", currency or ""):
        raise ValueError(f"invalid currency code: {currency!r}")
    return CURRENCY_EXPONENTS.get(currency, 2)


def parse_amount(text: str) -> Decimal:
    """Parse an amount as written in Polish and international files.

    Accepts spaces (incl. non-breaking) as thousand separators, decimal comma
    or dot, and a leading or trailing minus: ``"12 300,00"``, ``"1.234,56"``,
    ``"1,234.56"``, ``"-450"``, ``"12,30-"``. When both "," and "." occur, the
    last one is the decimal separator. Raises ValueError on anything else.
    """
    if not isinstance(text, str):
        raise TypeError(f"amount text must be str, got {type(text).__name__}")
    s = "".join(ch for ch in unicodedata.normalize("NFKC", text).strip() if ch not in _SPACES)
    s = s.removesuffix("PLN").removesuffix("zł").removesuffix("ZŁ")
    negative = False
    if s and s[0] in _MINUS_SIGNS:
        negative, s = True, s[1:]
    elif s and s[-1] in _MINUS_SIGNS:
        negative, s = True, s[:-1]
    elif s.startswith("+"):
        s = s[1:]
    if not s or not re.fullmatch(r"[0-9.,]+", s):
        raise ValueError(f"not an amount: {text!r}")

    last_comma, last_dot = s.rfind(","), s.rfind(".")
    if last_comma >= 0 and last_dot >= 0:
        decimal_sep = "," if last_comma > last_dot else "."
        thousands_sep = "." if decimal_sep == "," else ","
        integer, _, fraction = s.rpartition(decimal_sep)
        integer = _strip_thousands(integer, thousands_sep, text)
    elif last_comma >= 0 or last_dot >= 0:
        sep = "," if last_comma >= 0 else "."
        if s.count(sep) > 1:
            integer, fraction = _strip_thousands(s, sep, text), ""
        else:
            integer, _, fraction = s.partition(sep)
    else:
        integer, fraction = s, ""
    if not integer.isdigit() or (fraction and not fraction.isdigit()):
        raise ValueError(f"not an amount: {text!r}")
    value = Decimal(f"{integer}.{fraction}" if fraction else integer)
    return -value if negative else value


def _strip_thousands(part: str, sep: str, original: str) -> str:
    groups = part.split(sep)
    if not groups[0] or len(groups[0]) > 3 or any(len(g) != 3 for g in groups[1:]):
        raise ValueError(f"not an amount (bad thousand separators): {original!r}")
    return "".join(groups)


def to_minor(amount: Decimal | int | str, currency: str = "PLN", *, rounding: str | None = None) -> int:
    """Convert an amount to integer minor units of ``currency`` (PLN -> grosze).

    Floats are refused. An amount with more decimals than the currency has is
    an error unless ``rounding`` (a ``decimal`` rounding mode, e.g.
    ``ROUND_HALF_UP``) is given explicitly - used only for derived values such
    as a PLN equivalent computed from an exchange rate.
    """
    if isinstance(amount, bool) or isinstance(amount, float):
        raise TypeError("amounts must be Decimal, int or str - never float")
    if isinstance(amount, str):
        value = parse_amount(amount)
    elif isinstance(amount, int):
        value = Decimal(amount)
    elif isinstance(amount, Decimal):
        value = amount
    else:
        raise TypeError(f"unsupported amount type: {type(amount).__name__}")
    if not value.is_finite():
        raise ValueError(f"amount is not finite: {amount!r}")
    exponent = minor_exponent(currency)
    scaled = value.scaleb(exponent)
    integral = scaled.to_integral_value(rounding=rounding or ROUND_HALF_UP)
    if integral != scaled and rounding is None:
        raise ValueError(f"{amount!r} has more than {exponent} decimal places for {currency}; pass rounding explicitly")
    return int(integral)


def from_minor(minor: int, currency: str = "PLN") -> Decimal:
    """Inverse of :func:`to_minor`: ``12345`` -> ``Decimal("123.45")``."""
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise TypeError("minor units must be int")
    exponent = minor_exponent(currency)
    return Decimal(minor).scaleb(-exponent).quantize(Decimal(1).scaleb(-exponent))


# ---------------------------------------------------------------------------
# IBAN
# ---------------------------------------------------------------------------

# Lengths for countries we expect; others are checked against the generic 15-34 range.
IBAN_LENGTHS: dict[str, int] = {
    "AT": 20, "BE": 16, "CH": 21, "CZ": 24, "DE": 22, "DK": 18, "EE": 20, "ES": 24, "FI": 18, "FR": 27,
    "GB": 22, "IE": 22, "IT": 27, "LT": 20, "LU": 20, "LV": 21, "NL": 18, "NO": 15, "PL": 28, "PT": 25,
    "SE": 24, "SK": 24, "UA": 29,
}
_IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")


def _iban_checksum_ok(iban: str) -> bool:
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def normalize_iban(raw: str, *, default_country: str | None = "PL", validate: bool = True) -> str:
    """Normalize an IBAN: upper case, no spaces/dashes, optional "IBAN" prefix dropped.

    A bare 26-digit Polish account number (NRB) gets the ``default_country``
    prefix. With ``validate`` the country length and the mod-97 checksum are
    verified and ValueError is raised on failure.
    """
    if not isinstance(raw, str):
        raise TypeError(f"IBAN must be str, got {type(raw).__name__}")
    s = re.sub(r"[\s\-.]", "", unicodedata.normalize("NFKC", raw)).upper()
    s = s.removeprefix("IBAN").removeprefix(":")
    if default_country and s.isdigit() and len(s) == 26:
        s = default_country + s
    if not validate:
        return s
    if not _IBAN_RE.match(s):
        raise ValueError(f"not an IBAN: {raw!r}")
    expected = IBAN_LENGTHS.get(s[:2])
    if expected is not None and len(s) != expected:
        raise ValueError(f"IBAN for {s[:2]} must have {expected} characters: {raw!r}")
    if not _iban_checksum_ok(s):
        raise ValueError(f"IBAN checksum is invalid: {raw!r}")
    return s


def is_valid_iban(raw: str) -> bool:
    try:
        normalize_iban(raw)
    except (TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# NIP (Polish tax id)
# ---------------------------------------------------------------------------

_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)


def normalize_nip(raw: str, *, validate: bool = True) -> str:
    """Normalize a Polish NIP to 10 digits ("PL 526-000-12-46" -> "5260001246")."""
    if not isinstance(raw, str):
        raise TypeError(f"NIP must be str, got {type(raw).__name__}")
    s = re.sub(r"[\s\-.]", "", unicodedata.normalize("NFKC", raw)).upper().removeprefix("PL")
    if not re.fullmatch(r"[0-9]{10}", s):
        raise ValueError(f"not a NIP: {raw!r}")
    if validate:
        check = sum(int(d) * w for d, w in zip(s[:9], _NIP_WEIGHTS, strict=True)) % 11
        if check == 10 or check != int(s[9]):
            raise ValueError(f"NIP checksum is invalid: {raw!r}")
    return s
