"""Observations: after every decision of a person, "would each rule have done the same?"

``observe_decision(conn, decision_id)`` - for every learned rule (current version, stage hint /
shadow / auto, in scope) whose pattern holds for the subject of a HUMAN decision, one row of
``l2_rule_observation``: what the rule predicted, what the person decided, agreed or not. Decisions
of the automation are never observed (the system must not confirm itself - briefing, step 6).

* a disagreement with a rule in stage ``auto`` is a correction: the rule is suspended at once with a
  post-mortem draft (briefing: "jeden przypadek, w którym ją poprawisz → zawieszona");
* ``withdraw(conn, revoked_decision_id, revoke_id)`` - a revoked decision takes its points back;
  a rule in auto that falls below the threshold of readiness is suspended ("cofnięcie dowodu reguły
  odejmuje jej punkt i może ją zawiesić").

Built-in rules are not learned, so they are not observed; a correction of their automatic decision is
handled by ``kasa.core.decisions.revoke_decision``.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kasa.core.rules import lifecycle
from kasa.core.rules.apply import RuleHit, applicable_rules
from kasa.core.rules.facts import canonical_counterparty, subject_facts

SOURCE = "kasa.rules.observe"
EQUIVALENT_SUBTYPES = {"interest": "fee"}
OBSERVED_KINDS = ("match", "allocation", "category")


def _subtype(value: Any) -> Any:
    return EQUIVALENT_SUBTYPES.get(value, value)


def _shares(payload: dict[str, Any]) -> dict[str, Fraction]:
    out: dict[str, Fraction] = {}
    for share in payload.get("shares") or []:
        exact = share.get("share_exact")
        out[str(share["cost_object_id"])] = Fraction(str(exact)) if exact else Fraction(str(share["share"]))
    return out


def _document_counterparties(conn: psycopg.Connection, payload: dict[str, Any]) -> set[str]:
    ids = [i.get("document_id") for i in payload.get("items") or [] if i.get("document_id")]
    if not ids:
        return set()
    rows = conn.execute("SELECT DISTINCT counterparty_id FROM l1_document WHERE id = ANY(%s::uuid[])",
                        (ids,)).fetchall()
    return {str(canonical_counterparty(conn, r[0])) for r in rows if r[0]}


def compare(conn: psycopg.Connection, action: dict[str, Any], kind: str, payload: dict[str, Any]
            ) -> tuple[dict[str, Any], dict[str, Any], bool] | None:
    """(predicted, actual, agreed) or None when the rule says nothing about this kind of decision."""
    akind = (action or {}).get("kind")
    if akind == "final_status" and kind == "match":
        predicted = {"final_status": action.get("final_status"), "subtype": action.get("subtype")}
        actual = {"final_status": payload.get("final_status"), "subtype": payload.get("subtype")}
        agreed = predicted["final_status"] == actual["final_status"] and (
            predicted["subtype"] is None or _subtype(predicted["subtype"]) == _subtype(actual["subtype"]))
        return predicted, actual, agreed
    if akind == "match_strategy" and kind == "match":
        strategy = action.get("strategy")
        documents = [i for i in payload.get("items") or [] if i.get("document_id")]
        actual = {"final_status": payload.get("final_status"), "documents": len(documents)}
        if strategy == "monthly_bulk":
            return ({"final_status": "matched", "documents": "≥ 2"}, actual,
                    payload.get("final_status") == "matched" and len(documents) >= 2)
        if strategy == "payer_alias":
            client = str(action.get("counterparty_id"))
            parties = _document_counterparties(conn, payload)
            actual["document_counterparties"] = sorted(parties)
            return ({"final_status": "matched", "document_counterparty": client}, actual,
                    payload.get("final_status") == "matched" and parties == {client})
        return None
    if akind == "category" and kind == "category":
        predicted = {"category": action.get("category")}
        actual = {"category": payload.get("category")}
        return predicted, actual, predicted["category"] == actual["category"]
    if akind == "allocation" and kind == "allocation":
        shares = _shares(payload)
        actual = {"shares": {k: str(v) for k, v in shares.items()}}
        if action.get("split") == "equal":
            values = set(shares.values())
            return {"split": "equal"}, actual, len(shares) >= 2 and len(values) == 1
        target = str(action.get("cost_object_id"))
        share = Fraction(str(action.get("share", 1)))
        return ({"shares": {target: str(share)}}, actual, shares == {target: share})
    return None


def _decision(conn: psycopg.Connection, decision_id: UUID) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT id, company_id, kind::text AS kind, subject_type::text AS subject_type, subject_id, payload,
                   method::text AS method, decided_by, rule_id, rule_version
              FROM l3_decision_log WHERE id = %s
            """,
            (decision_id,),
        ).fetchone()


def hits_for(conn: psycopg.Connection, company_id: UUID, subject_type: str, subject_id: UUID) -> list[RuleHit]:
    facts = subject_facts(conn, company_id, subject_type, subject_id)
    if not facts:
        return []
    return [h for h in applicable_rules(conn, company_id, subject_type, facts, include_builtin=False, resolve=False)
            if h.stage in ("hint", "shadow", "auto")]


def observe_decision(conn: psycopg.Connection, decision_id: UUID) -> list[dict[str, Any]]:
    """Observations for one decision of a person (none for the automation). Returns what was written."""
    decision = _decision(conn, decision_id)
    if decision is None or decision["method"] != "human" or decision["kind"] not in OBSERVED_KINDS:
        return []
    written = []
    for hit in hits_for(conn, decision["company_id"], decision["subject_type"], decision["subject_id"]):
        compared = compare(conn, hit.action, decision["kind"], decision["payload"] or {})
        if compared is None:
            continue
        predicted, actual, agreed = compared
        row = conn.execute(
            """
            INSERT INTO l2_rule_observation (company_id, rule_id, rule_version, stage, subject_type, subject_id,
                                             decision_id, predicted, actual, agreed, source, method, evidence,
                                             created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'code', %s, %s)
            ON CONFLICT (rule_id, decision_id) WHERE decision_id IS NOT NULL DO NOTHING
            RETURNING id
            """,
            (decision["company_id"], hit.rule_id, hit.version, hit.stage, decision["subject_type"],
             decision["subject_id"], decision_id, Jsonb(predicted), Jsonb(actual), agreed, SOURCE,
             Jsonb({"rule_key": hit.rule_key, "conflict": hit.conflict, "decided_by": decision["decided_by"]}),
             SOURCE),
        ).fetchone()
        if row is None:
            continue
        conn.execute("UPDATE l3_rule SET last_hit_at = greatest(last_hit_at, now()) WHERE id = %s", (hit.rule_id,))
        entry = {"observation_id": row[0], "rule_id": hit.rule_id, "rule_key": hit.rule_key, "stage": hit.stage,
                 "agreed": agreed, "suspended": False}
        if hit.stage == "auto" and not agreed:
            lifecycle.suspend(conn, hit.rule_id, decided_by=decision["decided_by"],
                              reason=f"poprawka w etapie auto: decyzja {decision_id} różni się od reguły",
                              cause_decision_id=decision_id, predicted=predicted, actual=actual)
            entry["suspended"] = True
        written.append(entry)
    return written


def withdraw(conn: psycopg.Connection, revoked_decision_id: UUID, revoke_id: UUID, decided_by: str) -> list[UUID]:
    """Take back the points of a revoked decision; suspend a rule in auto that is no longer ready."""
    rows = conn.execute(
        """
        UPDATE l2_rule_observation SET withdrawn_at = now(), withdrawn_by_decision = %s
         WHERE decision_id = %s AND withdrawn_at IS NULL
        RETURNING rule_id, agreed
        """,
        (revoke_id, revoked_decision_id),
    ).fetchall()
    suspended = []
    for rule_id, agreed in rows:
        rule = lifecycle.rule_row(conn, rule_id)
        if rule["stage_text"] != "auto" or not agreed:
            continue
        info = lifecycle.state(conn, rule_id)
        if float(info.get("wilson_lower") or 0) < lifecycle.READY_WILSON:
            lifecycle.suspend(conn, rule_id, decided_by=decided_by,
                              reason=f"cofnięto dowód reguły (decyzja {revoked_decision_id}) - poniżej progu auto",
                              cause_decision_id=revoked_decision_id)
            suspended.append(rule_id)
    return suspended
