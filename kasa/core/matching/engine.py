"""Matching engine: transfer <-> invoice, layer by layer (briefing, step 4).

Every layer adds signals (with weights from :mod:`kasa.core.matching.weights`) to candidates;
no layer closes anything by itself - the automation (``auto.py``) closes a match only when the
rank-1 candidate has a calibrated score >= the threshold, hard evidence and a clear margin, or
the owner does it in the review queue.

1. Preparation (``data.py``): amounts in grosze, normalized numbers, counterparty by NIP/IBAN,
   own transfers, the legs of a split payment as one occurrence (the main leg is the subject,
   the VAT leg follows it), card holds left out.
2. Hard identifiers: KSeF number in the title, MPP block (number + NIP), invoice number in the
   title (sales for inflows, purchases for outflows). Amount agrees -> hard; amount differs ->
   the signal stays, a partial payment (underpayment) of the transfer amount, not hard.
3. Counterparty, amount, time: open invoices of the counterparty in -30..+90 days around the due
   date (or the issue date, flagged), exact amount, 2-6 invoices adding up to the grosz,
   overpayment and possible duplicate, a correction as a negative row, advances, refunds,
   foreign currency with the named difference "kurs" (``rate_for``), tolerance only for one invoice.
4. Title semantics (``title_semantics_current``, written once by the model, which never sees
   invoices): intent, period, persons - soft signals; zus/tax/fee/rent -> "no document" hypotheses.
5. Internal files (PM sheet): BLIK to a phone, person pseudonym, amount, date -> a candidate row
   (decision D2: never hard until a rule matures).
6. Learned rules (4C, ``kasa.core.rules.apply.applicable_rules`` via importlib; skipped when the
   module is missing); built-in rules ``builtin:*`` are applied here, in code.

The result per transaction: at most 3 ranked candidates (:class:`Candidate`); "no candidate" is
a ``no_document`` hypothesis with a subtype, a tie is "to clarify" first.
"""

from __future__ import annotations

import importlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.matching import weights as W
from kasa.core.matching.data import Doc, MatchContext, Tx, load_context, name_tokens
from kasa.core.matching.fx import convert_minor
from kasa.core.normalize import from_minor
from kasa.core.pseudonym import fold

ENGINE_VERSION = 1
SOURCE = "kasa.matching"
STRONG_LINKS = ("id", "nip", "iban")
AGREEING_LINKS = (*STRONG_LINKS, "name")
MATCH_KINDS = ("document", "internal_record", "related")
SALARY_WORDS = re.compile(r"wynagrodz|pensj|placa|plac[ey]\b|wyplat|premi|umowa o (?:dzielo|zlecenie)|umow[ay] zleceni")
ADVANCE_WORDS = re.compile(r"zaliczk|przedplat|zadatek")
DEPOSIT_WORDS = re.compile(r"kaucj|depozyt")
REFUND_WORDS = re.compile(r"\bzwrot|refund|reklamacj")
RENT_WORDS = re.compile(r"czynsz|najem|najmu")
DOC_INTENTS = ("invoice_payment", "rent", "subscription", "advance", "deposit")
CONFLICT_INTENTS = ("zus", "tax", "own_transfer", "fee")


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    tag: str
    weight: float
    fact_pl: str


@dataclass
class Item:
    """One row of a proposed settlement (one future ``l3_match`` row)."""

    document_id: UUID | None = None
    internal_record_id: UUID | None = None
    related_transaction_id: UUID | None = None
    amount_minor: int = 0                     # transaction side, positive (negative = correction row)
    difference_kind: str | None = None
    difference_minor: int | None = None
    document_amount_minor: int | None = None  # target side, target currency
    currency: str = "PLN"                     # target currency
    label: str = ""

    @property
    def target(self) -> tuple[str, UUID]:
        if self.document_id:
            return "document", self.document_id
        if self.internal_record_id:
            return "internal_record", self.internal_record_id
        return "transaction", self.related_transaction_id

    def proposal(self) -> dict[str, Any]:
        """Exactly the keys of the phase 4 contract."""
        return {
            "document_id": str(self.document_id) if self.document_id else None,
            "internal_record_id": str(self.internal_record_id) if self.internal_record_id else None,
            "related_transaction_id": str(self.related_transaction_id) if self.related_transaction_id else None,
            "amount_minor": int(self.amount_minor),
            "difference_kind": self.difference_kind,
            "difference_minor": int(self.difference_minor) if self.difference_minor is not None else None,
        }

    def detail(self) -> dict[str, Any]:
        return {"label": self.label, "document_amount_minor": self.document_amount_minor, "currency": self.currency}


@dataclass
class Candidate:
    kind: str                                  # document | internal_record | related | no_document | own_transfer | to_clarify
    items: list[Item] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    hard: bool = False
    final_status: str = "matched"
    subtype: str | None = None
    awaiting_until: date | None = None
    rule_key: str | None = None                # built-in rule behind the hard evidence (the automation's licence)
    rule: dict[str, Any] | None = None         # learned rule (4C) behind it: {id, version, rule_key, stage}
    raw: float = 0.0
    score: float = 0.0
    margin: float = 0.0
    tie: bool = False
    precedents: int = 0
    linked_evidence: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)   # why it can never be automatic (duplicate, sensitive...)

    def add(self, tag: str, fact: str, weight: float | None = None) -> Candidate:
        self.signals.append(Signal(tag, W.WEIGHTS.get(tag, 0.0) if weight is None else weight, fact))
        return self

    def has(self, tag: str) -> bool:
        return any(s.tag == tag for s in self.signals)

    @property
    def tags(self) -> list[str]:
        out = []
        for s in self.signals:
            if s.tag not in out:
                out.append(s.tag)
        if self.rule and f"rule:{self.rule['rule_key']}@{self.rule['version']}" not in out:
            out.append(f"rule:{self.rule['rule_key']}@{self.rule['version']}")
        return out

    @property
    def facts(self) -> list[str]:
        return [s.fact_pl for s in self.signals if s.fact_pl]

    @property
    def is_match(self) -> bool:
        return self.kind in MATCH_KINDS

    def key(self) -> tuple:
        return (self.final_status, self.subtype, tuple(sorted((i.target[0], str(i.target[1])) for i in self.items)))

    def proposal(self) -> dict[str, Any]:
        return {
            "items": [i.proposal() for i in self.items],
            "final_status": self.final_status,
            "subtype": self.subtype,
            "awaiting_until": self.awaiting_until.isoformat() if self.awaiting_until else None,
        }

    def compute(self, params: dict[str, float]) -> None:
        self.raw = W.BASE + sum(s.weight for s in self.signals)
        z = (self.raw - params.get("bias", 0.0)) / max(params.get("temperature", 1.0), 1e-6)
        self.score = 1.0 / (1.0 + math.exp(-max(min(z, 60.0), -60.0)))


@dataclass
class TxResult:
    tx: Tx
    candidates: list[Candidate]                 # ranked, at most MAX_SUGGESTIONS
    considered: list[Candidate]                 # everything generated (for explain and calibration)
    group: list[Tx] = field(default_factory=list)   # split payment legs (main first)
    amount_minor: int = 0                       # amount of the occurrence (MPP: all legs)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


@dataclass
class Evaluation:
    ctx: MatchContext
    results: list[TxResult]
    params: dict[str, float]


# ---------------------------------------------------------------------------
# Formatting (facts are Polish)
# ---------------------------------------------------------------------------


def money(minor: int, currency: str = "PLN") -> str:
    value = from_minor(int(minor), currency)
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    unit = "zł" if currency == "PLN" else currency
    return f"{sign}{' '.join(groups)},{frac} {unit}"


def _rate(rate: Decimal) -> str:
    return f"{rate.normalize():f}".replace(".", ",")


def _days(delta: int) -> str:
    if delta == 0:
        return "w dniu terminu"
    n = abs(delta)
    word = "dzień" if n == 1 else "dni"
    return f"{n} {word} {'po' if delta > 0 else 'przed'}"


# ---------------------------------------------------------------------------
# Counterparty links
# ---------------------------------------------------------------------------


def _tx_nips(ctx: MatchContext, tx: Tx) -> set[str]:
    out = set()
    if tx.cp and tx.cp.nip:
        out.add(tx.cp.nip)
    mpp = tx.extracted.get("mpp") or {}
    if mpp.get("idc_nip") and mpp["idc_nip"] != ctx.own_nip:
        out.add(mpp["idc_nip"])
    return out


def _tokens_in_title(tx: Tx) -> set[str]:
    return set(name_tokens(tx.title))


def link(ctx: MatchContext, tx: Tx, doc: Doc) -> tuple[str, str]:
    """(strength, fact) of the tie between the payer/payee of a transfer and the party of a document.

    id / nip / iban = strong (briefing: "nadawca po IBAN/NIP przypisany do kontrahenta"); name = the
    same name without legal forms; similar / title_name = weak; conflict = different NIPs.
    """
    if tx.cp and doc.cp and tx.cp.id == doc.cp.id and tx.cp.kind != "own":
        basis = "NIP" if tx.cp.nip else "IBAN" if tx.cp.ibans else "nazwa"
        return "id", f"kontrahent rozpoznany ({basis}): {doc.cp.name or doc.party_name}"
    doc_nip = doc.party_nip or (doc.cp.nip if doc.cp else None)
    tx_nips = _tx_nips(ctx, tx)
    if doc_nip and tx_nips:
        if doc_nip in tx_nips:
            return "nip", f"NIP kontrahenta zgodny ({doc_nip})"
        return "conflict", f"inny NIP: przelew {', '.join(sorted(tx_nips))}, dokument {doc_nip}"
    doc_ftid = doc.cp.foreign_tax_id if doc.cp else None
    if doc_ftid and tx.cp and tx.cp.foreign_tax_id == doc_ftid:
        return "nip", f"numer podatkowy kontrahenta zgodny ({doc_ftid})"
    if tx.counterparty_iban and tx.counterparty_iban not in ctx.own_ibans:
        doc_ibans = set(doc.cp.ibans if doc.cp else ())
        if doc.payment_iban and doc.payment_iban not in ctx.own_ibans:
            doc_ibans.add(doc.payment_iban)
        if tx.counterparty_iban in doc_ibans:
            return "iban", "rachunek kontrahenta zgodny z rachunkiem z faktury (IBAN)"
    tx_name = (tx.cp.name if tx.cp else None) or tx.counterparty_name
    doc_name = (doc.cp.name if doc.cp else None) or doc.party_name
    a, b = set(name_tokens(tx_name)), set(name_tokens(doc_name))
    if a and b:
        if a == b:
            return "name", f"ta sama nazwa kontrahenta ({doc_name})"
        common = {t for t in a & b if len(t) >= 4}
        if common and not (tx.cp and tx.cp.kind == "bank"):
            return "similar", f"podobna nazwa kontrahenta: {tx_name} / {doc_name}"
    if b and b <= _tokens_in_title(tx) and any(len(t) >= 4 for t in b):
        return "title_name", f"nazwa kontrahenta w tytule: {doc_name}"
    if a and b:
        return "different", f"inna nazwa kontrahenta: {tx_name} / {doc_name}"
    return "none", ""


LINK_SIGNAL = {
    "id": "counterparty_strong", "nip": "counterparty_strong", "iban": "counterparty_strong",
    "name": "counterparty_name", "similar": "counterparty_similar", "title_name": "counterparty_title_name",
    "conflict": "counterparty_conflict", "different": "counterparty_different",
}
LINK_RANK = {"id": 6, "nip": 6, "iban": 6, "name": 4, "similar": 2, "title_name": 2, "none": 1, "different": 0,
             "conflict": -1}


def _same_party(a: Tx, b: Tx) -> bool:
    if a.counterparty_id and a.counterparty_id == b.counterparty_id:
        return True
    if a.counterparty_iban and a.counterparty_iban == b.counterparty_iban:
        return True
    ta, tb = name_tokens(a.counterparty_name), name_tokens(b.counterparty_name)
    return bool(ta) and ta == tb


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------


def tolerance_minor(amount_minor: int, *, foreign: bool = False) -> int:
    """1% or 20 zł (foreign currency: 1.5% or 50 zł), whichever is larger - one invoice only."""
    pct = Decimal(W.FX_TOLERANCE_PCT if foreign else W.TOLERANCE_PCT)
    floor = W.FX_TOLERANCE_MIN_MINOR if foreign else W.TOLERANCE_MIN_MINOR
    return max(int(abs(amount_minor) * pct), floor)


def _doc_pln(ctx: MatchContext, doc: Doc, amount_ccy: int) -> tuple[int | None, str | None]:
    """PLN value of an amount of a foreign document: the document's own rate, else ``rate_for``."""
    if doc.currency == "PLN":
        return amount_ccy, None
    if doc.fx_rate_source == "document" and doc.fx_rate:
        rate = Decimal(doc.fx_rate)
        return convert_minor(amount_ccy, doc.currency, rate), f"kurs z faktury {_rate(rate)}"
    found = ctx.rates(doc.currency, doc.tax_point)
    if found:
        rate, table_day = found
        return (convert_minor(amount_ccy, doc.currency, rate),
                f"kurs NBP {_rate(rate)} z {table_day.isoformat()} (tabela A przed {doc.tax_point.isoformat()})")
    if doc.gross_pln_minor and doc.gross_minor:
        return round(amount_ccy * doc.gross_pln_minor / doc.gross_minor), "kwota PLN z faktury"
    return None, None


@dataclass
class Settlement:
    items: list[Item]
    relation: str              # exact | exact_currency | tolerance | fee | fx | underpayment | overpayment
    signals: list[tuple[str, str]]


def _chain_rows(doc: Doc) -> list[Item]:
    rows = []
    for member in doc.chain:
        if member.open_minor == 0 and member is not doc:
            continue
        correction = member is not doc
        rows.append(Item(document_id=member.id, amount_minor=member.open_minor,
                         document_amount_minor=member.open_minor, currency=member.currency,
                         difference_kind="correction" if correction else None,
                         difference_minor=member.open_minor if correction else None,
                         label=member.number_raw))
    return rows


def settle_single(ctx: MatchContext, tx: Tx, doc: Doc, amount_abs: int) -> Settlement | None:
    """How a transfer of ``amount_abs`` grosze settles one document (with its corrections)."""
    open_total = doc.chain_open_minor
    signals: list[tuple[str, str]] = []
    if doc.corrections:
        parts = " + ".join(f"{d.number_raw} {money(d.gross_minor, d.currency)}" for d in doc.chain)
        signals.append(("correction_chain", f"saldo z łańcucha korekt: {parts} = {money(doc.chain_gross_minor, doc.currency)}"))
    if doc.currency == "PLN" and tx.currency == "PLN":
        diff = amount_abs - open_total
        if open_total <= 0 < doc.gross_minor:
            item = Item(document_id=doc.id, amount_minor=amount_abs, document_amount_minor=0, currency="PLN",
                        difference_kind="overpayment", difference_minor=amount_abs, label=doc.number_raw)
            signals.append(("overpayment", f"faktura już rozliczona - nadpłata {money(amount_abs)}"))
            return Settlement([item], "overpayment", signals)
        rows = _chain_rows(doc)
        base = rows[0]
        if diff == 0:
            signals.append(("amount_exact", "kwota co do grosza" + (" (z korektą)" if doc.corrections else "")))
            return Settlement(rows, "exact", signals)
        if abs(diff) <= tolerance_minor(open_total):
            kind = "fee" if tx.op_type == "foreign_transfer" and diff < 0 else "tolerance"
            base.amount_minor += diff
            base.difference_kind, base.difference_minor = kind, diff
            name = "prowizja" if kind == "fee" else "tolerancja"
            signals.append(("amount_tolerance", f"różnica „{name}” {money(diff)} (tolerancja 1% albo 20 zł)"))
            return Settlement(rows, kind, signals)
        if diff < 0:
            item = Item(document_id=doc.id, amount_minor=amount_abs, document_amount_minor=amount_abs, currency="PLN",
                        difference_kind="underpayment", difference_minor=diff, label=doc.number_raw)
            signals.append(("underpayment", (f"niedopłata {money(-diff)}: zapłacono {money(amount_abs)} z "
                                             f"{money(open_total)} (częściowa zapłata)")))
            return Settlement([item], "underpayment", signals)
        base.amount_minor += diff
        base.difference_kind, base.difference_minor = "overpayment", diff
        signals.append(("overpayment", f"nadpłata {money(diff)} ponad saldo {money(open_total)}"))
        return Settlement(rows, "overpayment", signals)
    # foreign currency document (or transfer): compare in the document currency when the bank gives it
    open_pln, rate_fact = _doc_pln(ctx, doc, open_total)
    if open_pln is None:
        return None
    original = abs(tx.original_amount_minor) if tx.original_amount_minor and tx.original_currency == doc.currency \
        else (amount_abs if tx.currency == doc.currency else None)
    diff_pln = amount_abs - open_pln if tx.currency == "PLN" else 0
    item = Item(document_id=doc.id, amount_minor=amount_abs, document_amount_minor=open_total, currency=doc.currency,
                label=doc.number_raw)
    if original is not None:
        cdiff = original - open_total
        if cdiff == 0:
            signals.append(("amount_exact", f"kwota w {doc.currency} co do grosza ({money(open_total, doc.currency)})"))
            relation = "exact_currency"
        elif abs(cdiff) <= tolerance_minor(open_total, foreign=True) and cdiff < 0:
            signals.append(("amount_fx_tolerance", f"brakuje {money(-cdiff, doc.currency)} - prowizja banku"))
            item.difference_kind = "fee"
            relation = "fee"
        elif cdiff < 0:
            item.document_amount_minor = original
            item.difference_kind, item.difference_minor = "underpayment", diff_pln
            signals.append(("underpayment", f"niedopłata {money(-cdiff, doc.currency)} (częściowa zapłata)"))
            return Settlement([item], "underpayment", signals)
        else:
            item.difference_kind, item.difference_minor = "overpayment", diff_pln
            signals.append(("overpayment", f"nadpłata {money(cdiff, doc.currency)}"))
            return Settlement([item], "overpayment", signals)
        if tx.currency == "PLN":
            if relation == "exact_currency":
                item.difference_kind = "fx" if diff_pln else None
            item.difference_minor = diff_pln if item.difference_kind else None
            if diff_pln:
                signals.append(("fx", (f"różnica „kurs” {money(diff_pln)}: przelew {money(amount_abs)}, faktura "
                                       f"{money(open_pln)} ({rate_fact})")))
        return Settlement([item], relation, signals)
    if tx.op_type not in ("card", "foreign_transfer") and not tx.original_currency:
        return None                     # a domestic PLN transfer is never compared with a foreign invoice
    if abs(diff_pln) <= tolerance_minor(open_pln, foreign=True):
        item.difference_kind, item.difference_minor = ("fx", diff_pln) if diff_pln else (None, None)
        signals.append(("amount_fx_tolerance", (f"różnica „kurs” {money(diff_pln)}: {money(amount_abs)} wobec "
                                                f"{money(open_total, doc.currency)} = {money(open_pln)} "
                                                f"({rate_fact}); tolerancja 1,5% albo 50 zł")))
        return Settlement([item], "fx", signals)
    return None


def settle_many(docs: list[Doc], amount_abs: int) -> Settlement:
    """Several invoices in one transfer: the sum must agree to the grosz (no tolerance)."""
    total = sum(d.chain_open_minor for d in docs)
    rows: list[Item] = []
    if total == amount_abs:
        for d in docs:
            rows.extend(_chain_rows(d))
        return Settlement(rows, "exact", [("amount_exact", f"suma {len(docs)} faktur co do grosza ({money(total)})")])
    remaining = amount_abs
    ordered = sorted(docs, key=lambda d: (d.reference_date[0], d.number_raw))
    for index, d in enumerate(ordered):
        last = index == len(ordered) - 1
        share = remaining if last else min(remaining, d.chain_open_minor)
        rows.append(Item(document_id=d.id, amount_minor=share, document_amount_minor=min(share, d.chain_open_minor),
                         currency=d.currency, label=d.number_raw))
        remaining -= share
    diff = amount_abs - total
    rows[-1].difference_kind = "underpayment" if diff < 0 else "overpayment"
    rows[-1].difference_minor = diff
    word = "niedopłata" if diff < 0 else "nadpłata"
    return Settlement(rows, "underpayment" if diff < 0 else "overpayment",
                      [("underpayment" if diff < 0 else "overpayment",
                        f"{word} {money(abs(diff))}: suma faktur {money(total)}, przelew {money(amount_abs)}")])


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------


def _direction_ok(tx: Tx, doc: Doc) -> bool:
    """Inflow pays sales (or a purchase credit note), outflow pays purchases (or a sales credit note)."""
    reference = doc.chain_open_minor if doc.chain_open_minor != 0 else doc.chain_gross_minor
    if reference == 0:
        return False
    return (1 if tx.inflow else -1) == doc.money_sign * (1 if reference > 0 else -1)


def _window(tx: Tx, doc: Doc) -> tuple[bool, str]:
    ref, is_due = doc.reference_date
    delta = (tx.day - ref).days
    inside = -W.WINDOW_BEFORE <= delta <= W.WINDOW_AFTER
    if is_due:
        fact = f"termin płatności {ref.isoformat()}, przelew {_days(delta)}"
    else:
        fact = f"brak terminu płatności - okno liczone od daty wystawienia {ref.isoformat()} ({_days(delta)})"
    return inside, fact


def _doc_candidate(ctx: MatchContext, tx: Tx, docs: list[Doc], amount_abs: int, *, origin: str,
                   origin_fact: str) -> Candidate | None:
    settlement = settle_single(ctx, tx, docs[0], amount_abs) if len(docs) == 1 else settle_many(docs, amount_abs)
    if settlement is None:
        return None
    cand = Candidate("document", items=settlement.items)
    if origin_fact:
        cand.add(origin, origin_fact)
    links = [link(ctx, tx, d) for d in docs]
    weakest = min(links, key=lambda x: LINK_RANK[x[0]])
    if weakest[0] in LINK_SIGNAL:
        cand.add(LINK_SIGNAL[weakest[0]], weakest[1])
    for tag, fact in settlement.signals:
        cand.add(tag, fact)
    inside = [_window(tx, d) for d in docs]
    if all(i[0] for i in inside):
        cand.add("window", inside[0][1] if len(docs) == 1 else "terminy płatności w oknie -30..+90 dni")
    elif origin in ("counterparty", "amount", "subset"):
        return None
    for d in docs:
        if d.kind == "proforma":
            cand.add("proforma", f"{d.number_raw} to proforma")
    if any(d.kind == "advance" for d in docs) and (tx.intent == "advance" or ADVANCE_WORDS.search(tx.title_folded)):
        cand.add("advance_invoice", "zaliczka i faktura zaliczkowa")
    exact = settlement.relation in ("exact", "exact_currency")
    agree = all(lk[0] in AGREEING_LINKS for lk in links)
    if origin == "ksef_number" and exact:
        cand.hard, cand.rule_key = True, "builtin:match_ksef_number"
    elif origin == "mpp_block" and exact:
        cand.hard, cand.rule_key = True, "builtin:match_mpp_block"
    elif origin == "invoice_number" and exact and agree:
        cand.hard, cand.rule_key = True, "builtin:match_invoice_number_same_counterparty"
    cand.precedents = ctx.precedents.get(tx.counterparty_id, 0) if tx.counterparty_id else 0
    return cand


def _own_transfer(ctx: MatchContext, tx: Tx) -> list[Candidate]:
    if tx.op_type != "own" and not (tx.cp and tx.cp.kind == "own"):
        return []
    cand = Candidate("own_transfer", final_status="own_transfer")
    reason = tx.extracted.get("op_type_reason") or "rachunek drugiej strony to rachunek firmy"
    if tx.op_type == "own":
        cand.add("builtin_own_transfer", f"transfer własny ({reason})")
        cand.hard, cand.rule_key = True, "builtin:own_transfer"
    else:
        cand.add("builtin_own_transfer", "kontrahent to sama firma", weight=4.0)
    pairs = [o for o in ctx.txs.values()
             if o.id != tx.id and o.account_id != tx.account_id and o.amount_minor == -tx.amount_minor
             and abs((o.day - tx.day).days) <= W.OWN_PAIR_WINDOW and (o.op_type == "own" or (o.cp and o.cp.kind == "own"))]
    if pairs:
        pair = min(pairs, key=lambda o: (abs((o.day - tx.day).days), o.label))
        cand.add("own_pair", f"para na drugim rachunku: {pair.label} z {pair.day.isoformat()} ({money(pair.amount_minor)})")
    return [cand]


def _numbers_in_title(tx: Tx) -> list[dict[str, Any]]:
    out = []
    for item in tx.extracted.get("invoice_numbers") or []:
        forms = [item.get("normalized")] + list(item.get("alt") or [])
        out.append({"raw": item.get("raw"), "forms": [f for f in forms if f], "pattern": item.get("pattern")})
    entities = (tx.semantics or {}).get("entities") or {}
    known = {f for n in out for f in n["forms"]}
    for item in entities.get("invoice_numbers") or []:
        if item.get("source") == "model" and item.get("normalized") and item["normalized"] not in known:
            raw = item.get("raw")
            raw = raw.get("untrusted_text") if isinstance(raw, dict) else raw
            out.append({"raw": raw, "forms": [item["normalized"]], "pattern": "model"})
    return out


def _title_docs(ctx: MatchContext, tx: Tx) -> dict[int, list[Doc]]:
    """Documents (direction-consistent) named by each invoice number of the title."""
    found: dict[int, list[Doc]] = {}
    for index, number in enumerate(_numbers_in_title(tx)):
        docs = []
        for form in number["forms"]:
            for doc in ctx.by_number.get(form, []):
                if doc not in docs and _direction_ok(tx, doc):
                    docs.append(doc)
        if docs:
            found[index] = docs
    return found


def _hard_identifier_layer(ctx: MatchContext, tx: Tx, amount_abs: int) -> tuple[list[Candidate], set[UUID]]:
    candidates: list[Candidate] = []
    named: set[UUID] = set()
    # KSeF number (and from 2027 the collective payment identifier) in the title
    for item in tx.extracted.get("ksef_numbers") or []:
        doc = ctx.by_ksef.get(item.get("number"))
        if doc and item.get("checksum_ok", True) and _direction_ok(tx, doc):
            named.add(doc.id)
            cand = _doc_candidate(ctx, tx, [doc], amount_abs, origin="ksef_number",
                                  origin_fact=f"numer KSeF {doc.ksef_number} w tytule ({doc.number_raw})")
            if cand:
                candidates.append(cand)
    # MPP block: number + NIP of the issuer
    mpp = tx.extracted.get("mpp") or {}
    if mpp.get("inv_normalized"):
        for doc in ctx.by_number.get(mpp["inv_normalized"], []):
            if not _direction_ok(tx, doc):
                continue
            issuer = doc.seller_nip
            if mpp.get("idc_nip") and issuer == mpp["idc_nip"]:
                named.add(doc.id)
                cand = _doc_candidate(ctx, tx, [doc], amount_abs, origin="mpp_block",
                                      origin_fact=f"blok MPP: {mpp.get('inv')}, NIP {mpp.get('idc')}, VAT "
                                                  f"{money(mpp.get('vat_minor') or 0)}")
                if cand:
                    candidates.append(cand)
    # invoice numbers in the title
    by_number = _title_docs(ctx, tx)
    numbers = _numbers_in_title(tx)
    if len(by_number) == 1:
        for doc in next(iter(by_number.values())):
            if doc.id in named:
                continue
            named.add(doc.id)
            cand = _doc_candidate(ctx, tx, [doc], amount_abs, origin="invoice_number",
                                  origin_fact=f"{doc.number_raw} w tytule")
            if cand:
                if len(numbers) > 1:
                    cand.blockers.append("nie wszystkie numery z tytułu znalezione")
                candidates.append(cand)
    elif len(by_number) > 1:
        chosen = []
        for docs in by_number.values():
            best = max(docs, key=lambda d: (LINK_RANK[link(ctx, tx, d)[0]], d.chain_open_minor > 0, d.issue_date))
            if best not in chosen and best.id not in named:
                chosen.append(best)
        named |= {d.id for docs in by_number.values() for d in docs}
        if chosen:
            labels = ", ".join(d.number_raw for d in chosen)
            # an invoice and its correction named together are one chain, i.e. one document
            fact = (f"{len(chosen)} numery faktur w tytule: {labels}" if len(chosen) > 1
                    else f"{labels} w tytule (razem z korektą)")
            cand = _doc_candidate(ctx, tx, chosen, amount_abs, origin="invoice_number", origin_fact=fact)
            if cand:
                if len(by_number) < len(numbers):
                    cand.blockers.append("nie wszystkie numery z tytułu znalezione")
                candidates.append(cand)
    return candidates, named


def _subsets(docs: list[Doc], target: int) -> list[list[Doc]]:
    pool = sorted((d for d in docs if 0 < d.chain_open_minor <= target and d.currency == "PLN"),
                  key=lambda d: -d.chain_open_minor)[: W.SUBSET_POOL]
    found: list[list[Doc]] = []
    nodes = 0

    def dfs(start: int, chosen: list[Doc], total: int) -> None:
        nonlocal nodes
        if len(found) >= 4 or nodes > W.SUBSET_NODE_LIMIT:
            return
        if total == target and W.SUBSET_MIN <= len(chosen) <= W.SUBSET_MAX:
            found.append(list(chosen))
            return
        if len(chosen) >= W.SUBSET_MAX:
            return
        for index in range(start, len(pool)):
            nodes += 1
            amount = pool[index].chain_open_minor
            if total + amount > target:
                continue
            chosen.append(pool[index])
            dfs(index + 1, chosen, total + amount)
            chosen.pop()

    dfs(0, [], 0)
    return found


def _counterparty_layer(ctx: MatchContext, tx: Tx, amount_abs: int, named: set[UUID],
                        have_exact: bool = False) -> list[Candidate]:
    linked, named_exact = [], 0
    for doc in ctx.bases():
        if doc.chain_open_minor == 0 or not _direction_ok(tx, doc):
            continue
        strength, _fact = link(ctx, tx, doc)
        if strength not in AGREEING_LINKS or not _window(tx, doc)[0]:
            continue
        if doc.id in named:
            settlement = settle_single(ctx, tx, doc, amount_abs)
            named_exact += bool(settlement and settlement.relation in ("exact", "exact_currency"))
            continue
        linked.append((doc, strength))
    if not linked:
        return []
    out: list[Candidate] = []
    exact = []
    for doc, strength in linked:
        settlement = settle_single(ctx, tx, doc, amount_abs)
        if settlement and settlement.relation in ("exact", "exact_currency"):
            exact.append((doc, strength))
    for doc, strength in exact:
        cand = _doc_candidate(ctx, tx, [doc], amount_abs, origin="counterparty", origin_fact="")
        if cand is None:
            continue
        if len(exact) == 1 and not named_exact:
            if strength in STRONG_LINKS:
                cand.add("single_open_invoice", "jedyna otwarta faktura kontrahenta na dokładnie tę kwotę")
                cand.hard, cand.rule_key = True, "builtin:match_single_open_invoice_exact_amount"
            else:
                cand.add("single_open_invoice_weak", "jedyna otwarta faktura kontrahenta na tę kwotę "
                                                     "(kontrahent rozpoznany tylko po nazwie)")
        out.append(cand)
    if exact or have_exact:
        return out
    # one invoice within tolerance / foreign currency with the difference "kurs"
    for doc, _strength in linked:
        settlement = settle_single(ctx, tx, doc, amount_abs)
        if settlement and settlement.relation in ("tolerance", "fee", "fx"):
            cand = _doc_candidate(ctx, tx, [doc], amount_abs, origin="counterparty", origin_fact="")
            if cand:
                out.append(cand)
    # 2-6 invoices adding up to the grosz
    for subset in _subsets([d for d, _ in linked], amount_abs):
        cand = _doc_candidate(ctx, tx, subset, amount_abs, origin="subset", origin_fact="")
        if cand:
            cand.add("subset_sum", f"{len(subset)} otwarte faktury kontrahenta sumują się do kwoty przelewu: "
                                   + ", ".join(d.number_raw for d in subset))
            out.append(cand)
    # partial payment of the only open invoice of the counterparty (no invoice named in the title)
    if not out and len(linked) == 1 and not named:
        doc = linked[0][0]
        settlement = settle_single(ctx, tx, doc, amount_abs)
        if settlement and settlement.relation == "underpayment":
            cand = _doc_candidate(ctx, tx, [doc], amount_abs, origin="counterparty", origin_fact="")
            if cand:
                cand.add("single_open_invoice_weak", "jedyna otwarta faktura kontrahenta", weight=0.5)
                out.append(cand)
    return out


def _amount_layer(ctx: MatchContext, tx: Tx, amount_abs: int, named: set[UUID]) -> list[Candidate]:
    """Payer unknown or not linked (operator, card merchant): open documents for exactly this amount."""
    hits = []
    for doc in ctx.bases():
        if doc.id in named or doc.chain_open_minor == 0 or not _direction_ok(tx, doc):
            continue
        if not _window(tx, doc)[0]:
            continue
        strength, _ = link(ctx, tx, doc)
        if strength in AGREEING_LINKS or strength == "conflict":
            continue                     # handled by the counterparty layer, or another party for sure
        settlement = settle_single(ctx, tx, doc, amount_abs)
        if settlement and settlement.relation in ("exact", "exact_currency", "fx"):
            if settlement.relation == "fx" and strength == "different":
                continue                 # a near amount in another currency from an unrelated party: noise
            hits.append((doc, settlement.relation))
    out = []
    exact_count = sum(1 for _, rel in hits if rel != "fx")
    for doc, relation in hits[:5]:
        cand = _doc_candidate(ctx, tx, [doc], amount_abs, origin="amount", origin_fact="")
        if cand is None:
            continue
        if relation != "fx" and exact_count == 1:
            cand.add("single_amount_any", "jedyny otwarty dokument na dokładnie tę kwotę (płatnik nierozpoznany)")
        operator = tx.extracted.get("operator")
        if operator:
            cand.add("operator", f"płatność przez operatora {operator.get('name')} - klient tylko w tytule", weight=0.0)
        out.append(cand)
    return out


def _earlier_identical(ctx: MatchContext, tx: Tx) -> Tx | None:
    """An earlier identical payment (party, amount, title) - a possible duplicate.

    Only between parties: recurring identical bank fees, taxes or ZUS are not duplicates.
    """
    if tx.op_type in ("fee", "interest", "tax", "zus", "own") or _tx_party_kind(tx) in ("bank", "institution", "own"):
        return None
    norm = re.sub(r"\W+", " ", tx.title_folded).strip()
    found = [o for o in ctx.txs.values()
             if o.id != tx.id and o.inflow == tx.inflow and o.abs_minor == tx.abs_minor and _same_party(o, tx)
             and 0 <= (tx.day - o.day).days <= W.DUPLICATE_WINDOW and (o.day, o.label) < (tx.day, tx.label)
             and re.sub(r"\W+", " ", o.title_folded).strip() == norm and o.op_type != "own"]
    return min(found, key=lambda o: (o.day, o.label)) if found else None


def _is_refund(tx: Tx) -> bool:
    return tx.intent == "refund" or bool(REFUND_WORDS.search(tx.title_folded))


def _duplicate_refund_layer(ctx: MatchContext, tx: Tx, amount_abs: int) -> tuple[list[Candidate], Tx | None]:
    out: list[Candidate] = []
    original = _earlier_identical(ctx, tx)
    if original is not None:
        cand = Candidate("related", subtype="duplicate", items=[Item(
            related_transaction_id=original.id, amount_minor=amount_abs, difference_kind="overpayment",
            difference_minor=amount_abs, label=original.label, document_amount_minor=amount_abs, currency=tx.currency)])
        cand.add("duplicate_of_earlier", f"identyczna wcześniejsza wpłata {original.label} z "
                                         f"{original.day.isoformat()} (ten sam tytuł i kwota) - możliwy duplikat")
        cand.add("amount_exact", "kwota co do grosza")
        cand.add("counterparty_strong", "ten sam kontrahent")
        cand.blockers.append("możliwy duplikat - zawsze kolejka")
        out.append(cand)
    # a later opposite transfer that returns this amount
    for later in sorted(ctx.txs.values(), key=lambda o: (o.day, o.label)):
        if later.id == tx.id or later.inflow == tx.inflow or later.abs_minor != tx.abs_minor or later.op_type == "own":
            continue
        if not (0 < (later.day - tx.day).days <= W.REFUND_WINDOW) or not _same_party(later, tx) or not _is_refund(later):
            continue
        if ctx.related_used.get(later.id, 0) >= later.abs_minor:
            continue
        dated = tx.day in later.title_dates or tx.booking_date in later.title_dates
        if not dated and original is None:
            continue                    # a plain payment: the refund links itself from its side
        cand = Candidate("related", subtype="duplicate" if original else "refund", items=[Item(
            related_transaction_id=later.id, amount_minor=amount_abs, difference_kind="overpayment",
            difference_minor=amount_abs, label=later.label, document_amount_minor=amount_abs, currency=tx.currency)])
        cand.add("refunded_later", f"zwrot tej kwoty {later.label} z {later.day.isoformat()}")
        if dated:
            cand.add("refund_date_in_title", f"tytuł zwrotu wskazuje datę tej wpłaty ({tx.day.isoformat()})")
        if original is not None:
            cand.add("duplicate_of_earlier", f"identyczna wcześniejsza wpłata {original.label} - możliwy duplikat")
        cand.add("amount_exact", "kwota co do grosza")
        cand.add("counterparty_strong", "ten sam kontrahent")
        cand.blockers.append("zwrot / duplikat - zawsze kolejka")
        out.append(cand)
        break
    # this transfer is a refund: find the original payment
    if _is_refund(tx):
        originals = [o for o in ctx.txs.values()
                     if o.id != tx.id and o.inflow != tx.inflow and o.abs_minor == tx.abs_minor and o.op_type != "own"
                     and 0 < (tx.day - o.day).days <= W.REFUND_WINDOW and _same_party(o, tx)
                     and ctx.related_used.get(o.id, 0) < o.abs_minor]
        for o in sorted(originals, key=lambda o: (o.day, o.label), reverse=True)[:3]:
            dup_of = _earlier_identical(ctx, o)
            cand = Candidate("related", subtype="refund", items=[Item(
                related_transaction_id=o.id, amount_minor=amount_abs,
                difference_kind="overpayment" if dup_of else None, difference_minor=-amount_abs if dup_of else None,
                label=o.label, document_amount_minor=amount_abs, currency=tx.currency)])
            cand.add("refund_intent", "tytuł mówi o zwrocie" + (" nadpłaty" if dup_of else ""))
            if o.day in tx.title_dates or o.booking_date in tx.title_dates:
                cand.add("refund_date_in_title", f"tytuł wskazuje datę pierwotnej wpłaty {o.day.isoformat()}")
            if dup_of is not None:
                cand.add("duplicate_of_earlier", f"pierwotna wpłata {o.label} to możliwy duplikat wpłaty {dup_of.label}")
            cand.add("amount_exact", "kwota co do grosza")
            cand.add("counterparty_strong", f"ten sam kontrahent co wpłata {o.label} z {o.day.isoformat()}")
            cand.blockers.append("zwrot - zawsze kolejka")
            out.append(cand)
    return out, original


def _record_layer(ctx: MatchContext, tx: Tx, amount_abs: int) -> list[Candidate]:
    out = []
    persons, phones = tx.persons, tx.phones
    title_dates = tx.title_dates
    title_tokens = set(name_tokens(tx.title))
    for record in ctx.records.values():
        if record.currency != tx.currency or record.open_minor != amount_abs or record.record_date is None:
            continue
        delta = (tx.day - record.record_date).days
        if not -W.RECORD_WINDOW_BEFORE <= delta <= W.RECORD_WINDOW_AFTER:
            continue
        person = record.person_pseudonym is not None and record.person_pseudonym in persons
        phone = bool(record.phones & phones)
        if (persons or phones) and not (person or phone):
            continue                    # another person: never a candidate
        cand = Candidate("internal_record", items=[Item(
            internal_record_id=record.id, amount_minor=amount_abs, document_amount_minor=amount_abs,
            currency=record.currency, label=record.label)])
        cand.add("internal_record", f"arkusz {record.label}: {record.cost_object_ref or '-'}, "
                                    f"{record.record_date.isoformat()}, {money(record.amount_minor or 0)}", weight=0.0)
        if person:
            cand.add("person", f"ta sama osoba w arkuszu ({record.person_pseudonym})")
        if phone:
            cand.add("phone", f"telefon BLIK = telefon z arkusza ({', '.join(sorted(record.phones & phones))})")
        cand.add("amount_exact", "kwota co do grosza")
        if record.record_date in title_dates:
            cand.add("record_date_in_title", f"data z arkusza ({record.record_date.isoformat()}) w tytule")
        if abs(delta) <= W.RECORD_DATE_CLOSE:
            cand.add("record_date_close", f"data w arkuszu ±3 dni od przelewu ({_days(delta)})")
        label_tokens = set(name_tokens(record.cost_object_ref))
        if label_tokens and label_tokens <= title_tokens:
            cand.add("record_label_in_title", f"„{record.cost_object_ref}” w tytule")
        cand.blockers.append("arkusz PM nie jest twardym dowodem, dopóki nie dojrzeje reguła (D2)")
        out.append(cand)
    return out


def _tx_party_kind(tx: Tx) -> str | None:
    return tx.cp.kind if tx.cp else None


def _no_document_layer(ctx: MatchContext, tx: Tx) -> list[Candidate]:
    """'No document' is its own hypothesis with a subtype (briefing, step 4), not 'nothing found'."""
    out: list[Candidate] = []
    kind = _tx_party_kind(tx)
    title = tx.title_folded
    builtin = None
    if tx.op_type == "zus" and (kind in ("institution", None) or "zaklad ubezpieczen" in fold(tx.counterparty_name or "")):
        builtin = ("zus", "składka ZUS (rachunek / nazwa ZUS i typ operacji)")
    elif tx.op_type == "tax" and (kind in ("institution", None) or tx.extracted.get("tax")):
        form = (tx.extracted.get("tax") or {}).get("form")
        builtin = ("tax", "przelew do urzędu skarbowego" + (f" ({form})" if form else "") + " - typ operacji tax")
    elif tx.op_type in ("fee", "interest") and kind in ("bank", None):
        subtype = "interest" if tx.op_type == "interest" else "fee"
        builtin = (subtype, ("odsetki" if subtype == "interest" else "prowizja / opłata bankowa")
                   + " - operacja banku bez kontrahenta")
    if builtin is not None:
        cand = Candidate("no_document", final_status="no_document", subtype=builtin[0])
        cand.add("builtin_no_document", builtin[1])
        if tx.sensitive:
            cand.blockers.append("słowo wrażliwe - kategorię nadaje człowiek")
        else:
            cand.hard, cand.rule_key = True, "builtin:no_document_zus_tax_fee"
        return [cand]
    intent = tx.intent
    if not tx.inflow and kind == "person" and (intent == "salary" or SALARY_WORDS.search(title)):
        cand = Candidate("no_document", final_status="no_document", subtype="salary")
        if intent == "salary":
            cand.add("no_document_intent", "intencja tytułu: wynagrodzenie")
        if SALARY_WORDS.search(title):
            cand.add("no_document_keyword", "w tytule: wynagrodzenie / umowa")
        cand.add("no_document_person", "odbiorca to osoba")
        out.append(cand)
    if intent == "advance" or ADVANCE_WORDS.search(title):
        cand = Candidate("no_document", final_status="no_document", subtype="advance",
                         awaiting_until=tx.booking_date + timedelta(days=W.ADVANCE_AWAIT_DAYS))
        if intent == "advance":
            cand.add("no_document_intent", "intencja tytułu: zaliczka")
        if ADVANCE_WORDS.search(title):
            cand.add("no_document_keyword", "w tytule: zaliczka")
        cand.add("awaiting", f"czeka na fakturę zaliczkową do {cand.awaiting_until.isoformat()} (180 dni)", weight=0.0)
        out.append(cand)
    if intent == "deposit" or DEPOSIT_WORDS.search(title):
        cand = Candidate("no_document", final_status="no_document", subtype="deposit")
        cand.add("no_document_keyword", "kaucja / depozyt")
        out.append(cand)
    if tx.op_type == "card" and not tx.inflow:
        cand = Candidate("no_document", final_status="no_document", subtype="card_awaiting",
                         awaiting_until=tx.booking_date + timedelta(days=W.CARD_AWAIT_DAYS))
        cand.add("no_document_card", f"płatność kartą - czeka na fakturę do {cand.awaiting_until.isoformat()} (30 dni)")
        out.append(cand)
    if intent == "rent" or RENT_WORDS.search(title):
        cand = Candidate("no_document", final_status="no_document", subtype=None)
        cand.add("no_document_intent", "czynsz bez faktury w danych", weight=0.5)
        out.append(cand)
    return out


def _fallbacks(ctx: MatchContext, tx: Tx, candidates: list[Candidate]) -> list[Candidate]:
    out = []
    has_match = any(c.is_match for c in candidates)
    has_no_document = any(c.kind == "no_document" for c in candidates)
    person_outflow = not tx.inflow and _tx_party_kind(tx) == "person"
    if not has_match and person_outflow and tx.op_type in ("blik", "instant") and not SALARY_WORDS.search(tx.title_folded):
        cand = Candidate("to_clarify", final_status="to_clarify")
        cand.add("to_clarify_blik_person", "przelew na telefon / natychmiastowy do osoby bez wiersza w arkuszu "
                                           "i bez dokumentu - do wyjaśnienia")
        out.append(cand)
    if not has_no_document:
        cand = Candidate("no_document", final_status="no_document", subtype=None)
        cand.add("no_document_fallback", "brak dokumentu w danych - podtyp do ustalenia")
        if tx.sensitive:
            cand.add("sensitive", "słowo wrażliwe w tytule - kategorię nadaje człowiek", weight=0.0)
        out.append(cand)
    return out


def _semantics(tx: Tx, candidates: list[Candidate]) -> None:
    intent = tx.intent
    if not intent or intent == "unknown":
        return
    for cand in candidates:
        if cand.kind in ("document", "internal_record"):
            if intent in DOC_INTENTS:
                cand.add("intent_agrees", f"intencja tytułu (model): {intent}")
            elif intent in CONFLICT_INTENTS:
                cand.add("intent_conflicts", f"intencja tytułu (model) nie pasuje: {intent}")
    period = (tx.semantics or {}).get("entities", {}).get("period")
    if period:
        for cand in candidates:
            if cand.kind == "no_document" and cand.subtype in ("salary", "zus", "tax"):
                cand.add("period", f"okres w tytule: {period}", weight=0.3)


def _title_names_other(ctx: MatchContext, tx: Tx, candidates: list[Candidate], named: set[UUID]) -> None:
    if not named:
        return
    for cand in candidates:
        if cand.kind != "document":
            continue
        docs = {i.document_id for i in cand.items}
        heads = docs | {ctx.docs[d].corrects_id for d in docs if d in ctx.docs and ctx.docs[d].corrects_id}
        if not heads & named:
            cand.add("title_names_other_invoice", "tytuł wskazuje inną fakturę")
            cand.blockers.append("tytuł wskazuje inną fakturę")


def _merge_linked_evidence(candidates: list[Candidate]) -> list[Candidate]:
    """One cost, many proofs: a PM sheet row for the same amount as a document candidate is evidence, not a rival."""
    docs = [c for c in candidates if c.kind == "document" and c.hard]
    if not docs:
        return candidates
    kept = []
    for cand in candidates:
        if cand.kind == "internal_record" and cand.has("person") and \
                any(sum(i.amount_minor for i in d.items) == cand.items[0].amount_minor for d in docs):
            for d in docs:
                d.linked_evidence.append({"internal_record_id": str(cand.items[0].internal_record_id),
                                          "label": cand.items[0].label})
                d.add("linked_evidence", f"ten sam koszt także w arkuszu: {cand.items[0].label}", weight=0.0)
            continue
        kept.append(cand)
    return kept


def _learned_rules(conn: psycopg.Connection, ctx: MatchContext, tx: Tx, candidates: list[Candidate]) -> None:
    """Layer 5: rules learned from decisions (4C). Missing module = the layer is skipped."""
    try:
        module = importlib.import_module("kasa.core.rules.apply")
    except ImportError:
        return
    applicable = getattr(module, "applicable_rules", None)
    if applicable is None:
        return
    facts = {
        "counterparty_id": str(tx.counterparty_id) if tx.counterparty_id else None, "op_type": tx.op_type,
        "title": tx.title, "amount_minor": tx.amount_minor, "direction": "in" if tx.inflow else "out",
        "intent": tx.intent, "counterparty_kind": _tx_party_kind(tx), "counterparty_iban": tx.counterparty_iban,
        "transaction_id": str(tx.id),
    }
    try:
        hits = applicable(conn, ctx.company_id, "transaction", facts) or []
    except Exception:  # noqa: BLE001 - a broken optional layer must not stop matching
        return
    for hit in hits:
        rule_key = getattr(hit, "rule_key", None) or (hit.get("rule_key") if isinstance(hit, dict) else None)
        if not rule_key or str(rule_key).startswith("builtin:"):
            continue                         # built-in rules are applied in code, above
        get = (lambda k, h=hit: getattr(h, k, None) if not isinstance(h, dict) else h.get(k))
        stage, action, version = get("stage"), get("action") or {}, get("version")
        weight = {"hint": W.WEIGHTS["rule_hint"], "auto": W.WEIGHTS["rule_auto"]}.get(stage, W.WEIGHTS["rule_shadow"])
        tag = f"rule:{rule_key}@{version}"
        fact = f"reguła {rule_key} (wersja {version}, etap {stage}, {get('evidence_count') or 0} przypadków)"
        rule = {"id": get("rule_id"), "version": version, "rule_key": rule_key, "stage": stage}
        if action.get("kind") == "final_status":
            target = next((c for c in candidates if c.final_status == action.get("final_status")
                           and c.subtype == action.get("subtype")), None)
            if target is None:
                target = Candidate("no_document" if action.get("final_status") == "no_document" else "to_clarify",
                                   final_status=action.get("final_status") or "no_document",
                                   subtype=action.get("subtype"))
                candidates.append(target)
            target.add(tag, fact, weight=weight)
            if stage == "auto":
                target.rule, target.hard = rule, True
        elif action.get("kind") == "match_strategy":
            for cand in candidates:
                if cand.kind == "document":
                    cand.add(tag, fact, weight=weight / 2)
                    if stage == "auto" and cand.has("amount_exact"):
                        cand.rule, cand.hard = rule, True


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def rank(candidates: list[Candidate], params: dict[str, float]) -> list[Candidate]:
    """Score, deduplicate, order; a tie between two matches puts "to clarify" first. At most 3."""
    best: dict[tuple, Candidate] = {}
    for cand in candidates:
        cand.compute(params)
        key = cand.key()
        if key not in best or cand.score > best[key].score:
            best[key] = cand
    ordered = sorted(best.values(), key=lambda c: (-round(c.score, 6), MATCH_ORDER.get(c.kind, 9),
                                                   [str(i.target[1]) for i in c.items]))
    for cand in ordered:
        cand.tie, cand.margin = False, 0.0
    matches = [c for c in ordered if c.is_match]
    if len(matches) >= 2 and matches[0].score - matches[1].score < W.TIE_MARGIN and ordered[0] is matches[0]:
        a, b = matches[0], matches[1]
        a.tie = b.tie = True
        clarify = Candidate("to_clarify", final_status="to_clarify")
        clarify.add("tie", f"remis: {_describe(a)} i {_describe(b)} - niejednoznaczne, decyduje człowiek", weight=0.0)
        clarify.compute(params)
        clarify.score, clarify.raw, clarify.tie = a.score, a.raw, True
        ordered = [clarify, a, b] + [c for c in ordered if c is not a and c is not b and c.kind != "to_clarify"]
    top = ordered[: W.MAX_SUGGESTIONS]
    for index, cand in enumerate(top):
        nxt = top[index + 1].score if index + 1 < len(top) else 0.0
        cand.margin = max(0.0, cand.score - nxt)
    if top and len(top) > 1 and top[0].score - top[1].score < W.TIE_MARGIN:
        top[0].tie = True
    return top


MATCH_ORDER = {"own_transfer": 0, "document": 1, "internal_record": 2, "related": 3, "no_document": 4, "to_clarify": 5}


def _describe(cand: Candidate) -> str:
    labels = ", ".join(i.label for i in cand.items if i.label) or cand.kind
    amount = sum(i.amount_minor for i in cand.items)
    return f"{labels} ({money(amount)})"


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


def _reserve(ctx: MatchContext, cand: Candidate) -> None:
    """A hard rank-1 candidate claims its targets for the rest of the run (chronological order)."""
    for item in cand.items:
        side = item.document_amount_minor if item.document_amount_minor is not None else item.amount_minor
        if item.document_id and item.document_id in ctx.docs:
            ctx.docs[item.document_id].matched_minor += side
        elif item.internal_record_id and item.internal_record_id in ctx.records:
            ctx.records[item.internal_record_id].matched_minor += side
        elif item.related_transaction_id:
            ctx.related_used[item.related_transaction_id] = ctx.related_used.get(item.related_transaction_id, 0) + side


def mpp_group(ctx: MatchContext, tx: Tx) -> list[Tx]:
    """Legs of the split payment of ``tx`` that are settled together: main first, then the VAT leg(s)."""
    if not tx.mpp_group_id or tx.mpp_role != "main":
        return [tx]
    legs = [t for t in ctx.mpp_legs.get(tx.mpp_group_id, []) if t.mpp_role in ("main", "vat_leg")]
    legs.sort(key=lambda t: (t.mpp_role != "main", t.label))
    return legs or [tx]


def is_subject(ctx: MatchContext, tx: Tx) -> bool:
    """A VAT leg follows its main leg (one occurrence, one decision); it is never a subject of its own.

    A rebooking between the VAT account and the current account (``vat_rebooking``) is an own
    transfer (briefing: "przeksięgowanie = transfer własny") and is decided on its own.
    """
    if tx.mpp_group_id and tx.mpp_role == "vat_leg":
        return not any(t.mpp_role == "main" for t in ctx.mpp_legs.get(tx.mpp_group_id, []))
    return True


def candidates_for(conn: psycopg.Connection, ctx: MatchContext, tx: Tx) -> tuple[list[Candidate], list[Tx], int]:
    group = mpp_group(ctx, tx)
    amount_abs = sum(t.abs_minor for t in group)
    own = _own_transfer(ctx, tx)
    if own:
        candidates = own
    else:
        candidates, named = _hard_identifier_layer(ctx, tx, amount_abs)
        have_exact = any(c.kind == "document" and c.has("amount_exact") for c in candidates)
        candidates += _counterparty_layer(ctx, tx, amount_abs, named | {i.document_id for c in candidates
                                                                         for i in c.items if i.document_id},
                                          have_exact)
        if not any(c.kind == "document" and c.hard for c in candidates):
            taken = {i.document_id for c in candidates for i in c.items if i.document_id}
            candidates += _amount_layer(ctx, tx, amount_abs, named | taken)
        related, original = _duplicate_refund_layer(ctx, tx, amount_abs)
        if original is not None:
            for cand in candidates:
                if cand.kind == "document":
                    cand.add("possible_duplicate", f"możliwy duplikat wpłaty {original.label} z "
                                                   f"{original.day.isoformat()}")
                    cand.hard = False
                    cand.blockers.append("możliwy duplikat - zawsze kolejka")
        candidates += related
        candidates += _record_layer(ctx, tx, amount_abs)
        _title_names_other(ctx, tx, candidates, named)
        candidates += _no_document_layer(ctx, tx)
        _semantics(tx, candidates)
        _learned_rules(conn, ctx, tx, candidates)
        candidates = _merge_linked_evidence(candidates)
        candidates += _fallbacks(ctx, tx, candidates)
    for cand in candidates:
        if tx.sensitive and cand.hard:
            cand.hard = False
            cand.blockers.append("słowo wrażliwe w tytule - decyduje człowiek")
        if cand.blockers:
            cand.hard = False
    return candidates, group, amount_abs


def proposal_key(proposal: dict[str, Any]) -> str:
    """Canonical form of a proposal (idempotency: the same proposal is never written twice)."""
    return json.dumps(proposal, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_params(conn: psycopg.Connection, company_id: UUID) -> dict[str, float]:
    """Parameters chosen by the latest calibration run (``calibration_result``), else the defaults."""
    params = dict(W.DEFAULT_PARAMS)
    row = conn.execute(
        """
        SELECT params FROM calibration_result
         WHERE company_id = %s AND kind = 'match' AND chosen ORDER BY run_at DESC, id LIMIT 1
        """,
        (company_id,),
    ).fetchone() if conn.execute("SELECT to_regclass('calibration_result') IS NOT NULL").fetchone()[0] else None
    if row and isinstance(row[0], dict):
        for key in params:
            if isinstance(row[0].get(key), int | float):
                params[key] = float(row[0][key])
    params["auto_threshold"] = max(params["auto_threshold"], W.MIN_AUTO_THRESHOLD)
    params["margin_min"] = max(params["margin_min"], W.MIN_MARGIN)
    return params


def evaluate(conn: psycopg.Connection, company_id: UUID, *, period: tuple[date, date] | None = None,
             subject_ids: set[UUID] | None = None, include_decided: bool = False,
             params: dict[str, float] | None = None) -> Evaluation:
    """Candidates for every undecided booked transaction (or ``subject_ids``), in chronological order.

    Nothing is written. ``include_decided`` evaluates decided subjects as if undecided (calibration).
    """
    exclude = None
    if include_decided and subject_ids:
        # a decided split payment: every leg's matches are left out, not only the subject's
        exclude = set(subject_ids) | {row[0] for row in conn.execute(
            """
            SELECT id FROM l1_bank_transaction WHERE mpp_group_id IN (
                   SELECT mpp_group_id FROM l1_bank_transaction WHERE id = ANY(%s) AND mpp_group_id IS NOT NULL)
            """,
            (list(subject_ids),),
        ).fetchall()}
    ctx = load_context(conn, company_id, exclude_matches_of=exclude)
    params = params or load_params(conn, company_id)
    subjects = []
    for tx in ctx.txs.values():
        if subject_ids is not None and tx.id not in subject_ids:
            continue
        if period and not (period[0] <= tx.booking_date <= period[1]):
            continue
        if tx.id in ctx.decided or not is_subject(ctx, tx) or tx.amount_minor == 0:
            continue
        subjects.append(tx)
    subjects.sort(key=lambda t: (t.day, t.booking_date, t.label))
    results = []
    for tx in subjects:
        considered, group, amount = candidates_for(conn, ctx, tx)
        rejected = ctx.rejected.get(tx.id)
        if rejected:
            considered = [c for c in considered if proposal_key(c.proposal()) not in rejected]
        ranked = rank(considered, params)
        results.append(TxResult(tx, ranked, considered, group, amount))
        if ranked and ranked[0].hard and not ranked[0].tie:
            _reserve(ctx, ranked[0])
    return Evaluation(ctx, results, params)
