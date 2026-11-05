"""Decision on a match suggestion (L3), the one write path of matching decisions (phase 4 contract).

``apply_match(conn, suggestion_id, *, decided_by, method='human'|'rule', rule_id=None,
rule_version=None, reason=None) -> decision_id``:

* one ``l3_decision_log`` row (kind ``match``, subject = the transaction, payload = the proposal);
* ``l3_match`` rows, one ``group_id`` per suggestion (three invoices in one transfer, an invoice
  with its correction - negative row -, the legs of a split payment);
* ``l3_transaction_status`` for the transaction (with ``awaiting_until``); a split payment: ONE
  decision closes every leg (the VAT leg gets the same final status and its own match row with
  the amount of the leg);
* the suggestion -> accepted, the other open match suggestions of the same transaction(s) ->
  rejected, both with ``resolved_by_decision``.

``method='rule'`` is the automation: the database (KS004) refuses it unless ``rule_id`` /
``rule_version`` point at a rule of the company in stage ``auto``; here the automation is also
refused when the suggestion itself does not meet the three conditions (score >= 0.995, hard
evidence, margin >= 0.2) or a target is no longer open for the proposed amount. A human may decide anything
(truth check 2 shows a match above the amount of the transfer or of the invoice).

Revoking (4C, ``revoke_decision``) sets ``status = 'revoked'`` on these rows: balances are computed
from active rows only, so a revoked match gives its amount back to the invoice.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kasa.core.decisions import log_decision
from kasa.core.matching.weights import MIN_AUTO_THRESHOLD, MIN_MARGIN

AUTO_SOURCE = "kasa.matching"
HUMAN_SOURCE = "queue"


class MatchDecisionError(ValueError):
    """The suggestion cannot be accepted as asked."""


def _suggestion(conn: psycopg.Connection, suggestion_id: UUID) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """
            SELECT id, company_id, kind::text AS kind, subject_type::text AS subject_type, subject_id, proposal,
                   score, rank, status::text AS status, evidence
              FROM l2_suggestion WHERE id = %s FOR UPDATE
            """,
            (suggestion_id,),
        ).fetchone()
    if row is None:
        raise MatchDecisionError(f"nie ma sugestii {suggestion_id}")
    if row["kind"] != "match" or row["subject_type"] != "transaction":
        raise MatchDecisionError(f"sugestia {suggestion_id} nie jest dopasowaniem przelewu ({row['kind']})")
    if row["status"] != "open":
        raise MatchDecisionError(f"sugestia {suggestion_id} ma status {row['status']} - tylko otwarta")
    return row


def _legs(conn: psycopg.Connection, company_id: UUID, transaction_id: UUID) -> list[dict[str, Any]]:
    """The transaction, or every leg of its split payment that settles the invoice (main first)."""
    with conn.cursor(row_factory=dict_row) as cur:
        tx = cur.execute(
            """
            SELECT id, amount_minor, currency, mpp_group_id, mpp_role, status::text AS status, superseded_by
              FROM l1_bank_transaction WHERE id = %s AND company_id = %s
            """,
            (transaction_id, company_id),
        ).fetchone()
        if tx is None or tx["status"] != "booked" or tx["superseded_by"] is not None:
            raise MatchDecisionError(f"przelew {transaction_id} nie jest bieżącą zaksięgowaną operacją")
        if tx["mpp_group_id"] is None or tx["mpp_role"] != "main":
            return [tx]
        legs = cur.execute(
            """
            SELECT id, amount_minor, currency, mpp_group_id, mpp_role FROM l1_bank_transaction
             WHERE company_id = %s AND mpp_group_id = %s AND mpp_role IN ('main', 'vat_leg')
               AND superseded_by IS NULL AND status = 'booked'
             ORDER BY (mpp_role <> 'main'), id
            """,
            (company_id, tx["mpp_group_id"]),
        ).fetchall()
    return legs or [tx]


def _target_open(conn: psycopg.Connection, item: dict[str, Any]) -> int | None:
    """Open amount of a target now (document side, its currency), from active matches only."""
    if item.get("document_id"):
        row = conn.execute(
            """
            SELECT d.gross_minor - coalesce((
                   SELECT sum(coalesce(m.document_amount_minor, m.amount_minor)) FROM l3_match m
                     JOIN document_lineage l ON l.document_id = m.document_id
                    WHERE m.status = 'active' AND l.head_id = d.id), 0)
              FROM l1_document d WHERE d.id = %s
            """,
            (item["document_id"],),
        ).fetchone()
        return int(row[0]) if row else None
    if item.get("internal_record_id"):
        row = conn.execute(
            """
            SELECT r.amount_minor - coalesce((
                   SELECT sum(coalesce(m.document_amount_minor, m.amount_minor)) FROM l3_match m
                    WHERE m.status = 'active' AND m.internal_record_id = r.id), 0)
              FROM l1_internal_record r WHERE r.id = %s
            """,
            (item["internal_record_id"],),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    return None


def _document_side(item: dict[str, Any], detail: dict[str, Any] | None) -> int:
    """Amount of the target settled by an item, in the target currency.

    Taken from the suggestion's detail when present (a foreign invoice: the amount in EUR);
    otherwise derived: an underpayment settles what was paid, a correction row is itself,
    any other named difference (tolerance, fee, overpayment) closes the target at amount - difference.
    """
    if detail and detail.get("document_amount_minor") is not None:
        return int(detail["document_amount_minor"])
    amount = int(item["amount_minor"])
    kind, diff = item.get("difference_kind"), item.get("difference_minor")
    if kind in (None, "underpayment", "correction") or diff is None:
        return amount
    return amount - int(diff)


def _split(amount: int, legs: list[dict[str, Any]]) -> list[int]:
    """An occurrence amount over the legs of a split payment, in proportion to the legs (exact sum)."""
    total = sum(abs(int(leg["amount_minor"])) for leg in legs)
    if total == 0:
        return [amount] + [0] * (len(legs) - 1)
    shares = [amount * abs(int(leg["amount_minor"])) // total for leg in legs]
    shares[0] += amount - sum(shares)
    return shares


def apply_match(conn: psycopg.Connection, suggestion_id: UUID, *, decided_by: str, method: str = "human",
                rule_id: UUID | None = None, rule_version: int | None = None, reason: str | None = None,
                source: str | None = None) -> UUID:
    """Accept a match suggestion: decision log, matches, final status, suggestions resolved. Returns the decision id."""
    if method not in ("human", "rule"):
        raise MatchDecisionError(f"metoda {method!r} - decyduje człowiek albo reguła w etapie auto")
    if method == "rule" and (rule_id is None or rule_version is None):
        raise MatchDecisionError("automat musi wskazać regułę (rule_id i rule_version)")
    if method == "human" and (rule_id is not None or rule_version is not None):
        raise MatchDecisionError("decyzja człowieka nie wskazuje reguły")
    suggestion = _suggestion(conn, suggestion_id)
    company_id, transaction_id = suggestion["company_id"], suggestion["subject_id"]
    proposal = suggestion["proposal"]
    final_status = proposal.get("final_status")
    if final_status not in ("matched", "no_document", "own_transfer", "to_clarify"):
        raise MatchDecisionError(f"nieznany status końcowy {final_status!r}")
    items = list(proposal.get("items") or [])
    if final_status == "matched" and not items:
        raise MatchDecisionError("status matched wymaga co najmniej jednej pozycji dopasowania")
    legs = _legs(conn, company_id, transaction_id)
    leg_ids = [leg["id"] for leg in legs]
    decided = conn.execute(
        "SELECT transaction_id FROM l3_transaction_status WHERE transaction_id = ANY(%s) AND status = 'active'",
        (leg_ids,),
    ).fetchall()
    if decided:
        raise MatchDecisionError(f"przelew {decided[0][0]} ma już aktywną decyzję - najpierw ją cofnij")
    details = ((suggestion["evidence"] or {}).get("detail") or {}).get("items") or []
    awaiting = proposal.get("awaiting_until")
    awaiting_until = date.fromisoformat(awaiting) if awaiting else None

    if method == "rule":
        # the three conditions of the briefing hold on the suggestion itself, whoever calls
        evidence = suggestion["evidence"] or {}
        score_now = float(suggestion["score"] or 0)
        if not evidence.get("hard") or score_now < MIN_AUTO_THRESHOLD or float(evidence.get("margin") or 0) < MIN_MARGIN:
            raise MatchDecisionError(
                f"automat: sugestia {suggestion_id} nie spełnia trzech warunków (pewność {score_now:.4f}, "
                f"twardy dowód {bool(evidence.get('hard'))}, przewaga {float(evidence.get('margin') or 0):.2f})")
        for index, item in enumerate(items):
            side = _document_side(item, details[index] if index < len(details) else None)
            available = _target_open(conn, item)
            if available is None or item.get("difference_kind") == "overpayment":
                continue
            if (side > 0 and available < side) or (side < 0 and available > side):
                raise MatchDecisionError(f"automat: cel pozycji {index + 1} ma do rozliczenia {available}, "
                                         f"a propozycja {side} - sugestia nieaktualna, kolejka")

    source = source or (AUTO_SOURCE if method == "rule" else HUMAN_SOURCE)
    score = float(suggestion["score"]) if suggestion["score"] is not None else None
    decision_id = log_decision(
        conn, company_id=company_id, kind="match", subject_type="transaction", subject_id=transaction_id,
        decided_by=decided_by, source=source, method=method, payload=proposal, rule_id=rule_id,
        rule_version=rule_version, suggestion_id=suggestion_id, reason=reason,
        confidence=round(min(score, 1.0), 4) if score is not None else None,
        evidence={"suggestion_id": str(suggestion_id), "rank": suggestion["rank"],
                  "facts": (suggestion["evidence"] or {}).get("facts") or [],
                  "tags": (suggestion["evidence"] or {}).get("tags") or [],
                  "legs": [str(i) for i in leg_ids]},
    )
    group_id = uuid4()
    common = {"company_id": company_id, "group_id": group_id, "decision_id": decision_id, "rule_id": rule_id,
              "rule_version": rule_version, "source": source, "method": method,
              "confidence": round(min(score, 1.0), 4) if score is not None else None, "created_by": decided_by}
    for index, item in enumerate(items):
        detail = details[index] if index < len(details) else None
        side = _document_side(item, detail)
        amounts = _split(int(item["amount_minor"]), legs) if len(legs) > 1 else [int(item["amount_minor"])]
        sides = _split(side, legs) if len(legs) > 1 else [side]
        for leg, amount, doc_side in zip(legs, amounts, sides, strict=True):
            if len(legs) > 1 and amount == 0 and doc_side == 0:
                continue
            diff = item.get("difference_minor")
            conn.execute(
                """
                INSERT INTO l3_match (company_id, transaction_id, document_id, internal_record_id, related_transaction_id,
                                      amount_minor, document_amount_minor, difference_kind, difference_minor, group_id,
                                      decision_id, rule_id, rule_version, source, method, confidence, evidence,
                                      created_by)
                VALUES (%(company_id)s, %(tx)s, %(doc)s, %(rec)s, %(rel)s, %(amount)s, %(side)s, %(dkind)s, %(diff)s,
                        %(group_id)s, %(decision_id)s, %(rule_id)s, %(rule_version)s, %(source)s, %(method)s,
                        %(confidence)s, %(evidence)s, %(created_by)s)
                """,
                {**common, "tx": leg["id"], "doc": item.get("document_id"), "rec": item.get("internal_record_id"),
                 "rel": item.get("related_transaction_id"), "amount": amount, "side": doc_side,
                 "dkind": item.get("difference_kind") if leg is legs[0] else None,
                 "diff": int(diff) if diff is not None and leg is legs[0] else None,
                 "evidence": Jsonb({"suggestion_id": str(suggestion_id), "item": index + 1,
                                    "label": (detail or {}).get("label"), "mpp_role": leg.get("mpp_role")})},
            )
    for leg in legs:
        conn.execute(
            """
            INSERT INTO l3_transaction_status (company_id, transaction_id, final_status, subtype, awaiting_until,
                                               decision_id, rule_id, rule_version, source, method, confidence,
                                               evidence, created_by)
            VALUES (%(company_id)s, %(tx)s, %(status)s, %(subtype)s, %(awaiting)s, %(decision_id)s, %(rule_id)s,
                    %(rule_version)s, %(source)s, %(method)s, %(confidence)s, %(evidence)s, %(created_by)s)
            """,
            {**common, "tx": leg["id"], "status": final_status, "subtype": proposal.get("subtype"),
             "awaiting": awaiting_until,
             "evidence": Jsonb({"suggestion_id": str(suggestion_id), "group_id": str(group_id),
                                "mpp_role": leg.get("mpp_role"), "derived_from": str(transaction_id)
                                if leg["id"] != transaction_id else None})},
        )
    conn.execute(
        """
        UPDATE l2_suggestion SET status = 'accepted', resolved_at = now(), resolved_by_decision = %s WHERE id = %s
        """,
        (decision_id, suggestion_id),
    )
    conn.execute(
        """
        UPDATE l2_suggestion SET status = 'rejected', resolved_at = now(), resolved_by_decision = %s
         WHERE company_id = %s AND kind = 'match' AND subject_type = 'transaction' AND subject_id = ANY(%s)
           AND status = 'open' AND id <> %s
        """,
        (decision_id, company_id, leg_ids, suggestion_id),
    )
    return decision_id
