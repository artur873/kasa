"""Writing documents to L1 and deduplication across channels (briefing, step 2).

Every channel calls :func:`upsert_document` with the facts it read and the
observation (which raw payload, where in it, how it was read). The function:

1. ``(raw, locator)`` already read by the same parser/prompt version -> nothing (idempotent);
   read by another version and this payload created the document -> new version
   (``superseded_by`` on the old row);
2. finds the document the facts belong to, in order of certainty:
   KSeF number -> file digest -> (NIP, kind, normalized number) with the same
   date and amount. The same number of another NIP is never a duplicate;
3. links the observation (``l1_document_observation``) or creates the document.
   The document carries the fields of the most certain channel
   (KSeF > PDF > JPK > CSV): a better channel arriving later creates a new version;
4. same NIP and number but a different date or amount -> a new document flagged
   ``possible_duplicate`` and an L2 suggestion ``duplicate`` for the review queue.

Corrections point to the corrected document (``corrects_document_id``) and
settlement invoices to the advance (``settles_advance_document_id``); links that
cannot be resolved yet are kept as references and resolved by :func:`relink_pending`.
Counterparties are filled through ``kasa_upsert_counterparty`` when migration
0006 provides it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kasa.core.documents.facts import CHANNEL_RANK, InvoiceFacts, money_rounded
from kasa.core.normalize import from_minor, normalize_nip

log = logging.getLogger(__name__)

Method = Literal["code", "model", "human"]
LinkedBy = Literal["created", "ksef_number", "file_hash", "nip_kind_number", "human"]

SCHEMA_VERSION = 1
INVOICE_LIKE_KINDS = {"vat", "advance", "settlement", "foreign", "receipt", "bill", "proforma", "internal"}


# ---------------------------------------------------------------------------
# Schema capabilities (migration 0006 may or may not be present)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capabilities:
    counterparty_function: bool
    document_counterparty_column: bool
    company_nip_column: bool
    counterparty_named_args: bool = False      # 0006 signature with p_kind / p_foreign_tax_id ...
    own_counterparty_function: bool = False

    @property
    def counterparties(self) -> bool:
        return self.counterparty_function and self.document_counterparty_column


def capabilities(conn: psycopg.Connection) -> Capabilities:
    row = conn.execute(
        """
        SELECT to_regproc('kasa_upsert_counterparty') IS NOT NULL,
               EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_schema = current_schema() AND table_name = 'l1_document'
                          AND column_name = 'counterparty_id'),
               EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_schema = current_schema() AND table_name = 'company'
                          AND column_name = 'nip'),
               EXISTS (SELECT 1 FROM pg_proc
                        WHERE proname = 'kasa_upsert_counterparty'
                          AND proargnames @> ARRAY['p_kind', 'p_foreign_tax_id', 'p_source', 'p_created_by']),
               to_regproc('kasa_own_counterparty') IS NOT NULL
        """
    ).fetchone()
    return Capabilities(*row)


_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


# ---------------------------------------------------------------------------
# Company NIP (decides the direction of invoices)
# ---------------------------------------------------------------------------


class CompanyNipConflict(ValueError):
    """The file belongs to another company (its NIP differs from ours)."""


def get_company_nip(conn: psycopg.Connection, company_id: UUID) -> str | None:
    if not capabilities(conn).company_nip_column:
        return None
    row = conn.execute("SELECT nip FROM company WHERE id = %s", (company_id,)).fetchone()
    return row[0] if row else None


def ensure_company_nip(conn: psycopg.Connection, company_id: UUID, nip: str | None, *, source: str) -> str | None:
    """Remember our NIP the first time a trusted source (KSeF context, JPK Podmiot1) names it."""
    if not nip:
        return get_company_nip(conn, company_id)
    nip = normalize_nip(nip)
    current = get_company_nip(conn, company_id)
    if current is not None and current != nip:
        raise CompanyNipConflict(f"{source}: NIP {nip} różni się od NIP-u firmy {current}")
    if current is None:
        conn.execute("UPDATE company SET nip = %s WHERE id = %s AND nip IS NULL", (nip, company_id))
        log.info("NIP firmy ustalony na %s (źródło: %s)", nip, source)
    if capabilities(conn).own_counterparty_function:
        # The company as its own counterparty (0006) learns the NIP too.
        with conn.transaction():
            conn.execute("SELECT kasa_own_counterparty(%s, NULL, %s)", (company_id, nip))
    return nip


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """How one channel saw the document: raw payload, position in it, method."""

    raw_id: UUID
    source: str
    locator: str | None = None
    method: Method = "code"
    prompt_version: str | None = None
    confidence: Decimal | float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    created_by: str = "kasa.documents"


@dataclass
class UpsertResult:
    document_id: UUID                 # current document the observation belongs to
    action: Literal["created", "linked", "new_version", "unchanged"]
    linked_by: str | None = None
    observation_created: bool = False
    possible_duplicate_of: list[UUID] = field(default_factory=list)


@dataclass
class IngestReport:
    channel: str
    raw_created: int = 0
    raw_existing: int = 0
    documents_created: int = 0
    versions_created: int = 0
    observations_created: int = 0
    linked: dict[str, int] = field(default_factory=dict)
    unchanged: int = 0
    possible_duplicates: int = 0
    needs_review: int = 0
    internal_records_created: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    document_ids: list[UUID] = field(default_factory=list)

    def add(self, result: UpsertResult) -> None:
        self.document_ids.append(result.document_id)
        if result.action == "created":
            self.documents_created += 1
        elif result.action == "new_version":
            self.versions_created += 1
        elif result.action == "unchanged":
            self.unchanged += 1
        if result.observation_created:
            self.observations_created += 1
        if result.linked_by and result.linked_by != "created":
            self.linked[result.linked_by] = self.linked.get(result.linked_by, 0) + 1
        if result.possible_duplicate_of:
            self.possible_duplicates += 1

    def raw(self, created: bool) -> None:
        if created:
            self.raw_created += 1
        else:
            self.raw_existing += 1

    def summary(self) -> str:
        linked = ", ".join(f"{k}: {v}" for k, v in sorted(self.linked.items())) or "-"
        parts = [
            f"pliki nowe/znane: {self.raw_created}/{self.raw_existing}",
            f"dokumenty nowe: {self.documents_created}",
            f"nowe wersje: {self.versions_created}",
            f"obserwacje nowe: {self.observations_created} (dopięte: {linked})",
            f"bez zmian: {self.unchanged}",
        ]
        if self.internal_records_created:
            parts.append(f"wiersze wewnętrzne nowe: {self.internal_records_created}")
        if self.possible_duplicates:
            parts.append(f"możliwe duplikaty: {self.possible_duplicates}")
        if self.needs_review:
            parts.append(f"do przeglądu: {self.needs_review}")
        if self.skipped:
            parts.append(f"pominięte: {len(self.skipped)}")
        if self.errors:
            parts.append(f"błędy: {len(self.errors)}")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

_DOC_COLUMNS = (
    "id, company_id, direction, ksef_number, number_raw, number_normalized, kind, file_hash, issue_date, "
    "gross_minor, currency, source, method, prompt_version, evidence, superseded_by, "
    "seller_nip, seller_foreign_tax_id, buyer_nip, buyer_foreign_tax_id"
)


def _rows(conn: psycopg.Connection, query: str, params: tuple) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def _head(conn: psycopg.Connection, document_id: UUID) -> dict[str, Any]:
    rows = _rows(
        conn,
        f"SELECT {_DOC_COLUMNS} FROM l1_document WHERE id = "
        "(SELECT head_id FROM document_lineage WHERE document_id = %s)",
        (document_id,),
    )
    if not rows:
        raise LookupError(f"dokument {document_id} nie ma bieżącej wersji")
    return rows[0]


def _current_by(conn: psycopg.Connection, company_id: UUID, column: str, value: str) -> dict[str, Any] | None:
    rows = _rows(
        conn,
        f"SELECT {_DOC_COLUMNS} FROM l1_document WHERE company_id = %s AND {column} = %s AND superseded_by IS NULL",
        (company_id, value),
    )
    return rows[0] if rows else None


def _counterparty_key_sql() -> str:
    return ("CASE direction WHEN 'sales' THEN coalesce(buyer_nip, buyer_foreign_tax_id) "
            "ELSE coalesce(seller_nip, seller_foreign_tax_id) END")


def _same_number_candidates(conn: psycopg.Connection, company_id: UUID, direction: str, number_normalized: str,
                            tax_key: str) -> list[dict[str, Any]]:
    return _rows(
        conn,
        f"""
        SELECT {_DOC_COLUMNS} FROM l1_document
         WHERE company_id = %s AND superseded_by IS NULL AND direction = %s
           AND number_normalized = %s AND {_counterparty_key_sql()} = %s
         ORDER BY created_at
        """,
        (company_id, direction, number_normalized, tax_key),
    )


def _kinds_compatible(facts: InvoiceFacts, doc_kind: str, doc_kind_certain: bool) -> bool:
    if facts.kind == doc_kind:
        return True
    if facts.kind_certain and doc_kind_certain:
        return False
    # A channel that cannot tell kinds apart (JPK, CSV) only separates corrections from invoices.
    return (facts.kind == "correction") == (doc_kind == "correction")


def _reader_version(facts: InvoiceFacts, observation: Observation) -> tuple[int, str]:
    return facts.parser_version, observation.prompt_version or ""


# ---------------------------------------------------------------------------
# Relations, PLN, counterparty
# ---------------------------------------------------------------------------


def _find_related(conn: psycopg.Connection, company_id: UUID, facts: InvoiceFacts,
                  ref: dict[str, Any]) -> UUID | None:
    from kasa.core.normalize import normalize_invoice_number

    if ref.get("ksef_number"):
        found = _current_by(conn, company_id, "ksef_number", ref["ksef_number"])
        if found:
            return found["id"]
    number = ref.get("number")
    tax_key = facts.counterparty.tax_key
    if number and tax_key:
        try:
            normalized = normalize_invoice_number(number)
        except ValueError:
            return None
        rows = _same_number_candidates(conn, company_id, facts.direction, normalized, tax_key)
        if ref.get("issue_date"):
            dated = [r for r in rows if r["issue_date"].isoformat() == str(ref["issue_date"])[:10]]
            rows = dated or rows
        if len(rows) == 1:
            return rows[0]["id"]
    return None


def resolve_relations(conn: psycopg.Connection, company_id: UUID, facts: InvoiceFacts) -> dict[str, Any]:
    """Ids of the corrected document and of the settled advance, when they are already known."""
    corrects_id = None
    if facts.kind == "correction" and facts.corrects_ref:
        corrects_id = _find_related(conn, company_id, facts, facts.corrects_ref)
    advance_id = None
    for ref in facts.advance_refs:
        advance_id = _find_related(conn, company_id, facts, ref)
        if advance_id:
            break
    status = {}
    if facts.kind == "correction":
        status["corrects"] = "resolved" if corrects_id else "pending"
    if facts.advance_refs:
        status["advance"] = "resolved" if advance_id else "pending"
    return {"corrects_document_id": corrects_id, "settles_advance_document_id": advance_id, "status": status}


def derive_pln(conn: psycopg.Connection, company_id: UUID, facts: InvoiceFacts) -> None:
    """Foreign-currency document without a rate: NBP rate of the last day before the tax point."""
    if facts.currency == "PLN" or facts.fx_rate is not None:
        return
    tax_point = facts.sale_date or facts.issue_date
    row = conn.execute(
        """
        SELECT rate, rate_date, source FROM l1_fx_rate
         WHERE currency = %s AND rate_date < %s AND (company_id IS NULL OR company_id = %s)
           AND source LIKE 'nbp%%'
         ORDER BY rate_date DESC, company_id NULLS LAST LIMIT 1
        """,
        (facts.currency, tax_point, company_id),
    ).fetchone()
    if row is None:
        facts.checks.setdefault("issues", []).append(
            f"brak kursu NBP {facts.currency} przed {tax_point} - kwota PLN nieznana"
        )
        return
    facts.fx_rate, facts.fx_rate_date, facts.fx_rate_source = row[0], row[1], row[2]
    facts.gross_pln_minor = money_rounded(from_minor(facts.gross_minor, facts.currency) * row[0], "PLN")


def _counterparty_id(conn: psycopg.Connection, company_id: UUID, facts: InvoiceFacts,
                     caps: Capabilities) -> UUID | None:
    if not caps.counterparties:
        _warn_once("counterparty", "Brak kasa_upsert_counterparty / l1_document.counterparty_id (migracja 0006) - "
                                   "dokumenty bez kontrahenta, NIP zostaje w polach dokumentu.")
        return None
    party = facts.counterparty
    if not (party.nip or party.foreign_tax_id or party.name):
        return None
    nip = party.nip if party.nip and not _nip_checksum_bad(party.nip) else None
    iban = facts.payment_iban if facts.direction == "purchase" else None
    with conn.transaction():
        if caps.counterparty_named_args:
            kind = "person" if facts.kind == "bill" and not (party.nip or party.foreign_tax_id) else None
            row = conn.execute(
                """
                SELECT kasa_upsert_counterparty(
                    p_company_id => %s, p_nip => %s, p_name => %s, p_iban => %s,
                    p_kind => %s::counterparty_kind, p_foreign_tax_id => %s, p_source => %s,
                    p_evidence => %s, p_created_by => %s)
                """,
                (company_id, nip, party.name, iban, kind, party.foreign_tax_id, f"documents:{facts.channel}",
                 Jsonb({"document_number": facts.number_raw, "channel": facts.channel}), "kasa.documents"),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT kasa_upsert_counterparty(%s, %s, %s, %s)",
                (company_id, nip or party.foreign_tax_id, party.name, iban),
            ).fetchone()
    return row[0] if row else None


def _nip_checksum_bad(nip: str) -> bool:
    try:
        normalize_nip(nip)
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------------------
# Inserts
# ---------------------------------------------------------------------------


def _document_extra(facts: InvoiceFacts) -> dict[str, Any]:
    extra: dict[str, Any] = dict(facts.extra)
    if facts.payment:
        extra["payment"] = facts.payment
    if facts.issue_place:
        extra["issue_place"] = facts.issue_place
    for role in ("seller", "buyer"):
        party = getattr(facts, role).model_dump(mode="json", exclude_none=True, exclude={"nip", "name"})
        party = {k: v for k, v in party.items() if v not in ({}, [])}
        if party:
            extra[role] = party
    return extra


def _insert_document(conn: psycopg.Connection, company_id: UUID, facts: InvoiceFacts, observation: Observation,
                     caps: Capabilities, *, evidence: dict[str, Any]) -> UUID:
    relations = resolve_relations(conn, company_id, facts)
    derive_pln(conn, company_id, facts)
    evidence = {
        "channel": facts.channel,
        "parser": facts.parser,
        "parser_version": facts.parser_version,
        "raw_id": str(observation.raw_id),
        "locator": observation.locator,
        "lines_sum_ok": facts.lines_sum_ok,
        "kind_certain": facts.kind_certain,
        "checks": facts.checks,
        "relations": relations["status"],
        **observation.evidence,
        **evidence,
    }
    if facts.lines_sum_ok is False and facts.channel == "pdf":
        evidence["needs_review"] = True
    values: dict[str, Any] = {
        "company_id": company_id,
        "direction": facts.direction,
        "ksef_number": facts.ksef_number,
        "number_raw": facts.number_raw,
        "number_normalized": facts.number_normalized,
        "kind": facts.kind,
        "file_hash": facts.file_hash,
        "issuer_software": facts.issuer_software,
        "issue_date": facts.issue_date,
        "sale_date": facts.sale_date,
        "service_period_start": facts.service_period_start,
        "service_period_end": facts.service_period_end,
        "ksef_accepted_at": facts.ksef_accepted_at,
        "due_date": facts.due_date,
        "seller_nip": facts.seller.nip,
        "seller_foreign_tax_id": facts.seller.foreign_tax_id,
        "seller_name": facts.seller.name,
        "buyer_nip": facts.buyer.nip,
        "buyer_foreign_tax_id": facts.buyer.foreign_tax_id,
        "buyer_name": facts.buyer.name,
        "payment_iban": facts.payment_iban,
        "third_parties": Jsonb([p.model_dump(mode="json", exclude_none=True) for p in facts.third_parties]),
        "currency": facts.currency,
        "net_minor": facts.net_minor,
        "vat_minor": facts.vat_minor,
        "gross_minor": facts.gross_minor,
        "fx_rate": facts.fx_rate,
        "fx_rate_date": facts.fx_rate_date,
        "fx_rate_source": facts.fx_rate_source,
        "gross_pln_minor": facts.gross_pln_minor,
        "vat_summary": Jsonb([s.model_dump(mode="json", exclude_none=True) for s in facts.vat_summary]),
        "corrects_document_id": relations["corrects_document_id"],
        "corrects_ref": Jsonb(facts.corrects_ref) if facts.corrects_ref is not None else None,
        "settles_advance_document_id": relations["settles_advance_document_id"],
        "advance_refs": Jsonb(facts.advance_refs),
        "order_number": facts.order_number,
        "contract_ref": facts.contract_ref,
        "mpp_required": facts.mpp_required,
        "cash_method": facts.cash_method,
        "reverse_charge": facts.reverse_charge,
        "extra": Jsonb(_document_extra(facts)),
        "source": observation.source,
        "method": observation.method,
        "confidence": observation.confidence,
        "evidence": Jsonb(evidence),
        "schema_version": SCHEMA_VERSION,
        "prompt_version": observation.prompt_version,
        "created_by": observation.created_by,
    }
    if caps.document_counterparty_column:
        values["counterparty_id"] = _counterparty_id(conn, company_id, facts, caps)
    columns = ", ".join(values)
    placeholders = ", ".join(f"%({k})s" for k in values)
    document_id = conn.execute(
        f"INSERT INTO l1_document ({columns}) VALUES ({placeholders}) RETURNING id", values
    ).fetchone()[0]
    _insert_lines(conn, company_id, document_id, facts, observation)
    return document_id


def _insert_lines(conn: psycopg.Connection, company_id: UUID, document_id: UUID, facts: InvoiceFacts,
                  observation: Observation) -> None:
    seen: set[int] = set()
    next_no = max((line.line_no for line in facts.lines), default=0) + 1
    for line in facts.lines:
        extra = dict(line.extra)
        line_no = line.line_no
        if line_no in seen:
            extra["source_line_no"] = line_no
            line_no, next_no = next_no, next_no + 1
        seen.add(line_no)
        conn.execute(
            """
            INSERT INTO l1_document_line (
                company_id, document_id, line_no, name, quantity, unit, unit_price, unit_price_minor,
                net_minor, vat_rate, vat_minor, gross_minor, before_json, after_json, line_kind, extra,
                source, method, confidence, evidence, schema_version, prompt_version, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                company_id, document_id, line_no, line.name, line.quantity, line.unit, line.unit_price,
                line.unit_price_minor, line.net_minor, line.vat_rate, line.vat_minor, line.gross_minor,
                Jsonb(line.before) if line.before is not None else None,
                Jsonb(line.after) if line.after is not None else None,
                line.line_kind, Jsonb(extra),
                observation.source, observation.method, observation.confidence,
                Jsonb({"parser": facts.parser, "parser_version": facts.parser_version,
                       "raw_id": str(observation.raw_id)}),
                SCHEMA_VERSION, observation.prompt_version, observation.created_by,
            ),
        )


def _insert_observation(conn: psycopg.Connection, company_id: UUID, document_id: UUID, facts: InvoiceFacts,
                        observation: Observation, linked_by: str) -> bool:
    row = conn.execute(
        """
        INSERT INTO l1_document_observation (
            company_id, document_id, raw_id, locator, linked_by, observed, parser_version,
            source, method, confidence, evidence, schema_version, prompt_version, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (raw_id, locator, document_id) DO NOTHING
        RETURNING id
        """,
        (
            company_id, document_id, observation.raw_id, observation.locator, linked_by,
            Jsonb(facts.observed()), facts.parser_version, observation.source, observation.method,
            observation.confidence,
            Jsonb({"parser": facts.parser, "channel": facts.channel, **observation.evidence}),
            SCHEMA_VERSION, observation.prompt_version, observation.created_by,
        ),
    ).fetchone()
    return row is not None


def _supersede(conn: psycopg.Connection, old_id: UUID, new_id: UUID) -> None:
    conn.execute("UPDATE l1_document SET superseded_by = %s WHERE id = %s AND superseded_by IS NULL",
                 (new_id, old_id))


def _suggest_duplicate(conn: psycopg.Connection, company_id: UUID, document_id: UUID,
                       candidates: list[dict[str, Any]], facts: InvoiceFacts) -> None:
    for rank, candidate in enumerate(candidates, start=1):
        differences = {}
        if candidate["issue_date"] != facts.issue_date:
            differences["issue_date"] = [candidate["issue_date"].isoformat(), facts.issue_date.isoformat()]
        if candidate["gross_minor"] != facts.gross_minor or candidate["currency"] != facts.currency:
            differences["gross"] = [f"{candidate['gross_minor']} {candidate['currency']}",
                                    f"{facts.gross_minor} {facts.currency}"]
        if candidate["kind"] != facts.kind:
            differences["kind"] = [candidate["kind"], facts.kind]
        conn.execute(
            """
            INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, rank,
                                       source, method, evidence, created_by)
            VALUES (%s, 'duplicate', 'document', %s, %s, %s, 'documents.dedup', 'code', %s, 'kasa.documents')
            """,
            (
                company_id, document_id,
                Jsonb({"duplicate_of": str(candidate["id"]), "number": facts.number_raw,
                       "counterparty_tax_id": facts.counterparty.tax_key}),
                rank,
                Jsonb({"rule": "ten sam NIP i numer, inna data, kwota lub rodzaj", "differences": differences}),
            ),
        )


# ---------------------------------------------------------------------------
# The upsert
# ---------------------------------------------------------------------------


def upsert_document(conn: psycopg.Connection, company_id: UUID, facts: InvoiceFacts, observation: Observation,
                    *, caps: Capabilities | None = None) -> UpsertResult:
    """Link one observation to its document (creating or versioning the document when needed)."""
    caps = caps or capabilities(conn)
    with conn.transaction():
        return _upsert(conn, company_id, facts, observation, caps)


def _upsert(conn: psycopg.Connection, company_id: UUID, facts: InvoiceFacts, observation: Observation,
            caps: Capabilities) -> UpsertResult:
    version = _reader_version(facts, observation)

    # 0. this payload (and position in it) was read before
    seen = _rows(
        conn,
        """
        SELECT o.document_id, o.linked_by, o.parser_version, coalesce(o.prompt_version, '') AS prompt_version,
               l.head_id
          FROM l1_document_observation o
          JOIN document_lineage l ON l.document_id = o.document_id
         WHERE o.company_id = %s AND o.raw_id = %s AND o.locator IS NOT DISTINCT FROM %s
        """,
        (company_id, observation.raw_id, observation.locator),
    )
    if seen:
        head_id = seen[0]["head_id"]
        parser_version, prompt_version = version
        # Read already by this parser version or a newer one (same prompt): nothing to do - never go back.
        if any((s["parser_version"] or 0) >= parser_version and s["prompt_version"] == prompt_version for s in seen):
            return UpsertResult(head_id, "unchanged")
        head = _head(conn, head_id)
        head_evidence = head["evidence"] if isinstance(head["evidence"], dict) else {}
        # The current version carries the facts of this very payload: a new reading replaces it.
        if (head_evidence.get("raw_id") == str(observation.raw_id)
                and head_evidence.get("locator") == observation.locator):
            new_id = _insert_document(conn, company_id, facts, observation, caps,
                                      evidence={"supersedes": str(head_id), "reason": "nowa wersja odczytu"})
            _insert_observation(conn, company_id, new_id, facts, observation, "created")
            _supersede(conn, head_id, new_id)
            return UpsertResult(new_id, "new_version", "created", True)
        created = _insert_observation(conn, company_id, head_id, facts, observation, seen[0]["linked_by"])
        return UpsertResult(head_id, "linked" if created else "unchanged", seen[0]["linked_by"], created)

    # 1-3. find the document in order of certainty
    found: dict[str, Any] | None = None
    linked_by: str | None = None
    if facts.ksef_number:
        found = _current_by(conn, company_id, "ksef_number", facts.ksef_number)
        linked_by = "ksef_number" if found else None
    if found is None and facts.file_hash:
        found = _current_by(conn, company_id, "file_hash", facts.file_hash)
        linked_by = "file_hash" if found else None
    mismatched: list[dict[str, Any]] = []
    tax_key = facts.counterparty.tax_key
    if found is None and tax_key:
        candidates = _same_number_candidates(conn, company_id, facts.direction, facts.number_normalized, tax_key)
        matches = []
        for candidate in candidates:
            certain = candidate["evidence"].get("kind_certain", True) if isinstance(candidate["evidence"], dict) else True
            same = (
                _kinds_compatible(facts, candidate["kind"], certain)
                and candidate["issue_date"] == facts.issue_date
                and candidate["gross_minor"] == facts.gross_minor
                and candidate["currency"] == facts.currency
                and not (candidate["ksef_number"] and facts.ksef_number and candidate["ksef_number"] != facts.ksef_number)
            )
            (matches if same else mismatched).append(candidate)
        if len(matches) == 1:
            found, linked_by = matches[0], "nip_kind_number"
            mismatched = []
        elif len(matches) > 1:
            mismatched = matches + mismatched

    if found is None:
        evidence: dict[str, Any] = {}
        if mismatched:
            evidence["possible_duplicate"] = {
                "candidates": [str(c["id"]) for c in mismatched],
                "rule": "ten sam NIP i numer, ale inna data, kwota lub rodzaj - do kolejki",
            }
        document_id = _insert_document(conn, company_id, facts, observation, caps, evidence=evidence)
        _insert_observation(conn, company_id, document_id, facts, observation, "created")
        if mismatched:
            _suggest_duplicate(conn, company_id, document_id, mismatched, facts)
        return UpsertResult(document_id, "created", "created", True, [c["id"] for c in mismatched])

    # The document exists. A more certain channel brings its own fields as a new version.
    head_rank = CHANNEL_RANK.get(found["source"], 0)
    if CHANNEL_RANK.get(facts.channel, 0) > head_rank:
        new_id = _insert_document(conn, company_id, facts, observation, caps,
                                  evidence={"supersedes": str(found["id"]),
                                            "reason": f"kanał {facts.channel} pewniejszy niż {found['source']}"})
        _insert_observation(conn, company_id, new_id, facts, observation, linked_by)
        _supersede(conn, found["id"], new_id)
        return UpsertResult(new_id, "new_version", linked_by, True)
    created = _insert_observation(conn, company_id, found["id"], facts, observation, linked_by)
    return UpsertResult(found["id"], "linked" if created else "unchanged", linked_by, created)


# ---------------------------------------------------------------------------
# Links resolved later (a correction imported before the corrected invoice)
# ---------------------------------------------------------------------------

def _copy_columns(conn: psycopg.Connection, table: str, exclude: set[str]) -> str:
    """Every column of ``table`` except ``exclude`` (so columns added by later migrations are copied too)."""
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
         WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return ", ".join(name for (name,) in rows if name not in exclude)


def relink_pending(conn: psycopg.Connection, company_id: UUID) -> int:
    """Resolve correction/advance links that were unknown when the document was written.

    Each resolved link is a new version of the document (facts are never edited).
    Returns the number of new versions.
    """
    pending = _rows(
        conn,
        """
        SELECT id, kind, direction, corrects_ref, advance_refs, corrects_document_id, settles_advance_document_id,
               seller_nip, seller_foreign_tax_id, buyer_nip, buyer_foreign_tax_id, evidence
          FROM l1_document
         WHERE company_id = %s AND superseded_by IS NULL
           AND ((kind = 'correction' AND corrects_document_id IS NULL
                 AND (corrects_ref ? 'ksef_number' OR corrects_ref ? 'number'))
             OR (settles_advance_document_id IS NULL AND jsonb_array_length(advance_refs) > 0))
        """,
        (company_id,),
    )
    versions = 0
    document_columns = _copy_columns(conn, "l1_document", {
        "id", "created_at", "superseded_by", "corrects_document_id", "settles_advance_document_id", "evidence"})
    line_columns = _copy_columns(conn, "l1_document_line", {"id", "document_id", "created_at"})
    for doc in pending:
        tax_key = (doc["buyer_nip"] or doc["buyer_foreign_tax_id"]) if doc["direction"] == "sales" \
            else (doc["seller_nip"] or doc["seller_foreign_tax_id"])
        probe = _Probe(doc["direction"], tax_key)
        corrects = doc["corrects_document_id"]
        if doc["kind"] == "correction" and corrects is None:
            corrects = _find_related(conn, company_id, probe, doc["corrects_ref"] or {})  # type: ignore[arg-type]
        advance = doc["settles_advance_document_id"]
        if advance is None:
            for ref in doc["advance_refs"]:
                advance = _find_related(conn, company_id, probe, ref)  # type: ignore[arg-type]
                if advance:
                    break
        if corrects == doc["corrects_document_id"] and advance == doc["settles_advance_document_id"]:
            continue
        with conn.transaction():
            evidence = dict(doc["evidence"])
            evidence["supersedes"] = str(doc["id"])
            evidence["reason"] = "powiązanie rozpoznane później"
            evidence["relations"] = {**evidence.get("relations", {}),
                                     **({"corrects": "resolved"} if corrects else {}),
                                     **({"advance": "resolved"} if advance else {})}
            new_id = conn.execute(
                f"""
                INSERT INTO l1_document ({document_columns}, corrects_document_id, settles_advance_document_id,
                                         evidence)
                SELECT {document_columns}, %s, %s, %s FROM l1_document WHERE id = %s
                RETURNING id
                """,
                (corrects, advance, Jsonb(evidence), doc["id"]),
            ).fetchone()[0]
            conn.execute(
                f"""
                INSERT INTO l1_document_line (document_id, {line_columns})
                SELECT %s, {line_columns} FROM l1_document_line WHERE document_id = %s
                """,
                (new_id, doc["id"]),
            )
            _supersede(conn, doc["id"], new_id)
        versions += 1
    return versions


@dataclass(frozen=True)
class _Probe:
    """Minimal stand-in for InvoiceFacts in relation lookups of stored documents."""

    direction: str
    tax_key: str | None

    @property
    def counterparty(self) -> _Probe:
        return self


# ---------------------------------------------------------------------------
# KSeF channel
# ---------------------------------------------------------------------------


def ingest_ksef(conn: psycopg.Connection, company: Any, provider: Any, date_from: date, date_to: date,
                *, direction: str | None = None) -> IngestReport:
    """Fetch invoices from KSeF (or its mock), store the XML in L0 and the facts in L1."""
    from kasa.core.documents.fa3 import PARSER_VERSION, Fa3Error, parse_fa3
    from kasa.core.raw_store import store_raw

    report = IngestReport(channel="ksef")
    caps = capabilities(conn)
    company_nip = ensure_company_nip(conn, company.id, provider.subject_nip(company), source=f"KSeF ({provider.name})")
    for invoice in provider.fetch_invoices(company, date_from, date_to, direction):
        raw = store_raw(
            conn, company.id, "ksef", invoice.xml, content_type="application/xml",
            external_ref=invoice.ksef_number,
            meta={"provider": provider.name, "direction": invoice.direction,
                  "accepted_at": invoice.accepted_at.isoformat(), **invoice.meta},
        )
        report.raw(raw.created)
        try:
            facts = parse_fa3(invoice.xml, company_nip=company_nip, ksef_number=invoice.ksef_number,
                              accepted_at=invoice.accepted_at, direction_hint=invoice.direction)
        except Fa3Error as exc:
            report.errors.append(f"{invoice.ksef_number}: {exc}")
            continue
        if facts.direction != invoice.direction:
            report.warnings.append(
                f"{invoice.ksef_number}: KSeF podaje kierunek {invoice.direction}, z NIP-ów wynika {facts.direction}"
            )
        result = upsert_document(conn, company.id, facts, Observation(
            raw_id=raw.id, source="ksef", method="code",
            evidence={"provider": provider.name, "file_sha256": raw.sha256, "fa3_parser_version": PARSER_VERSION},
            created_by=f"ksef-sync:{provider.name}",
        ), caps=caps)
        report.add(result)
    report.versions_created += relink_pending(conn, company.id)
    return report


# ---------------------------------------------------------------------------
# JPK channel
# ---------------------------------------------------------------------------


def ingest_jpk(conn: psycopg.Connection, company: Any, payload: bytes, *, filename: str) -> IngestReport:
    """Import a JPK_V7M/V7K or JPK_FA file: every register row is an observation."""
    from kasa.core.documents.jpk import parse_jpk
    from kasa.core.raw_store import store_raw

    report = IngestReport(channel="jpk")
    caps = capabilities(conn)
    parsed = parse_jpk(payload, company_nip=get_company_nip(conn, company.id))
    ensure_company_nip(conn, company.id, parsed.subject_nip, source=f"{parsed.form} Podmiot1")
    raw = store_raw(conn, company.id, "file", payload, content_type="application/xml", external_ref=filename,
                    meta={"kind": parsed.form, "filename": filename, "period": parsed.period,
                          "declaration": parsed.declaration})
    report.raw(raw.created)
    report.skipped.extend(parsed.skipped)
    for locator, facts in parsed.rows:
        result = upsert_document(conn, company.id, facts, Observation(
            raw_id=raw.id, source="jpk", locator=locator, method="code",
            evidence={"file": filename, "form": parsed.form}, created_by="import-jpk",
        ), caps=caps)
        report.add(result)
    report.versions_created += relink_pending(conn, company.id)
    return report
