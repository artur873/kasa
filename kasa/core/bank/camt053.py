"""ISO 20022 camt.053 (bank-to-customer statement) -> intermediate model.

Written for camt.053.001.02 (the version Polish banks export) and tolerant of
later versions (.04, .08: ``Sts/Cd``, ``FinInstnId/BICFI``). Namespaces are
ignored. ``NtryRef`` is the same identifier as EnableBanking's
``entry_reference``, so the same transaction from both channels is one fact.
camt.053 has no balance after each entry; the ingestion computes it from the
opening balance (OPBD) in booking order.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal

from kasa.core.bank.model import BankBalance, BankEntry, ParsedStatement
from kasa.core.normalize import parse_amount, to_minor

SOURCE = "camt053"
_STATUS = {"BOOK": "booked", "PDNG": "pending", "INFO": "pending"}


class CamtError(ValueError):
    """The file is not a camt.053 statement this parser understands."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _strip_namespaces(root: ET.Element) -> ET.Element:
    for element in root.iter():
        element.tag = _local(element.tag)
    return root


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _date_of(node: ET.Element | None) -> date | None:
    if node is None:
        return None
    value = _text(node, "Dt") or _text(node, "DtTm")
    if not value:
        return None
    return datetime.fromisoformat(value).date() if "T" in value else date.fromisoformat(value[:10])


def _amount(node: ET.Element | None) -> tuple[Decimal, str] | None:
    if node is None or node.text is None:
        return None
    return parse_amount(node.text.strip()), node.get("Ccy") or "PLN"


def _bic(node: ET.Element | None) -> str | None:
    return _text(node, "FinInstnId/BIC") or _text(node, "FinInstnId/BICFI")


def _account_id(node: ET.Element | None) -> str | None:
    return _text(node, "Id/IBAN") or _text(node, "Id/Othr/Id")


def parse(payload: bytes) -> list[ParsedStatement]:
    """Every ``Stmt`` of the file (usually one per account)."""
    try:
        root = _strip_namespaces(ET.fromstring(payload))
    except ET.ParseError as exc:
        raise CamtError(f"to nie jest poprawny XML: {exc}") from exc
    report = root.find("BkToCstmrStmt")
    if report is None:
        raise CamtError("brak elementu BkToCstmrStmt (to nie jest camt.053)")
    statements = [_statement(stmt, index) for index, stmt in enumerate(report.findall("Stmt"))]
    if not statements:
        raise CamtError("plik camt.053 nie zawiera żadnego wyciągu (Stmt)")
    return statements


def _statement(stmt: ET.Element, index: int) -> ParsedStatement:
    acct = stmt.find("Acct")
    iban = _account_id(acct)
    if not iban:
        raise CamtError("wyciąg bez numeru rachunku (Acct/Id)")
    currency = _text(acct, "Ccy") or "PLN"
    balances = []
    for b_index, bal in enumerate(stmt.findall("Bal")):
        code = _text(bal, "Tp/CdOrPrtry/Cd") or _text(bal, "Tp/CdOrPrtry/Prtry")
        amount = _amount(bal.find("Amt"))
        day = _date_of(bal.find("Dt"))
        if not code or amount is None or day is None:
            continue
        value = amount[0] if _text(bal, "CdtDbtInd") != "DBIT" else -abs(amount[0])
        balances.append(BankBalance(
            source=SOURCE, account_iban=iban, balance_type=code.upper(), reference_date=day,
            amount_minor=to_minor(value, amount[1]), currency=amount[1],
            locator=f"Stmt[{index}]/Bal[{b_index}]",
        ))
    entries = [_entry(ntry, iban, currency, f"Stmt[{index}]/Ntry[{n_index}]")
               for n_index, ntry in enumerate(stmt.findall("Ntry"))]
    period = stmt.find("FrToDt")
    period_from = _text(period, "FrDtTm")
    period_to = _text(period, "ToDtTm")
    return ParsedStatement(
        source=SOURCE,
        account_iban=iban,
        currency=currency,
        owner_name=_text(acct, "Ownr/Nm"),
        bic=_bic(acct.find("Svcr") if acct is not None else None),
        statement_id=_text(stmt, "Id"),
        period_from=datetime.fromisoformat(period_from).date() if period_from else None,
        period_to=datetime.fromisoformat(period_to).date() if period_to else None,
        balances=balances,
        entries=entries,
    )


def _entry(ntry: ET.Element, iban: str, account_currency: str, locator: str) -> BankEntry:
    amount = _amount(ntry.find("Amt"))
    if amount is None:
        raise CamtError(f"{locator}: brak kwoty (Amt)")
    outgoing = _text(ntry, "CdtDbtInd") == "DBIT"
    signed = -abs(amount[0]) if outgoing else abs(amount[0])
    status_text = _text(ntry, "Sts") or _text(ntry, "Sts/Cd") or "BOOK"
    tx = ntry.find("NtryDtls/TxDtls")
    parties = tx.find("RltdPties") if tx is not None else None
    agents = tx.find("RltdAgts") if tx is not None else None
    if outgoing:
        party_name = _text(parties, "Cdtr/Nm") or _text(parties, "Cdtr/Pty/Nm")
        party_account = _account_id(parties.find("CdtrAcct") if parties is not None else None)
        party_bic = _bic(agents.find("CdtrAgt") if agents is not None else None)
    else:
        party_name = _text(parties, "Dbtr/Nm") or _text(parties, "Dbtr/Pty/Nm")
        party_account = _account_id(parties.find("DbtrAcct") if parties is not None else None)
        party_bic = _bic(agents.find("DbtrAgt") if agents is not None else None)

    lines: list[str] = []
    if tx is not None:
        lines += [u.text.strip() for u in tx.findall("RmtInf/Ustrd") if u.text and u.text.strip()]
        lines += [r.text.strip() for r in tx.findall("RmtInf/Strd/CdtrRefInf/Ref") if r.text and r.text.strip()]
    if not lines and _text(ntry, "AddtlNtryInf"):
        lines.append(_text(ntry, "AddtlNtryInf"))

    original_minor = original_currency = fx_rate = None
    instructed = tx.find("AmtDtls/InstdAmt") if tx is not None else None
    if instructed is not None:
        inst = _amount(instructed.find("Amt"))
        if inst and inst[1] != amount[1]:
            original_currency = inst[1]
            original_minor = to_minor(abs(inst[0]), original_currency) * (-1 if outgoing else 1)
            rate_text = _text(instructed, "CcyXchg/XchgRate")
            if rate_text:
                rate = parse_amount(rate_text)
                source_ccy = _text(instructed, "CcyXchg/SrcCcy")
                fx_rate = rate if source_ccy in (None, original_currency) else Decimal(1) / rate

    domain = _text(ntry, "BkTxCd/Domn/Cd")
    family = _text(ntry, "BkTxCd/Domn/Fmly/Cd")
    sub_family = _text(ntry, "BkTxCd/Domn/Fmly/SubFmlyCd")
    end_to_end = _text(tx, "Refs/EndToEndId")
    return BankEntry(
        source=SOURCE,
        account_iban=iban,
        account_currency=account_currency,
        locator=locator,
        entry_reference=_text(ntry, "NtryRef") or _text(ntry, "AcctSvcrRef"),
        status=_STATUS.get(status_text.upper(), "booked"),
        booking_date=_date_of(ntry.find("BookgDt")),
        value_date=_date_of(ntry.find("ValDt")),
        amount_minor=to_minor(signed, amount[1]),
        currency=amount[1],
        original_amount_minor=original_minor,
        original_currency=original_currency,
        fx_rate=fx_rate,
        counterparty_name=party_name,
        counterparty_iban=party_account,
        counterparty_bic=party_bic,
        title_lines=lines,
        bank_tx_code="/".join(p for p in (family, sub_family) if p) or None,
        bank_tx_description=_text(ntry, "BkTxCd/Prtry/Cd"),
        reference_number=_text(ntry, "AcctSvcrRef"),
        end_to_end_id=end_to_end if end_to_end and end_to_end != "NOTPROVIDED" else None,
        extra={k: v for k, v in (("domain", domain), ("tx_details_count",
                                  len(ntry.findall("NtryDtls/TxDtls")))) if v},
    )
