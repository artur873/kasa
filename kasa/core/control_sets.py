"""Control sets: reference labels for calibration (briefing, step 7 and "Plan budowy", phase 3).

The owner labels ~600 transactions and ~150 costs in the panel **without seeing any
proposal** - those labels calibrate the thresholds of phase 4 (gate 4: "0 false
automatic matches on a hand-labeled control set").

* Labels are reference truth, **not decisions**: they never go to ``l3_*``. A label is
  written once on ``control_set_item``; a change is kept by the database in
  ``control_set_label_history`` (migration 0008).
* About every 50th item is a sentinel with a known answer (``sentinel_expected``);
  whether it was passed is recorded, but shown only when the whole set is labeled.
* ``duration_ms`` = time from sending the card (``shown_at``) to the answer.
* Known answers (sentinels, fixture labels) are given to the core as ready labels
  ``{(subject_type, subject_id): label}``; reading them from a reference file (the
  fixtures' ``ground_truth.json``) is the job of ``kasa.reference.ground_truth``.
  ``export_set`` writes the labels in a shape comparable with that file.
"""

from __future__ import annotations

import random
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kasa.core.extension import ExtensionError, get_extension
from kasa.core.normalize import from_minor, normalize_invoice_number, parse_amount, to_minor
from kasa.core.sensitive import fold

KINDS = ("transaction", "cost")
SENTINEL_EVERY = 50
FAST_LABEL_MS = 800                 # briefing, step 7: a decision under 0.8 s is an alarm
FIXTURE_LABELER = "fixture"
STATUSES = ("matched", "no_document", "own_transfer", "to_clarify")
SUBTYPES = ("zus", "tax", "fee", "advance", "deposit", "card_awaiting", "salary", "other")
SUBTYPE_LABELS = {
    "zus": "ZUS", "tax": "podatek (US)", "fee": "prowizja / opłata / odsetki", "advance": "zaliczka",
    "deposit": "kaucja", "card_awaiting": "karta - czeka na fakturę", "salary": "wynagrodzenie", "other": "inne",
}
STATUS_LABELS = {
    "matched": "dopasowany do dokumentu", "no_document": "bez dokumentu", "own_transfer": "transfer własny",
    "to_clarify": "do wyjaśnienia",
}

class ControlSetError(ValueError):
    """A control set or a label cannot be created as asked."""


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


class DocRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["document", "internal_record", "transaction"]
    id: UUID


class TransactionLabel(BaseModel):
    """Final status of a transaction as the owner sees it (step 2: exactly one per booked transfer)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["matched", "no_document", "own_transfer", "to_clarify"]
    subtype: Literal["zus", "tax", "fee", "advance", "deposit", "card_awaiting", "salary", "other"] | None = None
    documents: list[DocRef] = Field(default_factory=list)
    other_document: bool = False            # matched, but to a document not on the list ("inna")
    note: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> TransactionLabel:
        if self.status == "matched":
            if not self.documents and not self.other_document:
                raise ValueError("status matched wymaga dokumentu z listy albo „inna”")
            if self.subtype is not None:
                raise ValueError("podtyp tylko dla statusu no_document")
        elif self.status == "no_document":
            if self.subtype is None:
                raise ValueError("status no_document wymaga podtypu")
            if self.documents or self.other_document:
                raise ValueError("bez dokumentu - nie wskazuje się dokumentu")
        elif self.subtype is not None or self.documents or self.other_document:
            raise ValueError(f"status {self.status} nie ma podtypu ani dokumentu")
        return self


class ShareItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    share: Decimal = Field(gt=0, le=1)


class CostLabel(BaseModel):
    """Category (from the extension) and cost object (by name - cost objects are approved only in phase 4)
    or UNASSIGNED."""

    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    unassigned: bool = False
    cost_object: str | None = None
    split: list[ShareItem] = Field(default_factory=list)
    note: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> CostLabel:
        targets = sum([self.unassigned, bool(self.cost_object and self.cost_object.strip()), bool(self.split)])
        if targets != 1:
            raise ValueError("wskaż jedno: obiekt kosztowy, podział albo UNASSIGNED")
        if self.split and abs(sum(s.share for s in self.split) - 1) > Decimal("0.0001"):
            raise ValueError("udziały podziału muszą sumować się do 100%")
        return self


def validate_label(kind: str, label: dict[str, Any], *, categories: Sequence[str] | None = None) -> dict[str, Any]:
    """The label as stored (JSON-ready); raises ControlSetError with a readable message."""
    try:
        model = TransactionLabel.model_validate(label) if kind == "transaction" else CostLabel.model_validate(label)
    except ValidationError as exc:
        messages = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
        raise ControlSetError(f"niepoprawna etykieta: {messages}") from None
    if isinstance(model, CostLabel) and model.category and categories and model.category not in categories:
        raise ControlSetError(f"kategoria {model.category!r} spoza listy rozszerzenia")
    return model.model_dump(mode="json", exclude_none=True, exclude_defaults=False)


# ---------------------------------------------------------------------------
# Subjects of a set
# ---------------------------------------------------------------------------


def _candidates(conn: psycopg.Connection, company_id: UUID, kind: str) -> list[tuple[str, UUID]]:
    if kind == "transaction":
        rows = conn.execute(
            """
            SELECT id FROM l1_bank_transaction
             WHERE company_id = %s AND superseded_by IS NULL AND status = 'booked' ORDER BY id
            """,
            (company_id,),
        ).fetchall()
        return [("transaction", r[0]) for r in rows]
    documents = conn.execute(
        """
        SELECT id FROM l1_document
         WHERE company_id = %s AND superseded_by IS NULL AND direction = 'purchase' ORDER BY id
        """,
        (company_id,),
    ).fetchall()
    records = conn.execute(
        "SELECT id FROM l1_internal_record WHERE company_id = %s AND superseded_by IS NULL ORDER BY id",
        (company_id,),
    ).fetchall()
    return [("document", r[0]) for r in documents] + [("internal_record", r[0]) for r in records]


def sentinel_passed(kind: str, label: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Did the label agree with the known answer?"""
    if kind == "transaction":
        if label.get("status") != expected.get("status"):
            return False
        if expected.get("status") == "no_document":
            return label.get("subtype") == expected.get("subtype")
        if expected.get("status") == "matched" and expected.get("documents"):
            got = {(d["type"], str(d["id"])) for d in label.get("documents") or []}
            want = {(d["type"], str(d["id"])) for d in expected["documents"]}
            return got == want
        return True
    if expected.get("unassigned"):
        return bool(label.get("unassigned"))
    if expected.get("cost_object"):
        return fold((label.get("cost_object") or "").strip()) == fold(expected["cost_object"].strip())
    if expected.get("split"):
        want = {fold(s["name"]): Decimal(str(s["share"])) for s in expected["split"]}
        got = {fold(s["name"]): Decimal(str(s["share"])) for s in label.get("split") or []}
        return want.keys() == got.keys() and all(abs(want[k] - got[k]) <= Decimal("0.01") for k in want)
    return True


# ---------------------------------------------------------------------------
# Creating a set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlSetInfo:
    id: UUID
    company_id: UUID
    name: str
    kind: str
    size_target: int
    items: int
    sentinels: int
    frozen_at: datetime | None
    completed_at: datetime | None
    seed: int | None


def _info(conn: psycopg.Connection, set_id: UUID) -> ControlSetInfo:
    row = conn.execute(
        """
        SELECT s.id, s.company_id, s.name, s.kind::text, s.size_target,
               (SELECT count(*) FROM control_set_item i WHERE i.set_id = s.id),
               (SELECT count(*) FROM control_set_item i WHERE i.set_id = s.id AND i.is_sentinel),
               s.frozen_at, s.completed_at, s.seed
          FROM control_set s WHERE s.id = %s
        """,
        (set_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma zbioru kontrolnego {set_id}")
    return ControlSetInfo(*row)


def _unique_name(conn: psycopg.Connection, company_id: UUID, base: str) -> str:
    name, n = base, 1
    while conn.execute("SELECT 1 FROM control_set WHERE company_id = %s AND name = %s", (company_id, name)).fetchone():
        n += 1
        name = f"{base}-{n}"
    return name


def _sentinel_positions(n: int, k: int) -> list[int]:
    """Order numbers (1-based) of k sentinels spread evenly over n items (about every 50th)."""
    return sorted({max(1, min(n, round((i + 1) * n / (k + 1)))) for i in range(k)})


def create_control_set(conn: psycopg.Connection, company: Any, *, kind: str, size: int, name: str | None = None,
                       seed: int | None = None, expected: dict[tuple[str, UUID], dict[str, Any]] | None = None,
                       created_by: str = "owner") -> ControlSetInfo:
    """Draw a set (at random, seeded) of ``size`` subjects - all of them when fewer exist - and add sentinels."""
    if kind not in KINDS:
        raise ControlSetError(f"rodzaj zbioru {kind!r} - dozwolone: {', '.join(KINDS)}")
    if size <= 0:
        raise ControlSetError("rozmiar zbioru musi być dodatni")
    seed = seed if seed is not None else secrets.randbelow(2**31)
    rng = random.Random(seed)
    candidates = _candidates(conn, company.id, kind)
    if not candidates:
        raise ControlSetError("brak danych do zbioru (najpierw import banku / dokumentów)")
    known = expected or {}
    n = min(size, len(candidates))
    pool = sorted(s for s in candidates if s in known)
    k = min(len(pool), n, max(1, n // SENTINEL_EVERY)) if pool else 0
    sentinels = rng.sample(pool, k) if k else []
    rest = sorted(set(candidates) - set(sentinels))
    regular = rng.sample(rest, n - k)
    rng.shuffle(regular)
    positions = _sentinel_positions(n, k)
    ordered: list[tuple[tuple[str, UUID], bool]] = []
    queue_regular, queue_sentinel = list(regular), list(sentinels)
    for order_no in range(1, n + 1):
        if queue_sentinel and order_no in positions:
            ordered.append((queue_sentinel.pop(0), True))
        else:
            ordered.append((queue_regular.pop(0), False))
    set_name = _unique_name(conn, company.id, name or f"{kind}-{date.today().isoformat()}-{n}")
    set_id = conn.execute(
        """
        INSERT INTO control_set (company_id, name, kind, size_target, seed, params, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (company.id, set_name, kind, size, seed,
         Jsonb({"available": len(candidates), "sentinel_pool": len(pool), "sentinel_every": SENTINEL_EVERY}),
         created_by),
    ).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO control_set_item (set_id, company_id, subject_type, subject_id, order_no, is_sentinel,
                                          sentinel_expected)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [(set_id, company.id, subject[0], subject[1], order_no, is_sentinel,
              Jsonb(known[subject]) if is_sentinel else None)
             for order_no, (subject, is_sentinel) in enumerate(ordered, start=1)],
        )
    conn.execute("UPDATE control_set SET frozen_at = now() WHERE id = %s", (set_id,))
    return _info(conn, set_id)


def list_sets(conn: psycopg.Connection, company_id: UUID) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT s.id, s.name, s.kind::text AS kind, s.size_target, s.created_at, s.completed_at,
                   count(i.id) AS total, count(i.label) AS labeled
              FROM control_set s LEFT JOIN control_set_item i ON i.set_id = s.id
             WHERE s.company_id = %s
             GROUP BY s.id ORDER BY s.created_at DESC, s.name
            """,
            (company_id,),
        ).fetchall()


def get_set(conn: psycopg.Connection, set_id: UUID) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """
            SELECT s.*, s.kind::text AS kind, c.slug AS company_slug, c.extension_slug
              FROM control_set s JOIN company c ON c.id = s.company_id WHERE s.id = %s
            """,
            (set_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma zbioru kontrolnego {set_id}")
    return row


def find_set(conn: psycopg.Connection, ref: str) -> UUID:
    """A set by id, id prefix or name."""
    rows = conn.execute(
        "SELECT id FROM control_set WHERE id::text LIKE %s || '%%' OR name = %s", (ref.lower(), ref),
    ).fetchall()
    if len(rows) != 1:
        raise LookupError(f"Zbiór kontrolny {ref!r}: " + ("nie ma takiego" if not rows else "niejednoznaczny"))
    return rows[0][0]


# ---------------------------------------------------------------------------
# Labeling
# ---------------------------------------------------------------------------


def _item(conn: psycopg.Connection, where: str, params: tuple) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT i.*, i.subject_type::text AS subject_type, s.kind::text AS set_kind,
                   (SELECT count(*) FROM control_set_item x WHERE x.set_id = i.set_id) AS total,
                   (SELECT count(x.label) FROM control_set_item x WHERE x.set_id = i.set_id) AS labeled
              FROM control_set_item i JOIN control_set s ON s.id = i.set_id
             WHERE {where}
            """,
            params,
        ).fetchone()


def get_item(conn: psycopg.Connection, set_id: UUID, item_id: UUID) -> dict[str, Any]:
    item = _item(conn, "i.set_id = %s AND i.id = %s", (set_id, item_id))
    if item is None:
        raise LookupError(f"Nie ma pozycji {item_id} w zbiorze {set_id}")
    return item


def next_item(conn: psycopg.Connection, set_id: UUID, *, after: int | None = None,
              mark_shown: bool = True) -> dict[str, Any] | None:
    """The next item without a label (after order number ``after``, wrapping around); records ``shown_at``."""
    item = None
    if after is not None:
        item = _item(conn, "i.set_id = %s AND i.label IS NULL AND i.order_no > %s ORDER BY i.order_no LIMIT 1",
                     (set_id, after))
    if item is None:
        item = _item(conn, "i.set_id = %s AND i.label IS NULL ORDER BY i.order_no LIMIT 1", (set_id,))
    if item is not None and mark_shown:
        item["shown_at"] = conn.execute(
            "UPDATE control_set_item SET shown_at = clock_timestamp() WHERE id = %s RETURNING shown_at",
            (item["id"],),
        ).fetchone()[0]
    return item


def save_label(conn: psycopg.Connection, set_id: UUID, item_id: UUID, label: dict[str, Any], *,
               labeled_by: str, duration_ms: int | None = None) -> dict[str, Any]:
    """Store a label (first time on the item, later changes go to the history - by the database)."""
    meta = get_set(conn, set_id)
    item = get_item(conn, set_id, item_id)
    categories = None
    if meta["kind"] == "cost":
        try:
            categories = get_extension(meta["extension_slug"]).categories
        except ExtensionError:
            categories = None
    stored = validate_label(meta["kind"], label, categories=categories)
    passed = sentinel_passed(meta["kind"], stored, item["sentinel_expected"]) if item["is_sentinel"] else None
    conn.execute(
        """
        UPDATE control_set_item
           SET label = %s, labeled_by = %s, labeled_at = clock_timestamp(),
               duration_ms = coalesce(%s, CASE WHEN shown_at IS NOT NULL AND label IS NULL
                   THEN (extract(epoch FROM clock_timestamp() - shown_at) * 1000)::integer END, duration_ms),
               sentinel_passed = %s
         WHERE id = %s
        """,
        (Jsonb(stored), labeled_by, duration_ms, passed, item_id),
    )
    remaining = conn.execute(
        "SELECT count(*) FROM control_set_item WHERE set_id = %s AND label IS NULL", (set_id,),
    ).fetchone()[0]
    if remaining == 0:
        conn.execute("UPDATE control_set SET completed_at = clock_timestamp() WHERE id = %s AND completed_at IS NULL",
                     (set_id,))
    return get_item(conn, set_id, item_id)


# ---------------------------------------------------------------------------
# What a card shows (plain data: the labeler is the owner) - never a proposal
# ---------------------------------------------------------------------------


def _money(minor: int | None, currency: str) -> str:
    if minor is None:
        return "-"
    value = from_minor(minor, currency)
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + f" {currency}"


def card(conn: psycopg.Connection, item: dict[str, Any]) -> dict[str, Any]:
    """Facts of the subject of an item, as the card shows them."""
    subject_type, subject_id = item["subject_type"], item["subject_id"]
    with conn.cursor(row_factory=dict_row) as cur:
        if subject_type == "transaction":
            row = cur.execute(
                """
                SELECT t.id, t.booking_date, t.amount_minor, t.currency, t.counterparty_name_raw, t.title_raw,
                       t.op_type::text AS op_type, t.counterparty_id, a.iban AS account_iban,
                       a.kind::text AS account_kind
                  FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id
                 WHERE t.id = %s
                """,
                (subject_id,),
            ).fetchone()
            return {"type": "transaction", **row, "amount": _money(row["amount_minor"], row["currency"]),
                    "direction": "wpływ" if row["amount_minor"] > 0 else "wypływ"}
        if subject_type == "document":
            row = cur.execute(
                """
                SELECT id, number_raw, kind::text AS kind, direction::text AS direction, issue_date, sale_date,
                       seller_name, seller_nip, buyer_name, currency, gross_minor, net_minor, source
                  FROM l1_document WHERE id = %s
                """,
                (subject_id,),
            ).fetchone()
            lines = cur.execute(
                "SELECT name, gross_minor, net_minor FROM l1_document_line WHERE document_id = %s ORDER BY line_no",
                (subject_id,),
            ).fetchall()
            return {"type": "document", **row, "amount": _money(row["gross_minor"], row["currency"]),
                    "lines": [{"name": line["name"], "amount": _money(line["gross_minor"] or line["net_minor"],
                                                                      row["currency"])} for line in lines]}
        row = cur.execute(
            """
            SELECT r.id, r.fields, r.record_date, r.amount_minor, r.currency, r.cost_object_ref, r.settlement_type,
                   f.external_ref AS file
              FROM l1_internal_record r JOIN l0_raw f ON f.id = r.file_raw_id WHERE r.id = %s
            """,
            (subject_id,),
        ).fetchone()
        return {"type": "internal_record", **row, "amount": _money(row["amount_minor"], row["currency"])}


def search_documents(conn: psycopg.Connection, company_id: UUID, *, query: str | None = None,
                     counterparty_id: UUID | None = None, direction: str | None = None,
                     limit: int = 25) -> list[dict[str, Any]]:
    """Documents (and rows of internal sheets) to pick for a "matched" label.

    Without ``query``: open documents of the counterparty (no active match). With it:
    any current document whose number contains the query or whose gross amount equals it,
    and sheet rows with that amount. Neutral order (newest first) - no ranking, no score.
    """
    amount_minor = None
    if query:
        try:
            amount_minor = to_minor(parse_amount(query.strip()), "PLN")
        except (ValueError, InvalidOperation, ArithmeticError):
            amount_minor = None
    try:
        number_key = normalize_invoice_number(query) if query else None
    except (TypeError, ValueError):
        number_key = None
    with conn.cursor(row_factory=dict_row) as cur:
        if not query:
            if counterparty_id is None:
                return []
            docs = cur.execute(
                """
                SELECT d.id, 'document' AS type, d.number_raw, d.issue_date, d.gross_minor, d.currency,
                       d.direction::text AS direction,
                       CASE d.direction WHEN 'sales' THEN d.buyer_name ELSE d.seller_name END AS party
                  FROM l1_document d
                 WHERE d.company_id = %s AND d.superseded_by IS NULL AND d.counterparty_id = %s
                   AND (%s::text IS NULL OR d.direction::text = %s::text)
                   AND NOT EXISTS (SELECT 1 FROM l3_match m WHERE m.document_id = d.id AND m.status = 'active')
                 ORDER BY d.issue_date DESC, d.number_raw LIMIT %s
                """,
                (company_id, counterparty_id, direction, direction, limit),
            ).fetchall()
            return [{**d, "amount": _money(d["gross_minor"], d["currency"])} for d in docs]
        docs = cur.execute(
            """
            SELECT d.id, 'document' AS type, d.number_raw, d.issue_date, d.gross_minor, d.currency,
                   d.direction::text AS direction,
                   CASE d.direction WHEN 'sales' THEN d.buyer_name ELSE d.seller_name END AS party
              FROM l1_document d
             WHERE d.company_id = %s AND d.superseded_by IS NULL
               AND ((%s::text IS NOT NULL AND d.number_normalized LIKE '%%' || %s::text || '%%')
                    OR (%s::bigint IS NOT NULL AND abs(d.gross_minor) = abs(%s::bigint)))
             ORDER BY d.issue_date DESC, d.number_raw LIMIT %s
            """,
            (company_id, number_key, number_key, amount_minor, amount_minor, limit),
        ).fetchall()
        records = []
        if amount_minor is not None:
            records = cur.execute(
                """
                SELECT r.id, 'internal_record' AS type, r.cost_object_ref AS number_raw, r.record_date AS issue_date,
                       r.amount_minor AS gross_minor, r.currency, NULL AS direction,
                       r.fields ->> (r.evidence -> 'column_map' ->> 'person') AS party
                  FROM l1_internal_record r
                 WHERE r.company_id = %s AND r.superseded_by IS NULL AND abs(r.amount_minor) = abs(%s::bigint)
                 ORDER BY r.record_date DESC LIMIT %s
                """,
                (company_id, amount_minor, limit),
            ).fetchall()
    return [{**d, "amount": _money(d["gross_minor"], d["currency"])} for d in [*docs, *records]]


def describe_refs(conn: psycopg.Connection, refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Number and amount of the documents a label points at (for export and the summary)."""
    out = []
    for ref in refs:
        kind, ref_id = ref["type"], ref["id"]
        if kind == "document":
            row = conn.execute(
                """
                SELECT d.ksef_number, d.number_raw, d.gross_minor, d.currency, d.source,
                       (SELECT r.meta -> 'email' ->> 'id' FROM document_lineage l
                          JOIN l1_document_observation o ON o.document_id = l.document_id
                          JOIN l0_raw r ON r.id = o.raw_id
                         WHERE l.head_id = d.id AND o.source = 'pdf' LIMIT 1)
                  FROM l1_document d WHERE d.id = %s
                """,
                (ref_id,),
            ).fetchone()
            if row:
                ksef, number, gross, currency, source, email_id = row
                doc_type, doc_id = (("ksef", ksef) if ksef else ("email_pdf", email_id or number) if source == "pdf"
                                    else ("document", str(ref_id)))
                out.append({"doc_type": doc_type, "doc_id": doc_id, "invoice_number": number,
                            "amount": str(from_minor(gross, currency)), "doc_currency": currency, "source": source,
                            "kasa_id": str(ref_id)})
        elif kind == "internal_record":
            row = conn.execute(
                """
                SELECT r.row_no, f.external_ref, r.amount_minor, r.currency, r.mapping_key
                  FROM l1_internal_record r JOIN l0_raw f ON f.id = r.file_raw_id WHERE r.id = %s
                """,
                (ref_id,),
            ).fetchone()
            if row:
                # doc_type = the extension's mapping key of the file; doc_id = file:R<data row> (row 1 = header)
                out.append({"doc_type": row[4], "doc_id": f"{Path(row[1] or '').stem}:R{row[0] - 1:02d}",
                            "sheet_row": row[0], "amount": str(from_minor(row[2], row[3])), "kasa_id": str(ref_id)})
        else:
            row = conn.execute("SELECT entry_reference FROM l1_bank_transaction WHERE id = %s", (ref_id,)).fetchone()
            if row:
                out.append({"doc_type": "bank_tx", "doc_id": row[0], "kasa_id": str(ref_id)})
    return out


# ---------------------------------------------------------------------------
# Status and export
# ---------------------------------------------------------------------------


def status(conn: psycopg.Connection, set_id: UUID) -> dict[str, Any]:
    """Progress; sentinel results only after the whole set is labeled."""
    meta = get_set(conn, set_id)
    rows = conn.execute(
        """
        SELECT count(*), count(label), count(*) FILTER (WHERE is_sentinel),
               count(*) FILTER (WHERE is_sentinel AND sentinel_passed),
               count(*) FILTER (WHERE is_sentinel AND sentinel_passed IS FALSE),
               count(*) FILTER (WHERE duration_ms IS NOT NULL AND duration_ms < %s),
               array_remove(array_agg(duration_ms), NULL),
               count(*) FILTER (WHERE labeled_by = %s)
          FROM control_set_item WHERE set_id = %s
        """,
        (FAST_LABEL_MS, FIXTURE_LABELER, set_id),
    ).fetchone()
    total, labeled, sentinels, passed, failed, fast, durations, fixture = rows
    changes = conn.execute(
        """
        SELECT count(*) FROM control_set_label_history h JOIN control_set_item i ON i.id = h.item_id
         WHERE i.set_id = %s
        """,
        (set_id,),
    ).fetchone()[0]
    out: dict[str, Any] = {
        "id": str(meta["id"]), "name": meta["name"], "kind": meta["kind"], "company": meta["company_slug"],
        "size_target": meta["size_target"], "total": total, "labeled": labeled,
        "progress": f"{labeled}/{total}", "completed": meta["completed_at"] is not None,
        "sentinels": sentinels, "label_changes": changes, "fixture_labels": fixture,
        "fast_labels": fast, "median_duration_ms": int(median(durations)) if durations else None,
    }
    if meta["completed_at"] is not None:
        out["sentinel_results"] = {"passed": passed, "failed": failed}
    return out


def export_set(conn: psycopg.Connection, set_id: UUID) -> dict[str, Any]:
    """Labels of a set in a shape comparable with the reference file of the fixtures (``ground_truth.json``)."""
    meta = get_set(conn, set_id)
    with conn.cursor(row_factory=dict_row) as cur:
        items = cur.execute(
            """
            SELECT i.*, i.subject_type::text AS subject_type FROM control_set_item i
             WHERE i.set_id = %s ORDER BY i.order_no
            """,
            (set_id,),
        ).fetchall()
    head = {
        "set": {"id": str(meta["id"]), "name": meta["name"], "kind": meta["kind"], "company": meta["company_slug"],
                "seed": meta["seed"], "created_at": meta["created_at"].isoformat(),
                "completed_at": meta["completed_at"].isoformat() if meta["completed_at"] else None},
        "exported_at": datetime.now().astimezone().isoformat(),
        "status": status(conn, set_id),
        "conventions": {
            "expected_status": "matched | no_document | own_transfer | to_clarify (etykieta człowieka)",
            "amounts": "kwoty jako napisy dziesiętne z kropką, transakcje ze znakiem (+ wpływ, - wypływ)",
            "note": "etykiety zbioru kontrolnego - prawda referencyjna do kalibracji, nie decyzje L3",
        },
    }
    entries = []
    for item in items:
        facts = card(conn, item)
        label = item["label"]
        base = {"order_no": item["order_no"], "subject_type": item["subject_type"],
                "subject_id": str(item["subject_id"]), "labeled": label is not None,
                "labeled_by": item["labeled_by"],
                "labeled_at": item["labeled_at"].isoformat() if item["labeled_at"] else None,
                "duration_ms": item["duration_ms"], "is_sentinel": item["is_sentinel"],
                "sentinel_passed": item["sentinel_passed"], "label": label}
        if meta["kind"] == "transaction":
            ref = conn.execute("SELECT entry_reference FROM l1_bank_transaction WHERE id = %s",
                               (item["subject_id"],)).fetchone()[0]
            entries.append({
                "entry_reference": ref, "booking_date": facts["booking_date"].isoformat(),
                "amount": str(from_minor(facts["amount_minor"], facts["currency"])), "currency": facts["currency"],
                "counterparty": facts["counterparty_name_raw"], "title": facts["title_raw"],
                "op_type": facts["op_type"],
                "expected_status": label.get("status") if label else None,
                "no_document_subtype": label.get("subtype") if label else None,
                "matched_documents": describe_refs(conn, label.get("documents") or []) if label else [],
                "other_document": bool(label.get("other_document")) if label else False,
                **base,
            })
        else:
            if facts["type"] == "document":
                ident = {"doc_type": "document", "number": facts["number_raw"], "party": facts["seller_name"],
                         "amount": str(from_minor(facts["gross_minor"], facts["currency"])),
                         "currency": facts["currency"]}
            else:
                ident = {"doc_type": "internal_record", "cost_object_ref": facts["cost_object_ref"],
                         "amount": str(from_minor(facts["amount_minor"], facts["currency"])),
                         "currency": facts["currency"]}
            allocation = None
            if label:
                if label.get("unassigned"):
                    allocation = [{"cost_object_name": "UNASSIGNED", "share": "1"}]
                elif label.get("cost_object"):
                    allocation = [{"cost_object_name": label["cost_object"], "share": "1"}]
                else:
                    allocation = [{"cost_object_name": s["name"], "share": str(s["share"])} for s in label["split"]]
            entries.append({**ident, "category": label.get("category") if label else None,
                            "final_allocation": allocation, **base})
    return {**head, ("transactions" if meta["kind"] == "transaction" else "costs"): entries}


def import_labels(conn: psycopg.Connection, company: Any, *, kind: str,
                  expected: dict[tuple[str, UUID], dict[str, Any]], set_id: UUID | None = None,
                  name: str | None = None, labeled_by: str = FIXTURE_LABELER,
                  source: str = "reference") -> ControlSetInfo:
    """Known labels as labels of a set (``labeled_by = 'fixture'``), e.g. from the fixtures' reference file.

    Without ``set_id`` a new set with every subject of ``expected`` is created (phase 4 gets a
    control set without clicking); with it, unlabeled items of that set get labels.
    """
    if kind not in KINDS:
        raise ControlSetError(f"rodzaj zbioru {kind!r} - dozwolone: {', '.join(KINDS)}")
    if not expected:
        raise ControlSetError("żaden podmiot z pliku prawdy referencyjnej nie istnieje w bazie")
    if set_id is None:
        set_name = _unique_name(conn, company.id, name or f"reference-{kind}")
        set_id = conn.execute(
            """
            INSERT INTO control_set (company_id, name, kind, size_target, params, created_by)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (company.id, set_name, kind, len(expected), Jsonb({"source": source}), labeled_by),
        ).fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO control_set_item (set_id, company_id, subject_type, subject_id, order_no)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [(set_id, company.id, s[0], s[1], n) for n, s in enumerate(sorted(expected), start=1)],
            )
        conn.execute("UPDATE control_set SET frozen_at = now() WHERE id = %s", (set_id,))
    meta = get_set(conn, set_id)
    if meta["kind"] != kind:
        raise ControlSetError(f"zbiór {meta['name']} jest rodzaju {meta['kind']}, nie {kind}")
    pending = conn.execute(
        "SELECT id, subject_type::text, subject_id FROM control_set_item WHERE set_id = %s AND label IS NULL",
        (set_id,),
    ).fetchall()
    for item_id, subject_type, subject_id in pending:
        label = expected.get((subject_type, subject_id))
        if label is not None:
            save_label(conn, set_id, item_id, label, labeled_by=labeled_by)
    return _info(conn, set_id)
