"""SWIFT MT940 statement (basic subset) -> intermediate model.

Fields read: ``:20:`` (statement reference), ``:25:`` (account), ``:28C:`` (statement
number), ``:60F:``/``:60M:`` (opening balance), ``:61:`` (statement line), ``:86:``
(information to the account owner), ``:62F:``/``:62M:`` (closing balance) and
``:64:`` (closing available balance). ``:60F:`` dated before the first booking is the
previous statement's closing balance (ISO ``PRCD``), otherwise the opening (``OPBD``).
``:86:`` in the structured form used by Polish banks (``NNN~00...~20...~38...``,
separator ``~``, ``^`` or ``<``) gives the title (fixed-width pieces ~20-~26, ~60-~63
glued back), counterparty name (~27-~29, ~32-~33) and IBAN (~38);
otherwise the whole text is the title. The bank reference after ``//`` in ``:61:``
becomes ``entry_reference``. Files are decoded as UTF-8, falling back to Windows-1250.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from kasa.core.bank.model import BankBalance, BankEntry, ParsedStatement
from kasa.core.normalize import parse_amount, to_minor

SOURCE = "mt940"
_TAG_RE = re.compile(r"^:(\d{2}[A-Z]?):(.*)$")
_LINE61_RE = re.compile(
    r"^(?P<value>\d{6})(?P<entry>\d{4})?(?P<mark>RC|RD|C|D)(?P<funds>[A-Z])?(?P<amount>\d+(?:,\d{0,2})?)"
    r"(?P<type>[NFS][A-Z0-9]{3})(?P<ref>[^/\n]*?)(?://(?P<bankref>[^\n]*))?(?:\n(?P<extra>.*))?$",
    re.DOTALL,
)
_BALANCE_RE = re.compile(r"^(?P<mark>[CD])(?P<date>\d{6})(?P<ccy>[A-Z]{3})(?P<amount>\d+(?:,\d{0,2})?)$")


class Mt940Error(ValueError):
    """The file is not an MT940 statement this parser understands."""


def _yymmdd(text: str) -> date:
    return date(2000 + int(text[:2]), int(text[2:4]), int(text[4:6]))


def _decode(payload: bytes) -> str:
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return payload.decode("cp1250")


def _fields(text: str) -> list[tuple[str, str]]:
    body = text
    block4 = re.search(r"\{4:\s*(.*?)\n-\}", text, re.DOTALL)
    if block4:
        body = block4.group(1)
    fields: list[list[str]] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r")
        match = _TAG_RE.match(line)
        if match:
            fields.append([match.group(1), match.group(2)])
        elif line.strip() == "-":
            fields.append(["-", ""])
        elif fields and line.strip():
            fields[-1][1] += "\n" + line
    return [(tag, value) for tag, value in fields]


def _balance(value: str, iban: str, kind: str, locator: str) -> BankBalance:
    match = _BALANCE_RE.match(value.strip())
    if not match:
        raise Mt940Error(f"{locator}: niepoprawne saldo {value!r}")
    amount = parse_amount(match["amount"])
    if match["mark"] == "D":
        amount = -amount
    return BankBalance(source=SOURCE, account_iban=iban, balance_type=kind, reference_date=_yymmdd(match["date"]),
                       amount_minor=to_minor(amount, match["ccy"]), currency=match["ccy"], locator=locator)


def parse_86(text: str) -> dict[str, object]:
    """Split ``:86:`` into code, description, title, counterparty name and IBAN."""
    flat = text.replace("\n", "")
    # the transaction code is three digits ("020~00...") or, in mBank, a letter and two digits ("D94~00...")
    if len(flat) > 4 and re.fullmatch(r"[A-Z0-9]\d{2}", flat[:3]) and flat[3] in "~^<":
        sep = flat[3]
        parts: dict[str, str] = {}
        for chunk in flat[4:].split(sep):
            if len(chunk) >= 2 and chunk[:2].isdigit():
                parts[chunk[:2]] = parts.get(chunk[:2], "") + chunk[2:]
        # ~20-~26 and ~60-~63 are fixed-width pieces of one title: glue them back without a separator
        title = ["".join(parts.get(k, "") for k in ("20", "21", "22", "23", "24", "25", "26", "60", "61", "62",
                                                    "63")).strip()]
        name = " ".join(parts[k] for k in ("32", "33") if parts.get(k)) or \
            " ".join(parts[k] for k in ("27", "28", "29") if parts.get(k))
        iban = parts.get("38") or None
        return {"code": flat[:3], "description": parts.get("00") or None, "title": title,
                "name": name.strip() or None, "iban": iban.replace(" ", "") if iban else None}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {"code": None, "description": None, "title": lines, "name": None, "iban": None}


def parse(payload: bytes) -> list[ParsedStatement]:
    text = _decode(payload)
    fields = _fields(text)
    if not any(tag == "25" for tag, _ in fields):
        raise Mt940Error("brak pola :25: (to nie jest wyciąg MT940)")
    statements: list[ParsedStatement] = []
    current: dict | None = None
    pending_entry: dict | None = None

    def close_entry() -> None:
        nonlocal pending_entry
        if pending_entry is not None and current is not None:
            current["entries"].append(pending_entry)
        pending_entry = None

    def close_statement() -> None:
        nonlocal current
        close_entry()
        if current is not None:
            statements.append(_build(current, len(statements)))
        current = None

    for tag, value in fields:
        if tag == "20":
            close_statement()
            current = {"ref": value.strip(), "iban": None, "number": None, "balances": [], "entries": []}
        elif tag == "-":
            close_statement()
        elif current is None:
            continue
        elif tag == "25":
            # "PL40114020040000350277812345", "/PL40...", "BREXPLPW/PL40..." or a bare 26-digit NRB
            account = re.sub(r"\s", "", value.strip().lstrip("/").split("/")[-1]).upper()
            current["iban"] = "PL" + account if account.isdigit() and len(account) == 26 else account
        elif tag == "28C":
            current["number"] = value.strip()
        elif tag in ("60F", "60M", "62F", "62M", "64"):
            close_entry()
            kind = {"60F": "OPBD", "60M": "OPBD", "62F": "CLBD", "62M": "CLBD", "64": "CLAV"}[tag]
            current["balances"].append((kind, value))
        elif tag == "61":
            close_entry()
            pending_entry = {"line": value, "info": ""}
        elif tag == "86" and pending_entry is not None:
            # mBank repeats :86: - first the bare code (":86:D94"), then the structured field; keep the fullest
            if len(value.strip()) >= len(pending_entry["info"].strip()):
                pending_entry["info"] = value
    close_statement()
    if not statements:
        raise Mt940Error("plik nie zawiera wyciągu MT940 (brak :20:)")
    return statements


def _build(data: dict, index: int) -> ParsedStatement:
    iban = data["iban"]
    if not iban:
        raise Mt940Error(f"wyciąg {data['ref']}: brak numeru rachunku (:25:)")
    balances = [_balance(value, iban, kind, f"stmt[{index}]/{kind}[{i}]")
                for i, (kind, value) in enumerate(data["balances"])]
    currency = balances[0].currency if balances else "PLN"
    entries = [_entry(e, iban, currency, f"stmt[{index}]/61[{i}]") for i, e in enumerate(data["entries"])]
    first_booking = min((e.booking_date for e in entries), default=None)
    for i, balance in enumerate(balances):
        # :60F: dated before the first booking is the previous statement's closing balance
        # (end of that day, ISO PRCD); dated on the first booking day it is that day's opening.
        if balance.balance_type == "OPBD" and first_booking and balance.reference_date < first_booking:
            balances[i] = balance.model_copy(update={"balance_type": "PRCD"})
    notes = _fix_balance_marks(balances, entries, data["ref"])
    opening = [b.reference_date for b in balances if b.balance_type == "OPBD"]
    opening += [b.reference_date + timedelta(days=1) for b in balances if b.balance_type == "PRCD"]
    closing = [b.reference_date for b in balances if b.balance_type == "CLBD"]
    return ParsedStatement(source=SOURCE, account_iban=iban, currency=currency, statement_id=data["ref"],
                           period_from=min(opening) if opening else first_booking,
                           period_to=max(closing) if closing else None, balances=balances, entries=entries,
                           notes=notes)


def _fix_balance_marks(balances: list[BankBalance], entries: list[BankEntry], ref: str) -> list[str]:
    """mBank's MT940 of a VAT account (11.2026) marks a positive balance with ``D``: ``:60F:D...30957,00`` +
    the operations = ``:62F:D...22803,51`` only when both are positive. When the statement does not close with the
    marks as given but closes to the grosz with every balance mark inverted, the balances are inverted (in place)
    and a note says so; otherwise nothing changes (the truth check reports the gap)."""
    openings = [b for b in balances if b.balance_type in ("OPBD", "PRCD")]
    closings = [b for b in balances if b.balance_type == "CLBD"]
    if len(openings) != 1 or len(closings) != 1:
        return []
    booked = sum(e.amount_minor for e in entries if e.status == "booked")
    opening, closing = openings[0].amount_minor, closings[0].amount_minor
    if opening + booked == closing or -opening + booked != -closing:
        return []
    for i, balance in enumerate(balances):
        balances[i] = balance.model_copy(update={"amount_minor": -balance.amount_minor,
                                                 "locator": f"{balance.locator}(mark inverted)"})
    return [f"wyciąg {ref}: znaki D/C sald (:60F:/:62F:) odwrócone - z odwróconymi wyciąg zamyka się co do grosza"]


def _entry(data: dict, iban: str, currency: str, locator: str) -> BankEntry:
    match = _LINE61_RE.match(data["line"].strip())
    if not match:
        raise Mt940Error(f"{locator}: niepoprawne pole :61: {data['line']!r}")
    value_date = _yymmdd(match["value"])
    booking_date = value_date
    if match["entry"]:
        month, day = int(match["entry"][:2]), int(match["entry"][2:])
        year = value_date.year + (1 if month < value_date.month - 6 else -1 if month > value_date.month + 6 else 0)
        booking_date = date(year, month, day)
    amount = parse_amount(match["amount"])
    debit = match["mark"] in ("D", "RC")      # RC = reversal of a credit
    info = parse_86(data["info"])
    bank_ref = (match["bankref"] or "").strip() or None
    own_ref = (match["ref"] or "").strip()
    return BankEntry(
        source=SOURCE,
        account_iban=iban,
        account_currency=currency,
        locator=locator,
        entry_reference=bank_ref if bank_ref and bank_ref.upper() != "NONREF" else None,
        status="booked",
        booking_date=booking_date,
        value_date=value_date,
        amount_minor=to_minor(-amount if debit else amount, currency),
        currency=currency,
        counterparty_name=info["name"],
        counterparty_iban=info["iban"],
        title_lines=list(info["title"]),
        bank_tx_code=f"SWIFT/{match['type']}",
        bank_tx_description=info["description"],
        reference_number=own_ref if own_ref and own_ref.upper() != "NONREF" else None,
        extra={k: v for k, v in (("gvc", info["code"]), ("supplementary", (match["extra"] or "").strip())) if v},
    )
