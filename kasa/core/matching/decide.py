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
            SELECT CASE WHEN EXISTS (SELECT 1 FROM l3_match e WHERE e.status = 'active' AND e.evidence_only
                                        AND e.internal_record_id = r.id)
                        THEN 0    -- evidence of a document: the cost is settled through the document
                        ELSE r.amount_minor - coalesce((
                             SELECT sum(coalesce(m.document_amount_minor, m.amount_minor)) FROM l3_match m
                              WHERE m.status = 'active' AND m.internal_record_id = r.id), 0) END
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


def _evidence_rows(conn: psycopg.Connection, company_id: UUID, suggestion: dict[str, Any],
                   items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows of internal files that prove the same cost as the documents of the match (``linked_evidence``).

    One cost, many pieces of evidence (briefing, step 5): written into the same group with
    ``evidence_only`` and amount 0 - never a second settlement. Only rows of this company that the
    proposal does not settle itself and that no other active match uses.
    """
    if not any(i.get("document_id") for i in items):
        return []
    linked = ((suggestion["evidence"] or {}).get("detail") or {}).get("linked_evidence") or []
    named = {str(i.get("internal_record_id")) for i in items if i.get("internal_record_id")}
    out, seen = [], set()
    for entry in linked:
        record_id = entry.get("internal_record_id")
        if not record_id or record_id in named or record_id in seen:
            continue
        seen.add(record_id)
        row = conn.execute(
            """
            SELECT r.id FROM l1_internal_record r
             WHERE r.id = %s AND r.company_id = %s AND r.superseded_by IS NULL
               AND NOT EXISTS (SELECT 1 FROM l3_match m WHERE m.internal_record_id = r.id AND m.status = 'active')
            """,
            (record_id, company_id),
        ).fetchone()
        if row:
            out.append({"internal_record_id": row[0], "label": entry.get("label")})
    return out


def _iban_link(conn: psycopg.Connection, company_id: UUID, transaction_id: UUID,
               items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A person's match ties the IBAN of the payer/payee to the counterparty with the NIP of the invoice.

    Review of 4A, point 2: an invoice number with a counterparty agreeing only by NAME is not hard evidence
    (a sales invoice has the buyer's NIP, the bank gives the buyer's name and IBAN). The first match of a
    person links that IBAN to the counterparty of the invoice (``kasa_upsert_counterparty`` with the IBAN);
    from then on the next transfer from that account agrees by IBAN - hard evidence. Only when it is safe:
    one counterparty with a NIP (or a foreign tax id) on the documents, the same name as the bank's, the IBAN
    not of an operator (Revolut, PayU...: the client is only in the title), not an own account and not on
    another NIP.
    """
    from kasa.core.matching.data import name_tokens
    from kasa.core.normalize import normalize_iban

    doc_ids = [i["document_id"] for i in items if i.get("document_id")]
    if not doc_ids:
        return None
    tx = conn.execute(
        """
        SELECT t.counterparty_iban_raw, t.counterparty_name_raw, t.op_type::text, t.extracted, c.name
          FROM l1_bank_transaction t LEFT JOIN l1_counterparty c ON c.id = t.counterparty_id
         WHERE t.id = %s
        """,
        (transaction_id,),
    ).fetchone()
    if tx is None or not tx[0] or tx[2] == "own" or (tx[3] or {}).get("operator"):
        return None
    try:
        iban = normalize_iban(tx[0])
    except (TypeError, ValueError):
        return None
    if conn.execute("SELECT 1 FROM l1_bank_account WHERE company_id = %s AND iban = %s",
                    (company_id, iban)).fetchone():
        return None
    parties = conn.execute(
        """
        SELECT DISTINCT c.id, coalesce(c.nip, c.foreign_tax_id), c.name, c.ibans, c.nip, c.foreign_tax_id
          FROM l1_document d
          JOIN l1_counterparty c ON c.id = d.counterparty_id
         WHERE d.id = ANY(%s) AND c.superseded_by IS NULL
        """,
        (doc_ids,),
    ).fetchall()
    if len(parties) != 1 or not parties[0][1]:
        return None                                     # the documents' party has no NIP / tax id
    cp_id, nip, name, ibans, polish_nip, foreign_tax_id = parties[0]
    if iban in (ibans or ()):
        return None                                     # already tied by this IBAN
    bank_names = {name_tokens(tx[1]), name_tokens(tx[4])} - {()}
    if not bank_names or name_tokens(name) not in bank_names:
        return None                                     # only "the same name" becomes an IBAN tie
    other = conn.execute(
        """
        SELECT 1 FROM l1_counterparty WHERE company_id = %s AND superseded_by IS NULL AND ibans @> ARRAY[%s]
           AND coalesce(nip, foreign_tax_id) IS NOT NULL AND coalesce(nip, foreign_tax_id) <> %s
        """,
        (company_id, iban, nip),
    ).fetchone()
    if other:
        return None                                     # the IBAN is already tied to another NIP
    return {"counterparty_id": str(cp_id), "nip": polish_nip, "foreign_tax_id": foreign_tax_id, "iban": iban,
            "name": name}


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
    evidence_rows = _evidence_rows(conn, company_id, suggestion, items) if final_status == "matched" else []
    iban_link = _iban_link(conn, company_id, transaction_id, items) \
        if method == "human" and final_status == "matched" else None
    decision_evidence = {"suggestion_id": str(suggestion_id), "rank": suggestion["rank"],
                         "facts": (suggestion["evidence"] or {}).get("facts") or [],
                         "tags": (suggestion["evidence"] or {}).get("tags") or [],
                         "legs": [str(i) for i in leg_ids]}
    if evidence_rows:
        decision_evidence["evidence_only"] = [{"internal_record_id": str(r["internal_record_id"]),
                                               "label": r["label"]} for r in evidence_rows]
    if iban_link:
        decision_evidence["iban_linked"] = iban_link
    decision_id = log_decision(
        conn, company_id=company_id, kind="match", subject_type="transaction", subject_id=transaction_id,
        decided_by=decided_by, source=source, method=method, payload=proposal, rule_id=rule_id,
        rule_version=rule_version, suggestion_id=suggestion_id, reason=reason,
        confidence=round(min(score, 1.0), 4) if score is not None else None,
        evidence=decision_evidence,
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
    for row in evidence_rows:
        # one cost, many pieces of evidence: the sheet row joins the group as evidence, amount 0
        conn.execute(
            """
            INSERT INTO l3_match (company_id, transaction_id, internal_record_id, amount_minor, document_amount_minor,
                                  evidence_only, group_id, decision_id, rule_id, rule_version, source, method,
                                  confidence, evidence, created_by)
            VALUES (%(company_id)s, %(tx)s, %(rec)s, 0, 0, true, %(group_id)s, %(decision_id)s, %(rule_id)s,
                    %(rule_version)s, %(source)s, %(method)s, %(confidence)s, %(evidence)s, %(created_by)s)
            """,
            {**common, "tx": legs[0]["id"], "rec": row["internal_record_id"],
             "evidence": Jsonb({"suggestion_id": str(suggestion_id), "label": row["label"],
                                "evidence_of": [str(i["document_id"]) for i in items if i.get("document_id")],
                                "note": "ten sam koszt - dowód przy dokumencie, nie drugie rozliczenie"})},
        )
    if iban_link:
        conn.execute(
            """
            SELECT kasa_upsert_counterparty(%s, %s, %s, %s, p_foreign_tax_id => %s, p_source => 'queue',
                                            p_method => 'human', p_evidence => %s, p_created_by => %s)
            """,
            (company_id, iban_link["nip"], iban_link["name"], iban_link["iban"], iban_link["foreign_tax_id"],
             Jsonb({"basis": "human_match", "decision_id": str(decision_id), "transaction_id": str(transaction_id)}),
             decided_by),
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
