"""A person's decisions about cost object candidates (the buttons of the card).

approve / split / merge / reject / "definitely different" - always ``method = 'human'``: a cost
object never comes to life, merges or splits by automation (briefing, step 5; the database refuses
it too, ``l3_cost_object_method_check``). Every decision is a row of ``l3_decision_log`` with
``kind = 'cost_object'`` and ``payload.action`` naming the button, and is evidence for the pair
model: after each decision ``retrain`` writes a new version of ``pair_model_weights`` (approve and
merge = "same", split and "definitely different" = "different"; a rejection says "no cost object",
not "different records" - it blocks the records from anchoring a new candidate instead).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.cost_objects_discovery.discover import (
    SOURCE,
    _upsert_suggestion,
    build_proposal,
    candidate_record,
    evidence_row_ids,
    insert_member,
    is_anchor_eligible,
)
from kasa.core.cost_objects_discovery.pairs import retrain
from kasa.core.cost_objects_discovery.records import DiscoveryRecord, load_records
from kasa.core.decisions import log_decision
from kasa.core.extension import Extension, get_extension


class CostObjectDecisionError(ValueError):
    """The decision is not possible in the current state of the cost object."""


@dataclass
class SplitResult:
    decision_id: UUID
    new_ids: list[UUID] = field(default_factory=list)


def _require_human(method: str) -> None:
    if method != "human":
        raise PermissionError("Obiekt kosztowy zatwierdza, łączy, dzieli i odrzuca tylko człowiek (method='human')")


def _object(conn: psycopg.Connection, cost_object_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT co.id, co.company_id, co.status::text, co.kind, co.name, co.attrs, co.evidence, co.system_code,
               c.extension_slug
          FROM l3_cost_object co JOIN company c ON c.id = co.company_id
         WHERE co.id = %s FOR UPDATE OF co
        """,
        (cost_object_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma obiektu kosztowego {cost_object_id}")
    keys = ("id", "company_id", "status", "kind", "name", "attrs", "evidence", "system_code", "extension_slug")
    obj = dict(zip(keys, row, strict=True))
    if obj["system_code"]:
        raise CostObjectDecisionError(f"Obiekt systemowy {obj['system_code']} nie podlega tej decyzji")
    return obj


def _members(conn: psycopg.Connection, cost_object_id: UUID, statuses=("open", "accepted")) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, record_type::text, record_id, link_strength, is_anchor, status::text, method::text
          FROM l2_cost_object_member WHERE candidate_id = %s AND status = ANY(%s)
         ORDER BY is_anchor DESC, link_strength DESC, record_type, record_id
        """,
        (cost_object_id, list(statuses)),
    ).fetchall()
    keys = ("id", "record_type", "record_id", "link_strength", "is_anchor", "status", "method")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def _ref(member: dict[str, Any]) -> dict[str, str]:
    return {"record_type": member["record_type"], "record_id": str(member["record_id"])}


def _side(members: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Anchor first, then the other members (the shape the pair model learns from)."""
    ordered = sorted(members, key=lambda m: (not m["is_anchor"], -float(m["link_strength"])))
    return [_ref(m) for m in ordered]


def _resolve_member_rows(conn: psycopg.Connection, cost_object_id: UUID, decision_id: UUID, status: str) -> None:
    conn.execute(
        """
        UPDATE l2_cost_object_member SET status = %s, resolved_at = now(), resolved_by_decision = %s
         WHERE candidate_id = %s AND status = 'open'
        """,
        (status, decision_id, cost_object_id),
    )


def _resolve_cards(conn: psycopg.Connection, cost_object_id: UUID, decision_id: UUID, status: str) -> None:
    conn.execute(
        """
        UPDATE l2_suggestion SET status = %s, resolved_at = now(), resolved_by_decision = %s
         WHERE kind = 'cost_object_candidate' AND subject_type = 'cost_object' AND subject_id = %s AND status = 'open'
        """,
        (status, decision_id, cost_object_id),
    )


def _open_card(conn: psycopg.Connection, cost_object_id: UUID) -> UUID | None:
    row = conn.execute(
        """
        SELECT id FROM l2_suggestion WHERE kind = 'cost_object_candidate' AND subject_type = 'cost_object'
           AND subject_id = %s AND status = 'open' ORDER BY created_at DESC LIMIT 1
        """,
        (cost_object_id,),
    ).fetchone()
    return row[0] if row else None


def _aliases(conn: psycopg.Connection, company_id: UUID, members: list[dict[str, Any]],
             records: list[DiscoveryRecord] | None = None) -> list[str]:
    records = records if records is not None else load_records(conn, company_id)
    by_key = {(r.record_type, r.record_id): r for r in records}
    names = []
    for m in members:
        record = by_key.get((m["record_type"], m["record_id"]))
        if record and record.display_name:
            names.append(record.display_name)
        if record and record.facts.get("cost_object_ref"):
            names.append(record.facts["cost_object_ref"])
    return list(dict.fromkeys(names))


def _pair(conn: psycopg.Connection, company_id: UUID, a: UUID, b: UUID, verdict: str, decision_id: UUID,
          decided_by: str) -> None:
    low, high = (a, b) if a < b else (b, a)
    conn.execute(
        """
        INSERT INTO cost_object_pair_decision (company_id, a_id, b_id, verdict, decision_id, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (company_id, low, high, verdict, decision_id, decided_by),
    )


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def approve_candidate(conn: psycopg.Connection, cost_object_id: UUID, *, decided_by: str,
                      attrs_override: dict[str, Any] | None = None, method: str = "human",
                      reason: str | None = None, source: str = "queue", retrain_model: bool = True) -> UUID:
    """A person approves a candidate (optionally correcting its attributes). Returns the decision id."""
    _require_human(method)
    obj = _object(conn, cost_object_id)
    if obj["status"] != "candidate":
        raise CostObjectDecisionError(
            f"Obiekt {obj['name']!r} ma status {obj['status']} - zatwierdzić można tylko kandydata")
    extension = get_extension(obj["extension_slug"])
    attrs = extension.validate_cost_object_attrs({**obj["attrs"], **(attrs_override or {})})
    name = extension.cost_object_name(attrs)
    members = _members(conn, cost_object_id)
    anchor = next((m for m in members if m["is_anchor"]), members[0] if members else None)
    suggestion_id = _open_card(conn, cost_object_id)
    decision_id = log_decision(
        conn, company_id=obj["company_id"], kind="cost_object", subject_type="cost_object",
        subject_id=cost_object_id, decided_by=decided_by, source=source, method="human",
        payload={"action": "approve", "name": name, "attrs": attrs,
                 "anchor": _ref(anchor) if anchor else None,
                 "members": [_ref(m) for m in members if m is not anchor],
                 "attrs_override": attrs_override or {}},
        suggestion_id=suggestion_id, reason=reason,
    )
    aliases = _aliases(conn, obj["company_id"], members)
    conn.execute(
        """
        UPDATE l3_cost_object
           SET status = 'approved', method = 'human', decision_id = %s, attrs = %s, name = %s,
               evidence = evidence || %s, updated_by = %s
         WHERE id = %s
        """,
        (decision_id, Jsonb(attrs), name, Jsonb({"aliases": aliases, "approved_by": decided_by}), decided_by,
         cost_object_id),
    )
    _resolve_member_rows(conn, cost_object_id, decision_id, "accepted")
    _resolve_cards(conn, cost_object_id, decision_id, "accepted")
    if retrain_model:
        retrain(conn, obj["company_id"], created_by=decided_by)
    return decision_id


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


def reject_candidate(conn: psycopg.Connection, cost_object_id: UUID, *, decided_by: str, reason: str | None = None,
                     method: str = "human", source: str = "queue") -> UUID:
    """A person says the candidate is no cost object. It is not proposed again by discovery."""
    _require_human(method)
    obj = _object(conn, cost_object_id)
    if obj["status"] != "candidate":
        raise CostObjectDecisionError(f"Odrzucić można tylko kandydata (obiekt ma status {obj['status']})")
    members = _members(conn, cost_object_id)
    anchor = next((m for m in members if m["is_anchor"]), None)
    decision_id = log_decision(
        conn, company_id=obj["company_id"], kind="cost_object", subject_type="cost_object",
        subject_id=cost_object_id, decided_by=decided_by, source=source, method="human",
        payload={"action": "reject", "anchor": _ref(anchor) if anchor else None,
                 "members": [_ref(m) for m in members if m is not anchor]},
        suggestion_id=_open_card(conn, cost_object_id), reason=reason,
    )
    conn.execute(
        "UPDATE l3_cost_object SET status = 'rejected', method = 'human', decision_id = %s, updated_by = %s "
        "WHERE id = %s",
        (decision_id, decided_by, cost_object_id),
    )
    _resolve_member_rows(conn, cost_object_id, decision_id, "rejected")
    _resolve_cards(conn, cost_object_id, decision_id, "rejected")
    retrain(conn, obj["company_id"], created_by=decided_by)
    return decision_id


# ---------------------------------------------------------------------------
# split
# ---------------------------------------------------------------------------


def _derive_for(extension: Extension, records: list[DiscoveryRecord], anchor_key, fallback: dict[str, Any]):
    cands = []
    for record in records:
        is_anchor = record.key == anchor_key
        cands.append(candidate_record(record, is_anchor=is_anchor,
                                      anchor_eligible=is_anchor or is_anchor_eligible(extension, record)))
    derived = extension.derive_cost_object_candidates(cands)
    if derived:
        attrs = extension.validate_cost_object_attrs(derived[0].attrs)
        return attrs, derived[0].name
    attrs = extension.validate_cost_object_attrs(fallback)
    return attrs, extension.cost_object_name(attrs)


def split_candidate(conn: psycopg.Connection, cost_object_id: UUID, groups: list[list[UUID]], *, decided_by: str,
                    reason: str | None = None, method: str = "human", source: str = "queue") -> SplitResult:
    """A person splits a candidate: every group of member ids becomes a new candidate.

    The old candidate is rejected (its card too); members left out of every group are dropped.
    The new candidates are "definitely different" from each other (pair verdicts) and the pair
    model learns: same within a group, different across groups.
    """
    _require_human(method)
    obj = _object(conn, cost_object_id)
    if obj["status"] != "candidate":
        raise CostObjectDecisionError(f"Podzielić można tylko kandydata (obiekt ma status {obj['status']})")
    members = {m["id"]: m for m in _members(conn, cost_object_id)}
    clean_groups = [[UUID(str(i)) for i in g] for g in groups if g]
    flat = [i for g in clean_groups for i in g]
    if len(clean_groups) < 2:
        raise CostObjectDecisionError("Podział wymaga co najmniej dwóch niepustych grup")
    if len(flat) != len(set(flat)):
        raise CostObjectDecisionError("Ten sam członek nie może być w dwóch grupach")
    unknown = [i for i in flat if i not in members]
    if unknown:
        raise CostObjectDecisionError(f"Członkowie spoza kandydata: {unknown}")

    extension = get_extension(obj["extension_slug"])
    records = load_records(conn, obj["company_id"])
    by_key = {r.key: r for r in records}
    group_members = [[members[i] for i in g] for g in clean_groups]
    for g in group_members:
        g.sort(key=lambda m: (not m["is_anchor"], -float(m["link_strength"])))
    decision_id = log_decision(
        conn, company_id=obj["company_id"], kind="cost_object", subject_type="cost_object",
        subject_id=cost_object_id, decided_by=decided_by, source=source, method="human",
        payload={"action": "split", "groups": [[_ref(m) for m in g] for g in group_members]},
        suggestion_id=_open_card(conn, cost_object_id), reason=reason,
    )
    result = SplitResult(decision_id)
    for g in group_members:
        recs = [by_key[(m["record_type"], m["record_id"])] for m in g if (m["record_type"], m["record_id"]) in by_key]
        anchor_member = next((m for m in g if m["is_anchor"]), None)
        anchor = None
        if anchor_member:
            anchor = by_key.get((anchor_member["record_type"], anchor_member["record_id"]))
        if anchor is None:
            anchor = next((r for r in recs if is_anchor_eligible(extension, r)), recs[0] if recs else None)
        if anchor is None:
            raise CostObjectDecisionError("Grupa nie ma rekordu, który istnieje w bazie")
        attrs, name = _derive_for(extension, recs, anchor.key, obj["attrs"])
        new_id = conn.execute(
            """
            INSERT INTO l3_cost_object (company_id, kind, name, attrs, status, decision_id, source, method,
                                        evidence, created_by)
            VALUES (%s, %s, %s, %s, 'candidate', %s, %s, 'human', %s, %s) RETURNING id
            """,
            (obj["company_id"], obj["kind"], name, Jsonb(attrs), decision_id, source,
             Jsonb({"split_from": str(cost_object_id)}), decided_by),
        ).fetchone()[0]
        result.new_ids.append(new_id)
        member_links = []
        for m in g:
            record = by_key.get((m["record_type"], m["record_id"]))
            if record is None:
                continue
            strength = 1.0 if record.key == anchor.key else float(m["link_strength"])
            insert_member(conn, obj["company_id"], new_id, record, strength, is_anchor=record.key == anchor.key,
                          method="human", source="split", created_by=decided_by,
                          evidence={"split_from": str(cost_object_id), "decision_id": str(decision_id)})
            if record.key != anchor.key:
                member_links.append((record, strength))
        proposal = build_proposal(extension, attrs, name, anchor, member_links,
                                  evidence_row_ids(conn, obj["company_id"]))
        _upsert_suggestion(conn, obj["company_id"], new_id, proposal,
                           sum(s for _, s in member_links) / len(member_links) if member_links else 1.0,
                           {"facts": [f"powstał z podziału kandydata {obj['name']!r}"], "badge": "medium",
                            "precedents": 0, "hard": False, "margin": None, "tags": ["split"]},
                           decided_by, source="split")
    conn.execute(
        "UPDATE l3_cost_object SET status = 'rejected', method = 'human', decision_id = %s, updated_by = %s, "
        "evidence = evidence || %s WHERE id = %s",
        (decision_id, decided_by, Jsonb({"split_into": [str(i) for i in result.new_ids]}), cost_object_id),
    )
    _resolve_member_rows(conn, cost_object_id, decision_id, "rejected")
    _resolve_cards(conn, cost_object_id, decision_id, "rejected")
    for a, b in combinations(result.new_ids, 2):
        _pair(conn, obj["company_id"], a, b, "different", decision_id, decided_by)
    retrain(conn, obj["company_id"], created_by=decided_by, records=records)
    return result


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def merge_candidates(conn: psycopg.Connection, ids: list[UUID], *, decided_by: str, into: UUID | None = None,
                     reason: str | None = None, method: str = "human", source: str = "queue") -> UUID:
    """A person merges cost objects into one (``into``, else the approved one, else the first).

    The others get status ``merged`` (``merged_into``), their members move to the target. A
    person may merge objects marked "different" before - the newer verdict wins.
    """
    _require_human(method)
    ids = list(dict.fromkeys(UUID(str(i)) for i in ids))
    if len(ids) < 2:
        raise CostObjectDecisionError("Połączyć można co najmniej dwa obiekty")
    objects = [_object(conn, i) for i in ids]
    if len({o["company_id"] for o in objects}) != 1 or len({o["kind"] for o in objects}) != 1:
        raise CostObjectDecisionError("Łączyć można tylko obiekty jednej firmy i jednego rodzaju")
    for o in objects:
        if o["status"] not in ("candidate", "approved"):
            raise CostObjectDecisionError(f"Obiekt {o['name']!r} ma status {o['status']} - nie da się go połączyć")
    approved = [o for o in objects if o["status"] == "approved"]
    target = next((o for o in objects if o["id"] == into), None) if into else None
    target = target or (approved[0] if approved else objects[0])
    company_id = target["company_id"]
    sides = {o["id"]: _members(conn, o["id"]) for o in objects}
    decision_id = log_decision(
        conn, company_id=company_id, kind="cost_object", subject_type="cost_object", subject_id=target["id"],
        decided_by=decided_by, source=source, method="human",
        payload={"action": "merge", "into": str(target["id"]), "merged": [str(o["id"]) for o in objects],
                 "sides": [_side(sides[o["id"]]) for o in objects]},
        reason=reason,
    )
    records = load_records(conn, company_id)
    by_key = {r.key: r for r in records}
    member_status = "accepted" if target["status"] == "approved" else "open"
    for o in objects:
        if o["id"] == target["id"]:
            continue
        for m in sides[o["id"]]:
            record = by_key.get((m["record_type"], m["record_id"]))
            if record is not None:
                conn.execute(
                    """
                    INSERT INTO l2_cost_object_member (company_id, candidate_id, record_type, record_id,
                                                       link_strength, is_anchor, status, resolved_at,
                                                       resolved_by_decision, source, method, evidence, created_by)
                    VALUES (%s, %s, %s, %s, %s, false, %s, CASE WHEN %s = 'open' THEN NULL ELSE now() END,
                            CASE WHEN %s = 'open' THEN NULL ELSE %s::uuid END, 'merge', 'human', %s, %s)
                    ON CONFLICT (candidate_id, record_type, record_id) DO NOTHING
                    """,
                    (company_id, target["id"], m["record_type"], m["record_id"], m["link_strength"], member_status,
                     member_status, member_status, decision_id, Jsonb({"merged_from": str(o["id"])}), decided_by),
                )
        conn.execute(
            """
            UPDATE l2_cost_object_member SET status = 'expired', resolved_at = now(), resolved_by_decision = %s
             WHERE candidate_id = %s AND status IN ('open', 'accepted')
            """,
            (decision_id, o["id"]),
        )
        _resolve_cards(conn, o["id"], decision_id, "rejected")
        conn.execute(
            "UPDATE l3_cost_object SET status = 'merged', merged_into = %s, method = 'human', decision_id = %s, "
            "updated_by = %s WHERE id = %s",
            (target["id"], decision_id, decided_by, o["id"]),
        )
        _pair(conn, company_id, target["id"], o["id"], "same", decision_id, decided_by)
    if target["status"] == "candidate":
        extension = get_extension(target["extension_slug"])
        members = _members(conn, target["id"])
        anchor_m = next((m for m in members if m["is_anchor"]), None)
        anchor = by_key.get((anchor_m["record_type"], anchor_m["record_id"])) if anchor_m else None
        if anchor is not None:
            links = [(by_key[(m["record_type"], m["record_id"])], float(m["link_strength"])) for m in members
                     if m is not anchor_m and (m["record_type"], m["record_id"]) in by_key]
            proposal = build_proposal(extension, target["attrs"], target["name"], anchor, links,
                                      evidence_row_ids(conn, company_id))
            _upsert_suggestion(conn, company_id, target["id"], proposal,
                               sum(s for _, s in links) / len(links) if links else 1.0,
                               {"facts": [f"połączony z {len(objects) - 1} innymi kandydatami"], "badge": "medium",
                                "precedents": 0, "hard": False, "margin": None, "tags": ["merge"]},
                               decided_by, source=SOURCE)
    retrain(conn, company_id, created_by=decided_by, records=records)
    return decision_id


# ---------------------------------------------------------------------------
# definitely different
# ---------------------------------------------------------------------------


def mark_distinct(conn: psycopg.Connection, a: UUID, b: UUID, *, decided_by: str, reason: str | None = None,
                  method: str = "human", source: str = "queue") -> UUID:
    """A person says two cost objects are different: discovery never glues them (nor their records)."""
    _require_human(method)
    if a == b:
        raise CostObjectDecisionError("Obiekt nie może być różny od samego siebie")
    first, second = _object(conn, a), _object(conn, b)
    if first["company_id"] != second["company_id"]:
        raise CostObjectDecisionError("Obiekty należą do różnych firm")
    decision_id = log_decision(
        conn, company_id=first["company_id"], kind="cost_object", subject_type="cost_object", subject_id=a,
        decided_by=decided_by, source=source, method="human",
        payload={"action": "distinct", "other": str(b),
                 "sides": [_side(_members(conn, a)), _side(_members(conn, b))]},
        reason=reason,
    )
    _pair(conn, first["company_id"], a, b, "different", decision_id, decided_by)
    retrain(conn, first["company_id"], created_by=decided_by)
    return decision_id
