"""The automation of matching (briefing, step 4: "Kiedy automat, a kiedy kolejka").

A rank-1 suggestion is closed by the automation only when ALL hold:

* calibrated score >= the threshold (default 0.995, calibration may only raise it);
* hard evidence (identifier, MPP block, invoice number with an agreeing counterparty, the only
  open invoice of the counterparty for exactly this amount, ZUS/US/fee by sender and operation
  type, own transfer, or a learned rule in stage auto);
* a clear margin over the second candidate (>= 0.2; a tie within 0.02 is never automatic);
* a rule in stage ``auto`` that licenses it: the built-in rule of the evidence (``builtin:*``) or
  the learned rule (4C) behind the candidate. The decision is signed with that rule and version
  (``method='rule'``), and the database refuses it if the rule is not in stage auto (KS004).

Two invoices of 4 500 zł and a transfer of 4 500 "zapłata" -> never automatic: a tie.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.matching.decide import MatchDecisionError, apply_match
from kasa.core.matching.engine import Candidate


@dataclass
class AutoDecision:
    transaction_id: UUID
    suggestion_id: UUID
    decision_id: UUID | None
    rule_key: str
    error: str | None = None


def licence(cand: Candidate, rules: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The rule in stage auto that allows the automation to close this candidate, if any."""
    if cand.rule and cand.rule.get("stage") == "auto" and cand.rule.get("id"):
        return {"rule_key": cand.rule["rule_key"], "id": cand.rule["id"], "version": cand.rule["version"]}
    if cand.rule_key:
        rule = rules.get(cand.rule_key)
        if rule and rule["stage"] == "auto":
            return {"rule_key": cand.rule_key, "id": rule["id"], "version": rule["version"]}
    return None


def eligibility(cand: Candidate | None, params: dict[str, float], rules: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    """(may the automation close it, why / why not - Polish)."""
    if cand is None:
        return False, "brak kandydata"
    if not cand.hard:
        why = "; ".join(cand.blockers) if cand.blockers else "brak twardego dowodu"
        return False, why
    if cand.tie or cand.margin < params["margin_min"]:
        return False, f"brak wyraźnej przewagi (margines {cand.margin:.2f} < {params['margin_min']:.2f})"
    if cand.score < params["auto_threshold"]:
        return False, f"pewność {cand.score:.4f} poniżej progu {params['auto_threshold']:.3f}"
    rule = licence(cand, rules)
    if rule is None:
        return False, f"reguła {cand.rule_key or '-'} nie jest w etapie auto"
    return True, f"automat: {rule['rule_key']} (wersja {rule['version']})"


def close(conn: psycopg.Connection, transaction_id: UUID, suggestion_id: UUID, cand: Candidate,
          rules: dict[str, dict[str, Any]]) -> AutoDecision:
    """Apply one automatic decision in a savepoint (a refusal leaves the suggestion in the queue)."""
    rule = licence(cand, rules)
    assert rule is not None
    try:
        with conn.transaction():
            decision_id = apply_match(
                conn, suggestion_id, decided_by=f"rule:{rule['rule_key']}@{rule['version']}", method="rule",
                rule_id=rule["id"], rule_version=rule["version"],
                reason=f"automat: pewność {cand.score:.4f}, twardy dowód, przewaga {cand.margin:.2f}")
    except (MatchDecisionError, psycopg.Error) as exc:
        return AutoDecision(transaction_id, suggestion_id, None, rule["rule_key"], str(exc).strip())
    return AutoDecision(transaction_id, suggestion_id, decision_id, rule["rule_key"])
