"""EnableBanking JSON (``/accounts/{id}/transactions`` and ``/balances``) -> intermediate model."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from kasa.core.bank.model import BankBalance, BankEntry
from kasa.core.normalize import parse_amount, to_minor

SOURCE = "enablebanking"
_STATUS = {"BOOK": "booked", "PDNG": "pending", "booked": "booked", "pending": "pending"}


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _amount(node: dict[str, Any] | None) -> tuple[Decimal, str] | None:
    if not node or node.get("amount") in (None, ""):
        return None
    return parse_amount(str(node["amount"])), node.get("currency") or "PLN"


def _iban(node: dict[str, Any] | None) -> str | None:
    if not node:
        return None
    return node.get("iban") or (node.get("other") or {}).get("identification") or node.get("bban")


def parse_transactions(payload: bytes, account_iban: str, account_currency: str = "PLN",
                       locator_prefix: str = "") -> list[BankEntry]:
    """Every transaction of one response, in the order given by the bank."""
    data = json.loads(payload)
    entries = []
    for index, tx in enumerate(data.get("transactions", [])):
        entries.append(parse_transaction(tx, account_iban, account_currency, f"{locator_prefix}transactions[{index}]"))
    return entries


def parse_transaction(tx: dict[str, Any], account_iban: str, account_currency: str, locator: str) -> BankEntry:
    amount, currency = _amount(tx.get("transaction_amount")) or (Decimal(0), account_currency)
    indicator = (tx.get("credit_debit_indicator") or "").upper()
    if indicator == "DBIT":
        signed = -abs(amount)
    elif indicator == "CRDT":
        signed = abs(amount)
    else:
        signed = amount
    outgoing = signed < 0
    party = (tx.get("creditor") if outgoing else tx.get("debtor")) or {}
    party_account = tx.get("creditor_account") if outgoing else tx.get("debtor_account")
    party_agent = (tx.get("creditor_agent") if outgoing else tx.get("debtor_agent")) or {}

    original_minor = original_currency = fx_rate = fx_type = None
    exchange = tx.get("exchange_rate") or tx.get("currency_exchange") or None
    if isinstance(exchange, list):          # Berlin Group style: a list of exchanges
        exchange = exchange[0] if exchange else None
    if exchange:
        instructed = _amount(exchange.get("instructed_amount"))
        if instructed and instructed[1] != currency:
            original_currency = instructed[1]
            original_minor = to_minor(abs(instructed[0]), original_currency)
            if outgoing:
                original_minor = -original_minor
        if exchange.get("exchange_rate"):
            fx_rate = parse_amount(str(exchange["exchange_rate"]))
            if exchange.get("unit_currency") == currency and original_currency:
                fx_rate = Decimal(1) / fx_rate  # quoted the other way round
        fx_type = exchange.get("rate_type")
        if original_currency is None:
            fx_rate = None

    balance = _amount(tx.get("balance_after_transaction"))
    code = tx.get("bank_transaction_code") or {}
    code_parts = [p for p in (code.get("code"), code.get("sub_code")) if p]
    remittance = tx.get("remittance_information") or []
    if isinstance(remittance, str):
        remittance = [remittance]
    extra = {k: tx[k] for k in ("note", "debtor_account_additional_identification",
                                "creditor_account_additional_identification", "ultimate_debtor",
                                "ultimate_creditor", "purpose_code") if tx.get(k)}
    return BankEntry(
        source=SOURCE,
        account_iban=account_iban,
        account_currency=account_currency,
        locator=locator,
        entry_reference=tx.get("entry_reference") or None,
        status=_STATUS.get(str(tx.get("status") or "BOOK"), "booked"),
        booking_date=_date(tx.get("booking_date")),
        value_date=_date(tx.get("value_date")),
        transaction_date=_date(tx.get("transaction_date")),
        amount_minor=to_minor(signed, currency),
        currency=currency,
        original_amount_minor=original_minor,
        original_currency=original_currency,
        fx_rate=fx_rate,
        fx_rate_type=fx_type,
        balance_after_minor=to_minor(balance[0], balance[1]) if balance else None,
        counterparty_name=party.get("name") or None,
        counterparty_iban=_iban(party_account),
        counterparty_bic=party_agent.get("bic_fi") or None,
        title_lines=list(remittance),
        bank_tx_code="/".join(code_parts) or None,
        bank_tx_description=code.get("description") or None,
        merchant_category_code=tx.get("merchant_category_code") or None,
        reference_number=tx.get("reference_number") or None,
        end_to_end_id=tx.get("end_to_end_id") or None,
        extra=extra,
    )


def parse_balances(payload: bytes, account_iban: str) -> list[BankBalance]:
    data = json.loads(payload)
    balances = []
    for index, node in enumerate(data.get("balances", [])):
        amount = _amount(node.get("balance_amount"))
        reference = _date(node.get("reference_date"))
        if amount is None or reference is None:
            continue
        value = amount[0]
        if (node.get("credit_debit_indicator") or "").upper() == "DBIT":
            value = -abs(value)
        changed = node.get("last_change_date_time")
        balances.append(BankBalance(
            source=SOURCE,
            account_iban=account_iban,
            balance_type=str(node.get("balance_type") or "XPCD").upper(),
            reference_date=reference,
            amount_minor=to_minor(value, amount[1]),
            currency=amount[1],
            name=node.get("name"),
            last_change_at=datetime.fromisoformat(changed) if changed else None,
            last_committed_transaction=node.get("last_committed_transaction"),
            locator=f"balances[{index}]",
        ))
    return balances
