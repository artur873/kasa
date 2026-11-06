"""What code (regexes, no model) reads from a transfer title (briefing, step 2).

``extract_title(lines, ...)`` returns a JSON-ready dict stored in
``l1_bank_transaction.extracted``:

* ``invoice_numbers`` - every invoice-like number (``FV 7/10/2026``, ``FS/2026/10/0007``,
  ``F-2026-10-7``, ``214/09/2026``, ``F/40020041/10/26``...) as written, normalized by
  :func:`kasa.core.normalize.normalize_invoice_number`, with ``alt`` forms (without the
  ``FV`` prefix, or with it) because a title does not say whether "FV" is part of the number;
* ``ksef_numbers`` - KSeF numbers (with the CRC-8 checksum verified);
* ``mpp`` - the split payment block ``/VAT/kwota/IDC/nip/INV/numer/TXT/tekst``;
* ``tax`` - the tax transfer block ``/TI/.../OKR/.../SFP/.../TXT/...``;
* ``periods`` (``"czynsz listopad"``, ``"za 09/2026"``, tax ``26M09``), ``dates`` mentioned
  in the title, ``contract_refs``, ``case_refs`` (bailiff), ``other_refs`` (decisions),
  ``installment`` (``rata 10/36``), ``card`` (masked number), ``original_amount``
  (``Kwota oryg. 99,00 EUR, kurs 4,4154``), ``phones`` and ``operator`` (Revolut, PayU,
  Stripe, ... with its reference) and ``payer_hint`` - the company with its legal form written in the
  title of an operator's payment (Revolut, PayU, Stripe: the client is only in the title).

``classify_op_type`` gives the mechanical operation type from the bank code, the
title and the counterparty - never a category.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from kasa.core.bank.model import BankEntry
from kasa.core.normalize import normalize_iban, normalize_invoice_number, normalize_nip, parse_amount, to_minor
from kasa.core.pseudonym import detect, fold

EXTRACTOR_VERSION = 2              # 2: payer_hint

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

MPP_RE = re.compile(
    r"/VAT/(?P<vat>[0-9][0-9 .,]*)/IDC/(?P<idc>[^/]+)/INV/(?P<inv>.+?)(?:/TXT/(?P<txt>.*))?$", re.IGNORECASE
)
TAX_RE = re.compile(
    r"/TI/(?P<id_type>[A-Z1-9])(?P<id>[^/]+)/OKR/(?P<period>[^/]+)/SFP/(?P<form>[^/]+)(?:/TXT/(?P<txt>.*))?$",
    re.IGNORECASE,
)
KSEF_RE = re.compile(r"(?<![0-9A-Z])(\d{10}-\d{8}-[0-9A-F]{12}-[0-9A-F]{2})(?![0-9A-Z])")
CARD_RE = re.compile(r"\b(\d{4})\s?[*X]{4}\s?[*X]{4}\s?(\d{4})\b")
ORIGINAL_AMOUNT_RE = re.compile(
    r"Kwota\s+oryg\.?\s*(?P<amount>[0-9][0-9 .,]*?)\s*(?P<ccy>[A-Z]{3}),?\s*kurs\s*(?P<rate>[0-9][0-9.,]*)",
    re.IGNORECASE,
)
CONTRACT_RE = re.compile(
    r"\bumow(?:a|y|ie|ę|e)\b(?:\s+o\s+\w+)?(?:\s+(?:nr|numer)\.?)?\s+(?P<ref>[A-Z0-9][\w/\-.]*\d[\w/\-]*)",
    re.IGNORECASE,
)
CASE_RE = re.compile(r"\b(?P<ref>(?:GKM|KMP|KMS|KM|Km)\s*\.?\s*\d+/\d+)\b")
DECISION_RE = re.compile(
    r"\b(?:postanowieni\w*|decyzj\w*|sygn\.?(?:\s*akt)?|wyrok\w*)\s+(?:[A-Z]{2,6}\s+)?(?P<ref>[\w./\-]*\d[\w./\-]*)",
    re.IGNORECASE,
)
INSTALLMENT_RE = re.compile(r"\brat[aay]\s+(?:nr\s+)?(?P<no>\d{1,3})\s*/\s*(?P<of>\d{1,3})\b", re.IGNORECASE)
INVOICE_KEYWORDS = (
    "FV", "FVS", "FVZ", "FVM", "FS", "FA", "FK", "FZ", "F", "KOR", "FAKT", "FAKTURA", "FAKTURY", "FAKTURE",
    "FAKTURĘ", "FAKTURZE", "FAKTUR", "RACHUNEK", "RACH", "NOTA", "KOREKTA", "KOREKTY", "INVOICE", "INV", "NR",
)
# Keywords that may be part of the number itself ("FV 7/10/2026").
NUMBER_PREFIX_KEYWORDS = frozenset({"FV", "FVS", "FVZ", "FVM", "FS", "FA", "FK", "FZ", "F", "KOR"})
_NUM = r"[A-Z0-9]+(?:[/\-.][A-Z0-9]+)*"
_KW = "(?:" + "|".join(sorted(INVOICE_KEYWORDS, key=len, reverse=True)) + ")"
_SEP = r"\s*(?:,|;|&|\+|\bi\b|\boraz\b)\s*"
_PREFIX_KW = "(?:" + "|".join(sorted(NUMBER_PREFIX_KEYWORDS, key=len, reverse=True)) + r")\.?\s+"
KEYWORD_RE = re.compile(
    # a keyword is followed by a space, a dot, a colon or a digit ("FV 7/...", "FV7/...", "Faktura: ..."),
    # never by "/" or a letter - "FV/2026/10/033" and "F/40020041/10/26" are whole numbers (structured)
    r"(?<![\w/])(?P<kw>(?:" + _KW + r"(?=[\s.:\d])[.:]?\s*(?:(?:NR|NUMER)\.?\s*)?)+)\s*(?P<list>" + _NUM +
    r"(?:" + _SEP + r"(?:" + _PREFIX_KW + r")?" + _NUM + r")*)",
    re.IGNORECASE,
)
LIST_ITEM_RE = re.compile(r"^(?:(?P<kw>" + _PREFIX_KW + r"))?(?P<num>" + _NUM + r")$", re.IGNORECASE)
STRUCTURED_RE = re.compile(r"(?<![\w/.\-*])([A-Za-z0-9]+(?:[/\-.][A-Za-z0-9]+)+)(?![\w/\-])")
FULL_DATE_RE = re.compile(r"(?<![\d/.])(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>20\d{2})(?![\d.])")
ISO_DATE_RE = re.compile(r"(?<![\d/.-])(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})(?![\d-])")
SHORT_DATE_RE = re.compile(r"(?<![\d/.,-])(?P<d>\d{1,2})(?:-(?P<d2>\d{1,2}))?\.(?P<m>\d{1,2})(?![\d.,/])")
NUM_PERIOD_RE = re.compile(r"(?<![\d/.\-])(?P<m>0?[1-9]|1[0-2])[/.\-](?P<y>20\d{2})(?![\d/.\-])")
ISO_PERIOD_RE = re.compile(r"(?<![\d/.\-])(?P<y>20\d{2})[/\-](?P<m>0[1-9]|1[0-2])(?![\d/.\-])")
TAX_PERIOD_RE = re.compile(r"^(?P<y>\d{2})(?P<kind>[MKR])(?P<n>\d{2})?$")

MONTHS: dict[str, int] = {}
for _number, _forms in enumerate((
    "styczen stycznia styczniu", "luty lutego lutym", "marzec marca marcu", "kwiecien kwietnia kwietniu",
    "maj maja maju", "czerwiec czerwca czerwcu", "lipiec lipca lipcu", "sierpien sierpnia sierpniu",
    "wrzesien wrzesnia wrzesniu", "pazdziernik pazdziernika pazdzierniku", "listopad listopada listopadzie",
    "grudzien grudnia grudniu",
), start=1):
    for _form in _forms.split():
        MONTHS[_form] = _number
MONTH_RE = re.compile(r"(?P<word>[^\W\d_]+)(?:\s+(?P<y>20\d{2}))?")

OPERATORS: dict[str, tuple[re.Pattern[str], re.Pattern[str] | None]] = {
    "revolut": (re.compile(r"\brevolut\b", re.I), re.compile(r"\b(REV[\w\-]*\d[\w\-]*)\b")),
    "payu": (re.compile(r"\bpay\s?u\b", re.I),
             re.compile(r"\b(?:PAYU|PayU)\W{0,3}(?:nr\.?\s*)?(\d{6,}|[A-Z0-9]{10,})\b")),
    "stripe": (re.compile(r"\bstripe\b", re.I), re.compile(r"\b((?:po|py|pi|ch|tr|txn)_[A-Za-z0-9]{8,})\b")),
    "paypal": (re.compile(r"\bpaypal\b", re.I), re.compile(r"\b(\d{4}[A-Z0-9]{9,})\b")),
    "przelewy24": (re.compile(r"\b(?:przelewy24|p24)\b", re.I),
                   re.compile(r"\b(P24-[A-Z0-9]{3}-[A-Z0-9]{3}-[A-Z0-9]{3})\b")),
    "tpay": (re.compile(r"\btpay\b", re.I), re.compile(r"\b(TR-[A-Z0-9]{3,}-[A-Z0-9]{3,})\b")),
    "wise": (re.compile(r"\b(?:wise|transferwise)\b", re.I), None),
    "adyen": (re.compile(r"\badyen\b", re.I), None),
}


# The payer behind an operator: a company with its legal form written in the title
# ("KWADRAT DEVELOPMENT SP. Z O.O. - wynajem naglosnienia" from Revolut Bank UAB).
PAYER_HINT_OPERATORS = frozenset({"revolut", "payu", "stripe"})
LEGAL_FORM_RE = re.compile(
    r"(?<![\w.])(?P<form>sp\.?\s*z\s*o\.?\s*o\.?|sp\.?\s*k\.?|sp\.?\s*j\.?|s\.?\s?k\.?\s?a\.?"
    r"|s\.\s?a\.?|sa(?=\s*$|\s*[-,;|/])|p\.?s\.?a\.?|gmbh|ltd\.?|limited|inc\.?|llc|uab|plc"
    r"|b\.v\.?|n\.v\.?|sarl|s\.p\.a\.?|corp\.?)(?!\w)",
    re.IGNORECASE,
)
REFERENCE_WORD_RE = re.compile(r"^[A-Za-z]{1,6}[-_/]?\w*\d\w*$|^\d{5,}$")   # REV-123, TR-ABC-1, 202610...
_HINT_STOP = frozenset("""
od dla za na do w z i oraz przelew wplata platnosc zaplata zaplaty oplata faktura faktury fv fa nr numer tytulem
tytul klient klienta zamowienie zamowienia ref revolut payu stripe from payment to
""".split())


def payer_hint(lines: list[str]) -> dict[str, Any] | None:
    """A company name with its legal form written in a title (the payer behind an operator), or None.

    Up to five name-like words before the legal form (a capital letter or a digit, not a filler word
    such as "zapłata od"), e.g. ``{"name": "KWADRAT DEVELOPMENT SP. Z O.O.", "legal_form": "SP. Z O.O."}``.
    Only companies - a person's name in a title is personal data and is never kept here.
    """
    for line in lines:
        for m in LEGAL_FORM_RE.finditer(line):
            words = re.findall(r"[^\s,;:|()\[\]]+", line[: m.start()])
            name: list[str] = []
            for word in reversed(words):
                bare = word.strip("\"'„”.-/")
                if not bare or fold(bare) in _HINT_STOP or REFERENCE_WORD_RE.match(bare) \
                        or not (bare[0].isupper() or bare[0].isdigit() or bare[0] == "&"):
                    break
                name.insert(0, bare)
                if len(name) == 5:
                    break
            if not name or not any(re.search(r"[^\W\d_]{2,}", w) for w in name):
                continue
            form = re.sub(r"\s+", " ", m.group("form").strip())
            return {"name": f"{' '.join(name)} {form}", "legal_form": form}
    return None


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def ksef_checksum_ok(number: str) -> bool:
    """KSeF number: ``NIP-YYYYMMDD-12 hex-CRC8`` (CRC-8, polynomial 0x07, over the first 32 characters)."""
    if len(number) != 35:
        return False
    try:
        return _crc8(number[:32].encode("ascii")) == int(number[33:], 16)
    except ValueError:
        return False


def _nearest_year(month: int, reference: date | None, day: int = 15) -> int:
    if reference is None:
        return date.today().year
    best = None
    for year in (reference.year - 1, reference.year, reference.year + 1):
        distance = abs((year - reference.year) * 12 + month - reference.month)
        if best is None or distance < best[0]:
            best = (distance, year)
    return best[1]


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


class _Spans:
    def __init__(self) -> None:
        self.spans: list[tuple[int, int]] = []

    def add(self, start: int, end: int) -> None:
        self.spans.append((start, end))

    def free(self, start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in self.spans)


def _is_date_like(token: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}(?:-\d{1,2})?\.\d{1,2}(?:\.\d{2,4})?|\d{4}-\d{2}-\d{2}", token))


def _invoice(raw: str, pattern: str, alt_raw: list[str]) -> dict[str, Any] | None:
    try:
        normalized = normalize_invoice_number(raw)
    except ValueError:
        return None
    alts = []
    for other in alt_raw:
        try:
            value = normalize_invoice_number(other)
        except ValueError:
            continue
        if value != normalized and value not in alts:
            alts.append(value)
    return {"raw": raw, "normalized": normalized, "alt": alts, "pattern": pattern}


def _normalized_or_none(raw: str) -> str | None:
    try:
        return normalize_invoice_number(raw)
    except ValueError:
        return None


def _number_ok(token: str, keyword_anchored: bool) -> bool:
    if not re.search(r"\d", token):
        return False
    if _is_date_like(token):
        return False
    if keyword_anchored:
        return True
    segments = re.split(r"[/\-.]", token)
    if len(segments) < 2:
        return False
    if re.search(r"[A-Za-z]\.|\.[A-Za-z]", token):       # domains, "G.CO", "BOLT.EU"
        return False
    if NUM_PERIOD_RE.fullmatch(token) or ISO_PERIOD_RE.fullmatch(token):
        return False
    has_letter_prefix = bool(re.match(r"[A-Za-z]", segments[0]))
    numeric = [s for s in segments if s.isdigit()]
    if has_letter_prefix:
        return len(numeric) >= 1
    return len(segments) >= 3 and len(numeric) == len(segments) and "/" in token


def _invoice_numbers(line: str, spans: _Spans) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for m in KEYWORD_RE.finditer(line):
        if not spans.free(m.start("list"), m.end("list")):
            continue
        keywords = [k.upper().rstrip(".") for k in re.findall(r"[^\W\d_]+", m.group("kw"))]
        prefix = next((k for k in reversed(keywords) if k in NUMBER_PREFIX_KEYWORDS), None)
        accepted = False
        for index, part in enumerate(re.split(_SEP, m.group("list"), flags=re.IGNORECASE)):
            item_match = LIST_ITEM_RE.match(part.strip())
            if item_match is None:
                continue
            value = item_match.group("num").rstrip(".-")
            if not _number_ok(value, keyword_anchored=True):
                continue
            own_kw = (item_match.group("kw") or "").strip().rstrip(".").upper() or None
            item_prefix = own_kw or (prefix if index == 0 else None)
            if item_prefix:
                item = _invoice(f"{item_prefix} {value}", "keyword", [value])
            else:
                item = _invoice(value, "keyword", [f"{prefix} {value}"] if prefix else [])
            if item:
                found.append(item)
                accepted = True
        if accepted:
            spans.add(m.start(), m.end())
    for m in STRUCTURED_RE.finditer(line):
        token = m.group(1).rstrip(".-")
        if not spans.free(m.start(1), m.start(1) + len(token)) or not _number_ok(token, keyword_anchored=False):
            continue
        head = re.match(r"([A-Za-z]+)[/\-](.+)$", token)
        alt = [head.group(2)] if head and head.group(1).upper() in NUMBER_PREFIX_KEYWORDS else []
        item = _invoice(token, "structured", alt)
        if item:
            found.append(item)
            spans.add(m.start(1), m.start(1) + len(token))
    return found


def _tax_period(text: str) -> dict[str, Any] | None:
    m = TAX_PERIOD_RE.match(text.strip().upper())
    if not m:
        return None
    year = 2000 + int(m["y"])
    if m["kind"] == "M" and m["n"] and 1 <= int(m["n"]) <= 12:
        return {"raw": text, "month": f"{year:04d}-{int(m['n']):02d}", "kind": "tax"}
    if m["kind"] == "K" and m["n"] and 1 <= int(m["n"]) <= 4:
        return {"raw": text, "quarter": f"{year:04d}-Q{int(m['n'])}", "kind": "tax"}
    return {"raw": text, "year": year, "kind": "tax"}


def extract_title(lines: list[str], *, reference_date: date | None = None,
                  counterparty_name: str | None = None) -> dict[str, Any]:
    """Identifiers read by code from the title lines (and the counterparty name for operators)."""
    out: dict[str, Any] = {"extractor_version": EXTRACTOR_VERSION}
    invoices: list[dict[str, Any]] = []
    ksef: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    dates: list[str] = []
    contract_refs: list[str] = []
    case_refs: list[str] = []
    other_refs: list[str] = []
    phones: list[str] = []

    def add_period(item: dict[str, Any]) -> None:
        if item not in periods:
            periods.append(item)

    for line in lines:
        spans = _Spans()
        mpp = MPP_RE.search(line)
        if mpp and "mpp" not in out:
            vat_text = mpp["vat"].strip()
            try:
                vat_value = parse_amount(vat_text)
                vat_minor = to_minor(vat_value, "PLN")
            except ValueError:
                vat_value, vat_minor = None, None
            idc = mpp["idc"].strip()
            try:
                idc_nip = normalize_nip(idc)
            except ValueError:
                idc_nip = None
            inv = mpp["inv"].strip()
            out["mpp"] = {
                "vat": str(vat_value) if vat_value is not None else vat_text,
                "vat_minor": vat_minor,
                "idc": idc,
                "idc_nip": idc_nip,
                "inv": inv,
                "inv_normalized": _normalized_or_none(inv),
                "txt": (mpp["txt"] or "").strip() or None,
            }
            item = _invoice(inv, "mpp", [re.sub(r"^(?:FV|FS|FA)\s*", "", inv, flags=re.I)])
            if item:
                invoices.append(item)
            # the invoice part is consumed; the free text after /TXT/ is still read
            spans.add(mpp.start(), mpp.start("txt") if mpp["txt"] is not None else mpp.end())
        tax = TAX_RE.search(line)
        if tax and "tax" not in out:
            period = _tax_period(tax["period"])
            out["tax"] = {"id_type": tax["id_type"].upper(), "id": tax["id"].strip(), "period": tax["period"].strip(),
                          "form": tax["form"].strip(), "txt": (tax["txt"] or "").strip() or None}
            if period:
                add_period(period)
            spans.add(tax.start(), tax.start("txt") if tax["txt"] is not None else tax.end())
        for m in KSEF_RE.finditer(line):
            ksef.append({"number": m.group(1), "checksum_ok": ksef_checksum_ok(m.group(1))})
            spans.add(m.start(1), m.end(1))
        card = CARD_RE.search(line)
        if card:
            out["card"] = {"masked": f"{card.group(1)}********{card.group(2)}"}
            spans.add(card.start(), card.end())
        original = ORIGINAL_AMOUNT_RE.search(line)
        if original:
            try:
                out["original_amount"] = {
                    "amount": str(parse_amount(original["amount"].strip())),
                    "currency": original["ccy"].upper(),
                    "rate": str(parse_amount(original["rate"])),
                }
            except ValueError:
                pass
            spans.add(original.start(), original.end())
        for d in detect(line):
            if d.kind == "phone":
                if d.normalized not in phones:
                    phones.append(d.normalized)
                spans.add(d.start, d.end)
            elif d.kind in ("iban", "email", "pesel"):
                spans.add(d.start, d.end)
        for pattern, target in ((CONTRACT_RE, contract_refs), (CASE_RE, case_refs), (DECISION_RE, other_refs)):
            for m in pattern.finditer(line):
                if spans.free(m.start("ref"), m.end("ref")):
                    ref = m.group("ref").rstrip(".,")
                    if pattern is CASE_RE:
                        ref = re.sub(r"\s+", " ", ref)
                    if ref not in target:
                        target.append(ref)
                    spans.add(m.start("ref"), m.end("ref"))
        installment = INSTALLMENT_RE.search(line)
        if installment and spans.free(installment.start(), installment.end()):
            out["installment"] = {"no": int(installment["no"]), "of": int(installment["of"])}
            spans.add(installment.start(), installment.end())
        for pattern in (FULL_DATE_RE, ISO_DATE_RE):
            for m in pattern.finditer(line):
                if not spans.free(m.start(), m.end()):
                    continue
                day = _safe_date(int(m["y"]), int(m["m"]), int(m["d"]))
                if day:
                    dates.append(day.isoformat())
                    spans.add(m.start(), m.end())
        invoices.extend(_invoice_numbers(line, spans))
        for m in SHORT_DATE_RE.finditer(line):
            if not spans.free(m.start(), m.end()):
                continue
            month = int(m["m"])
            if not 1 <= month <= 12:
                continue
            year = _nearest_year(month, reference_date)
            first = _safe_date(year, month, int(m["d"]))
            last = _safe_date(year, month, int(m["d2"])) if m["d2"] else None
            if first:
                dates.append(first.isoformat())
                if last and last > first:
                    dates.append(last.isoformat())
                spans.add(m.start(), m.end())
        for pattern in (NUM_PERIOD_RE, ISO_PERIOD_RE):
            for m in pattern.finditer(line):
                if spans.free(m.start(), m.end()):
                    add_period({"raw": m.group(0), "month": f"{int(m['y']):04d}-{int(m['m']):02d}", "kind": "numeric"})
                    spans.add(m.start(), m.end())
        for m in MONTH_RE.finditer(line):
            word = fold(m["word"])
            if word not in MONTHS or not spans.free(m.start(), m.end()):
                continue
            if m["word"][0].isupper() and word in ("maja", "maj") and not m["y"]:
                continue  # "Maja" is also a first name
            month = MONTHS[word]
            year = int(m["y"]) if m["y"] else _nearest_year(month, reference_date)
            add_period({"raw": m.group(0).strip(), "month": f"{year:04d}-{month:02d}", "kind": "month_name"})
            spans.add(m.start(), m.end())

    seen: set[str] = set()
    unique_invoices = []
    for item in invoices:
        if item["normalized"] not in seen:
            seen.add(item["normalized"])
            unique_invoices.append(item)

    text = " | ".join(lines)
    operator = None
    for name, (detector, ref_re) in OPERATORS.items():
        if detector.search(counterparty_name or "") or detector.search(text):
            ref_match = ref_re.search(text) if ref_re else None
            operator = {"name": name, "ref": ref_match.group(1) if ref_match else None,
                        "via": "counterparty" if detector.search(counterparty_name or "") else "title"}
            break
    if operator and operator["name"] in PAYER_HINT_OPERATORS:
        hint = payer_hint(lines)
        if hint:
            out["payer_hint"] = hint

    for key, value in (("invoice_numbers", unique_invoices), ("ksef_numbers", ksef), ("periods", periods),
                       ("dates", sorted(dict.fromkeys(dates))), ("contract_refs", contract_refs),
                       ("case_refs", case_refs), ("other_refs", other_refs), ("phones", phones)):
        if value:
            out[key] = value
    if operator:
        out["operator"] = operator
    return out


# --------------------------------------------------------------------------
# Operation type
# --------------------------------------------------------------------------

def _iban_or_none(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_iban(value)
    except ValueError:
        return None


def is_zus_account(iban: str | None) -> bool:
    """ZUS accounts are in NBP's "ZUS" range: bank code 6000 0002."""
    return bool(iban) and iban.startswith("PL") and iban[4:12] == "60000002"


def is_tax_microaccount(iban: str | None) -> bool:
    """Individual tax micro-account: NBP 1010 0071, then 222."""
    return bool(iban) and iban.startswith("PL") and iban[4:12] == "10100071" and iban[12:15] == "222"


# A tax transfer in the layout of the bank (mBank, VAT account): "N5342650778 ... 25M10 ... PIT-8AR"
_TAX_TRANSFER_RE = re.compile(r"\b[NPR]\d{10}\b.*\b\d{2}[MKRPDJ]\d{2}\b", re.DOTALL)


def classify_op_type(entry: BankEntry, extracted: dict[str, Any], *, own_ibans: set[str],
                     account_kind: str = "current") -> tuple[str, str]:
    """(op_type, reason): transfer, instant, card, blik, fee, tax, zus, own, vat, direct_debit,
    foreign_transfer, interest or other."""
    code = (entry.bank_tx_code or "").upper()
    parts = code.split("/")
    family = parts[-2] if len(parts) >= 2 else ""
    sub = parts[-1] if parts else ""
    swift = parts[-1] if parts and parts[0] == "SWIFT" else ""
    desc = fold(entry.bank_tx_description or "")
    title = fold(entry.title)
    party = fold(entry.counterparty_name or "")
    iban = _iban_or_none(entry.counterparty_iban)

    if account_kind == "vat":
        # mBank gives our current account as the counterparty of every operation on the VAT account (the bank
        # moves the VAT part through it), so on a VAT account the content decides before the counterparty IBAN.
        if extracted.get("mpp") or "mpp" in desc:
            return "vat", "split payment leg on the VAT account"
        if _TAX_TRANSFER_RE.search(entry.title):
            return "tax", "tax transfer from the VAT account (taxpayer id and period in the title)"
        if re.search(r"\bzus\b", title):
            return "zus", "ZUS payment from the VAT account"
    if iban and iban in own_ibans:
        return "own", "counterparty IBAN is an own account"
    if "wlasny" in desc or "przeksiegowanie" in desc or "przeksiegowanie z rachunku vat" in title:
        return "own", f"bank description: {entry.bank_tx_description}"
    if extracted.get("tax") or sub == "TAXS" or "urzad skarbow" in party or "urzedu skarbowego" in desc \
            or is_tax_microaccount(iban):
        return "tax", "tax payment (code TAXS, /TI/ block or tax office)"
    if is_zus_account(iban) or "zaklad ubezpieczen spolecznych" in party or re.search(r"\bzus\b", desc):
        return "zus", "ZUS account or name"
    if account_kind == "vat" and (extracted.get("mpp") or "mpp" in desc):
        return "vat", "split payment leg on the VAT account"
    if family == "CCRD" or "karta" in desc or "kartowa" in desc:
        return "card", f"card operation ({code or desc})"
    if "blik" in desc:
        return "blik", f"bank description: {entry.bank_tx_description}"
    if sub == "INTR" or swift == "NINT" or "odsetk" in desc:
        return "interest", "interest"
    if sub in ("CHRG", "COMM") or family == "MDOP" or swift in ("NCHG", "NCOM") or "prowizj" in desc \
            or desc.startswith("oplata"):
        return "fee", "bank fee or commission"
    if family == "IDDT" or swift == "NDDT" or "polecenie zaplaty" in desc:
        return "direct_debit", "direct debit"
    if sub in ("SDVA", "RRTP", "INST") or "express elixir" in desc or "natychmiast" in desc or "sorbnet" in desc:
        return "instant", "instant / same-day transfer"
    if sub == "XBCT" or "sepa" in desc or "zagraniczn" in desc or "swift" in desc or \
            (entry.original_currency and family in ("RCDT", "ICDT")):
        return "foreign_transfer", "cross-border transfer"
    if family in ("RCDT", "ICDT") or swift == "NTRF" or "przelew" in desc:
        return "transfer", "credit transfer"
    return "other", "no rule matched"


def fx_note(entry: BankEntry) -> dict[str, Any] | None:
    """The bank's FX data of a foreign-currency operation, JSON-ready."""
    if entry.original_currency is None:
        return None
    return {
        "original_currency": entry.original_currency,
        "original_amount_minor": entry.original_amount_minor,
        "bank_rate": str(entry.fx_rate) if entry.fx_rate is not None else None,
        "rate_type": entry.fx_rate_type,
    }

