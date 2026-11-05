"""Who is on the other side of a bank operation - as a fact from the bank data.

The hypothesis is written by code into ``l1_counterparty`` through the SQL
function ``kasa_upsert_counterparty`` (the same one document ingestion uses):
by IBAN, by the NIP of a split payment block (outgoing payments only - in an
incoming one the NIP is ours), or a new counterparty from the bank's name with
low confidence. Our own accounts resolve to the "own" counterparty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.bank.extract import is_tax_microaccount, is_zus_account
from kasa.core.bank.model import BankEntry
from kasa.core.normalize import normalize_iban
from kasa.core.pseudonym import PseudonymContext, fold, is_capital_company, looks_like_person

# Confidence of a counterparty created from bank data (an existing row keeps its own):
CONFIDENCE_BY_NIP = 0.9           # NIP from the /IDC/ of our split payment
CONFIDENCE_BY_IBAN = 0.4          # new party known by the bank's name and IBAN: low
CONFIDENCE_BY_NAME = 0.3          # name only (card merchant, BLIK recipient): lowest
CONFIDENCE_OWN = 1.0

_INSTITUTION_RE = re.compile(
    r"\b(urzad skarbow\w*|zaklad ubezpieczen spolecznych|zus|krus|komornik\w*|sad (?:rejonowy|okregowy)\w*"
    r"|urzad (?:miasta|gminy|marszalkowski)\w*|gmina|starostwo|pfron|ministerstwo|izba administracji)\b"
)


@dataclass(frozen=True)
class CounterpartyFact:
    id: UUID | None
    kind: str
    confidence: float | None
    basis: str                  # 'own_account' / 'iban' / 'mpp_nip' / 'name' / 'bank' / 'none'


def _iban(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_iban(value)
    except ValueError:
        return None


def classify_kind(entry: BankEntry, op_type: str, context: PseudonymContext | None = None) -> str:
    name = entry.counterparty_name or ""
    iban = _iban(entry.counterparty_iban)
    if op_type == "own":
        return "own"
    if is_zus_account(iban) or is_tax_microaccount(iban) or _INSTITUTION_RE.search(fold(name)):
        return "institution"
    if not is_capital_company(name) and (
        op_type == "blik"
        or (entry.bank_tx_code or "").endswith("SALA")
        or looks_like_person(name, context)
    ):
        return "person"
    return "company"


def resolve_counterparty(
    conn: psycopg.Connection,
    company_id: UUID,
    entry: BankEntry,
    extracted: dict[str, Any],
    op_type: str,
    *,
    own_nip: str | None,
    bank_name: str | None,
    context: PseudonymContext | None = None,
) -> CounterpartyFact:
    """Find or create the counterparty of one operation (kasa_upsert_counterparty)."""
    iban = _iban(entry.counterparty_iban)
    name = entry.counterparty_name
    kind = classify_kind(entry, op_type, context)
    nip = None
    mpp = extracted.get("mpp") or {}
    if entry.amount_minor < 0 and mpp.get("idc_nip") and mpp["idc_nip"] != own_nip:
        nip = mpp["idc_nip"]   # we pay a supplier: IDC is the supplier's NIP

    if op_type == "own":
        row = conn.execute("SELECT kasa_own_counterparty(%s, %s, %s)", (company_id, name, own_nip)).fetchone()
        return CounterpartyFact(row[0], "own", CONFIDENCE_OWN, "own_account")
    if not (name or iban or nip):
        if op_type in ("fee", "interest") and bank_name:
            # Fees and interest: the account's bank is the other side.
            name, kind = bank_name, "bank"
        else:
            return CounterpartyFact(None, kind, None, "none")

    basis = "mpp_nip" if nip else "iban" if iban else "bank" if kind == "bank" else "name"
    confidence = {"mpp_nip": CONFIDENCE_BY_NIP, "iban": CONFIDENCE_BY_IBAN}.get(basis, CONFIDENCE_BY_NAME)
    evidence = {"basis": basis, "bank_name": entry.counterparty_name, "iban": iban, "nip_from_mpp": nip,
                "entry_reference": entry.entry_reference, "source": entry.source}
    row = conn.execute(
        """
        SELECT kasa_upsert_counterparty(
            %s, %s, %s, %s,
            p_kind => %s::counterparty_kind, p_source => %s, p_method => 'code',
            p_confidence => %s, p_evidence => %s, p_created_by => 'kasa.bank')
        """,
        (company_id, nip, name, iban, kind, entry.source, confidence, Jsonb(evidence)),
    ).fetchone()
    return CounterpartyFact(row[0], kind, confidence, basis)
