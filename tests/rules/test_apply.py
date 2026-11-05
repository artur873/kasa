"""applicable_rules: the contract function 4A and 4B call (pattern, stages, scope, conflicts, builtin)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from kasa.core.matching.engine import evaluate
from kasa.core.rules.apply import (
    PatternError,
    RuleHit,
    applicable_rules,
    condition_holds,
    pattern_holds,
    resolve_conflicts,
    validate_pattern,
)
from kasa.core.rules.facts import canonical_counterparty, subject_facts

from .helpers import counterparty


def _rule(conn, company_id, key, pattern, action, *, stage="hint", scope=None, evidence_count=10, created_at=None):
    """A rule row as a person left it (stage set by a person; no decision row needed for the test)."""
    return conn.execute(
        """
        INSERT INTO l3_rule (company_id, rule_key, version, stage, pattern, action, evidence_count, scope, source,
                             method, created_by, created_at)
        VALUES (%s, %s, 1, %s, %s, %s, %s, %s, 'test', %s, 'pytest', coalesce(%s, now())) RETURNING id
        """,
        (company_id, key, stage, Jsonb(pattern), Jsonb(action), evidence_count, Jsonb(scope or {}),
         "code" if stage == "candidate" else "human", created_at),
    ).fetchone()[0]


def test_pattern_language_of_the_contract():
    facts = {"counterparty_id": "a", "title": "Skladki ZUS za 09/2026", "amount_minor": -1438247, "op_type": "zus",
             "direction": "out", "counterparty_kind": None}
    assert condition_holds({"field": "op_type", "op": "eq", "value": "zus"}, facts)
    assert condition_holds({"field": "title_regex", "op": "regex", "value": r"(?i)zus"}, facts)
    assert condition_holds({"field": "amount_minor", "op": "between", "value": [-1500000, -1000000]}, facts)
    assert condition_holds({"field": "counterparty_kind", "op": "in", "value": ["institution", None]}, facts)
    assert not condition_holds({"field": "op_type", "op": "in", "value": ["tax", "fee"]}, facts)
    assert not condition_holds({"field": "intent", "op": "eq", "value": "zus"}, facts)     # missing fact = no hit
    assert pattern_holds({"all": [{"field": "op_type", "op": "eq", "value": "zus"},
                                  {"field": "direction", "op": "eq", "value": "out"}]}, facts)
    assert not pattern_holds({"all": []}, facts)
    for bad in ({"any": []}, {"all": [{"field": "x", "op": "like", "value": 1}]},
                {"all": [{"field": "title", "op": "regex", "value": "("}]},
                {"all": [{"field": "amount_minor", "op": "between", "value": 5}]}):
        with pytest.raises(PatternError):
            validate_pattern(bad)


def test_scope_known_counterparties_only_and_builtin_rules_are_returned(qconn, odm):
    zus = counterparty(qconn, odm.id, "Zakład Ubezpieczeń%")
    key = "R-T1"
    _rule(qconn, odm.id, key, {"all": [{"field": "op_type", "op": "eq", "value": "zus"}]},
          {"kind": "category", "category": "Wynagrodzenia i daniny / ZUS"}, stage="hint",
          scope={"counterparties": [str(zus)], "subject_types": ["transaction"]})
    tx = qconn.execute("SELECT id FROM l1_bank_transaction WHERE counterparty_id = %s", (zus,)).fetchone()[0]
    facts = subject_facts(qconn, odm.id, "transaction", tx)
    hits = applicable_rules(qconn, odm.id, "transaction", facts)
    keys = {h.rule_key: h for h in hits}
    assert key in keys and keys[key].stage == "hint" and not keys[key].builtin
    # the built-in ZUS rule comes back too (4A applies it in its own code and skips it here)
    assert "builtin:no_document_zus_tax_fee" in keys and keys["builtin:no_document_zus_tax_fee"].builtin
    assert isinstance(hits[0], RuleHit) and hasattr(hits[0], "evidence_count") and hits[0].get("stage")
    # a new counterparty with the same pattern: no hit (briefing: "nie obejmuje nowych kontrahentów")
    other = dict(facts, counterparty_id=str(UUID(int=7)))
    assert key not in {h.rule_key for h in applicable_rules(qconn, odm.id, "transaction", other)}
    # another subject type: no hit
    assert key not in {h.rule_key for h in applicable_rules(qconn, odm.id, "document", facts)}


def test_canonical_counterparty_twelve_ibans_are_one_orange(qconn, odm):
    ids = [r[0] for r in qconn.execute(
        "SELECT counterparty_id FROM l1_bank_transaction WHERE title_raw LIKE 'F/400%%'").fetchall()]
    assert len(set(ids)) == 12
    canonical = {canonical_counterparty(qconn, i) for i in ids}
    nip = qconn.execute("SELECT id FROM l1_counterparty WHERE nip = '5263451926'").fetchone()[0]
    assert canonical == {nip}


def test_stages_candidate_and_suspended_never_hit_dormant_comes_back_as_hint(qconn, odm):
    pattern = {"all": [{"field": "op_type", "op": "eq", "value": "zus"}]}
    _rule(qconn, odm.id, "R-CAND", pattern, {"kind": "category", "category": "Wynagrodzenia i daniny / ZUS"}, stage="candidate")
    _rule(qconn, odm.id, "R-SUSP", pattern, {"kind": "category", "category": "Wynagrodzenia i daniny / ZUS"}, stage="suspended")
    _rule(qconn, odm.id, "R-OLD", pattern, {"kind": "category", "category": "Wynagrodzenia i daniny / ZUS"}, stage="auto",
          created_at=datetime.now(UTC) - timedelta(days=400))      # 13 months without a hit
    _rule(qconn, odm.id, "R-NEW", pattern, {"kind": "category", "category": "Wynagrodzenia i daniny / ZUS"}, stage="auto")
    stages = dict(qconn.execute("SELECT rule_key, effective_stage::text FROM rule_stage_effective "
                                "WHERE rule_key IN ('R-OLD', 'R-NEW')").fetchall())
    assert stages == {"R-OLD": "dormant", "R-NEW": "auto"}
    facts = {"op_type": "zus", "counterparty_id": None}
    hits = {h.rule_key: h for h in applicable_rules(qconn, odm.id, "transaction", facts)}
    assert "R-CAND" not in hits and "R-SUSP" not in hits
    assert hits["R-OLD"].stage == "hint"          # never auto after a sleep: a hint until a person says otherwise
    assert hits["R-NEW"].stage == "auto"


def test_conflict_more_specific_with_evidence_wins_otherwise_both_to_the_queue():
    def hit(key, conditions, category, evidence=10):
        return RuleHit(rule_id=UUID(int=len(key)), rule_key=key, version=1, stage="auto",
                       action={"kind": "category", "category": category}, evidence_count=evidence,
                       pattern={"all": conditions}, specificity=len(conditions))

    a = {"field": "counterparty_id", "op": "eq", "value": "x"}
    b = {"field": "title_regex", "op": "regex", "value": "abonament"}
    general, specific = hit("R-1", [a], "Biuro / IT i subskrypcje (SaaS)"), hit("R-22", [a, b], "Inne / Inne / do wyjaśnienia")
    assert [h.rule_key for h in resolve_conflicts([general, specific])] == ["R-22"]
    # the specific one without evidence does not win - both variants, capped at hint
    out = resolve_conflicts([general, hit("R-22", [a, b], "Inne / Inne / do wyjaśnienia", evidence=0)])
    assert {h.rule_key for h in out} == {"R-1", "R-22"} and all(h.conflict and h.stage == "hint" for h in out)
    # not comparable (different conditions): never "the first one wins"
    out = resolve_conflicts([hit("R-1", [a], "Biuro / IT i subskrypcje (SaaS)"), hit("R-3", [b], "Inne / Inne / do wyjaśnienia")])
    assert len(out) == 2 and all(h.conflict and h.stage == "hint" for h in out)
    # the same claim twice is no conflict
    assert not any(h.conflict for h in resolve_conflicts([general, hit("R-4", [b], "Biuro / IT i subskrypcje (SaaS)")]))


def test_matching_engine_uses_a_learned_rule_in_auto_as_hard_evidence(qconn, odm):
    """4A's layer 6 through importlib: a learned final_status rule in auto (scoped) makes the candidate hard."""
    tx = qconn.execute("SELECT id, counterparty_id FROM l1_bank_transaction WHERE entry_reference = "
                       "'202610097692101583'").fetchone()           # salary of Anna Mazur
    _rule(qconn, odm.id, "R-SAL", {"all": [{"field": "counterparty_id", "op": "eq", "value": str(tx[1])}]},
          {"kind": "final_status", "final_status": "no_document", "subtype": "salary"}, stage="auto",
          scope={"counterparties": [str(tx[1])], "subject_types": ["transaction"]})
    revoke_all = qconn.execute("SELECT id FROM l3_transaction_status WHERE transaction_id = %s AND status = 'active'",
                               (tx[0],)).fetchall()
    assert not revoke_all
    result = evaluate(qconn, odm.id, subject_ids={tx[0]}, period=(date(2026, 10, 1), date(2026, 10, 31))).results[0]
    best = result.candidates[0]
    assert best.final_status == "no_document" and best.subtype == "salary"
    assert any(t.startswith("rule:R-SAL@") for t in best.tags) and best.hard
