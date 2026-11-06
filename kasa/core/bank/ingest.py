"""Bank ingestion: provider -> L0 (raw) -> intermediate model -> L1 facts.

``ingest_account(conn, company, provider, account, date_from, date_to)``:

1. every response is stored in ``l0_raw`` (channel ``bank``, meta = endpoint + range);
2. parsed to :class:`~kasa.core.bank.model.BankEntry` / ``BankBalance``;
3. written to ``l1_bank_account`` (upsert by IBAN, VAT account -> its current
   account), ``l1_bank_balance`` and ``l1_bank_transaction`` with the wide record of
   the briefing: identifiers, fingerprint, dates, signed amount, FX, balance after
   (from the bank, or computed from the opening balance and marked so), raw
   counterparty and title, ``extracted`` (code only), ``op_type``, status, the
   pseudonymized title/counterparty/IBAN, ``sensitive``, ``counterparty_id``;
4. split payment legs are glued into one occurrence (``mpp_group_id``/``mpp_role``);
5. repeating the same range changes nothing; a new ``PARSER_SCHEMA_VERSION`` adds a
   new row per transaction and points the old one at it (``superseded_by``).

``import_statement_file`` does steps 1-5 for camt.053 / MT940 files (fallback
channels, decision D4).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.bank import camt053, mt940
from kasa.core.bank import enablebanking as eb
from kasa.core.bank.counterparty import CounterpartyFact, resolve_counterparty
from kasa.core.bank.extract import classify_op_type, extract_title, fx_note
from kasa.core.bank.fx import rate_before
from kasa.core.bank.model import BankBalance, BankEntry, ParsedStatement
from kasa.core.companies import Company
from kasa.core.extension import ExtensionError, get_extension
from kasa.core.normalize import from_minor, normalize_iban, normalize_nip, to_minor
from kasa.core.providers.bank import AccountInfo, BankProvider
from kasa.core.pseudonym import (
    PseudonymContext,
    fold,
    load_context,
    pseudonym_for,
    pseudonymize_text,
    register_person,
    sensitive_hits,
)
from kasa.core.raw_store import RawRecord, store_raw

log = logging.getLogger(__name__)

# Bump when parsing or extraction changes: the next ingestion writes new fact
# rows (schema_version = this) and supersedes the old ones.
# 2: extracted.phones keeps only the pseudonym (no plain number in the facts).
# 3: extracted.payer_hint (a company with its legal form in the title of an operator's payment); `sensitive`
#    from the title AND the counterparty name with the list of the semantics gate (database terms too).
# 4: the balance after an operation from the bank gets its sign back when the bank sends it without one (mBank
#    through EnableBanking on an account below zero); an interim booked balance (ITBD) of the day of the request
#    anchors the computed balances when the bank gives no dated balance (Bank Millennium).
PARSER_SCHEMA_VERSION = 4
CREATED_BY = "kasa.bank"
MPP_GROUP_NAMESPACE = uuid.UUID("6b617361-6d70-7000-8000-000000000001")
MPP_DATE_TOLERANCE = timedelta(days=1)


@dataclass
class IngestResult:
    account_iban: str
    source: str
    raw_created: int = 0
    raw_existing: int = 0
    booked: int = 0
    pending: int = 0
    inserted: int = 0
    unchanged: int = 0
    superseded: int = 0
    pending_resolved: int = 0
    balances_stored: int = 0
    mpp_legs_grouped: int = 0
    first_booking: date | None = None
    last_booking: date | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.account_iban}: zaksięgowane {self.booked}, oczekujące {self.pending}; "
                f"nowe {self.inserted}, bez zmian {self.unchanged}, nowa wersja {self.superseded}; "
                f"surowe nowe {self.raw_created}/istniejące {self.raw_existing}; salda {self.balances_stored}")


@dataclass(frozen=True)
class AccountRow:
    id: UUID
    iban: str
    currency: str
    kind: str
    bank: str | None


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def upsert_account(conn: psycopg.Connection, company_id: UUID, info: AccountInfo, *,
                   source: str | None = None) -> AccountRow:
    """Insert the account (by IBAN + currency) or fill its missing attributes; link VAT accounts."""
    iban = normalize_iban(info.iban)
    row = conn.execute(
        """
        INSERT INTO l1_bank_account (company_id, iban, currency, bank, kind, name, provider, external_id, bic,
                                     owner_name, source, method, confidence, evidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'code', 1, %s, %s)
        ON CONFLICT (company_id, iban, currency) DO NOTHING
        RETURNING id
        """,
        (company_id, iban, info.currency, info.bank_name, info.kind, info.details or info.product, info.provider,
         info.uid, info.bic, info.owner_name, source or info.provider,
         Jsonb({"product": info.product, "details": info.details, "kind_from": "provider"}), CREATED_BY),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            UPDATE l1_bank_account
               SET bank = coalesce(bank, %s), name = coalesce(name, %s), provider = coalesce(provider, %s),
                   external_id = coalesce(external_id, %s), bic = coalesce(bic, %s),
                   owner_name = coalesce(owner_name, %s)
             WHERE company_id = %s AND iban = %s AND currency = %s
               AND (bank IS NULL OR name IS NULL OR provider IS NULL OR external_id IS NULL OR bic IS NULL
                    OR owner_name IS NULL)
            """,
            (info.bank_name, info.details or info.product, info.provider, info.uid, info.bic, info.owner_name,
             company_id, iban, info.currency),
        )
    _link_vat_accounts(conn, company_id)
    return get_account(conn, company_id, iban, info.currency)


def _link_vat_accounts(conn: psycopg.Connection, company_id: UUID) -> None:
    """A VAT account belongs to the only PLN current account of the same bank."""
    conn.execute(
        """
        UPDATE l1_bank_account v
           SET parent_account_id = c.id
          FROM l1_bank_account c
         WHERE v.company_id = %(c)s AND v.kind = 'vat' AND v.parent_account_id IS NULL
           AND c.company_id = v.company_id AND c.kind = 'current' AND c.currency = 'PLN'
           AND c.bic IS NOT DISTINCT FROM v.bic
           AND (SELECT count(*) FROM l1_bank_account x
                 WHERE x.company_id = v.company_id AND x.kind = 'current' AND x.currency = 'PLN'
                   AND x.bic IS NOT DISTINCT FROM v.bic) = 1
        """,
        {"c": company_id},
    )


def get_account(conn: psycopg.Connection, company_id: UUID, iban: str, currency: str | None = None) -> AccountRow:
    row = conn.execute(
        """
        SELECT id, iban, currency, kind, bank FROM l1_bank_account
         WHERE company_id = %s AND iban = %s AND (%s::text IS NULL OR currency = %s)
         ORDER BY currency LIMIT 1
        """,
        (company_id, normalize_iban(iban), currency, currency),
    ).fetchone()
    if row is None:
        raise LookupError(f"rachunek {iban} nie jest zarejestrowany")
    return AccountRow(row[0], row[1], row[2].strip(), row[3], row[4])


def register_accounts(conn: psycopg.Connection, company: Company, accounts: Sequence[AccountInfo]) -> list[AccountRow]:
    rows = [upsert_account(conn, company.id, info) for info in accounts]
    owner = next((a.owner_name for a in accounts if a.owner_name), None)
    conn.execute("SELECT kasa_own_counterparty(%s, %s, %s)", (company.id, owner, _company_nip(conn, company.id)))
    return rows


def _own_ibans(conn: psycopg.Connection, company_id: UUID) -> set[str]:
    return {r[0] for r in conn.execute("SELECT iban FROM l1_bank_account WHERE company_id = %s", (company_id,))}


def _company_nip(conn: psycopg.Connection, company_id: UUID) -> str | None:
    """Our NIP: from the own counterparty, else company.nip (added by document ingestion), else unknown."""
    row = conn.execute(
        "SELECT nip FROM l1_counterparty WHERE company_id = %s AND kind = 'own' AND superseded_by IS NULL",
        (company_id,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    has_column = conn.execute(
        """
        SELECT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_schema = current_schema() AND table_name = 'company' AND column_name = 'nip')
        """
    ).fetchone()[0]
    if has_column:
        row = conn.execute("SELECT nip FROM company WHERE id = %s", (company_id,)).fetchone()
        if row and row[0]:
            return row[0]
    return None


def _infer_own_nip(entries: Iterable[BankEntry], extracted: dict[int, dict[str, Any]]) -> tuple[str | None, str]:
    """Our NIP as the bank data shows it: /TI/N<nip> of our tax payments, or the IDC of incoming MPP."""
    tax: Counter[str] = Counter()
    mpp: Counter[str] = Counter()
    for entry in entries:
        data = extracted.get(id(entry)) or {}
        if entry.amount_minor < 0 and (data.get("tax") or {}).get("id_type") == "N":
            try:
                tax[normalize_nip(data["tax"]["id"])] += 1
            except ValueError:
                pass
        if entry.amount_minor > 0 and (data.get("mpp") or {}).get("idc_nip"):
            mpp[data["mpp"]["idc_nip"]] += 1
    if tax:
        return tax.most_common(1)[0][0], "tax_title"
    if mpp and mpp.most_common(1)[0][1] >= 2:
        return mpp.most_common(1)[0][0], "incoming_mpp"
    return None, ""


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def store_balances(conn: psycopg.Connection, company_id: UUID, account: AccountRow, balances: Iterable[BankBalance],
                   raw_id: UUID | None) -> int:
    stored = 0
    for balance in balances:
        current = conn.execute(
            """
            SELECT id, amount_minor FROM l1_bank_balance
             WHERE account_id = %s AND balance_type = %s AND reference_date = %s AND source = %s
               AND superseded_by IS NULL
            """,
            (account.id, balance.balance_type, balance.reference_date, balance.source),
        ).fetchone()
        if current is not None and current[1] == balance.amount_minor:
            continue
        new_id = conn.execute(
            """
            INSERT INTO l1_bank_balance (company_id, account_id, balance_type, reference_date, amount_minor, currency,
                                         name, last_change_at, last_committed_transaction, raw_id, source, method,
                                         confidence, evidence, schema_version, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'code', 1, %s, %s, %s)
            RETURNING id
            """,
            (company_id, account.id, balance.balance_type, balance.reference_date, balance.amount_minor,
             balance.currency, balance.name, balance.last_change_at, balance.last_committed_transaction, raw_id,
             balance.source, Jsonb({"raw_id": str(raw_id) if raw_id else None, "locator": balance.locator}),
             PARSER_SCHEMA_VERSION, CREATED_BY),
        ).fetchone()[0]
        if current is not None:
            conn.execute("UPDATE l1_bank_balance SET superseded_by = %s WHERE id = %s", (new_id, current[0]))
        stored += 1
    return stored


def _fill_balances(entries: list[BankEntry], balances: Sequence[BankBalance], date_from: date | None,
                   date_to: date | None, warnings: list[str]) -> None:
    """Balance after each booked entry: from the bank when given, otherwise computed and marked.

    Forward from the opening balance (OPBD of the first day), or backward from the
    closing balance (CLBD at or after the last booking) when no opening is known.
    """
    # stable sort: across days by booking date, within a day in the bank's order
    booked = sorted((e for e in entries if e.status == "booked"), key=lambda e: e.booking_date)
    if not booked:
        return
    first_day = min(e.booking_date for e in booked)
    last_day = max(e.booking_date for e in booked)
    _restore_balance_signs(booked, _closing_anchor(balances, last_day, None), warnings)
    # OPBD = at the start of its day; PRCD/CLBD = at the end of its day
    starts = {first_day, date_from} - {None}
    opening = next((b.amount_minor for b in balances if b.balance_type == "OPBD" and b.reference_date in starts),
                   None)
    if opening is None:
        before = [b for b in balances if b.balance_type in ("PRCD", "CLBD") and b.reference_date < first_day
                  and (date_from is None or b.reference_date >= date_from - timedelta(days=1))]
        if before:
            opening = max(before, key=lambda b: b.reference_date).amount_minor
    if opening is None and booked[0].balance_after_minor is not None:
        opening = booked[0].balance_after_minor - booked[0].amount_minor
    if opening is not None:
        running = opening
        for entry in booked:
            if entry.balance_after_minor is not None:
                if running + entry.amount_minor != entry.balance_after_minor:
                    warnings.append(
                        f"{entry.entry_reference or entry.locator}: saldo po operacji z banku "
                        f"{entry.balance_after_minor} ≠ wyliczone {running + entry.amount_minor}")
                running = entry.balance_after_minor
            else:
                running += entry.amount_minor
                entry.balance_after_minor = running
                entry.balance_after_computed = True
        return
    running = _closing_anchor(balances, last_day, date_to)
    if running is None:
        warnings.append(f"{booked[0].account_iban}: brak salda otwarcia i zamknięcia - saldo po operacji nieznane")
        return
    for entry in reversed(booked):
        if entry.balance_after_minor is None:
            entry.balance_after_minor = running
            entry.balance_after_computed = True
        running = entry.balance_after_minor - entry.amount_minor


def _closing_anchor(balances: Sequence[BankBalance], last_day: date, date_to: date | None) -> int | None:
    """Balance after the last booked operation: the bank's closing balance (CLBD/CLAV) on or after the last booking
    day (within the range), else its interim booked balance (ITBD) of the day of the request, which covers every
    operation booked until then (the fetch reaches that day when the bank gives no dated balance)."""
    closing = [b for b in balances if b.balance_type in ("CLBD", "CLAV") and b.reference_date >= last_day
               and (date_to is None or b.reference_date <= date_to)]
    if closing:
        return min(closing, key=lambda b: b.reference_date).amount_minor
    interim = [b for b in balances if b.balance_type == "ITBD" and b.reference_date >= last_day]
    if interim:
        return max(interim, key=lambda b: b.reference_date).amount_minor
    return None


def _restore_balance_signs(booked: Sequence[BankEntry], anchor: int | None, warnings: list[str]) -> None:
    """mBank through EnableBanking sends the balance after an operation without its sign: below zero the account
    shows ``47371.45`` where the balance is ``-47371.45``. Of the two signs, the bank's is the one that links the
    operation to the chain of balances, whatever the order of the operations within a day:

    1. the balance before the operation (after - amount) is the balance after another operation - with exactly one
       of the two signs (the absolute values are compared, the other operation's sign is not known yet);
    2. otherwise (the first operation, or both signs link) the sign that continues the chain walked back from the
       anchor (the closing or interim booked balance after the last operation), or forward from the first balance
       as given when there is no anchor.

    A restored sign is noted in ``extra.balance_after_sign`` of the entry."""
    given = [e for e in booked if e.balance_after_minor is not None and not e.balance_after_computed]
    if not given:
        return
    magnitudes = Counter(abs(e.balance_after_minor) for e in given)
    decided: dict[int, int] = {}
    for entry in given:
        value = entry.balance_after_minor
        if value == 0:
            decided[id(entry)] = 0
            continue
        links = [sign for sign in (1, -1) if magnitudes[abs(sign * abs(value) - entry.amount_minor)]
                 - (1 if abs(sign * abs(value) - entry.amount_minor) == abs(value) else 0) > 0]
        if len(links) == 1:
            decided[id(entry)] = links[0] * abs(value)

    expected: int | None = anchor
    backward = anchor is not None
    restored = 0
    for entry in (reversed(booked) if backward else iter(booked)):
        value = entry.balance_after_minor
        if value is None or entry.balance_after_computed:
            if expected is not None:
                expected = expected - entry.amount_minor if backward else expected + entry.amount_minor
            continue
        chosen = decided.get(id(entry))
        if chosen is None:
            target = expected if backward else (None if expected is None else expected + entry.amount_minor)
            chosen = value if target is None else min((value, -value), key=lambda v: abs(v - target))
        if chosen != value:
            entry.balance_after_minor = chosen
            entry.extra = {**entry.extra, "balance_after_sign": "restored"}
            restored += 1
        expected = chosen - entry.amount_minor if backward else chosen
    if restored:
        warnings.append(f"{booked[0].account_iban}: saldo po operacji bez znaku w {restored} operacjach - znak "
                        f"przywrócony z ciągłości sald")


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def _title_key(text: str) -> str:
    return " ".join(fold(text).split())


def fingerprints(entries: Sequence[BankEntry]) -> dict[int, str]:
    """Own identity: account + status + date + amount + title + ordinal of identical operations that day."""
    seen: Counter[tuple] = Counter()
    result: dict[int, str] = {}
    for entry in entries:
        key = (normalize_iban(entry.account_iban), entry.status, (entry.effective_date or date.min).isoformat(),
               entry.amount_minor, entry.currency, _title_key(entry.title))
        seen[key] += 1
        material = "|".join(str(part) for part in (*key, seen[key]))
        result[id(entry)] = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return result


@dataclass
class _Prepared:
    entry: BankEntry
    fingerprint: str
    extracted: dict[str, Any]
    op_type: str
    op_reason: str
    counterparty: CounterpartyFact
    raw: RawRecord | None
    raw_meta: dict[str, Any]


def _sensitive_words(conn: psycopg.Connection, company: Company) -> list:
    """The same list as the semantics gate (``semantics.gate.terms_for``): core + extension + database terms.

    The flag is computed once at ingestion from the title AND the counterparty name (review of 3/4B,
    point 13); the gate reads both as well, and a subject it flags stays flagged (``flagged_subjects``).
    """
    from kasa.core.sensitive import load_terms

    try:
        words = get_extension(company.extension_slug).sensitive_words
    except ExtensionError:
        words = []
    return load_terms(conn, company.id, words)


def _nbp_note(conn: psycopg.Connection, entry: BankEntry) -> dict[str, Any] | None:
    note = fx_note(entry)
    if note is None:
        return None
    day = entry.booking_date or entry.transaction_date or entry.value_date
    rate = rate_before(conn, entry.original_currency, day) if day else None
    if rate is not None:
        note.update({
            "nbp_rate": format(rate.rate.normalize(), "f"),
            "nbp_date": rate.rate_date.isoformat(),
            "nbp_table": rate.table_ref,
            "pln_at_nbp_minor": to_minor(
                from_minor(entry.original_amount_minor, entry.original_currency) * rate.rate, "PLN",
                rounding=ROUND_HALF_UP),
        })
    return note


def _find_existing(conn: psycopg.Connection, account_id: UUID, entry: BankEntry, fp: str) -> tuple | None:
    """Latest row with the same identity (entry_reference, else fingerprint), current or not."""
    if entry.entry_reference:
        where, value = "entry_reference = %s", entry.entry_reference
    else:
        where, value = "fingerprint = %s", fp
    return conn.execute(
        f"""
        SELECT t.id, t.schema_version, t.superseded_by, s.status::text,
               t.status::text, t.booking_date, t.value_date, t.transaction_date, t.amount_minor, t.currency,
               t.title_raw, t.counterparty_name_raw, t.counterparty_iban_raw, t.balance_after_minor,
               t.balance_after_computed, t.source
          FROM l1_bank_transaction t
          LEFT JOIN l1_bank_transaction s ON s.id = t.superseded_by
         WHERE t.account_id = %s AND t.{where}
         ORDER BY (t.superseded_by IS NULL) DESC, t.created_at DESC, t.id
         LIMIT 1
        """,
        (account_id, value),
    ).fetchone()


def _content_changed(existing: tuple, entry: BankEntry) -> list[str]:
    """Fields of the bank's content that differ from the stored fact (the same identity, the same parser version):
    the bank corrected the operation, so it gets a new version. Only the same source is compared: the same
    operation from another channel (camt.053 after EnableBanking) is the same fact, told with other fields. A
    computed balance after is ours, not the bank's, and does not count."""
    (_id, _version, _superseded, _successor, status, booking, value, tx_date, amount, currency, title, name, iban,
     balance, balance_computed, source) = existing
    if source != entry.source:
        return []
    pairs = {
        "status": (status, entry.status), "booking_date": (booking, entry.booking_date),
        "value_date": (value, entry.value_date), "transaction_date": (tx_date, entry.transaction_date),
        "amount_minor": (amount, entry.amount_minor), "currency": ((currency or "").strip(), entry.currency),
        "title": (title, entry.title or None), "counterparty_name": (name, entry.counterparty_name),
        "counterparty_iban": (iban, entry.counterparty_iban),
    }
    if not balance_computed and not entry.balance_after_computed:
        pairs["balance_after_minor"] = (balance, entry.balance_after_minor)
    return [field for field, (old, new) in pairs.items() if old != new]


def _resolve_pending(conn: psycopg.Connection, account_id: UUID, booked_id: UUID, entry: BankEntry) -> bool:
    """A booked operation resolves the matching card hold (same amount, party, date ±3 days).

    The hold stays a current fact (``superseded_by`` is only for versions of a fact); its
    state is ``resolved_by_transaction_id`` = the booked operation (migration 0009).
    """
    day = entry.transaction_date or entry.booking_date
    row = conn.execute(
        """
        SELECT id FROM l1_bank_transaction
         WHERE account_id = %s AND status = 'pending' AND superseded_by IS NULL
           AND resolved_by_transaction_id IS NULL AND amount_minor = %s
           AND coalesce(transaction_date, value_date, booking_date) BETWEEN %s::date - 3 AND %s::date + 3
           AND kasa_name_key(counterparty_name_raw) IS NOT DISTINCT FROM kasa_name_key(%s)
         ORDER BY abs(coalesce(transaction_date, value_date, booking_date) - %s::date), created_at
         LIMIT 1
        """,
        (account_id, entry.amount_minor, day, day, entry.counterparty_name, day),
    ).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE l1_bank_transaction SET resolved_by_transaction_id = %s WHERE id = %s", (booked_id, row[0]))
    return True


def store_entries(
    conn: psycopg.Connection,
    company: Company,
    account: AccountRow,
    entries: list[BankEntry],
    raws: dict[int, tuple[RawRecord, dict[str, Any]]],
    result: IngestResult,
    *,
    balances: Sequence[BankBalance] = (),
    date_from: date | None = None,
    date_to: date | None = None,
    context: PseudonymContext | None = None,
) -> None:
    """Write parsed entries of one account as L1 facts (idempotent, versioned)."""
    _fill_balances(entries, balances, date_from, date_to, result.warnings)
    days = [e.booking_date for e in entries if e.status == "booked" and e.booking_date]
    if days:
        result.first_booking = min([d for d in (result.first_booking, min(days)) if d])
        result.last_booking = max([d for d in (result.last_booking, max(days)) if d])
    fps = fingerprints(entries)
    own_ibans = _own_ibans(conn, company.id)
    extracted_by_entry = {
        id(e): extract_title(e.title_lines, reference_date=e.effective_date, counterparty_name=e.counterparty_name)
        for e in entries
    }
    own_nip = _company_nip(conn, company.id)
    if own_nip is None:
        inferred, basis = _infer_own_nip(entries, extracted_by_entry)
        if inferred:
            conn.execute("SELECT kasa_own_counterparty(%s, NULL, %s)", (company.id, inferred))
            own_nip = inferred
            result.warnings.append(f"NIP firmy {inferred} odczytany z danych banku ({basis})")
    context = context if context is not None else load_context(conn, company.id)
    words = _sensitive_words(conn, company)

    # Pass 1: facts that do not depend on pseudonymization; register every party first,
    # so that company names and persons are known before any title is pseudonymized.
    prepared: list[_Prepared] = []
    for entry in entries:
        extracted = extracted_by_entry[id(entry)]
        op_type, reason = classify_op_type(entry, extracted, own_ibans=own_ibans, account_kind=account.kind)
        party = resolve_counterparty(conn, company.id, entry, extracted, op_type, own_nip=own_nip,
                                     bank_name=account.bank, context=context)
        if party.kind == "person" and entry.counterparty_name:
            register_person(conn, company.id, entry.counterparty_name, context)
        elif entry.counterparty_name:
            context.add_company_name(entry.counterparty_name)
        raw, meta = raws.get(id(entry), (None, {}))
        prepared.append(_Prepared(entry, fps[id(entry)], extracted, op_type, reason, party, raw, meta))

    # Pass 2: pseudonymize and write.
    for item in prepared:
        entry = item.entry
        if entry.status == "booked":
            result.booked += 1
        else:
            result.pending += 1
        existing = _find_existing(conn, account.id, entry, item.fingerprint)
        if existing is not None:
            existing_id, existing_version, superseded_by, successor_status = existing[:4]
            if superseded_by is not None and successor_status == "booked" and entry.status == "pending":
                result.unchanged += 1          # the hold was already replaced by its booking
                continue
            if superseded_by is None and existing_version >= PARSER_SCHEMA_VERSION:
                changed = _content_changed(existing, entry)
                if not changed:
                    result.unchanged += 1
                    continue
                result.warnings.append(f"{entry.entry_reference or entry.locator}: bank zmienił treść operacji "
                                       f"({', '.join(changed)}) - nowa wersja faktu")
            if superseded_by is not None:
                existing = None                # only an old version remains; write a fresh current row
        new_id = _insert_transaction(conn, company, account, item, context, words)
        if existing is not None:
            conn.execute("UPDATE l1_bank_transaction SET superseded_by = %s WHERE id = %s", (new_id, existing[0]))
            result.superseded += 1
        else:
            result.inserted += 1
        if entry.status == "booked" and _resolve_pending(conn, account.id, new_id, entry):
            result.pending_resolved += 1


def _insert_transaction(conn: psycopg.Connection, company: Company, account: AccountRow, item: _Prepared,
                        context: PseudonymContext, words: list) -> UUID:
    entry = item.entry
    extracted = dict(item.extracted)
    title_ps, hits = pseudonymize_text(conn, company.id, entry.title or None, context=context)
    if item.counterparty.kind == "person" and entry.counterparty_name:
        counterparty_ps = pseudonym_for(conn, company.id, "person", entry.counterparty_name, context=context)
    else:
        counterparty_ps, _ = pseudonymize_text(conn, company.id, entry.counterparty_name, context=context)
    iban_ps = None
    if entry.counterparty_iban:
        iban_ps = pseudonym_for(conn, company.id, "iban", entry.counterparty_iban, context=context)
    if item.counterparty.id is not None and counterparty_ps:
        conn.execute(
            "UPDATE l1_counterparty SET name_pseudonymized = %s WHERE id = %s AND name_pseudonymized IS NULL",
            (counterparty_ps, item.counterparty.id),
        )
    # the title and the name of the other party: a sensitive word in either bypasses the model
    sensitive = sorted(set(sensitive_hits(entry.title, words)) | set(sensitive_hits(entry.counterparty_name, words)))
    if extracted.get("phones"):
        # only the pseudonym is kept in the facts; the number itself stays in title_raw (phase 2 review)
        extracted["phones"] = [
            {"pseudonym": pseudonym_for(conn, company.id, "phone", number, context=context, basis="regex")}
            for number in extracted["phones"]
        ]
    persons = sorted({h.pseudonym for h in hits if h.kind == "person"})
    if persons:
        extracted["persons"] = persons
    if sensitive:
        extracted["sensitive_words"] = sensitive
    extracted["op_type_reason"] = item.op_reason
    nbp = _nbp_note(conn, entry)
    if nbp:
        extracted["fx"] = nbp
    evidence = {
        "raw_id": str(item.raw.id) if item.raw else None,
        "locator": entry.locator,
        "endpoint": item.raw_meta.get("endpoint"),
        "parser": entry.source,
        "balance_after": None if entry.balance_after_minor is None else
        ("computed" if entry.balance_after_computed else "bank"),
        "counterparty_basis": item.counterparty.basis,
        "counterparty_confidence": item.counterparty.confidence,
        "extra": entry.extra or None,
    }
    return conn.execute(
        """
        INSERT INTO l1_bank_transaction (
            company_id, account_id, entry_reference, fingerprint, status, booking_date, value_date, transaction_date,
            amount_minor, currency, original_amount_minor, original_currency, fx_rate, balance_after_minor,
            balance_after_computed, counterparty_name_raw, counterparty_iban_raw, counterparty_bic, title_raw,
            extracted, op_type, bank_tx_code, bank_tx_description, merchant_category_code, reference_number,
            counterparty_id, title_pseudonymized, counterparty_pseudonym, counterparty_iban_pseudonym, sensitive,
            raw_id, raw_locator, source, method, confidence, evidence, schema_version, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, 'code', 1, %s, %s, %s)
        RETURNING id
        """,
        (company.id, account.id, entry.entry_reference, item.fingerprint, entry.status, entry.booking_date,
         entry.value_date, entry.transaction_date, entry.amount_minor, entry.currency, entry.original_amount_minor,
         entry.original_currency, entry.fx_rate, entry.balance_after_minor, entry.balance_after_computed,
         entry.counterparty_name, entry.counterparty_iban, entry.counterparty_bic, entry.title or None,
         Jsonb(extracted), item.op_type, entry.bank_tx_code, entry.bank_tx_description, entry.merchant_category_code,
         entry.reference_number, item.counterparty.id, title_ps, counterparty_ps, iban_ps, bool(sensitive),
         item.raw.id if item.raw else None, entry.locator, entry.source, Jsonb(evidence), PARSER_SCHEMA_VERSION,
         CREATED_BY),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Split payment (MPP): one occurrence among facts
# ---------------------------------------------------------------------------


def glue_mpp(conn: psycopg.Connection, company_id: UUID, date_from: date | None = None,
             date_to: date | None = None) -> int:
    """Give the legs of each split payment one ``mpp_group_id`` (roles main / vat_leg / vat_rebooking).

    main = the transfer on the current account carrying ``/VAT/``; vat_leg = the
    operation on the VAT account with the same direction, the VAT amount of the
    block and a booking date within ±1 day (same block, else same counterparty);
    vat_rebooking = an own transfer between that VAT account and its current
    account of the VAT amount in the same window (both legs). Existing groups are
    kept. Returns the number of legs grouped now.
    """
    start = (date_from or date.min + timedelta(days=2)) - timedelta(days=2)
    end = (date_to or date.max - timedelta(days=2)) + timedelta(days=2)
    rows = conn.execute(
        """
        SELECT t.id, t.account_id, a.kind::text, a.parent_account_id, t.booking_date, t.amount_minor,
               t.extracted -> 'mpp', t.counterparty_id, t.counterparty_iban_raw, t.fingerprint, t.op_type::text,
               t.mpp_group_id
          FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id
         WHERE t.company_id = %s AND t.superseded_by IS NULL AND t.status = 'booked'
           AND t.booking_date BETWEEN %s AND %s
         ORDER BY t.booking_date, t.account_id, t.id
        """,
        (company_id, start, end),
    ).fetchall()
    taken = {r[0] for r in rows if r[11] is not None}
    vat_accounts = {r[1]: r[3] for r in rows if r[2] == "vat"}
    grouped = 0

    def key(mpp: dict | None) -> tuple | None:
        if not mpp:
            return None
        return (mpp.get("vat_minor"), mpp.get("idc_nip") or mpp.get("idc"), mpp.get("inv_normalized"))

    for main in rows:
        main_id, account_id, kind, _parent, day, amount, mpp, cp_id, cp_iban, fp, _op, group = main
        if kind == "vat" or not mpp or not mpp.get("vat_minor") or main_id in taken:
            continue
        vat_minor = mpp["vat_minor"]
        sign = 1 if amount > 0 else -1
        candidates = [
            r for r in rows
            if r[0] not in taken and r[2] == "vat" and r[5] == sign * vat_minor
            and abs(r[4] - day) <= MPP_DATE_TOLERANCE
            and (vat_accounts.get(r[1]) in (None, account_id))
            and r[10] != "own"
        ]
        candidates.sort(key=lambda r: (key(r[6]) != key(mpp),
                                       not ((cp_id and r[7] == cp_id) or (cp_iban and r[8] == cp_iban)),
                                       abs(r[4] - day)))
        if not candidates:
            continue
        leg = candidates[0]
        if key(leg[6]) != key(mpp) and not ((cp_id and leg[7] == cp_id) or (cp_iban and leg[8] == cp_iban)):
            continue
        group_id = uuid.uuid5(MPP_GROUP_NAMESPACE, f"{company_id}:{account_id}:{fp}")
        members = [(main_id, "main"), (leg[0], "vat_leg")]
        # Rebooking between this VAT account and its current account for the same VAT amount.
        for r in rows:
            if r[0] in taken or r[10] != "own" or abs(r[4] - day) > MPP_DATE_TOLERANCE or abs(r[5]) != vat_minor:
                continue
            if (r[1] == leg[1] or r[1] == account_id) and (r[0], "vat_rebooking") not in members:
                members.append((r[0], "vat_rebooking"))
        for member_id, role in members:
            updated = conn.execute(
                """
                UPDATE l1_bank_transaction
                   SET mpp_group_id = %s, mpp_role = %s,
                       evidence = evidence || jsonb_build_object('mpp_group', %s::jsonb)
                 WHERE id = %s AND mpp_group_id IS NULL
                """,
                (group_id, role, Jsonb({"main": str(main_id), "vat_minor": vat_minor, "rule": "glue_mpp/1"}),
                 member_id),
            ).rowcount
            grouped += updated
            taken.add(member_id)
    return grouped


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def ingest_account(conn: psycopg.Connection, company: Company, provider: BankProvider, account: AccountInfo,
                   date_from: date, date_to: date, *, context: PseudonymContext | None = None,
                   today: date | None = None) -> IngestResult:
    """Fetch, store raw, parse and write one account for [date_from, date_to].

    When the bank gives no dated balance (only the interim one of the moment of the request), the operations are
    fetched up to the day of the request, so that the interim balance closes the chain of computed balances.
    Whatever the bank returns beyond the range (banks do not always respect it) is stored as well: the facts are
    idempotent and the truth checks work per period."""
    if date_to < date_from:
        raise ValueError("date_to przed date_from")
    today = today or date.today()
    row = upsert_account(conn, company.id, account)
    conn.execute("SELECT kasa_own_counterparty(%s, %s, NULL)", (company.id, account.owner_name))
    result = IngestResult(account_iban=row.iban, source=account.provider)

    balance_batch = provider.fetch_balances(account)
    balance_raw = _store_batch(conn, company.id, balance_batch, result)
    balances = eb.parse_balances(balance_batch.payload, row.iban, fetched_on=today)
    result.balances_stored += store_balances(conn, company.id, row, balances, balance_raw.id)
    dated = any(b.balance_type in ("OPBD", "CLBD", "PRCD") for b in balances)
    fetch_to = date_to if dated else max(date_to, today)

    entries: list[BankEntry] = []
    raws: dict[int, tuple[RawRecord, dict[str, Any]]] = {}
    for batch_no, batch in enumerate(provider.fetch_transactions(account, date_from, fetch_to)):
        raw = _store_batch(conn, company.id, batch, result)
        parsed = eb.parse_transactions(batch.payload, row.iban, row.currency, locator_prefix=f"batch[{batch_no}].")
        for entry in parsed:
            raws[id(entry)] = (raw, batch.meta())
        entries.extend(parsed)
    store_entries(conn, company, row, entries, raws, result, balances=balances, date_from=date_from,
                  date_to=fetch_to, context=context)
    result.mpp_legs_grouped = glue_mpp(conn, company.id, date_from, date_to)
    for warning in result.warnings:
        log.warning("%s", warning)
    return result


def ingest_company(conn: psycopg.Connection, company: Company, provider: BankProvider, date_from: date,
                   date_to: date, *, ibans: Sequence[str] | None = None) -> list[IngestResult]:
    """Register every account first (own transfers and VAT links need them all), then ingest each."""
    accounts = provider.list_accounts(company)
    register_accounts(conn, company, accounts)
    wanted = {normalize_iban(i) for i in ibans} if ibans else None
    context = load_context(conn, company.id)
    results = [
        ingest_account(conn, company, provider, account, date_from, date_to, context=context)
        for account in accounts if wanted is None or normalize_iban(account.iban) in wanted
    ]
    grouped = glue_mpp(conn, company.id, date_from, date_to)
    if results:
        results[-1].mpp_legs_grouped += grouped
    return results


def _store_batch(conn: psycopg.Connection, company_id: UUID, batch, result: IngestResult) -> RawRecord:
    raw = store_raw(conn, company_id, "bank", batch.payload, content_type=batch.content_type,
                    external_ref=f"{batch.provider}:{batch.endpoint}", meta=batch.meta())
    if raw.created:
        result.raw_created += 1
    else:
        result.raw_existing += 1
    return raw


def import_statement_file(conn: psycopg.Connection, company: Company, payload: bytes, *, fmt: str,
                          filename: str | None = None, account_kind: str | None = None) -> list[IngestResult]:
    """camt.053 or MT940 file (fallback channel): raw in L0 (channel ``file``), then the same facts as EB."""
    parser = {"camt053": camt053.parse, "mt940": mt940.parse}.get(fmt)
    if parser is None:
        raise ValueError(f"nieznany format wyciągu: {fmt!r}")
    statements: list[ParsedStatement] = parser(payload)
    raw = store_raw(conn, company.id, "file", payload,
                    content_type="application/xml" if fmt == "camt053" else "text/plain",
                    external_ref=filename, meta={"format": fmt, "filename": filename,
                                                 "statements": len(statements)})
    results = []
    context = load_context(conn, company.id)
    for statement in statements:
        known = conn.execute(
            "SELECT kind::text FROM l1_bank_account WHERE company_id = %s AND iban = %s",
            (company.id, normalize_iban(statement.account_iban)),
        ).fetchone()
        info = AccountInfo(
            uid=statement.account_iban, iban=statement.account_iban, currency=statement.currency,
            kind=account_kind or (known[0] if known else "current"), provider=fmt, owner_name=statement.owner_name,
            bic=statement.bic,
        )
        row = upsert_account(conn, company.id, info, source=fmt)
        result = IngestResult(account_iban=row.iban, source=fmt)
        result.raw_created, result.raw_existing = (1, 0) if raw.created else (0, 1)
        result.first_booking, result.last_booking = statement.period_from, statement.period_to
        result.balances_stored = store_balances(conn, company.id, row, statement.balances, raw.id)
        meta = {"endpoint": f"file:{filename or fmt}", "statement_id": statement.statement_id}
        raws = {id(e): (raw, meta) for e in statement.entries}
        store_entries(conn, company, row, list(statement.entries), raws, result, balances=statement.balances,
                      date_from=statement.period_from, date_to=statement.period_to, context=context)
        days = [e.booking_date for e in statement.entries if e.booking_date]
        if days:
            result.mpp_legs_grouped = glue_mpp(conn, company.id, min(days), max(days))
        results.append(result)
    return results
