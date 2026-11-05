"""Which rules hit a subject - the contract function of phase 4 (4A and 4B call it via importlib).

``applicable_rules(conn, company_id, subject_type, subject_facts) -> list[RuleHit]``

* pattern ``{"all": [{"field", "op", "value"}]}`` - ops ``eq`` / ``regex`` / ``between`` / ``in``;
  ``title_regex`` reads ``facts["title"]``, ``counterparty_id`` compares the canonical counterparty
  (``facts.canonical_counterparty``) as well as the raw one;
* only the CURRENT version of a rule, in effective stage ``hint`` / ``shadow`` / ``auto`` (view
  ``rule_stage_effective``: derived from observations); a dormant rule that hits comes back as a
  ``hint`` (briefing, step 6: "wraca jako podpowiedź przy pierwszym trafieniu");
* scope: a learned rule covers only the counterparties of its evidence (``scope.counterparties``) and the
  subject types it was learned on (``scope.subject_types``) - a new counterparty is no hit (briefing:
  "nie obejmuje nowych kontrahentów"; review of 4A/4B, point 3);
* conflict of two learned rules that say different things about the same subject: the more specific
  one (its conditions contain the other's, and it has evidence) wins; otherwise BOTH come back as
  ``hint`` with ``conflict = True`` - both variants go to the queue, never "the first one wins";
* built-in rules (``builtin:*``) are returned too; 4A/4B apply them in their own code and skip them.

The lookup runs in a savepoint: a failure never aborts the caller's transaction (4A/4B treat the
layer as optional).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from kasa.core.rules.facts import canonical_counterparty

log = logging.getLogger(__name__)

LIVE_STAGES = ("hint", "shadow", "auto")
OPS = ("eq", "regex", "between", "in")
FIELDS_WITH_TEXT = {"title_regex": "title", "title": "title"}


@dataclass(frozen=True)
class RuleHit:
    rule_id: UUID
    rule_key: str
    version: int
    stage: str                       # effective stage: hint | shadow | auto
    action: dict[str, Any]
    evidence_count: int
    pattern: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    question: str | None = None
    builtin: bool = False
    conflict: bool = False
    specificity: int = 0
    observations: int = 0
    agreed: int = 0
    wilson_lower: float = 0.0
    ready_for_auto: bool = False
    stored_stage: str | None = None

    def get(self, key: str, default: Any = None) -> Any:     # dict-like access for callers that expect it
        return getattr(self, key, default)

    def as_dict(self) -> dict[str, Any]:
        return {"rule_id": str(self.rule_id), "rule_key": self.rule_key, "version": self.version,
                "stage": self.stage, "action": self.action, "evidence_count": self.evidence_count,
                "conflict": self.conflict}


class PatternError(ValueError):
    """A rule pattern is not in the contract's language."""


def validate_pattern(pattern: dict[str, Any]) -> dict[str, Any]:
    """The pattern as stored; raises PatternError with a readable message."""
    if not isinstance(pattern, dict) or not isinstance(pattern.get("all"), list) or not pattern["all"]:
        raise PatternError('wzorzec reguły to {"all": [{"field", "op", "value"}, ...]} (co najmniej jeden warunek)')
    for cond in pattern["all"]:
        if not isinstance(cond, dict) or not cond.get("field") or cond.get("op") not in OPS:
            raise PatternError(f"warunek {cond!r}: potrzebne field, op ∈ {OPS} i value")
        if cond["op"] == "regex":
            try:
                re.compile(str(cond.get("value")))
            except re.error as exc:
                raise PatternError(f"wyrażenie regularne {cond.get('value')!r}: {exc}") from None
        if cond["op"] == "between" and (not isinstance(cond.get("value"), list) or len(cond["value"]) != 2):
            raise PatternError("between wymaga listy [od, do]")
        if cond["op"] == "in" and not isinstance(cond.get("value"), list):
            raise PatternError("in wymaga listy wartości")
    return pattern


def _number(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def condition_holds(cond: dict[str, Any], facts: dict[str, Any], canonical: str | None = None) -> bool:
    name, op, value = cond.get("field"), cond.get("op"), cond.get("value")
    if name == "counterparty_id":
        candidates = [v for v in (facts.get("counterparty_id"), canonical) if v]
        candidates = [str(v) for v in candidates] or [None]
    else:
        raw = facts.get(FIELDS_WITH_TEXT.get(name, name))
        candidates = [raw]
    for fact in candidates:
        if op == "eq":
            if fact is not None and value is not None and str(fact) == str(value):
                return True
            if fact is None and value is None:
                return True
            if isinstance(value, bool) and fact is value:
                return True
        elif op == "regex":
            if fact is not None and value is not None and re.search(str(value), str(fact)):
                return True
        elif op == "between":
            number = _number(fact)
            low, high = (_number(v) for v in (value or [None, None])[:2])
            if number is not None and low is not None and high is not None and low <= number <= high:
                return True
        elif op == "in":
            values = value if isinstance(value, list) else [value]
            if fact in values or (fact is not None and str(fact) in {str(v) for v in values if v is not None}):
                return True
    return False


def pattern_holds(pattern: dict[str, Any], facts: dict[str, Any], canonical: str | None = None) -> bool:
    conditions = (pattern or {}).get("all") or []
    if not conditions:
        return False
    return all(condition_holds(c, facts, canonical) for c in conditions)


def in_scope(scope: dict[str, Any], subject_type: str, facts: dict[str, Any], canonical: str | None) -> bool:
    types = (scope or {}).get("subject_types")
    if types and subject_type not in types:
        return False
    known = (scope or {}).get("counterparties")
    if known is None:
        return True
    known = {str(k) for k in known}
    return bool({str(v) for v in (facts.get("counterparty_id"), canonical) if v} & known)


def load_rules(conn: psycopg.Connection, company_id: UUID, *, stages: tuple[str, ...] = (*LIVE_STAGES, "dormant"),
               ) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT rule_id, rule_key, version, stored_stage::text AS stored_stage,
                   effective_stage::text AS effective_stage, pattern, action, scope, question, evidence_count,
                   observations, agreed, disagreed, wilson_lower, ready_for_auto
              FROM rule_stage_effective
             WHERE company_id = %s AND effective_stage::text = ANY(%s)
             ORDER BY rule_key
            """,
            (company_id, list(stages)),
        ).fetchall()


def _hit(row: dict[str, Any]) -> RuleHit:
    stage = row["effective_stage"]
    if stage == "dormant":
        stage = "hint"           # the first hit after 12 months of sleep: a hint again
    conditions = (row["pattern"] or {}).get("all") or []
    return RuleHit(
        rule_id=row["rule_id"], rule_key=row["rule_key"], version=int(row["version"]), stage=stage,
        action=row["action"] or {}, evidence_count=int(row["evidence_count"] or 0), pattern=row["pattern"] or {},
        scope=row["scope"] or {}, question=row["question"], builtin=str(row["rule_key"]).startswith("builtin:"),
        specificity=len(conditions), observations=int(row["observations"] or 0), agreed=int(row["agreed"] or 0),
        wilson_lower=float(row["wilson_lower"] or 0), ready_for_auto=bool(row["ready_for_auto"]),
        stored_stage=row["stored_stage"],
    )


def dimension(action: dict[str, Any]) -> str:
    kind = (action or {}).get("kind")
    return {"final_status": "match", "match_strategy": "match"}.get(kind, kind or "other")


def _says(action: dict[str, Any]) -> str:
    """What an action asserts, comparable between rules (two match strategies never contradict)."""
    kind = (action or {}).get("kind")
    if kind == "match_strategy":
        return "matched"
    if kind == "final_status":
        return json.dumps({"status": action.get("final_status"), "subtype": action.get("subtype")}, sort_keys=True)
    if kind == "allocation":
        keys = ("cost_object_id", "share", "split")
        return json.dumps({k: action.get(k) for k in keys}, sort_keys=True, default=str)
    return json.dumps(action, sort_keys=True, default=str)


def _conditions(hit: RuleHit) -> set[str]:
    return {json.dumps(c, sort_keys=True, default=str) for c in (hit.pattern or {}).get("all") or []}


def resolve_conflicts(hits: list[RuleHit]) -> list[RuleHit]:
    """More specific with evidence wins; otherwise every variant stays, capped at hint, flagged conflict."""
    learned = [h for h in hits if not h.builtin]
    out = [h for h in hits if h.builtin]
    by_dimension: dict[str, list[RuleHit]] = {}
    for hit in learned:
        by_dimension.setdefault(dimension(hit.action), []).append(hit)
    for group in by_dimension.values():
        claims = {_says(h.action) for h in group}
        if len(claims) <= 1:
            out.extend(group)
            continue
        # a rule dominates when its conditions strictly contain another's and it has evidence
        winners = [h for h in group
                   if h.evidence_count > 0 and all(
                       _says(o.action) == _says(h.action) or (_conditions(o) < _conditions(h)) for o in group
                       if o is not h)]
        if len({_says(w.action) for w in winners}) == 1 and winners:
            claim = _says(winners[0].action)
            out.extend(h for h in group if _says(h.action) == claim)
            continue
        for hit in group:
            out.append(RuleHit(**{**hit.__dict__, "stage": "hint" if hit.stage in LIVE_STAGES else hit.stage,
                                  "conflict": True}))
    return sorted(out, key=lambda h: (h.builtin, -h.specificity, h.rule_key))


def applicable_rules(conn: psycopg.Connection, company_id: UUID, subject_type: str, subject_facts: dict[str, Any],
                     *, include_builtin: bool = True, resolve: bool = True) -> list[RuleHit]:
    """Rules (current version, live stage, in scope) whose pattern holds for the subject."""
    facts = dict(subject_facts or {})
    try:
        with conn.transaction():
            rows = load_rules(conn, company_id)
            canonical = None
            if facts.get("counterparty_id") and any(
                    (r["scope"] or {}).get("counterparties") is not None
                    or any(c.get("field") == "counterparty_id" for c in (r["pattern"] or {}).get("all") or [])
                    for r in rows):
                found = canonical_counterparty(conn, facts["counterparty_id"])
                canonical = str(found) if found else None
    except psycopg.Error as exc:            # never break the caller's transaction
        log.warning("reguły niedostępne: %s", str(exc).strip())
        return []
    hits: list[RuleHit] = []
    for row in rows:
        builtin = str(row["rule_key"]).startswith("builtin:")
        if builtin and not include_builtin:
            continue
        if not builtin and not in_scope(row["scope"] or {}, subject_type, facts, canonical):
            continue
        if pattern_holds(row["pattern"] or {}, facts, canonical):
            hits.append(_hit(row))
    return resolve_conflicts(hits) if resolve else hits
