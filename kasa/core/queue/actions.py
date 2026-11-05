"""The buttons of the queue: one entry point for the panel, the API and the CLI.

``answer(conn, item_id, action, *, decided_by, suggestion_id=None, extra=None, reason=None, until=None)``

| action          | card                          | effect                                                         |
| --------------- | ----------------------------- | -------------------------------------------------------------- |
| ``accept``      | any (rank 1 or the chosen one) | ``decisions.accept_suggestion`` / rule ready: auto / audit: confirmed |
| ``other``       | match, allocation, category   | ``decisions.decide_other`` ("Inne…")                           |
| ``reject``      | any                           | ``decisions.reject_suggestion``                                |
| ``defer``       | any                           | ``decisions.defer_suggestion`` (default: tomorrow)             |
| ``bulk``        | a card of a homogeneous batch | ``decisions.bulk_accept``                                      |
| ``error``       | audit                         | the automatic decision revoked, its rule suspended             |
| ``not_sensitive`` | sensitive                   | term retired (a person's) or an exception for the subject      |
| ``approve`` / ``split`` / ``merge`` / ``distinct`` | cost object candidate | 4B's decisions               |
| ``correct``     | rule candidate                | a new version of the rule                                      |

A sentinel answers like a match card, but nothing is decided: the answer is compared with the
known one and stored (hidden). ``history`` lists the person's decisions with undo, ``stats`` the
KPI of the queue: minutes of work and errors weighted by amount (never "percent done by AI").
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kasa.core import decisions as dec
from kasa.core.matching.engine import money
from kasa.core.queue import cards, state
from kasa.core.queue.build import normalize_answer


class QueueError(ValueError):
    """The button does not fit the card."""


def list_items(conn: psycopg.Connection, company_id: UUID, *, include_deferred: bool = True,
               limit: int | None = None) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT * FROM queue_state
             WHERE company_id = %s AND closed_at IS NULL
               AND (%s OR deferred_until IS NULL OR deferred_until <= now())
             ORDER BY position NULLS LAST, priority DESC, id
            """ + (" LIMIT %s" if limit else ""),
            (company_id, include_deferred, *((limit,) if limit else ())),
        ).fetchall()


def next_item(conn: psycopg.Connection, company_id: UUID, *, mark_shown: bool = True,
              skip: list[UUID] | None = None) -> dict[str, Any] | None:
    """The first card that is not deferred (records when it was first shown)."""
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """
            SELECT * FROM queue_state
             WHERE company_id = %s AND closed_at IS NULL AND (deferred_until IS NULL OR deferred_until <= now())
               AND NOT (id = ANY(%s))
             ORDER BY position NULLS LAST, priority DESC, id LIMIT 1
            """,
            (company_id, list(skip or [])),
        ).fetchone()
    if row is not None and mark_shown:
        row["shown_at"] = state.mark_shown(conn, row["id"])
    return row


def open_item(conn: psycopg.Connection, item_id: UUID) -> dict[str, Any]:
    item = state.get(conn, item_id)
    if item["closed_at"] is not None:
        raise QueueError("ta karta jest już rozstrzygnięta")
    return item


def card(conn: psycopg.Connection, item_id: UUID, *, mark_shown: bool = True) -> dict[str, Any]:
    item = state.get(conn, item_id)
    if mark_shown and item["closed_at"] is None:
        item["shown_at"] = state.mark_shown(conn, item_id)
    return cards.card(conn, item)


# ---------------------------------------------------------------------------
# sentinel
# ---------------------------------------------------------------------------


def _answer_sentinel(conn: psycopg.Connection, item: dict[str, Any], action: str, *, decided_by: str,
                     suggestion_id: UUID | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    if action in ("accept", "bulk"):
        chosen = suggestion_id or (item["suggestion_ids"][0] if item["suggestion_ids"] else None)
        proposal = conn.execute("SELECT proposal FROM l2_suggestion WHERE id = %s", (chosen,)).fetchone()
        answer = normalize_answer(proposal[0] if proposal else {})
    elif action == "other":
        extra = extra or {}
        kind = extra.get("kind") or "matched"
        answer = normalize_answer({"final_status": "matched" if kind == "match" else kind,
                                   "subtype": extra.get("subtype"), "items": extra.get("targets") or []})
    elif action == "reject":
        answer = {"final_status": None, "rejected": str(suggestion_id or (item["suggestion_ids"] or [None])[0])}
    elif action == "defer":
        state.defer(conn, item["company_id"], "sentinel", item["subject_type"], item["subject_id"], None,
                    decided_by=decided_by)
        return {"message": "Odłożone", "decision_ids": []}
    else:
        raise QueueError(f"przycisk {action!r} nie pasuje do tej karty")
    sentinel = conn.execute("SELECT expected FROM queue_sentinel WHERE id = %s", (item["sentinel_id"],)).fetchone()
    expected = sentinel[0] if sentinel else {}
    passed = (answer.get("final_status") == expected.get("final_status")
              and answer.get("subtype") == expected.get("subtype")
              and answer.get("targets", []) == expected.get("targets", []))
    closed = state.close_item(conn, item["id"], decision_id=None, decided_by=decided_by, outcome="answered")
    conn.execute(
        """
        UPDATE queue_sentinel SET answer = %s, answered_at = clock_timestamp(), answered_by = %s, passed = %s,
               duration_ms = %s
         WHERE id = %s AND answered_at IS NULL
        """,
        (Jsonb(answer), decided_by, passed, (closed or {}).get("duration_ms"), item["sentinel_id"]),
    )
    # the result stays hidden: the reply is the same as for any card
    return {"message": "Zapisano", "decision_ids": [], "undo_until": None}


# ---------------------------------------------------------------------------
# answer
# ---------------------------------------------------------------------------


def _first(item: dict[str, Any]) -> UUID:
    if not item["suggestion_ids"]:
        raise QueueError("karta nie ma propozycji - użyj „Inne…”")
    return item["suggestion_ids"][0]


def answer(conn: psycopg.Connection, item_id: UUID, action: str, *, decided_by: str,
           suggestion_id: UUID | None = None, extra: dict[str, Any] | None = None, reason: str | None = None,
           until: datetime | None = None) -> dict[str, Any]:
    """Press a button of a card. Returns {message, decision_ids, undo_until, batch_id?}."""
    from kasa.core.rules import audit, lifecycle

    item = open_item(conn, item_id)
    kind = item["card_kind"]
    reason = (reason or "").strip() or None
    extra = dict(extra or {})
    if suggestion_id is not None and kind != "sentinel" and UUID(str(suggestion_id)) not in [
            UUID(str(s)) for s in item["suggestion_ids"] or []] and action not in ("bulk",):
        raise QueueError("ta propozycja nie należy do karty")
    if kind == "sentinel":
        return _answer_sentinel(conn, item, action, decided_by=decided_by, suggestion_id=suggestion_id, extra=extra)
    result: dec.DecisionResult | None = None
    if kind == "rule_ready":
        if action == "accept":
            decision = lifecycle.enable_auto(conn, item["subject_id"], decided_by=decided_by, reason=reason)
            state.mark_decided(conn, item["company_id"], "rule", item["subject_id"], ("rule_ready",),
                               decision_id=decision, decided_by=decided_by, outcome="accepted")
            return {"message": "Reguła działa sama (auto)", "decision_ids": [str(decision)],
                    "undo_until": _undo(conn, decision)}
        if action in ("defer", "reject"):
            state.defer(conn, item["company_id"], "rule_ready", "rule", item["subject_id"], until,
                        decided_by=decided_by)
            return {"message": "Odłożone", "decision_ids": []}
        raise QueueError(f"przycisk {action!r} nie pasuje do karty reguły gotowej")
    if kind == "audit":
        if action in ("accept", "confirmed", "error"):
            verdict = "error" if action == "error" else "confirmed"
            out = audit.verdict(conn, item["audit_sample_id"], verdict, decided_by=decided_by, note=reason)
            state.close_item(conn, item["id"], decision_id=out["review_decision_id"], decided_by=decided_by,
                             outcome=verdict)
            return {"message": "Audyt: potwierdzone" if verdict == "confirmed" else
                    "Audyt: błąd - decyzja cofnięta, reguła zawieszona",
                    "decision_ids": [str(out["review_decision_id"])] if out["review_decision_id"] else []}
        if action == "defer":
            state.defer(conn, item["company_id"], "audit", item["subject_type"], item["subject_id"], until,
                        decided_by=decided_by)
            return {"message": "Odłożone", "decision_ids": []}
        raise QueueError(f"przycisk {action!r} nie pasuje do karty audytu")

    if action == "accept":
        result = dec.accept_suggestion(conn, suggestion_id or _first(item), decided_by=decided_by, reason=reason,
                                       extra=extra)
    elif action in ("approve", "split", "merge", "distinct", "correct"):
        result = dec.accept_suggestion(conn, suggestion_id or _first(item), decided_by=decided_by, reason=reason,
                                       extra={**extra, "action": action})
    elif action == "reject":
        result = dec.reject_suggestion(conn, suggestion_id or _first(item), decided_by=decided_by, reason=reason)
    elif action == "defer":
        moment = state.defer(conn, item["company_id"], kind, item["subject_type"], item["subject_id"], until,
                             decided_by=decided_by)
        return {"message": f"Odłożone do {moment:%Y-%m-%d %H:%M}", "decision_ids": []}
    elif action == "other":
        other_kind = extra.pop("kind", None) or {"allocation": "allocation", "category": "category",
                                                 "sensitive": "category"}.get(kind)
        if other_kind is None:
            raise QueueError("„Inne…”: wybierz rodzaj decyzji (dopasowanie, bez dokumentu, …)")
        result = dec.decide_other(conn, item["company_id"], item["subject_type"], item["subject_id"],
                                  decided_by=decided_by, kind=other_kind, targets=extra.get("targets"),
                                  subtype=extra.get("subtype"), shares=extra.get("shares"),
                                  category=extra.get("category"), reason=reason)
    elif action == "bulk":
        ids = extra.get("suggestion_ids") or ((cards.card(conn, item).get("batch") or {}).get("suggestion_ids"))
        if not ids:
            raise QueueError("ta karta nie należy do jednorodnej paczki")
        bulk = dec.bulk_accept(conn, [UUID(str(i)) for i in ids], decided_by, reason=reason)
        return {"message": f"Paczka: {len(bulk.decision_ids)} decyzji, {money(bulk.amount_minor)}",
                "decision_ids": [str(d) for d in bulk.decision_ids], "batch_id": str(bulk.batch_id),
                "undo_until": _undo(conn, bulk.decision_ids[0]) if bulk.decision_ids else None}
    elif action == "not_sensitive":
        if kind != "sensitive":
            raise QueueError("„to nie jest wrażliwe” tylko na karcie ze słowem wrażliwym")
        out = dec.not_sensitive(conn, _first(item), decided_by=decided_by, reason=reason)
        return {"message": "Zapisano: to nie jest wrażliwe", "decision_ids": [], **out}
    else:
        raise QueueError(f"nieznany przycisk {action!r}")
    reply = result.as_dict()
    reply["undo_until"] = result.undo_until.isoformat() if result.undo_until else None
    return reply


def _undo(conn: psycopg.Connection, decision_id: UUID) -> str | None:
    row = conn.execute("SELECT undo_until FROM l3_decision_log WHERE id = %s", (decision_id,)).fetchone()
    return row[0].isoformat() if row and row[0] else None


def undo(conn: psycopg.Connection, decision_id: UUID, *, decided_by: str, reason: str | None = None) -> dict[str, Any]:
    """"Cofnij" (toast or history): the decision - or its whole batch - is revoked."""
    row = conn.execute("SELECT batch_id FROM l3_decision_log WHERE id = %s", (decision_id,)).fetchone()
    if row is None:
        raise LookupError(f"Nie ma decyzji {decision_id}")
    revokes = [dec.revoke_decision(conn, decision_id, decided_by, reason)]
    return {"message": "Cofnięte", "revoke_ids": [str(r) for r in revokes]}


def undo_batch(conn: psycopg.Connection, batch_id: UUID, *, decided_by: str, reason: str | None = None) -> dict:
    revokes = dec.revoke_batch(conn, batch_id, decided_by, reason)
    return {"message": f"Cofnięto paczkę: {len(revokes)} decyzji", "revoke_ids": [str(r) for r in revokes]}


# ---------------------------------------------------------------------------
# history and KPI
# ---------------------------------------------------------------------------


def history(conn: psycopg.Connection, company_id: UUID, *, limit: int = 50, include_auto: bool = False
            ) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT d.id, d.kind::text AS kind, d.subject_type::text AS subject_type, d.subject_id, d.decided_by,
                   d.decided_at, d.method::text AS method, d.undo_until, d.batch_id, d.reason, d.payload,
                   d.reverts_decision_id,
                   (SELECT v.id FROM l3_decision_log v WHERE v.reverts_decision_id = d.id LIMIT 1) AS revoked_by
              FROM l3_decision_log d
             WHERE d.company_id = %s AND (%s OR d.method = 'human')
             ORDER BY d.decided_at DESC, d.id DESC LIMIT %s
            """,
            (company_id, include_auto, limit),
        ).fetchall()
    for row in rows:
        row["label"] = describe_decision(conn, row)
        row["can_undo"] = row["kind"] != "revoke" and row["revoked_by"] is None
        row["in_toast_window"] = bool(row["undo_until"] and row["undo_until"] >= datetime.now(row["undo_until"].tzinfo))
    return rows


def describe_decision(conn: psycopg.Connection, row: dict[str, Any]) -> str:
    payload = row.get("payload") or {}
    kind = row["kind"]
    if kind == "match":
        return cards.proposal_label(conn, "match", payload, {})
    if kind == "allocation":
        names = {str(r[0]): r[1] for r in conn.execute(
            "SELECT id, name FROM l3_cost_object WHERE id = ANY(%s::uuid[])",
            ([s["cost_object_id"] for s in payload.get("shares") or []],)).fetchall()}
        return " · ".join(f"{names.get(s['cost_object_id'], '?')} {round(float(s['share']) * 100, 2):g}%"
                          for s in payload.get("shares") or [])
    if kind == "category":
        return f"kategoria {payload.get('category')}"
    if kind == "cost_object":
        return f"obiekt kosztowy: {payload.get('action')} {payload.get('name') or ''}".strip()
    if kind == "rule":
        return f"reguła {payload.get('rule_key')}: {payload.get('action')} ({payload.get('from_stage')} → " \
               f"{payload.get('to_stage')})"
    if kind == "revoke":
        return f"cofnięcie decyzji {payload.get('reverts_kind')}"
    return kind


def stats(conn: psycopg.Connection, company_id: UUID) -> dict[str, Any]:
    """KPI of the queue (briefing, step 7): minutes of the person's work and errors weighted by amount."""
    open_by_kind = dict(conn.execute(
        "SELECT card_kind, count(*) FROM queue_state WHERE company_id = %s AND closed_at IS NULL GROUP BY 1",
        (company_id,)).fetchall())
    deferred = conn.execute(
        "SELECT count(*) FROM queue_state WHERE company_id = %s AND closed_at IS NULL AND deferred_until > now()",
        (company_id,)).fetchone()[0]
    decided = conn.execute(
        """
        SELECT count(*), coalesce(sum(duration_ms), 0), percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms),
               count(*) FILTER (WHERE fast_alarm)
          FROM queue_state WHERE company_id = %s AND decided_at IS NOT NULL AND outcome <> 'resolved_elsewhere'
        """,
        (company_id,)).fetchone()
    by_day = [{"day": r[0].isoformat(), "cards": r[1], "minutes": round(int(r[2]) / 60000, 2)} for r in conn.execute(
        """
        SELECT decided_at::date, count(*), coalesce(sum(duration_ms), 0) FROM queue_state
         WHERE company_id = %s AND decided_at IS NOT NULL AND outcome <> 'resolved_elsewhere'
         GROUP BY 1 ORDER BY 1
        """,
        (company_id,)).fetchall()]
    sentinels = conn.execute(
        "SELECT count(*) FILTER (WHERE answered_at IS NOT NULL), count(*) FILTER (WHERE passed), "
        "count(*) FILTER (WHERE passed IS FALSE) FROM queue_sentinel WHERE company_id = %s",
        (company_id,)).fetchone()
    audit = conn.execute(
        """
        SELECT count(*), count(*) FILTER (WHERE verdict = 'confirmed'), count(*) FILTER (WHERE verdict = 'error'),
               coalesce(sum(amount_minor) FILTER (WHERE verdict = 'error'), 0), count(*) FILTER (WHERE verdict IS NULL)
          FROM l2_audit_sample WHERE company_id = %s
        """,
        (company_id,)).fetchone()
    revokes = conn.execute(
        """
        SELECT count(*) FILTER (WHERE (r.payload ->> 'within_undo_window')::boolean),
               count(*) FILTER (WHERE NOT coalesce((r.payload ->> 'within_undo_window')::boolean, false)),
               coalesce(sum(abs(CASE d.subject_type
                   WHEN 'transaction' THEN (SELECT t.amount_minor FROM l1_bank_transaction t WHERE t.id = d.subject_id)
                   WHEN 'document' THEN (SELECT x.gross_minor FROM l1_document x WHERE x.id = d.subject_id)
                   WHEN 'line' THEN (SELECT coalesce(l.gross_minor, l.net_minor) FROM l1_document_line l
                                      WHERE l.id = d.subject_id)
                   WHEN 'internal_record' THEN (SELECT i.amount_minor FROM l1_internal_record i WHERE i.id = d.subject_id)
               END)) FILTER (WHERE NOT coalesce((r.payload ->> 'within_undo_window')::boolean, false)), 0),
               count(*) FILTER (WHERE d.method = 'rule')
          FROM l3_decision_log r JOIN l3_decision_log d ON d.id = r.reverts_decision_id
         WHERE r.company_id = %s AND r.kind = 'revoke'
        """,
        (company_id,)).fetchone()
    automatic = conn.execute(
        "SELECT count(*) FROM l3_decision_log d WHERE d.company_id = %s AND d.method = 'rule' AND d.kind <> 'revoke'",
        (company_id,)).fetchone()[0]
    human = conn.execute(
        "SELECT count(*) FROM l3_decision_log d WHERE d.company_id = %s AND d.method = 'human' AND d.kind <> 'revoke'",
        (company_id,)).fetchone()[0]
    warnings = []
    if decided[3]:
        warnings.append(f"{decided[3]} decyzji szybciej niż 0,8 s na kartach o niskiej odznace - sprawdź uważność")
    if sentinels[2]:
        warnings.append(f"wartownicy: {sentinels[2]} błędnych odpowiedzi na {sentinels[0]}")
    if audit[2]:
        warnings.append(f"audyt: {audit[2]} potwierdzonych błędów automatu ({money(int(audit[3]))})")
    return {
        "open_cards": sum(open_by_kind.values()), "open_by_kind": open_by_kind, "deferred": deferred,
        "decided_cards": decided[0], "work_minutes": round(int(decided[1]) / 60000, 2),
        "median_seconds": round(float(decided[2]) / 1000, 2) if decided[2] is not None else None,
        "by_day": by_day, "fast_alarms": decided[3],
        "sentinels": {"answered": sentinels[0], "passed": sentinels[1], "failed": sentinels[2]},
        "audit": {"sampled": audit[0], "confirmed": audit[1], "errors": audit[2],
                  "errors_amount_minor": int(audit[3]), "open": audit[4]},
        "revocations": {"in_toast_window": revokes[0], "later": revokes[1], "later_amount_minor": int(revokes[2]),
                        "of_automation": revokes[3]},
        "errors_weighted_minor": int(audit[3]) + int(revokes[2]),
        "decisions": {"human": human, "automatic": automatic},
        "warnings": warnings,
    }


def render_stats(s: dict[str, Any]) -> str:
    lines = [
        f"Kolejka: otwartych kart {s['open_cards']} ({', '.join(f'{k} {v}' for k, v in sorted(s['open_by_kind'].items()))}), "
        f"odłożonych {s['deferred']}",
        f"Twoja praca: {s['decided_cards']} kart, {s['work_minutes']} min"
        + (f" (mediana {s['median_seconds']} s na kartę)" if s["median_seconds"] is not None else ""),
        f"Błędy ważone kwotą: {money(s['errors_weighted_minor'])} (audyt {s['audit']['errors']} błędów "
        f"{money(s['audit']['errors_amount_minor'])}; cofnięcia po czasie {s['revocations']['later']} "
        f"{money(s['revocations']['later_amount_minor'])}; cofnięcia w 10 s: {s['revocations']['in_toast_window']})",
        f"Audyt: wylosowano {s['audit']['sampled']}, potwierdzone {s['audit']['confirmed']}, otwarte {s['audit']['open']}",
        f"Wartownicy: odpowiedzi {s['sentinels']['answered']}, dobre {s['sentinels']['passed']}, "
        f"złe {s['sentinels']['failed']}",
        f"Decyzje: człowiek {s['decisions']['human']}, automat {s['decisions']['automatic']} "
        "(KPI to minuty pracy i błędy ważone kwotą, nie „procent przez AI”)",
    ]
    for day in s["by_day"]:
        lines.append(f"  {day['day']}: {day['cards']} kart, {day['minutes']} min")
    for warning in s["warnings"]:
        lines.append(f"UWAGA: {warning}")
    return "\n".join(lines)
