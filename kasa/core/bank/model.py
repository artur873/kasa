"""Intermediate model shared by every bank format (EnableBanking, camt.053, MT940).

A parser turns one raw payload into :class:`BankEntry` and :class:`BankBalance`
objects; the ingestion turns those into L1 rows. Amounts are signed minor units
of the account currency (inflow > 0, outflow < 0).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Status = Literal["booked", "pending"]


class BankEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str                                   # 'enablebanking' | 'camt053' | 'mt940'
    account_iban: str
    account_currency: str = "PLN"
    locator: str                                  # position inside the raw payload
    entry_reference: str | None = None
    status: Status
    booking_date: date | None = None
    value_date: date | None = None
    transaction_date: date | None = None
    amount_minor: int
    currency: str
    original_amount_minor: int | None = None      # FX: amount in the original currency
    original_currency: str | None = None
    fx_rate: Decimal | None = None                # account currency per 1 unit of original currency
    fx_rate_type: str | None = None
    balance_after_minor: int | None = None
    balance_after_computed: bool = False
    counterparty_name: str | None = None
    counterparty_iban: str | None = None          # as given by the bank
    counterparty_bic: str | None = None
    title_lines: list[str] = Field(default_factory=list)
    bank_tx_code: str | None = None               # e.g. 'PMNT/ICDT/DMCT' or 'ICDT/DMCT'
    bank_tx_description: str | None = None
    merchant_category_code: str | None = None
    reference_number: str | None = None
    end_to_end_id: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title_lines")
    @classmethod
    def _clean_lines(cls, value: list[str]) -> list[str]:
        return [line.strip() for line in value if line and line.strip()]

    @property
    def title(self) -> str:
        return " | ".join(self.title_lines)

    @property
    def effective_date(self) -> date | None:
        return self.booking_date or self.transaction_date or self.value_date

    @property
    def is_credit(self) -> bool:
        return self.amount_minor > 0


class BankBalance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    account_iban: str
    balance_type: str                             # ISO 20022: OPBD, CLBD, ITAV, CLAV, ...
    reference_date: date
    amount_minor: int
    currency: str
    name: str | None = None
    last_change_at: datetime | None = None
    last_committed_transaction: str | None = None
    locator: str | None = None


class ParsedStatement(BaseModel):
    """A bank statement file (camt.053, MT940): one account, balances and entries."""

    model_config = ConfigDict(extra="forbid")

    source: str
    account_iban: str
    currency: str = "PLN"
    owner_name: str | None = None
    bic: str | None = None
    statement_id: str | None = None
    period_from: date | None = None
    period_to: date | None = None
    notes: list[str] = Field(default_factory=list)     # what the parser corrected, for the import result
    balances: list[BankBalance] = Field(default_factory=list)
    entries: list[BankEntry] = Field(default_factory=list)
