"""A person's decisions about a rule (briefing, step 6) - versioned, never overwritten.

Stored by a person (a decision in ``l3_decision_log``, ``kind = 'rule'``, subject = the rule row):

* ``accept`` - candidate -> hint: a NEW version row (stage ``hint``, method ``human``);
* ``correct`` - pattern / action / scope changed: a NEW version row (stage ``hint``; observations start over);
* ``reject`` - an explicit "no": a NEW version row in stage ``suspended`` (mining never proposes it again;
  the absence of cases never creates a negative rule - only this);
* ``enable_auto`` - the rule is ready (derived, view ``rule_stage_effective``) -> ``auto`` with one click;
* ``suspend`` - ``suspended`` with a reason and a post-mortem draft (one correction in auto, a confirmed
  audit error, or the person's own wish);
* ``resume`` - suspended -> hint (or auto, when ready).

Derived (never written by code): hint -> shadow after 30 observations, shadow -> ready when the lower
Wilson bound >= 0.95, dormant after 12 months without a hit.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kasa.core.decisions import log_decision
from kasa.core.rules.apply import PatternError, validate_pattern

SOURCE = "kasa.rules"
ACTION_KINDS = ("category", "match_strategy", "allocation", "final_status")
SHADOW_AFTER = 30
READY_WILSON = 0.95


class RuleDecisionError(ValueError):
    """The decision about a rule is not possible in its current state."""


def rule_row(conn: psycopg.Connection, rule_id: UUID, *, lock: bool = False) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT *, stage::text AS stage_text, method::text AS method_text FROM l3_rule WHERE id = %s"
            + (" FOR UPDATE" if lock else ""),
            (rule_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma reguły {rule_id}")
    return row


def find_rule(conn: psycopg.Connection, company_id: UUID | None, ref: str) -> UUID:
    """A rule by id (or its prefix) or by key (the current version)."""
    ref = ref.strip()
    params: list[Any] = [ref]
    where = "rule_key = %s"
    if len(ref) >= 6 and all(ch in "0123456789abcdef-" for ch in ref.lower()):
        where = "(rule_key = %s OR id::text LIKE %s)"
        params.append(ref.lower() + "%")
    if company_id is not None:
        where += " AND company_id = %s"
        params.append(company_id)
    rows = conn.execute(
        f"SELECT id, rule_key, version FROM l3_rule WHERE {where} ORDER BY rule_key, version DESC", params
    ).fetchall()
    if not rows:
        raise LookupError(f"Nie ma reguły {ref!r}")
    by_id = [r for r in rows if str(r[0]).startswith(ref.lower())]
    if by_id and len(by_id) == 1:
        return by_id[0][0]
    keys = {r[1] for r in rows}
    if len(keys) > 1:
        raise LookupError(f"Niejednoznaczne: {ref!r} pasuje do {sorted(keys)}")
    return rows[0][0]


def current_version_id(conn: psycopg.Connection, company_id: UUID, rule_key: str) -> UUID:
    row = conn.execute(
        "SELECT id FROM l3_rule WHERE company_id = %s AND rule_key = %s ORDER BY version DESC LIMIT 1",
        (company_id, rule_key),
    ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma reguły {rule_key}")
    return row[0]


def state(conn: psycopg.Connection, rule_id: UUID) -> dict[str, Any]:
    """Effective state of the CURRENT version of the rule's key (stage, observations, Wilson, ready)."""
    rule = rule_row(conn, rule_id)
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """
            SELECT rule_id, rule_key, version, stored_stage::text AS stored_stage,
                   effective_stage::text AS effective_stage, observations, agreed, disagreed, automatic_decisions,
                   wilson_lower, ready_for_auto, last_hit_at, stage_since
              FROM rule_stage_effective WHERE company_id = %s AND rule_key = %s
            """,
            (rule["company_id"], rule["rule_key"]),
        ).fetchone()
    return dict(row) if row else {}


def _resolve_card(conn: psycopg.Connection, rule_id: UUID, decision_id: UUID, status: str) -> UUID | None:
    row = conn.execute(
        """
        UPDATE l2_suggestion SET status = %s, resolved_at = now(), resolved_by_decision = %s
         WHERE kind = 'rule_candidate' AND subject_type = 'rule' AND subject_id = %s AND status = 'open'
        RETURNING id
        """,
        (status, decision_id, rule_id),
    ).fetchone()
    return row[0] if row else None


def _open_card(conn: psycopg.Connection, rule_id: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT id FROM l2_suggestion WHERE kind = 'rule_candidate' AND subject_type = 'rule' AND subject_id = %s "
        "AND status = 'open' ORDER BY created_at DESC LIMIT 1",
        (rule_id,),
    ).fetchone()
    return row[0] if row else None


def _decide(conn: psycopg.Connection, rule: dict[str, Any], *, action: str, decided_by: str, reason: str | None,
            payload: dict[str, Any] | None = None, suggestion_id: UUID | None = None, source: str = "queue") -> UUID:
    return log_decision(
        conn, company_id=rule["company_id"], kind="rule", subject_type="rule", subject_id=rule["id"],
        decided_by=decided_by, source=source, method="human", reason=reason, suggestion_id=suggestion_id,
        payload={"action": action, "rule_key": rule["rule_key"], "version": rule["version"],
                 "from_stage": rule["stage_text"], **(payload or {})},
    )


def _next_version(conn: psycopg.Connection, company_id: UUID, rule_key: str) -> int:
    return int(conn.execute("SELECT coalesce(max(version), 0) + 1 FROM l3_rule WHERE company_id = %s AND rule_key = %s",
                            (company_id, rule_key)).fetchone()[0])


def _validate_action(action: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(action, dict) or action.get("kind") not in ACTION_KINDS:
        raise RuleDecisionError(f"akcja reguły: kind ∈ {ACTION_KINDS}")
    if action["kind"] == "category" and not action.get("category"):
        raise RuleDecisionError("akcja category wymaga kategorii")
    if action["kind"] == "final_status" and action.get("final_status") == "no_document" and not action.get("subtype"):
        raise RuleDecisionError("bez dokumentu zawsze z podtypem (przegląd 4A/4B, pkt 11)")
    if action["kind"] == "allocation" and not (action.get("cost_object_id") or action.get("split")):
        raise RuleDecisionError("akcja allocation wymaga cost_object_id albo split")
    return action


def _insert_version(conn: psycopg.Connection, rule: dict[str, Any], *, stage: str, decision_id: UUID,
                    decided_by: str, pattern: dict[str, Any] | None = None, action: dict[str, Any] | None = None,
                    scope: dict[str, Any] | None = None, question: str | None = None,
                    suspended_reason: str | None = None, evidence_extra: dict[str, Any] | None = None) -> tuple[UUID, int]:
    version = _next_version(conn, rule["company_id"], rule["rule_key"])
    evidence = dict(rule["evidence"] or {})
    evidence.update({"previous_version": rule["version"], "previous_rule_id": str(rule["id"]), **(evidence_extra or {})})
    row = conn.execute(
        """
        INSERT INTO l3_rule (company_id, rule_key, version, stage, pattern, action, evidence_count, exceptions,
                             decision_id, question, scope, suspended_reason, source, method, confidence, evidence,
                             created_by, updated_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'human', %s, %s, %s, %s)
        RETURNING id, version
        """,
        (rule["company_id"], rule["rule_key"], version, stage, Jsonb(pattern or rule["pattern"]),
         Jsonb(action or rule["action"]), rule["evidence_count"], Jsonb(rule["exceptions"] or []), decision_id,
         question or rule["question"], Jsonb(scope if scope is not None else (rule["scope"] or {})), suspended_reason,
         SOURCE, rule["confidence"], Jsonb(evidence), decided_by, decided_by),
    ).fetchone()
    return row[0], row[1]


def _require_current(conn: psycopg.Connection, rule: dict[str, Any]) -> None:
    current = current_version_id(conn, rule["company_id"], rule["rule_key"])
    if current != rule["id"] and rule["stage_text"] != "candidate":
        raise RuleDecisionError(f"{rule['rule_key']} wersja {rule['version']} nie jest bieżąca - decyduj o bieżącej")


# ---------------------------------------------------------------------------
# candidate -> hint / corrected / rejected
# ---------------------------------------------------------------------------


def accept(conn: psycopg.Connection, rule_id: UUID, *, decided_by: str, reason: str | None = None,
           source: str = "queue") -> tuple[UUID, UUID]:
    """A person accepts a candidate: new version in stage hint. Returns (decision id, new rule row id)."""
    rule = rule_row(conn, rule_id, lock=True)
    if rule["stage_text"] != "candidate":
        raise RuleDecisionError(f"{rule['rule_key']}: zaakceptować można kandydata (etap {rule['stage_text']})")
    card = _open_card(conn, rule_id)
    decision = _decide(conn, rule, action="accept", decided_by=decided_by, reason=reason, suggestion_id=card,
                       payload={"to_stage": "hint", "new_version": _next_version(conn, rule["company_id"],
                                                                                  rule["rule_key"])},
                       source=source)
    new_id, _ = _insert_version(conn, rule, stage="hint", decision_id=decision, decided_by=decided_by)
    _resolve_card(conn, rule_id, decision, "accepted")
    return decision, new_id


def correct(conn: psycopg.Connection, rule_id: UUID, *, decided_by: str, pattern: dict[str, Any] | None = None,
            action: dict[str, Any] | None = None, scope: dict[str, Any] | None = None, question: str | None = None,
            reason: str | None = None, source: str = "queue") -> tuple[UUID, UUID]:
    """A person changes the pattern / action / scope: a new version in stage hint (never an overwrite)."""
    rule = rule_row(conn, rule_id, lock=True)
    _require_current(conn, rule)
    if pattern is None and action is None and scope is None and question is None:
        raise RuleDecisionError("popraw: podaj nowy wzorzec, akcję, zakres albo pytanie")
    try:
        if pattern is not None:
            validate_pattern(pattern)
    except PatternError as exc:
        raise RuleDecisionError(str(exc)) from None
    if action is not None:
        _validate_action(action)
    if rule["stage_text"] in ("suspended",) and rule["suspended_reason"] and \
            str(rule["suspended_reason"]).startswith("odrzucona"):
        raise RuleDecisionError("reguła odrzucona - mining zaproponuje nowego kandydata, gdy dowody się zmienią")
    card = _open_card(conn, rule_id)
    decision = _decide(conn, rule, action="correct", decided_by=decided_by, reason=reason, suggestion_id=card,
                       payload={"to_stage": "hint", "pattern": pattern, "rule_action": action, "scope": scope,
                                "new_version": _next_version(conn, rule["company_id"], rule["rule_key"])},
                       source=source)
    new_id, _ = _insert_version(conn, rule, stage="hint", decision_id=decision, decided_by=decided_by,
                                pattern=pattern, action=action, scope=scope, question=question,
                                evidence_extra={"corrected": True})
    _resolve_card(conn, rule_id, decision, "accepted")
    return decision, new_id


def reject(conn: psycopg.Connection, rule_id: UUID, *, decided_by: str, reason: str | None = None,
           source: str = "queue") -> UUID:
    """An explicit "no": a new version in stage suspended (``suspended_reason`` = odrzucona)."""
    rule = rule_row(conn, rule_id, lock=True)
    if rule["stage_text"] != "candidate":
        return suspend(conn, rule_id, decided_by=decided_by, reason=f"odrzucona: {reason or 'decyzja człowieka'}",
                       source=source)
    card = _open_card(conn, rule_id)
    decision = _decide(conn, rule, action="reject", decided_by=decided_by, reason=reason, suggestion_id=card,
                       payload={"to_stage": "suspended",
                                "new_version": _next_version(conn, rule["company_id"], rule["rule_key"])},
                       source=source)
    _insert_version(conn, rule, stage="suspended", decision_id=decision, decided_by=decided_by,
                    suspended_reason=f"odrzucona: {reason or 'jawne „nie” człowieka'}",
                    evidence_extra={"rejected": True})
    _resolve_card(conn, rule_id, decision, "rejected")
    return decision


# ---------------------------------------------------------------------------
# stage changes of the current version (state, not content: same row, new decision)
# ---------------------------------------------------------------------------


def _set_stage(conn: psycopg.Connection, rule: dict[str, Any], stage: str, decision_id: UUID, decided_by: str, *,
               suspended_reason: str | None = None, post_mortem: str | None = None) -> None:
    conn.execute(
        """
        UPDATE l3_rule SET stage = %s, method = 'human', decision_id = %s, updated_by = %s,
               suspended_reason = %s, post_mortem = coalesce(%s, post_mortem)
         WHERE id = %s
        """,
        (stage, decision_id, decided_by, suspended_reason, post_mortem, rule["id"]),
    )


def enable_auto(conn: psycopg.Connection, rule_id: UUID, *, decided_by: str, reason: str | None = None,
                force: bool = False, source: str = "queue") -> UUID:
    """One click when the rule is ready (derived): stage auto. ``force`` only for a person's explicit override."""
    rule = rule_row(conn, rule_id, lock=True)
    _require_current(conn, rule)
    if rule["stage_text"] in ("candidate", "auto"):
        raise RuleDecisionError(f"{rule['rule_key']}: etap {rule['stage_text']} - auto włącza się z podpowiedzi/cienia")
    info = state(conn, rule_id)
    if not force and not info.get("ready_for_auto"):
        raise RuleDecisionError(
            f"{rule['rule_key']} nie jest gotowa do auto: {info.get('observations', 0)} obserwacji, zgodnych "
            f"{info.get('agreed', 0)}, dolna granica Wilsona {float(info.get('wilson_lower') or 0):.3f} < {READY_WILSON}")
    decision = _decide(conn, rule, action="enable_auto", decided_by=decided_by, reason=reason, source=source,
                       payload={"to_stage": "auto", "observations": info.get("observations"),
                                "agreed": info.get("agreed"), "wilson_lower": float(info.get("wilson_lower") or 0),
                                "forced": force})
    _set_stage(conn, rule, "auto", decision, decided_by)
    return decision


def post_mortem_draft(conn: psycopg.Connection, rule: dict[str, Any], *, cause: str,
                      cause_decision_id: UUID | None = None, predicted: Any = None, actual: Any = None) -> str:
    info = state(conn, rule["id"])
    lines = [
        f"Post-mortem (szkic) - reguła {rule['rule_key']} wersja {rule['version']}"
        + (f": {rule['question']}" if rule.get("question") else ""),
        f"Co się stało: {cause}.",
    ]
    if cause_decision_id:
        row = conn.execute(
            "SELECT kind::text, subject_type::text, subject_id, decided_by, decided_at FROM l3_decision_log WHERE id = %s",
            (cause_decision_id,),
        ).fetchone()
        if row:
            lines.append(f"Decyzja: {row[0]} {row[1]} {row[2]} ({row[3]}, {row[4]:%Y-%m-%d %H:%M}).")
    if predicted is not None or actual is not None:
        lines.append(f"Reguła mówiła: {predicted}; człowiek: {actual}.")
    lines += [
        f"Etap przed zawieszeniem: {rule['stage_text']}; obserwacje {info.get('observations', 0)} "
        f"(zgodne {info.get('agreed', 0)}, niezgodne {info.get('disagreed', 0)}), decyzje automatu "
        f"{info.get('automatic_decisions', 0)}.",
        f"Wzorzec: {rule['pattern']}; akcja: {rule['action']}; zakres: {rule['scope'] or 'bez ograniczeń'}.",
        "Do sprawdzenia: czy wzorzec jest za szeroki (nowy rodzaj dokumentu, inny podtyp, zmiana u kontrahenta), "
        "czy poprzednie decyzje automatu tej reguły są dobre (audyt). Potem: popraw (nowa wersja) albo wznów.",
    ]
    return "\n".join(lines)


def suspend(conn: psycopg.Connection, rule_id: UUID, *, decided_by: str, reason: str, post_mortem: str | None = None,
            cause_decision_id: UUID | None = None, predicted: Any = None, actual: Any = None,
            source: str = "queue") -> UUID:
    """Stage suspended with a reason and a post-mortem draft. A person's decision (even when triggered by a correction)."""
    rule = rule_row(conn, rule_id, lock=True)
    if rule["stage_text"] == "suspended":
        return rule["decision_id"]
    draft = post_mortem or post_mortem_draft(conn, rule, cause=reason, cause_decision_id=cause_decision_id,
                                             predicted=predicted, actual=actual)
    decision = _decide(conn, rule, action="suspend", decided_by=decided_by, reason=reason, source=source,
                       payload={"to_stage": "suspended",
                                "cause_decision_id": str(cause_decision_id) if cause_decision_id else None})
    _set_stage(conn, rule, "suspended", decision, decided_by, suspended_reason=reason, post_mortem=draft)
    return decision


def resume(conn: psycopg.Connection, rule_id: UUID, *, decided_by: str, to_stage: str = "hint",
           reason: str | None = None, source: str = "queue") -> UUID:
    rule = rule_row(conn, rule_id, lock=True)
    _require_current(conn, rule)
    if rule["stage_text"] != "suspended":
        raise RuleDecisionError(f"{rule['rule_key']}: wznowić można zawieszoną regułę (etap {rule['stage_text']})")
    if str(rule["suspended_reason"] or "").startswith("odrzucona") and not (rule["evidence"] or {}).get("builtin"):
        if (rule["evidence"] or {}).get("rejected"):
            raise RuleDecisionError("reguła odrzucona - zamiast wznawiać, zaakceptuj nowego kandydata")
    if to_stage not in ("hint", "auto"):
        raise RuleDecisionError("wznowienie: etap hint albo auto")
    if to_stage == "auto" and not (rule["evidence"] or {}).get("builtin"):
        info = state(conn, rule_id)
        if not info.get("observations") or float(info.get("wilson_lower") or 0) < READY_WILSON:
            raise RuleDecisionError(f"{rule['rule_key']} nie spełnia progu auto - wznów jako podpowiedź")
    decision = _decide(conn, rule, action="resume", decided_by=decided_by, reason=reason, source=source,
                       payload={"to_stage": to_stage})
    _set_stage(conn, rule, to_stage, decision, decided_by)
    return decision


def revert(conn: psycopg.Connection, decision: dict[str, Any], revoke_id: UUID, decided_by: str) -> None:
    """Undo a rule decision (``revoke_decision``): stage back, or the version it created suspended + card back."""
    payload = decision["payload"] or {}
    action = payload.get("action")
    rule = rule_row(conn, decision["subject_id"], lock=True)
    if action in ("enable_auto", "suspend", "resume"):
        if rule["decision_id"] != decision["id"]:
            raise RuleDecisionError(f"{rule['rule_key']}: po tej decyzji była już następna - cofnij najpierw ją")
        _set_stage(conn, rule, payload.get("from_stage") or "hint", revoke_id, decided_by,
                   suspended_reason=rule["suspended_reason"] if payload.get("from_stage") == "suspended" else None)
        return
    if action in ("accept", "correct", "reject"):
        created = conn.execute(
            "SELECT id FROM l3_rule WHERE company_id = %s AND rule_key = %s AND version = %s",
            (rule["company_id"], rule["rule_key"], payload.get("new_version")),
        ).fetchone()
        if created:
            version_row = rule_row(conn, created[0], lock=True)
            if version_row["decision_id"] != decision["id"]:
                raise RuleDecisionError(f"{rule['rule_key']}: po tej decyzji była już następna - cofnij najpierw ją")
            if version_row["stage_text"] != "suspended":
                _set_stage(conn, version_row, "suspended", revoke_id, decided_by,
                           suspended_reason=f"cofnięto decyzję {action}")
            else:
                conn.execute("UPDATE l3_rule SET decision_id = %s, method = 'human', updated_by = %s, "
                             "suspended_reason = %s WHERE id = %s",
                             (revoke_id, decided_by, f"cofnięto decyzję {action}", version_row["id"]))
        if rule["stage_text"] == "candidate":
            restore_card(conn, rule, revoke_id)
        return
    raise RuleDecisionError(f"nie umiem cofnąć decyzji o regule {action!r}")


def restore_card(conn: psycopg.Connection, rule: dict[str, Any], revoke_id: UUID) -> UUID | None:
    """The rule card back to the queue (a copy of the last card, status open)."""
    if _open_card(conn, rule["id"]):
        return None
    row = conn.execute(
        """
        INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, score, rank, source, method,
                                   confidence, evidence, created_by)
        SELECT company_id, kind, subject_type, subject_id, proposal, score, rank, source, method, confidence,
               evidence || jsonb_build_object('restored_from', id, 'restored_after_revoke', %s::text), 'kasa.queue'
          FROM l2_suggestion
         WHERE kind = 'rule_candidate' AND subject_type = 'rule' AND subject_id = %s
         ORDER BY created_at DESC LIMIT 1
        RETURNING id
        """,
        (str(revoke_id), rule["id"]),
    ).fetchone()
    return row[0] if row else None
