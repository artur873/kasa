"""Layer 1 of matching (briefing, step 4: "Przygotowanie"): the facts the engine works on.

One :class:`MatchContext` per run holds, for one company:

* booked current transactions (amounts in grosze, signed) with the counterparty, the title
  extraction of code and the current title semantics (``title_semantics_current``);
* current documents with their open balance: gross minus the active matches over the whole
  version chain (``document_lineage``), corrections attached to the invoice they correct
  (``corrects_document_id``), so the balance of a chain is invoice + corrections - matches;
* rows of internal files (PM sheet) with their open amount and phone pseudonyms;
* the decided transactions (an active ``l3_transaction_status``), never proposed again.

Only ``active`` matches reduce a balance: a revoked match gives its amount back.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from kasa.core.matching.fx import RateCache
from kasa.core.normalize import normalize_iban
from kasa.core.pseudonym import fold, normalize_value, value_hash

# Legal forms and filler words left out when names are compared ("Notion Labs, Inc." = "NOTION LABS INC").
NAME_STOPWORDS = frozenset("""
sp z o oo zoo spolka spolki ograniczona ograniczonej odpowiedzialnoscia odpowiedzialnosci akcyjna komandytowa
jawna cywilna s a sa sk ska spk spj gmbh ag kg ug inc ltd limited llc plc pbc bv nv srl sro oy ab as co corp
company the and i oraz of
""".split())
PHONE_RE = re.compile(r"(?<!\d)(?:\+?48[ -]?)?\d{3}[ -]?\d{3}[ -]?\d{3}(?!\d)")


@lru_cache(maxsize=65536)
def name_tokens(name: str | None) -> tuple[str, ...]:
    """Distinctive words of a name: folded, without legal forms (for comparing parties)."""
    if not name:
        return ()
    words = re.findall(r"[a-z0-9]+", fold(name))
    return tuple(w for w in words if w not in NAME_STOPWORDS and not (len(w) == 1 and w.isalpha()))


def _iban(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_iban(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Counterparty:
    id: UUID
    nip: str | None
    foreign_tax_id: str | None
    name: str | None
    kind: str
    ibans: tuple[str, ...]

    @property
    def tax_id(self) -> str | None:
        return self.nip or self.foreign_tax_id

    @property
    def tokens(self) -> tuple[str, ...]:
        return name_tokens(self.name)


@dataclass(eq=False)
class Tx:
    id: UUID
    account_id: UUID
    account_kind: str
    entry_reference: str | None
    booking_date: date
    transaction_date: date | None
    amount_minor: int
    currency: str
    original_amount_minor: int | None
    original_currency: str | None
    counterparty_id: UUID | None
    counterparty_name: str | None
    counterparty_iban: str | None
    counterparty_pseudonym: str | None
    title: str
    extracted: dict[str, Any]
    op_type: str
    mpp_group_id: UUID | None
    mpp_role: str | None
    sensitive: bool
    cp: Counterparty | None = None
    intent: str | None = None
    semantics: dict[str, Any] | None = None

    @property
    def day(self) -> date:
        """Day of the operation (card and BLIK: the transaction date, else the booking date)."""
        return self.transaction_date or self.booking_date

    @property
    def inflow(self) -> bool:
        return self.amount_minor > 0

    @property
    def abs_minor(self) -> int:
        return abs(self.amount_minor)

    @property
    def label(self) -> str:
        return self.entry_reference or str(self.id)

    @property
    def title_folded(self) -> str:
        return fold(self.title or "")

    @property
    def phones(self) -> set[str]:
        return {p.get("pseudonym") for p in self.extracted.get("phones") or [] if isinstance(p, dict)} - {None}

    @property
    def persons(self) -> set[str]:
        out = set(self.extracted.get("persons") or [])
        if self.counterparty_pseudonym and re.fullmatch(r"P-[0-9a-f]{4,16}", self.counterparty_pseudonym):
            out.add(self.counterparty_pseudonym)
        if self.semantics:
            out |= set((self.semantics.get("entities") or {}).get("person_codes") or [])
        return out

    @property
    def title_dates(self) -> set[date]:
        out = set()
        for value in self.extracted.get("dates") or []:
            try:
                out.add(date.fromisoformat(value))
            except (TypeError, ValueError):
                continue
        return out


@dataclass(eq=False)
class Doc:
    id: UUID
    direction: str
    kind: str
    number_raw: str
    number_normalized: str
    ksef_number: str | None
    issue_date: date
    sale_date: date | None
    due_date: date | None
    seller_nip: str | None
    buyer_nip: str | None
    seller_name: str | None
    buyer_name: str | None
    payment_iban: str | None
    currency: str
    gross_minor: int
    gross_pln_minor: int | None
    fx_rate: Any
    fx_rate_date: date | None
    fx_rate_source: str | None
    corrects_id: UUID | None
    counterparty_id: UUID | None
    source: str
    mpp_required: bool
    cp: Counterparty | None = None
    matched_minor: int = 0                  # active matches, document currency, over the version chain
    corrections: list[Doc] = field(default_factory=list)

    @property
    def open_minor(self) -> int:
        return self.gross_minor - self.matched_minor

    @property
    def chain(self) -> list[Doc]:
        out = [self]
        for c in self.corrections:
            out.extend(c.chain)
        return out

    @property
    def chain_open_minor(self) -> int:
        return sum(d.open_minor for d in self.chain)

    @property
    def chain_gross_minor(self) -> int:
        return sum(d.gross_minor for d in self.chain)

    @property
    def party_name(self) -> str | None:
        """The other party as printed on the document."""
        return self.buyer_name if self.direction == "sales" else self.seller_name

    @property
    def party_nip(self) -> str | None:
        return self.buyer_nip if self.direction == "sales" else self.seller_nip

    @property
    def reference_date(self) -> tuple[date, bool]:
        """(date the window is centered on, True when it is the due date; False = issue date, flagged)."""
        return (self.due_date, True) if self.due_date else (self.issue_date, False)

    @property
    def tax_point(self) -> date:
        return self.sale_date or self.issue_date

    @property
    def money_sign(self) -> int:
        """+1 when paying this document brings money in (sales), -1 when it takes money out (purchase)."""
        return 1 if self.direction == "sales" else -1


@dataclass(eq=False)
class Record:
    id: UUID
    row_no: int
    mapping_key: str
    file_ref: str | None
    person_pseudonym: str | None
    record_date: date | None
    amount_minor: int | None
    currency: str
    cost_object_ref: str | None
    settlement_type: str | None
    fields: dict[str, Any]
    phones: set[str] = field(default_factory=set)
    matched_minor: int = 0

    @property
    def open_minor(self) -> int:
        return (self.amount_minor or 0) - self.matched_minor

    @property
    def label(self) -> str:
        stem = (self.file_ref or self.mapping_key).rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return f"{stem}:R{self.row_no - 1:02d}"


@dataclass
class MatchContext:
    company_id: UUID
    own_nip: str | None
    own_ibans: set[str]
    txs: dict[UUID, Tx]
    docs: dict[UUID, Doc]                       # current documents (heads), corrections included
    records: dict[UUID, Record]
    counterparties: dict[UUID, Counterparty]
    decided: dict[UUID, dict[str, Any]]         # transaction id -> active status
    related_used: dict[UUID, int]               # transaction id -> amount already linked as related target
    builtin_rules: dict[str, dict[str, Any]]    # rule_key -> {id, version, stage}
    rates: RateCache
    precedents: dict[UUID, int]                 # counterparty id -> decided transactions
    rejected: dict[UUID, set[str]] = field(default_factory=dict)   # transaction -> rejected proposals
    today: date = field(default_factory=date.today)                # the day of the run (waiting for KSeF)
    by_number: dict[str, list[Doc]] = field(default_factory=dict)
    by_ksef: dict[str, Doc] = field(default_factory=dict)
    mpp_legs: dict[UUID, list[Tx]] = field(default_factory=dict)

    def bases(self) -> list[Doc]:
        """Documents that can be paid on their own: everything except corrections attached to an invoice."""
        return [d for d in self.docs.values() if not (d.kind == "correction" and d.corrects_id in self.docs)]


def _counterparties(conn: psycopg.Connection, company_id: UUID) -> dict[UUID, Counterparty]:
    rows = conn.execute(
        """
        SELECT id, nip, foreign_tax_id, name, kind::text, ibans FROM l1_counterparty
         WHERE company_id = %s AND superseded_by IS NULL
        """,
        (company_id,),
    ).fetchall()
    out = {r[0]: Counterparty(r[0], r[1], r[2], r[3], r[4], tuple(r[5] or ())) for r in rows}
    # a merged (superseded) counterparty resolves to its survivor
    for old_id, survivor in conn.execute(
        "SELECT id, superseded_by FROM l1_counterparty WHERE company_id = %s AND superseded_by IS NOT NULL",
        (company_id,),
    ).fetchall():
        if survivor in out:
            out[old_id] = out[survivor]
    return out


def _sensitive_flags(conn: psycopg.Connection, company_id: UUID) -> tuple[set[UUID], set[UUID]]:
    """(transfers flagged sensitive by the semantics gate, transfers a person declared not sensitive).

    The gate's flag (``flag_sensitive``: a category card "a person must categorize this") is permanent: it
    is never retried (``semantics.store.flagged_subjects``) and matching treats the transfer as sensitive
    too, even when ingestion did not see the word (a term added later). "To nie jest wrażliwe" of a person
    (``sensitive_exception``) lifts both.
    """
    flagged = {r[0] for r in conn.execute(
        """
        SELECT DISTINCT subject_id FROM l2_suggestion
         WHERE company_id = %s AND kind = 'category' AND subject_type = 'transaction'
           AND proposal @> '{"needs_human": true, "reason": "sensitive"}'::jsonb
        """,
        (company_id,),
    ).fetchall()}
    excepted: set[UUID] = set()
    if conn.execute("SELECT to_regclass('sensitive_exception') IS NOT NULL").fetchone()[0]:
        excepted = {r[0] for r in conn.execute(
            "SELECT DISTINCT subject_id FROM sensitive_exception "
            "WHERE company_id = %s AND subject_type = 'transaction'",
            (company_id,),
        ).fetchall()}
    return flagged, excepted


def _transactions(conn: psycopg.Connection, company_id: UUID,
                  counterparties: dict[UUID, Counterparty]) -> dict[UUID, Tx]:
    flagged, excepted = _sensitive_flags(conn, company_id)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT t.id, t.account_id, a.kind::text AS account_kind, t.entry_reference, t.booking_date,
                   t.transaction_date, t.amount_minor, t.currency, t.original_amount_minor, t.original_currency,
                   t.counterparty_id, t.counterparty_name_raw, t.counterparty_iban_raw, t.counterparty_pseudonym,
                   coalesce(t.title_raw, '') AS title, t.extracted, t.op_type::text AS op_type, t.mpp_group_id,
                   t.mpp_role, t.sensitive, s.intent, s.entities, s.summary, s.model
              FROM l1_bank_transaction t
              JOIN l1_bank_account a ON a.id = t.account_id
              LEFT JOIN title_semantics_current s ON s.transaction_id = t.id
             WHERE t.company_id = %s AND t.superseded_by IS NULL AND t.status = 'booked'
            """,
            (company_id,),
        ).fetchall()
    out = {}
    for r in rows:
        semantics = None
        if r["intent"] is not None or r["entities"]:
            semantics = {"intent": r["intent"], "entities": r["entities"] or {}, "summary": r["summary"],
                         "model": r["model"]}
        sensitive = (bool(r["sensitive"]) or r["id"] in flagged) and r["id"] not in excepted
        tx = Tx(
            id=r["id"], account_id=r["account_id"], account_kind=r["account_kind"],
            entry_reference=r["entry_reference"], booking_date=r["booking_date"],
            transaction_date=r["transaction_date"], amount_minor=int(r["amount_minor"]),
            currency=r["currency"].strip(), original_amount_minor=r["original_amount_minor"],
            original_currency=(r["original_currency"] or "").strip() or None,
            counterparty_id=r["counterparty_id"], counterparty_name=r["counterparty_name_raw"],
            counterparty_iban=_iban(r["counterparty_iban_raw"]), counterparty_pseudonym=r["counterparty_pseudonym"],
            title=r["title"], extracted=r["extracted"] or {}, op_type=r["op_type"], mpp_group_id=r["mpp_group_id"],
            mpp_role=r["mpp_role"], sensitive=sensitive,
            cp=counterparties.get(r["counterparty_id"]) if r["counterparty_id"] else None,
            # a sensitive title is never read by a model; its semantics are not used either
            intent=None if sensitive else r["intent"], semantics=None if sensitive else semantics,
        )
        out[tx.id] = tx
    return out


def _documents(conn: psycopg.Connection, company_id: UUID, counterparties: dict[UUID, Counterparty],
               exclude_matches_of: set[UUID]) -> dict[UUID, Doc]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT d.id, d.direction::text AS direction, d.kind::text AS kind, d.number_raw, d.number_normalized,
                   d.ksef_number, d.issue_date, d.sale_date, d.due_date, d.seller_nip, d.buyer_nip, d.seller_name,
                   d.buyer_name, d.payment_iban, d.currency, d.gross_minor, d.gross_pln_minor, d.fx_rate,
                   d.fx_rate_date, d.fx_rate_source, d.counterparty_id, d.source, d.mpp_required,
                   (SELECT l.head_id FROM document_lineage l WHERE l.document_id = d.corrects_document_id) AS corrects
              FROM l1_document d
             WHERE d.company_id = %s AND d.superseded_by IS NULL
            """,
            (company_id,),
        ).fetchall()
    docs = {}
    for r in rows:
        docs[r["id"]] = Doc(
            id=r["id"], direction=r["direction"], kind=r["kind"], number_raw=r["number_raw"],
            number_normalized=r["number_normalized"], ksef_number=r["ksef_number"], issue_date=r["issue_date"],
            sale_date=r["sale_date"], due_date=r["due_date"], seller_nip=r["seller_nip"], buyer_nip=r["buyer_nip"],
            seller_name=r["seller_name"], buyer_name=r["buyer_name"], payment_iban=_iban(r["payment_iban"]),
            currency=r["currency"].strip(), gross_minor=int(r["gross_minor"]),
            gross_pln_minor=r["gross_pln_minor"], fx_rate=r["fx_rate"], fx_rate_date=r["fx_rate_date"],
            fx_rate_source=r["fx_rate_source"], corrects_id=r["corrects"], counterparty_id=r["counterparty_id"],
            source=r["source"], mpp_required=bool(r["mpp_required"]),
            cp=counterparties.get(r["counterparty_id"]) if r["counterparty_id"] else None,
        )
    # active matches over the version chain (document side, document currency); revoked ones do not count
    for head_id, matched in conn.execute(
        """
        SELECT l.head_id, sum(coalesce(m.document_amount_minor, m.amount_minor))
          FROM l3_match m JOIN document_lineage l ON l.document_id = m.document_id
         WHERE m.company_id = %s AND m.status = 'active' AND m.document_id IS NOT NULL
           AND NOT (m.transaction_id = ANY(%s))
         GROUP BY l.head_id
        """,
        (company_id, list(exclude_matches_of)),
    ).fetchall():
        if head_id in docs:
            docs[head_id].matched_minor = int(matched)
    for doc in docs.values():
        if doc.kind == "correction" and doc.corrects_id in docs and doc.corrects_id != doc.id:
            docs[doc.corrects_id].corrections.append(doc)
    return docs


def _records(conn: psycopg.Connection, company_id: UUID, exclude_matches_of: set[UUID]) -> dict[UUID, Record]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT r.id, r.row_no, r.mapping_key, f.external_ref, r.person_pseudonym, r.record_date, r.amount_minor,
                   r.currency, r.cost_object_ref, r.settlement_type, r.fields
              FROM l1_internal_record r JOIN l0_raw f ON f.id = r.file_raw_id
             WHERE r.company_id = %s AND r.superseded_by IS NULL
            """,
            (company_id,),
        ).fetchall()
    records = {
        r["id"]: Record(r["id"], r["row_no"], r["mapping_key"], r["external_ref"], r["person_pseudonym"],
                        r["record_date"], r["amount_minor"], r["currency"].strip(), r["cost_object_ref"],
                        r["settlement_type"], r["fields"] or {})
        for r in rows
    }
    # phones written in the row (e.g. "tel. 500 085 853") -> their pseudonyms, if the key exists (lookup only)
    wanted: dict[str, set[UUID]] = defaultdict(set)
    for record in records.values():
        for value in record.fields.values():
            if isinstance(value, str):
                for m in PHONE_RE.finditer(value):
                    wanted[value_hash("phone", normalize_value("phone", m.group(0)))].add(record.id)
    if wanted:
        for digest, pseudonym in conn.execute(
            "SELECT value_hash, pseudonym FROM pseudonym_key WHERE company_id = %s AND kind = 'phone' "
            "AND value_hash = ANY(%s)",
            (company_id, list(wanted)),
        ).fetchall():
            for record_id in wanted[digest]:
                records[record_id].phones.add(pseudonym)
    evidence = "evidence_only" if _has_evidence_only(conn) else "false"
    for record_id, matched, proof in conn.execute(
        f"""
        SELECT internal_record_id, sum(coalesce(document_amount_minor, amount_minor)), bool_or({evidence})
          FROM l3_match
         WHERE company_id = %s AND status = 'active' AND internal_record_id IS NOT NULL
           AND NOT (transaction_id = ANY(%s))
         GROUP BY internal_record_id
        """,
        (company_id, list(exclude_matches_of)),
    ).fetchall():
        if record_id in records:
            # a row that is evidence of a document (one cost, many proofs) is settled by that document
            records[record_id].matched_minor = (records[record_id].amount_minor or 0) if proof else int(matched)
    return records


def _has_evidence_only(conn: psycopg.Connection) -> bool:
    return bool(conn.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'l3_match' "
        "AND column_name = 'evidence_only')"
    ).fetchone()[0])


def builtin_rules(conn: psycopg.Connection, company_id: UUID) -> dict[str, dict[str, Any]]:
    """Latest version of every built-in rule of the company: rule_key -> {id, version, stage}."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (rule_key) rule_key, id, version, stage::text FROM l3_rule
         WHERE company_id = %s AND rule_key LIKE 'builtin:%%'
         ORDER BY rule_key, version DESC
        """,
        (company_id,),
    ).fetchall()
    return {r[0]: {"id": r[1], "version": r[2], "stage": r[3]} for r in rows}


def load_context(conn: psycopg.Connection, company_id: UUID, *,
                 exclude_matches_of: set[UUID] | None = None) -> MatchContext:
    """Everything one run needs. ``exclude_matches_of``: evaluate these transactions as if undecided."""
    exclude = set(exclude_matches_of or ())
    counterparties = _counterparties(conn, company_id)
    own_nip = conn.execute("SELECT nip FROM company WHERE id = %s", (company_id,)).fetchone()
    own_nip = own_nip[0] if own_nip else None
    if own_nip is None:
        row = conn.execute(
            "SELECT nip FROM l1_counterparty WHERE company_id = %s AND kind = 'own' AND superseded_by IS NULL",
            (company_id,),
        ).fetchone()
        own_nip = row[0] if row else None
    own_ibans = {r[0] for r in conn.execute("SELECT iban FROM l1_bank_account WHERE company_id = %s",
                                            (company_id,)).fetchall()}
    txs = _transactions(conn, company_id, counterparties)
    docs = _documents(conn, company_id, counterparties, exclude)
    records = _records(conn, company_id, exclude)
    decided = {}
    for tx_id, status, subtype in conn.execute(
        """
        SELECT transaction_id, final_status::text, subtype FROM l3_transaction_status
         WHERE company_id = %s AND status = 'active'
        """,
        (company_id,),
    ).fetchall():
        if tx_id not in exclude:
            decided[tx_id] = {"final_status": status, "subtype": subtype}
    related_used = defaultdict(int)
    for target, amount in conn.execute(
        """
        SELECT related_transaction_id, sum(amount_minor) FROM l3_match
         WHERE company_id = %s AND status = 'active' AND related_transaction_id IS NOT NULL
           AND NOT (transaction_id = ANY(%s))
         GROUP BY 1
        """,
        (company_id, list(exclude)),
    ).fetchall():
        related_used[target] = int(amount)
    precedents: dict[UUID, int] = defaultdict(int)
    for tx_id in decided:
        tx = txs.get(tx_id)
        if tx and tx.counterparty_id:
            precedents[tx.counterparty_id] += 1
    ctx = MatchContext(
        company_id=company_id, own_nip=own_nip, own_ibans=own_ibans, txs=txs, docs=docs, records=records,
        counterparties=counterparties, decided=decided, related_used=dict(related_used),
        builtin_rules=builtin_rules(conn, company_id), rates=RateCache(conn, company_id),
        precedents=dict(precedents),
    )
    for doc in docs.values():
        # a correction named in a title stands for the chain of the invoice it corrects
        target = doc
        while target.kind == "correction" and target.corrects_id in docs and target.corrects_id != target.id:
            target = docs[target.corrects_id]
        if target not in ctx.by_number.setdefault(doc.number_normalized, []):
            ctx.by_number[doc.number_normalized].append(target)
        if doc.ksef_number:
            ctx.by_ksef[doc.ksef_number] = target
    for tx in txs.values():
        if tx.mpp_group_id:
            ctx.mpp_legs.setdefault(tx.mpp_group_id, []).append(tx)
    for subject_id, proposal in conn.execute(
        """
        SELECT s.subject_id, s.proposal FROM l2_suggestion s
         WHERE s.company_id = %s AND s.kind = 'match' AND s.subject_type = 'transaction' AND s.status = 'rejected'
           -- a rejection by a decision that was revoked later does not count any more
           AND (s.resolved_by_decision IS NULL OR NOT EXISTS (
                SELECT 1 FROM l3_decision_log r WHERE r.reverts_decision_id = s.resolved_by_decision))
        """,
        (company_id,),
    ).fetchall():
        ctx.rejected.setdefault(subject_id, set()).add(
            json.dumps(proposal, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return ctx
