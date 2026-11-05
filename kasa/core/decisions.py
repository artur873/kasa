"""Decision log (l3_decision_log): every decision, human or auto rule, append-only - and the queue's decisions.

``log_decision`` is the one way into the log (4A, 4B and the queue use it). Phase 4C adds the
dispatcher of the review queue (phase 4 contract, "Zapis decyzji"):

* ``accept_suggestion(conn, suggestion_id, *, decided_by, reason=None, extra=None)`` - by ``kind``:
  match -> ``apply_match`` (4A); allocation -> ``apply_allocation`` (4B, ``extra.shares`` completes a
  partial proposal); category -> ``decide_category``; cost_object_candidate -> approve / split / merge /
  reject / distinct (4B, ``extra.action``); rule_candidate -> accept / correct / reject
  (``kasa.core.rules.lifecycle``); duplicate -> a verdict;
* ``reject_suggestion``, ``defer_suggestion(until)``, ``decide_other`` ("Inne…": a match chosen by the
  person, no document WITH a subtype, own transfer, to clarify, a manual allocation, a category);
* ``revoke_decision`` - a NEW decision ``revoke`` that undoes the effects by kind (rows ``revoked``,
  the subject back in the queue, an invoice's balance back), takes back the rule observations of the
  decision and - when the decision was the automation's - suspends its rule (a correction);
* ``bulk_accept`` - only a homogeneous batch (same rule/tag, same counterparty, all "high"): every item
  its own decision, one ``batch_id``; ``revoke_batch`` undoes exactly those decisions.

After every decision of a person: ``kasa.core.rules.observe`` (observations), incremental mining of
rule candidates for the counterparty (``kasa.core.rules.mine``) and the queue state (time of decision).
The reason ("dlaczego") is always optional.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DecisionKind = Literal["match", "allocation", "category", "transaction_status", "cost_object", "rule", "revoke",
                       "duplicate"]
SubjectType = Literal["transaction", "document", "line", "internal_record", "cost_object", "rule"]

QUEUE_SOURCE = "queue"
MANUAL_SOURCE = "queue.other"
AWAITING_DAYS = {"advance": 180, "card_awaiting": 30}
NO_DOCUMENT_SUBTYPES = ("zus", "tax", "fee", "advance", "deposit", "card_awaiting", "salary", "refund", "duplicate",
                        "interest", "other")
HARD_TAGS = ("ksef_number", "mpp_block", "invoice_number", "single_open_invoice", "builtin_own_transfer",
             "builtin_no_document")
CARDS_OF_KIND = {
    "match": ("match",), "allocation": ("allocation",), "category": ("category", "sensitive"),
    "cost_object": ("cost_object_candidate",), "rule": ("rule_candidate", "rule_ready"),
    "duplicate": ("duplicate",), "transaction_status": ("match",),
}

_BATCH: ContextVar[UUID | None] = ContextVar("kasa_decision_batch", default=None)


@contextmanager
def decision_batch(batch_id: UUID | None = None) -> Iterator[UUID]:
    """Every decision logged inside gets this ``batch_id`` (a homogeneous batch of the queue)."""
    batch_id = batch_id or uuid4()
    token = _BATCH.set(batch_id)
    try:
        yield batch_id
    finally:
        _BATCH.reset(token)


def log_decision(
    conn: psycopg.Connection,
    *,
    company_id: UUID,
    kind: DecisionKind,
    subject_type: SubjectType,
    subject_id: UUID,
    decided_by: str,
    source: str,
    method: Literal["human", "rule"] = "human",
    payload: dict[str, Any] | None = None,
    rule_id: UUID | None = None,
    rule_version: int | None = None,
    reverts_decision_id: UUID | None = None,
    suggestion_id: UUID | None = None,
    reason: str | None = None,
    confidence: float | None = None,
    evidence: dict[str, Any] | list[Any] | None = None,
    created_by: str | None = None,
    batch_id: UUID | None = None,
    undo_until: datetime | None = None,
) -> UUID:
    """Append one decision and return its id. Guards in the database do the policing.

    ``undo_until`` (toast of the queue) is set by the database for a person's decision (+10 s);
    ``batch_id`` comes from ``decision_batch`` when not given.
    """
    row = conn.execute(
        """
        INSERT INTO l3_decision_log (
            company_id, kind, subject_type, subject_id, payload, rule_id, rule_version,
            decided_by, reverts_decision_id, suggestion_id, reason,
            source, method, confidence, evidence, created_by, batch_id, undo_until
        ) VALUES (
            %(company_id)s, %(kind)s, %(subject_type)s, %(subject_id)s, %(payload)s, %(rule_id)s, %(rule_version)s,
            %(decided_by)s, %(reverts)s, %(suggestion_id)s, %(reason)s,
            %(source)s, %(method)s, %(confidence)s, %(evidence)s, %(created_by)s, %(batch_id)s, %(undo_until)s
        )
        RETURNING id
        """,
        {
            "company_id": company_id,
            "kind": kind,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "payload": Jsonb(payload or {}),
            "rule_id": rule_id,
            "rule_version": rule_version,
            "decided_by": decided_by,
            "reverts": reverts_decision_id,
            "suggestion_id": suggestion_id,
            "reason": reason,
            "source": source,
            "method": method,
            "confidence": confidence,
            "evidence": Jsonb(evidence if evidence is not None else {}),
            "created_by": created_by or decided_by,
            "batch_id": batch_id or _BATCH.get(),
            "undo_until": undo_until,
        },
    ).fetchone()
    return row[0]


# ===========================================================================
# The queue's decisions (phase 4C)
# ===========================================================================


class DecisionError(ValueError):
    """The decision is not possible as asked (Polish message for the person)."""


@dataclass
class DecisionResult:
    decision_ids: list[UUID] = field(default_factory=list)
    kind: str | None = None
    subject_type: str | None = None
    subject_id: UUID | None = None
    undo_until: datetime | None = None
    batch_id: UUID | None = None
    message: str = ""
    observations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def decision_id(self) -> UUID | None:
        return self.decision_ids[0] if self.decision_ids else None

    def as_dict(self) -> dict[str, Any]:
        return {"decision_ids": [str(d) for d in self.decision_ids], "kind": self.kind,
                "subject_type": self.subject_type, "subject_id": str(self.subject_id) if self.subject_id else None,
                "undo_until": self.undo_until.isoformat() if self.undo_until else None,
                "batch_id": str(self.batch_id) if self.batch_id else None, "message": self.message,
                "rule_observations": len(self.observations),
                "rules_suspended": [str(o["rule_key"]) for o in self.observations if o.get("suspended")]}


def load_suggestion(conn: psycopg.Connection, suggestion_id: UUID, *, lock: bool = False) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """
            SELECT id, company_id, kind::text AS kind, subject_type::text AS subject_type, subject_id, proposal,
                   score, rank, status::text AS status, evidence, source, resolved_by_decision, created_at
              FROM l2_suggestion WHERE id = %s
            """ + (" FOR UPDATE" if lock else ""),
            (suggestion_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma sugestii {suggestion_id}")
    return row


def _decision(conn: psycopg.Connection, decision_id: UUID) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """
            SELECT id, company_id, kind::text AS kind, subject_type::text AS subject_type, subject_id, payload,
                   method::text AS method, decided_by, decided_at, rule_id, rule_version, suggestion_id,
                   undo_until, batch_id, reverts_decision_id
              FROM l3_decision_log WHERE id = %s
            """,
            (decision_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma decyzji {decision_id}")
    return row


def _undo_until(conn: psycopg.Connection, decision_id: UUID | None) -> datetime | None:
    if decision_id is None:
        return None
    row = conn.execute("SELECT undo_until FROM l3_decision_log WHERE id = %s", (decision_id,)).fetchone()
    return row[0] if row else None


def _company_extension(conn: psycopg.Connection, company_id: UUID) -> str:
    row = conn.execute("SELECT extension_slug FROM company WHERE id = %s", (company_id,)).fetchone()
    if row is None:
        raise LookupError(f"Nie ma firmy {company_id}")
    return row[0]


def _after(conn: psycopg.Connection, company_id: UUID, decision_ids: list[UUID], *, subject_type: str,
           subject_id: UUID, card_kinds: tuple[str, ...], decided_by: str, outcome: str) -> list[dict[str, Any]]:
    """Observations of rules, incremental mining, queue state - after a person's decision."""
    from kasa.core.queue import state as queue_state
    from kasa.core.rules import mine, observe
    from kasa.core.rules.facts import canonical_counterparty, subject_facts

    observations: list[dict[str, Any]] = []
    for decision_id in decision_ids:
        observations += observe.observe_decision(conn, decision_id)
    queue_state.mark_decided(conn, company_id, subject_type, subject_id, card_kinds,
                             decision_id=decision_ids[0] if decision_ids else None, decided_by=decided_by,
                             outcome=outcome)
    if subject_type in ("transaction", "document", "line"):
        facts = subject_facts(conn, company_id, subject_type, subject_id)
        party = canonical_counterparty(conn, facts.get("counterparty_id")) if facts else None
        if party is not None:
            mine.mine(conn, company_id, counterparties={party})
    return observations


# ---------------------------------------------------------------------------
# category decisions (l3_category_decision) - the queue's own writer
# ---------------------------------------------------------------------------


def decide_category(conn: psycopg.Connection, company_id: UUID, subject_type: str, subject_id: UUID, category: str, *,
                    decided_by: str, method: str = "human", rule_id: UUID | None = None,
                    rule_version: int | None = None, suggestion_id: UUID | None = None, reason: str | None = None,
                    subtype: str | None = None, source: str = QUEUE_SOURCE) -> UUID:
    """Category of a subject (names from the extension). A person may replace a category; the automation never."""
    from kasa.core.extension import ExtensionError, get_extension

    if subject_type not in ("transaction", "document", "line", "internal_record"):
        raise DecisionError(f"kategorię nadaje się przelewowi, dokumentowi, pozycji albo wierszowi - nie {subject_type}")
    try:
        categories = list(get_extension(_company_extension(conn, company_id)).categories)
    except ExtensionError:
        categories = []
    if categories and category not in categories:
        raise DecisionError(f"kategoria {category!r} spoza listy rozszerzenia ({', '.join(categories)})")
    existing = conn.execute(
        """
        SELECT id, category FROM l3_category_decision
         WHERE company_id = %s AND subject_type = %s AND subject_id = %s AND status = 'active' FOR UPDATE
        """,
        (company_id, subject_type, subject_id),
    ).fetchall()
    if existing and method != "human":
        raise DecisionError("podmiot ma już kategorię - automat niczego nie zastępuje")
    decision_id = log_decision(
        conn, company_id=company_id, kind="category", subject_type=subject_type, subject_id=subject_id,
        decided_by=decided_by, source=source, method=method, rule_id=rule_id, rule_version=rule_version,
        suggestion_id=suggestion_id, reason=reason,
        payload={"category": category, "subtype": subtype, "replaces": [str(r[0]) for r in existing]},
    )
    if existing:
        conn.execute(
            "UPDATE l3_category_decision SET status = 'revoked', revoked_by_decision = %s, method = 'human', "
            "updated_by = %s WHERE id = ANY(%s)",
            (decision_id, decided_by, [r[0] for r in existing]),
        )
    conn.execute(
        """
        INSERT INTO l3_category_decision (company_id, subject_type, subject_id, category, decision_id, rule_id,
                                          rule_version, source, method, evidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (company_id, subject_type, subject_id, category, decision_id, rule_id, rule_version, source, method,
         Jsonb({"suggestion_id": str(suggestion_id) if suggestion_id else None, "subtype": subtype}), decided_by),
    )
    conn.execute(
        """
        UPDATE l2_suggestion SET status = CASE WHEN id = %s THEN 'accepted'::suggestion_status
                                               ELSE 'rejected'::suggestion_status END,
               resolved_at = now(), resolved_by_decision = %s
         WHERE company_id = %s AND kind = 'category' AND subject_type = %s AND subject_id = %s AND status = 'open'
        """,
        (suggestion_id, decision_id, company_id, subject_type, subject_id),
    )
    return decision_id


# ---------------------------------------------------------------------------
# accept / reject / defer
# ---------------------------------------------------------------------------


def accept_suggestion(conn: psycopg.Connection, suggestion_id: UUID, *, decided_by: str, reason: str | None = None,
                      extra: dict[str, Any] | None = None) -> DecisionResult:
    """A person accepts a suggestion (rank 1 = "Akceptuj", rank 2-3 = "alternatywa"). Dispatches by kind."""
    from kasa.core.allocation.decide import apply_allocation
    from kasa.core.cost_objects_discovery import decide as co
    from kasa.core.matching.decide import apply_match
    from kasa.core.rules import lifecycle

    extra = dict(extra or {})
    s = load_suggestion(conn, suggestion_id, lock=True)
    if s["status"] != "open":
        raise DecisionError(f"sugestia ma status {s['status']} - przyjąć można tylko otwartą")
    kind, company_id = s["kind"], s["company_id"]
    proposal = s["proposal"] or {}
    outcome = "accepted" if (s["rank"] or 1) == 1 else "alternative"
    result = DecisionResult(kind=kind, subject_type=s["subject_type"], subject_id=s["subject_id"])
    if kind == "match":
        result.decision_ids = [apply_match(conn, suggestion_id, decided_by=decided_by, method="human", reason=reason,
                                           source=QUEUE_SOURCE)]
        from kasa.core.queue.cards import proposal_label

        result.message = f"Zapisano: {proposal_label(conn, 'match', proposal, s['evidence'] or {})}"[:160]
    elif kind == "allocation":
        result.decision_ids = [apply_allocation(conn, suggestion_id, decided_by=decided_by, method="human",
                                                reason=reason, shares=extra.get("shares"), source=QUEUE_SOURCE)]
        result.message = "Alokacja zapisana"
    elif kind == "category":
        category = extra.get("category") or proposal.get("category")
        if not category:
            raise DecisionError("wybierz kategorię (słowo wrażliwe: kategorię nadaje człowiek)")
        result.decision_ids = [decide_category(conn, company_id, s["subject_type"], s["subject_id"], category,
                                               decided_by=decided_by, suggestion_id=suggestion_id, reason=reason,
                                               subtype=extra.get("subtype") or proposal.get("subtype"))]
        result.message = f"Kategoria: {category}"
    elif kind == "cost_object_candidate":
        action = extra.get("action") or "approve"
        cost_object_id = s["subject_id"]
        if action == "approve":
            decision = co.approve_candidate(conn, cost_object_id, decided_by=decided_by,
                                            attrs_override=extra.get("attrs"), reason=reason, source=QUEUE_SOURCE)
        elif action == "split":
            decision = co.split_candidate(conn, cost_object_id, extra.get("groups") or [], decided_by=decided_by,
                                          reason=reason, source=QUEUE_SOURCE).decision_id
        elif action == "merge":
            others = [UUID(str(i)) for i in (extra.get("with") or [])]
            decision = co.merge_candidates(conn, [cost_object_id, *others], decided_by=decided_by,
                                           into=UUID(str(extra["into"])) if extra.get("into") else None,
                                           reason=reason, source=QUEUE_SOURCE)
        elif action == "reject":
            decision = co.reject_candidate(conn, cost_object_id, decided_by=decided_by, reason=reason,
                                           source=QUEUE_SOURCE)
        elif action == "distinct":
            if not extra.get("other"):
                raise DecisionError("„na pewno różne” - wskaż drugi obiekt")
            decision = co.mark_distinct(conn, cost_object_id, UUID(str(extra["other"])), decided_by=decided_by,
                                        reason=reason, source=QUEUE_SOURCE)
        else:
            raise DecisionError(f"nieznany przycisk karty kandydata: {action!r}")
        result.decision_ids = [decision]
        result.message = {"approve": "Obiekt zatwierdzony", "split": "Kandydat podzielony",
                          "merge": "Połączono", "reject": "Kandydat odrzucony",
                          "distinct": "Zapisano: na pewno różne"}[action]
        outcome = "accepted" if action == "approve" else ("rejected" if action == "reject" else "other")
        if extra.get("refresh", True):
            result.message += _refresh_after_objects(conn, company_id, cost_object_id, decision, decided_by, outcome)
    elif kind == "rule_candidate":
        action = extra.get("action") or "accept"
        rule_id = s["subject_id"]
        if action == "accept":
            decision, _ = lifecycle.accept(conn, rule_id, decided_by=decided_by, reason=reason, source=QUEUE_SOURCE)
        elif action == "correct":
            decision, _ = lifecycle.correct(conn, rule_id, decided_by=decided_by, pattern=extra.get("pattern"),
                                            action=extra.get("rule_action"), scope=extra.get("scope"),
                                            question=extra.get("question"), reason=reason, source=QUEUE_SOURCE)
        elif action == "reject":
            decision = lifecycle.reject(conn, rule_id, decided_by=decided_by, reason=reason, source=QUEUE_SOURCE)
            outcome = "rejected"
        else:
            raise DecisionError(f"nieznany przycisk karty reguły: {action!r}")
        result.decision_ids = [decision]
        result.message = {"accept": "Reguła przyjęta jako podpowiedź", "correct": "Nowa wersja reguły",
                          "reject": "Reguła odrzucona"}[action]
    elif kind == "duplicate":
        result.decision_ids = [_duplicate_verdict(conn, s, "same", decided_by, reason)]
        result.message = "Zapisano: duplikat"
    else:
        raise DecisionError(f"sugestii rodzaju {kind} kolejka nie rozstrzyga")
    result.undo_until = _undo_until(conn, result.decision_id)
    result.batch_id = _BATCH.get()
    result.observations = _after(conn, company_id, result.decision_ids, subject_type=s["subject_type"],
                                 subject_id=s["subject_id"], card_kinds=CARDS_OF_KIND.get(
                                     {"cost_object_candidate": "cost_object", "rule_candidate": "rule"}.get(kind, kind),
                                     ()), decided_by=decided_by, outcome="batch" if result.batch_id else outcome)
    return result


def _refresh_after_objects(conn: psycopg.Connection, company_id: UUID, decided_object: UUID, decision_id: UUID,
                           decided_by: str, outcome: str) -> str:
    """The last open candidate is decided: allocation proposals were waiting for it - refresh them (4B) and the queue."""
    from kasa.core.allocation.run import run_allocation
    from kasa.core.companies import get_company
    from kasa.core.queue import build as queue_build
    from kasa.core.queue import state as queue_state

    still_open = conn.execute(
        """
        SELECT count(*) FROM l2_suggestion
         WHERE company_id = %s AND kind = 'cost_object_candidate' AND status = 'open' AND subject_id <> %s
        """,
        (company_id, decided_object),
    ).fetchone()[0]
    if still_open:
        return ""
    slug = conn.execute("SELECT slug FROM company WHERE id = %s", (company_id,)).fetchone()[0]
    company = get_company(conn, slug)
    # the card of the decided object is closed before the rebuild (it must not look "resolved elsewhere")
    queue_state.mark_decided(conn, company_id, "cost_object", decided_object, ("cost_object_candidate",),
                             decision_id=decision_id, decided_by=decided_by, outcome=outcome)
    report = run_allocation(conn, company)
    queue_build.build(conn, company, mine_rules=False, auto_rules=False, sentinels=False)
    return f"; alokacje odświeżone (sugestie nowe/zmienione {report.suggested}, automat {report.auto.get('applied', 0)})"


def _duplicate_verdict(conn: psycopg.Connection, s: dict[str, Any], verdict: str, decided_by: str,
                       reason: str | None) -> UUID:
    decision = log_decision(
        conn, company_id=s["company_id"], kind="duplicate", subject_type=s["subject_type"], subject_id=s["subject_id"],
        decided_by=decided_by, source=QUEUE_SOURCE, suggestion_id=s["id"], reason=reason,
        payload={"verdict": verdict, "duplicate_of": (s["proposal"] or {}).get("duplicate_of")},
    )
    conn.execute(
        "UPDATE l2_suggestion SET status = %s, resolved_at = now(), resolved_by_decision = %s WHERE id = %s",
        ("accepted" if verdict == "same" else "rejected", decision, s["id"]),
    )
    return decision


def reject_suggestion(conn: psycopg.Connection, suggestion_id: UUID, *, decided_by: str,
                      reason: str | None = None) -> DecisionResult:
    """"Odrzuć": a candidate or a rule card is a decision; a proposal is only rejected (it never comes back)."""
    s = load_suggestion(conn, suggestion_id, lock=True)
    if s["status"] != "open":
        raise DecisionError(f"sugestia ma status {s['status']}")
    if s["kind"] in ("cost_object_candidate", "rule_candidate"):
        return accept_suggestion(conn, suggestion_id, decided_by=decided_by, reason=reason, extra={"action": "reject"})
    result = DecisionResult(kind=s["kind"], subject_type=s["subject_type"], subject_id=s["subject_id"])
    if s["kind"] == "duplicate":
        result.decision_ids = [_duplicate_verdict(conn, s, "different", decided_by, reason)]
        result.undo_until = _undo_until(conn, result.decision_id)
        result.message = "Zapisano: to nie duplikat"
        from kasa.core.queue import state as queue_state
        queue_state.mark_decided(conn, s["company_id"], s["subject_type"], s["subject_id"], ("duplicate",),
                                 decision_id=result.decision_id, decided_by=decided_by, outcome="rejected")
        return result
    conn.execute(
        "UPDATE l2_suggestion SET status = 'rejected', resolved_at = now(), "
        "evidence = evidence || %s WHERE id = %s",
        (Jsonb({"rejected_by": decided_by, "rejected_reason": reason}), suggestion_id),
    )
    from kasa.core.queue import state as queue_state
    queue_state.note_rejection(conn, s["company_id"], s["kind"], s["subject_type"], s["subject_id"], suggestion_id)
    result.message = "Propozycja odrzucona - nie wróci"
    return result


def defer_suggestion(conn: psycopg.Connection, suggestion_id: UUID, until: datetime | date | None = None, *,
                     decided_by: str = QUEUE_SOURCE) -> datetime:
    """"Odłóż": the card leaves the top of the queue until ``until`` (default: tomorrow)."""
    from kasa.core.queue import state as queue_state

    s = load_suggestion(conn, suggestion_id)
    card_kind = {"category": "category", "cost_object_candidate": "cost_object_candidate",
                 "rule_candidate": "rule_candidate"}.get(s["kind"], s["kind"])
    if s["kind"] == "category" and (s["proposal"] or {}).get("needs_human"):
        card_kind = "sensitive"
    return queue_state.defer(conn, s["company_id"], card_kind, s["subject_type"], s["subject_id"], until,
                             decided_by=decided_by, suggestion_ids=[suggestion_id])


# ---------------------------------------------------------------------------
# "Inne…"
# ---------------------------------------------------------------------------


def _occurrence_amount(conn: psycopg.Connection, transaction_id: UUID) -> tuple[int, date]:
    row = conn.execute(
        """
        SELECT t.amount_minor, t.booking_date, t.mpp_group_id, t.mpp_role FROM l1_bank_transaction t WHERE t.id = %s
        """,
        (transaction_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma przelewu {transaction_id}")
    amount, booked, group, role = row
    if group and role == "main":
        amount = conn.execute(
            "SELECT sum(amount_minor) FROM l1_bank_transaction WHERE mpp_group_id = %s AND mpp_role IN ('main', "
            "'vat_leg') AND superseded_by IS NULL AND status = 'booked'", (group,),
        ).fetchone()[0]
    return abs(int(amount)), booked


def _open_amount(conn: psycopg.Connection, target: dict[str, Any]) -> int | None:
    from kasa.core.matching.decide import _target_open

    return _target_open(conn, target)


def _manual_items(conn: psycopg.Connection, transaction_id: UUID, targets: list[dict[str, Any]]) -> list[dict]:
    amount, _ = _occurrence_amount(conn, transaction_id)
    items = []
    for target in targets:
        item = {"document_id": None, "internal_record_id": None, "related_transaction_id": None,
                "amount_minor": None, "difference_kind": None, "difference_minor": None}
        for key in ("document_id", "internal_record_id", "related_transaction_id"):
            if target.get(key):
                item[key] = str(target[key])
        if sum(1 for k in ("document_id", "internal_record_id", "related_transaction_id") if item[k]) != 1:
            raise DecisionError("każda pozycja wskazuje dokładnie jeden dokument, wiersz arkusza albo przelew")
        if target.get("amount_minor") is not None:
            item["amount_minor"] = int(target["amount_minor"])
        if target.get("difference_kind"):
            item["difference_kind"], item["difference_minor"] = target["difference_kind"], int(
                target.get("difference_minor") or 0)
        items.append(item)
    if len(items) == 1 and items[0]["amount_minor"] is None:
        items[0]["amount_minor"] = amount
    elif any(i["amount_minor"] is None for i in items):
        opens = [_open_amount(conn, i) for i in items]
        if any(o is None for o in opens) or sum(opens) != amount:
            raise DecisionError("kilka dokumentów: podaj kwotę każdego (suma otwartych sald ≠ kwota przelewu)")
        for item, value in zip(items, opens, strict=True):
            item["amount_minor"] = value
    return items


def _manual_suggestion(conn: psycopg.Connection, company_id: UUID, kind: str, subject_type: str, subject_id: UUID,
                       proposal: dict[str, Any], decided_by: str) -> UUID:
    """The person's own choice as a proposal written by the queue (so that the one write path of 4A/4B applies)."""
    return conn.execute(
        """
        INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, source, method, evidence,
                                   created_by)
        VALUES (%s, %s, %s, %s, %s, %s, 'code', %s, 'kasa.queue') RETURNING id
        """,
        (company_id, kind, subject_type, subject_id, Jsonb(proposal), MANUAL_SOURCE,
         Jsonb({"facts": ["wybór człowieka: „Inne…”"], "manual": True, "entered_by": decided_by, "badge": None,
                "precedents": 0, "hard": False, "margin": None, "tags": ["manual"]})),
    ).fetchone()[0]


def decide_other(conn: psycopg.Connection, company_id: UUID, subject_type: str, subject_id: UUID, *, decided_by: str,
                 kind: str, targets: list[dict[str, Any]] | None = None, subtype: str | None = None,
                 shares: list[dict[str, Any]] | list[tuple] | None = None, category: str | None = None,
                 reason: str | None = None) -> DecisionResult:
    """"Inne…" on a card: the person's own decision.

    ``kind``: ``matched`` (``targets`` = documents / sheet rows / transfers, amounts optional for one),
    ``no_document`` (``subtype`` REQUIRED), ``own_transfer``, ``to_clarify``, ``allocation`` (``shares``)
    or ``category``.
    """
    from kasa.core.allocation.decide import allocate_manually
    from kasa.core.matching.decide import apply_match

    result = DecisionResult(kind=kind, subject_type=subject_type, subject_id=subject_id)
    if kind in ("matched", "match", "no_document", "own_transfer", "to_clarify"):
        if subject_type != "transaction":
            raise DecisionError("status końcowy ma tylko przelew")
        status = "matched" if kind == "match" else kind
        proposal: dict[str, Any] = {"items": [], "final_status": status, "subtype": None, "awaiting_until": None}
        if status == "matched":
            if not targets:
                raise DecisionError("dopasowanie: wskaż dokument, wiersz arkusza albo przelew")
            proposal["items"] = _manual_items(conn, subject_id, targets)
        elif status == "no_document":
            if not subtype:
                raise DecisionError("bez dokumentu: podtyp jest obowiązkowy (ZUS, podatek, prowizja, zaliczka, …)")
            if subtype not in NO_DOCUMENT_SUBTYPES:
                raise DecisionError(f"nieznany podtyp {subtype!r} ({', '.join(NO_DOCUMENT_SUBTYPES)})")
            proposal["subtype"] = subtype
            if subtype in AWAITING_DAYS:
                _, booked = _occurrence_amount(conn, subject_id)
                proposal["awaiting_until"] = (booked + timedelta(days=AWAITING_DAYS[subtype])).isoformat()
        suggestion = _manual_suggestion(conn, company_id, "match", "transaction", subject_id, proposal, decided_by)
        result.decision_ids = [apply_match(conn, suggestion, decided_by=decided_by, method="human", reason=reason,
                                           source=QUEUE_SOURCE)]
        result.kind, card_kinds = "match", ("match",)
        result.message = {"matched": "Dopasowanie zapisane", "no_document": f"Bez dokumentu: {subtype}",
                          "own_transfer": "Transfer własny", "to_clarify": "Do wyjaśnienia"}[status]
    elif kind == "allocation":
        if not shares:
            raise DecisionError("alokacja: podaj obiekty i udziały")
        result.decision_ids = [allocate_manually(conn, company_id, subject_type, subject_id, shares,
                                                 decided_by=decided_by, reason=reason, source=QUEUE_SOURCE)]
        card_kinds = ("allocation",)
        result.message = "Alokacja zapisana"
    elif kind == "category":
        if not category:
            raise DecisionError("wybierz kategorię")
        result.decision_ids = [decide_category(conn, company_id, subject_type, subject_id, category,
                                               decided_by=decided_by, reason=reason, subtype=subtype)]
        card_kinds = ("category", "sensitive")
        result.message = f"Kategoria: {category}"
    else:
        raise DecisionError(f"„Inne…”: nieznany rodzaj decyzji {kind!r}")
    result.undo_until = _undo_until(conn, result.decision_id)
    result.batch_id = _BATCH.get()
    result.observations = _after(conn, company_id, result.decision_ids, subject_type=subject_type,
                                 subject_id=subject_id, card_kinds=card_kinds, decided_by=decided_by, outcome="other")
    return result


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def _restore_suggestions(conn: psycopg.Connection, decision_id: UUID, revoke_id: UUID) -> int:
    """The proposals resolved by the revoked decision come back to the queue as new open rows (history kept)."""
    rows = conn.execute(
        """
        INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, score, rank, source, method,
                                   confidence, evidence, schema_version, prompt_version, created_by)
        SELECT company_id, kind, subject_type, subject_id, proposal, score, rank, source, method, confidence,
               evidence || jsonb_build_object('restored_from', id, 'restored_after_revoke', %s::text),
               schema_version, prompt_version, 'kasa.queue'
          FROM l2_suggestion
         WHERE resolved_by_decision = %s AND status IN ('accepted', 'rejected') AND source <> %s
           AND kind IN ('match', 'allocation', 'category', 'duplicate', 'cost_object_candidate')
         ORDER BY rank NULLS LAST, created_at
        RETURNING id
        """,
        (str(revoke_id), decision_id, MANUAL_SOURCE),
    ).fetchall()
    return len(rows)


def _revoke_rows(conn: psycopg.Connection, table: str, decision_id: UUID, revoke_id: UUID, decided_by: str) -> int:
    return conn.execute(
        f"UPDATE {table} SET status = 'revoked', revoked_by_decision = %s, method = 'human', updated_by = %s "
        "WHERE decision_id = %s AND status = 'active'",
        (revoke_id, decided_by, decision_id),
    ).rowcount


def _revert_cost_object(conn: psycopg.Connection, decision: dict[str, Any], revoke_id: UUID, decided_by: str) -> None:
    from kasa.core.cost_objects_discovery.pairs import retrain

    action = (decision["payload"] or {}).get("action")
    if action not in ("approve", "reject"):
        raise DecisionError(f"cofnięcie decyzji „{action}” o obiekcie kosztowym nie jest obsługiwane - "
                            "zdecyduj ponownie (połącz / podziel / zatwierdź)")
    cost_object_id = decision["subject_id"]
    row = conn.execute("SELECT status::text, decision_id FROM l3_cost_object WHERE id = %s FOR UPDATE",
                       (cost_object_id,)).fetchone()
    if row is None or row[1] != decision["id"]:
        raise DecisionError("po tej decyzji obiekt zmienił się jeszcze raz - cofnij najpierw późniejszą decyzję")
    if action == "approve":
        used = conn.execute("SELECT count(*) FROM l3_allocation WHERE cost_object_id = %s AND status = 'active'",
                            (cost_object_id,)).fetchone()[0]
        if used:
            raise DecisionError(f"obiekt ma {used} aktywnych alokacji - najpierw je cofnij")
    conn.execute(
        "UPDATE l3_cost_object SET status = 'candidate', method = 'human', decision_id = %s, updated_by = %s "
        "WHERE id = %s",
        (revoke_id, decided_by, cost_object_id),
    )
    conn.execute(
        """
        UPDATE l2_cost_object_member SET status = 'open', resolved_at = NULL, resolved_by_decision = NULL
         WHERE candidate_id = %s AND resolved_by_decision = %s AND status IN ('accepted', 'rejected')
        """,
        (cost_object_id, decision["id"]),
    )
    retrain(conn, decision["company_id"], created_by=decided_by)


def revoke_decision(conn: psycopg.Connection, decision_id: UUID, decided_by: str, reason: str | None = None, *,
                    source: str = QUEUE_SOURCE) -> UUID:
    """Undo a decision with a NEW decision ``revoke`` (nothing is deleted). Returns the revoke's id."""
    from kasa.core.queue import state as queue_state
    from kasa.core.rules import lifecycle, observe

    decision = _decision(conn, decision_id)
    if decision["kind"] == "revoke":
        raise DecisionError("cofnięcia nie cofa się - podejmij decyzję od nowa")
    if conn.execute("SELECT 1 FROM l3_decision_log WHERE reverts_decision_id = %s", (decision_id,)).fetchone():
        raise DecisionError(f"decyzja {decision_id} jest już cofnięta")
    revoke_id = log_decision(
        conn, company_id=decision["company_id"], kind="revoke", subject_type=decision["subject_type"],
        subject_id=decision["subject_id"], decided_by=decided_by, source=source, method="human",
        reverts_decision_id=decision_id, reason=reason,
        payload={"reverts_kind": decision["kind"], "reverts_method": decision["method"],
                 "rule_id": str(decision["rule_id"]) if decision["rule_id"] else None,
                 "within_undo_window": bool(decision["undo_until"] and decision["undo_until"] >= datetime.now(
                     decision["undo_until"].tzinfo))},
    )
    kind = decision["kind"]
    if kind in ("match", "transaction_status"):
        _revoke_rows(conn, "l3_match", decision_id, revoke_id, decided_by)
        _revoke_rows(conn, "l3_transaction_status", decision_id, revoke_id, decided_by)
        _restore_suggestions(conn, decision_id, revoke_id)
    elif kind == "allocation":
        _revoke_rows(conn, "l3_allocation", decision_id, revoke_id, decided_by)
        _restore_suggestions(conn, decision_id, revoke_id)
    elif kind == "category":
        _revoke_rows(conn, "l3_category_decision", decision_id, revoke_id, decided_by)
        _restore_suggestions(conn, decision_id, revoke_id)
    elif kind == "cost_object":
        _revert_cost_object(conn, decision, revoke_id, decided_by)
        _restore_suggestions(conn, decision_id, revoke_id)
    elif kind == "rule":
        lifecycle.revert(conn, decision, revoke_id, decided_by)
    elif kind == "duplicate":
        _restore_suggestions(conn, decision_id, revoke_id)
    observe.withdraw(conn, decision_id, revoke_id, decided_by)
    if decision["method"] == "rule" and decision["rule_id"]:
        rule = lifecycle.rule_row(conn, decision["rule_id"])
        if rule["stage_text"] == "auto":
            lifecycle.suspend(conn, decision["rule_id"], decided_by=decided_by,
                              reason=f"poprawka decyzji automatu {decision_id}" + (f": {reason}" if reason else ""),
                              cause_decision_id=decision_id, actual=f"cofnięte przez {decided_by}",
                              predicted=decision["payload"])
    queue_state.mark_revoked(conn, decision_id, revoke_id)
    return revoke_id


def revoke_batch(conn: psycopg.Connection, batch_id: UUID, decided_by: str, reason: str | None = None) -> list[UUID]:
    """Undo a batch: exactly its decisions, each by its own revoke (one new batch id for the revokes)."""
    rows = conn.execute(
        """
        SELECT d.id FROM l3_decision_log d
         WHERE d.batch_id = %s AND d.kind <> 'revoke'
           AND NOT EXISTS (SELECT 1 FROM l3_decision_log v WHERE v.reverts_decision_id = d.id)
         ORDER BY d.decided_at, d.id
        """,
        (batch_id,),
    ).fetchall()
    if not rows:
        raise DecisionError(f"paczka {batch_id} nie ma decyzji do cofnięcia")
    with decision_batch():
        return [revoke_decision(conn, r[0], decided_by, reason or f"cofnięcie paczki {batch_id}") for r in rows]


# ---------------------------------------------------------------------------
# homogeneous batch
# ---------------------------------------------------------------------------


def suggestion_counterparty(conn: psycopg.Connection, s: dict[str, Any]) -> UUID | None:
    """The counterparty a suggestion is about: of the proposed documents (when one), else of the subject."""
    from kasa.core.rules.facts import canonical_counterparty, subject_facts

    proposal = s["proposal"] or {}
    documents = [i["document_id"] for i in proposal.get("items") or [] if i.get("document_id")]
    if documents:
        parties = {r[0] for r in conn.execute(
            "SELECT DISTINCT counterparty_id FROM l1_document WHERE id = ANY(%s::uuid[])", (documents,)).fetchall()}
        canonical = {canonical_counterparty(conn, p) for p in parties if p}
        if len(canonical) == 1:
            return next(iter(canonical))
    if s["subject_type"] in ("transaction", "document", "line"):
        facts = subject_facts(conn, s["company_id"], s["subject_type"], s["subject_id"])
        return canonical_counterparty(conn, facts.get("counterparty_id")) if facts else None
    return None


def batch_key(conn: psycopg.Connection, s: dict[str, Any]) -> str | None:
    """Key of a homogeneous batch: kind | rule or hard tag | counterparty | outcome - only for open, rank 1, high."""
    evidence = s["evidence"] or {}
    if s["status"] != "open" or (s["rank"] or 0) != 1 or evidence.get("badge") != "high":
        return None
    if s["kind"] not in ("match", "allocation", "category"):
        return None
    tags = evidence.get("tags") or []
    rule = ((evidence.get("detail") or {}).get("rule_key") or next((t for t in tags if t.startswith("rule:")), None)
            or next((t for t in HARD_TAGS if t in tags), None) or (evidence.get("rule") or {}).get("rule_key"))
    if not rule:
        return None
    party = suggestion_counterparty(conn, s)
    if party is None:
        return None
    proposal = s["proposal"] or {}
    if s["kind"] == "match":
        outcome = f"{proposal.get('final_status')}/{proposal.get('subtype') or '-'}"
    elif s["kind"] == "allocation":
        outcome = ",".join(sorted(str(x.get("cost_object_id")) for x in proposal.get("shares") or []))
    else:
        outcome = str(proposal.get("category"))
    return f"{s['kind']}|{rule}|{party}|{outcome}"


@dataclass
class BulkResult:
    batch_id: UUID
    decision_ids: list[UUID]
    amount_minor: int
    key: str

    def as_dict(self) -> dict[str, Any]:
        return {"batch_id": str(self.batch_id), "decisions": len(self.decision_ids),
                "decision_ids": [str(d) for d in self.decision_ids], "amount_minor": self.amount_minor,
                "key": self.key}


def bulk_accept(conn: psycopg.Connection, suggestion_ids: list[UUID], decided_by: str,
                reason: str | None = None) -> BulkResult:
    """Accept a HOMOGENEOUS batch (same rule/tag, same counterparty, all high). Each item is its own decision."""
    ids = list(dict.fromkeys(UUID(str(i)) for i in suggestion_ids))
    if len(ids) < 2:
        raise DecisionError("paczka to co najmniej dwie pozycje")
    suggestions = [load_suggestion(conn, i, lock=True) for i in ids]
    keys = {batch_key(conn, s) for s in suggestions}
    if None in keys:
        bad = [str(s["id"]) for s in suggestions if batch_key(conn, s) is None]
        raise DecisionError(f"paczka tylko z otwartych propozycji z odznaką „wysoka” i regułą/tagiem: {bad[:3]}")
    if len(keys) != 1:
        raise DecisionError("paczka nie jest jednorodna (różne reguły, kontrahenci albo rozstrzygnięcia)")
    amount = 0
    decisions: list[UUID] = []
    with decision_batch() as batch_id:
        for s in suggestions:
            result = accept_suggestion(conn, s["id"], decided_by=decided_by, reason=reason)
            decisions += result.decision_ids
            if s["subject_type"] == "transaction":
                amount += abs(int(conn.execute("SELECT amount_minor FROM l1_bank_transaction WHERE id = %s",
                                               (s["subject_id"],)).fetchone()[0]))
    return BulkResult(batch_id, decisions, amount, next(iter(keys)))


# ---------------------------------------------------------------------------
# "to nie jest wrażliwe"
# ---------------------------------------------------------------------------


def not_sensitive(conn: psycopg.Connection, suggestion_id: UUID, *, decided_by: str,
                  reason: str | None = None) -> dict[str, Any]:
    """A person's "this is not sensitive" on a sensitive card (review of phase 3, point 3).

    A term added by a person is retired (``retired_at``); a core or extension term stays for everybody
    else - this subject gets an exception (``sensitive_exception``). The card leaves the queue.
    """
    from kasa.core.queue import state as queue_state
    from kasa.core.sensitive import find_sensitive, load_terms

    s = load_suggestion(conn, suggestion_id, lock=True)
    if s["kind"] != "category" or not (s["proposal"] or {}).get("needs_human"):
        raise DecisionError("„to nie jest wrażliwe” dotyczy karty ze słowem wrażliwym")
    text = ""
    if s["subject_type"] == "transaction":
        row = conn.execute("SELECT coalesce(title_raw, '') || ' ' || coalesce(counterparty_name_raw, '') "
                           "FROM l1_bank_transaction WHERE id = %s", (s["subject_id"],)).fetchone()
        text = row[0] if row else ""
    elif s["subject_type"] == "line":
        row = conn.execute("SELECT name FROM l1_document_line WHERE id = %s", (s["subject_id"],)).fetchone()
        text = row[0] if row else ""
    terms = load_terms(conn, s["company_id"])
    found = set(find_sensitive(text, terms))
    retired, kept = [], []
    for term in terms:
        if term.term not in found:
            continue
        if term.source == "human":
            conn.execute("UPDATE sensitive_term SET retired_at = now() WHERE stem = %s AND source = 'human' "
                         "AND retired_at IS NULL AND (company_id = %s OR company_id IS NULL)",
                         (term.stem, s["company_id"]))
            retired.append(term.term)
        else:
            kept.append(term.term)
    conn.execute(
        """
        INSERT INTO sensitive_exception (company_id, subject_type, subject_id, terms, reason, decided_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (s["company_id"], s["subject_type"], s["subject_id"], kept, reason, decided_by),
    )
    conn.execute(
        "UPDATE l2_suggestion SET status = 'rejected', resolved_at = now(), evidence = evidence || %s WHERE id = %s",
        (Jsonb({"not_sensitive_by": decided_by, "retired_terms": retired, "exception_terms": kept}), suggestion_id),
    )
    queue_state.mark_decided(conn, s["company_id"], s["subject_type"], s["subject_id"], ("sensitive",),
                             decision_id=None, decided_by=decided_by, outcome="not_sensitive")
    return {"retired_terms": retired, "exception_terms": kept}
