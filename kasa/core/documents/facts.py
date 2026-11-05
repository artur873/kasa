"""Facts read from one document by one channel (KSeF, PDF, JPK, CSV/XLSX).

``InvoiceFacts`` is what a parser returns and what ``upsert_document`` writes to
``l1_document`` / ``l1_document_line`` / ``l1_document_observation``. Amounts are
integer minor units of the document currency; the PLN equivalent is derived.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kasa.core.normalize import from_minor, normalize_invoice_number, to_minor

Channel = Literal["ksef", "pdf", "jpk", "csv", "xlsx"]
Direction = Literal["sales", "purchase"]
DocumentKind = Literal[
    "vat", "correction", "advance", "settlement", "proforma", "internal", "receipt", "foreign", "bill"
]
LineKind = Literal["invoice", "order", "correction"]

# Which channel's fields the document carries when several channels saw it
# (briefing, step 2: KSeF > PDF > JPK > CSV). Higher wins.
CHANNEL_RANK: dict[str, int] = {"ksef": 40, "pdf": 30, "jpk": 20, "csv": 10, "xlsx": 10}

KSEF_NUMBER_RE = re.compile(r"^([0-9]{10})-([0-9]{8})-([0-9A-F]{12})-([0-9A-F]{2})$")


def crc8(data: bytes) -> int:
    """CRC-8 (polynomial 0x07, init 0x00): checksum of the KSeF number."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def ksef_number_valid(number: str) -> bool:
    """Format ``NIP-YYYYMMDD-12 hex-2 hex`` (35 characters) and the CRC-8 checksum."""
    match = KSEF_NUMBER_RE.match(number or "")
    return bool(match) and crc8(number[:32].encode()) == int(match.group(4), 16)


def ksef_number_date(number: str) -> date | None:
    """Date of acceptance encoded in the KSeF number (characters 12-19)."""
    match = KSEF_NUMBER_RE.match(number or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(2), "%Y%m%d").date()
    except ValueError:
        return None


def money(value: Decimal | int | str | None, currency: str) -> int | None:
    """Amount as printed -> minor units (exact; raises if it has too many decimals)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return to_minor(Decimal(str(value).strip()) if isinstance(value, str) else value, currency)


def money_rounded(value: Decimal, currency: str) -> int:
    """Derived amount (e.g. quantity x price, amount x rate) rounded half up to minor units."""
    return to_minor(value, currency, rounding=ROUND_HALF_UP)


class Party(BaseModel):
    """Seller, buyer or a third party (factor, payer, recipient ...)."""

    model_config = ConfigDict(extra="forbid")

    nip: str | None = None                   # Polish NIP, 10 digits
    foreign_tax_id: str | None = None        # VAT-UE / foreign id with country prefix, e.g. DE686148513
    country: str | None = None
    name: str | None = None
    address: str | None = None
    email: str | None = None
    phone: str | None = None
    role: str | None = None                  # third party: FA(3) Rola code or own label
    role_label: str | None = None
    share: Decimal | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("nip")
    @classmethod
    def _nip_format(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9]{10}", value):
            raise ValueError(f"NIP musi mieć 10 cyfr: {value!r}")
        return value

    @property
    def tax_key(self) -> str | None:
        """The identifier used by deduplication: NIP, else the foreign tax id."""
        return self.nip or self.foreign_tax_id


class VatRateTotal(BaseModel):
    """Summary of one VAT rate group (FA(3) P_13_x / P_14_x)."""

    model_config = ConfigDict(extra="forbid")

    rate: str
    field: str | None = None
    net_minor: int
    vat_minor: int | None = None
    vat_pln_minor: int | None = None


class InvoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int = Field(gt=0)
    name: str
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    unit_price_minor: int | None = None
    net_minor: int | None = None
    vat_rate: str | None = None
    vat_minor: int | None = None
    gross_minor: int | None = None
    line_kind: LineKind = "invoice"
    before: dict[str, Any] | None = None     # correction: line state before
    after: dict[str, Any] | None = None      # correction: line state after
    extra: dict[str, Any] = Field(default_factory=dict)


class InvoiceFacts(BaseModel):
    """Everything one channel says about one document."""

    model_config = ConfigDict(extra="forbid")

    # what read it
    channel: Channel
    parser: str
    parser_version: int = Field(gt=0)

    # identity
    direction: Direction
    kind: DocumentKind
    kind_certain: bool = True                # JPK/CSV cannot tell an advance from a VAT invoice
    ksef_number: str | None = None
    number_raw: str
    number_normalized: str = ""
    file_hash: str | None = None             # digest of the file when the file IS the document
    issuer_software: str | None = None

    # dates
    issue_date: date
    issue_place: str | None = None
    sale_date: date | None = None
    service_period_start: date | None = None
    service_period_end: date | None = None
    ksef_accepted_at: datetime | None = None
    due_date: date | None = None

    # parties
    seller: Party = Field(default_factory=Party)
    buyer: Party = Field(default_factory=Party)
    third_parties: list[Party] = Field(default_factory=list)
    payment_iban: str | None = None
    payment: dict[str, Any] = Field(default_factory=dict)

    # amounts (minor units of the document currency)
    currency: str = "PLN"
    net_minor: int | None = None
    vat_minor: int | None = None
    gross_minor: int
    fx_rate: Decimal | None = None
    fx_rate_date: date | None = None
    fx_rate_source: str | None = None
    gross_pln_minor: int | None = None
    vat_summary: list[VatRateTotal] = Field(default_factory=list)

    # lines
    lines: list[InvoiceLine] = Field(default_factory=list)

    # relations
    corrects_ref: dict[str, Any] | None = None
    advance_refs: list[dict[str, Any]] = Field(default_factory=list)
    order_number: str | None = None
    contract_ref: str | None = None

    # flags
    mpp_required: bool = False
    cash_method: bool = False
    reverse_charge: bool = False

    # code checks and everything the parser did not map to a field
    lines_sum_ok: bool | None = None
    checks: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        value = (value or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError(f"niepoprawny kod waluty: {value!r}")
        return value

    @field_validator("ksef_number")
    @classmethod
    def _ksef_format(cls, value: str | None) -> str | None:
        if value is not None and not KSEF_NUMBER_RE.match(value):
            raise ValueError(f"niepoprawny numer KSeF: {value!r}")
        return value

    @model_validator(mode="after")
    def _derived(self) -> InvoiceFacts:
        self.number_raw = self.number_raw.strip()
        if not self.number_normalized:
            self.number_normalized = normalize_invoice_number(self.number_raw)
        if self.currency == "PLN":
            self.fx_rate = None
            self.gross_pln_minor = self.gross_minor
        elif self.fx_rate is not None and self.gross_pln_minor is None:
            self.gross_pln_minor = money_rounded(from_minor(self.gross_minor, self.currency) * self.fx_rate, "PLN")
        if self.kind == "correction" and self.corrects_ref is None:
            self.corrects_ref = {"note": f"kanał {self.channel} nie podaje dokumentu korygowanego"}
        return self

    @property
    def counterparty(self) -> Party:
        """The other party: buyer of our sale, seller of our purchase."""
        return self.buyer if self.direction == "sales" else self.seller

    def observed(self) -> dict[str, Any]:
        """Fields as this channel saw them (stored in l1_document_observation.observed)."""
        return self.model_dump(mode="json", exclude_none=True)


def allocate(total: int, weights: list[int]) -> list[int] | None:
    """Split ``total`` over ``weights`` proportionally so the parts sum exactly (largest remainder).

    Used to give every line its share of a VAT total printed once per rate.
    Returns None when the weights are all zero or have mixed signs.
    """
    if not weights or all(w == 0 for w in weights):
        return None
    if any(w > 0 for w in weights) and any(w < 0 for w in weights):
        return None
    sign = -1 if total < 0 else 1
    magnitude = abs(total)
    abs_weights = [abs(w) for w in weights]
    whole = sum(abs_weights)
    raw = [Decimal(magnitude) * w / whole for w in abs_weights]
    floors = [int(r) for r in raw]
    remainder = magnitude - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - floors[i]), reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return [sign * f for f in floors]
