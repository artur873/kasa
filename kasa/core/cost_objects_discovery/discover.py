"""Discovery run: records -> pair model -> groups -> cost object candidates with their card.

``discover(conn, company)`` is idempotent: a second run on the same data writes nothing. An
existing cost object (candidate or approved) keeps its records and may gain new members; two
existing objects are never merged, a group is never glued to a group a person marked
"different", and a candidate a person rejected is not proposed again. A candidate is written
with ``method = 'code'`` and status ``candidate`` - only a person approves, merges or splits it
(``decide.py``).

The card is an ``l2_suggestion`` of kind ``cost_object_candidate`` (format of the phase 4
contract): name, attributes from the extension, anchor, members with link strength, the weakest
link, revenue and cost of the members (memorial, PLN minor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.cost_objects import create_candidate
from kasa.core.cost_objects_discovery.grouping import (
    Group,
    attach_members,
    cluster_anchors,
    default_anchor_eligible,
    link,
    primary_anchor,
    qualifies,
)
from kasa.core.cost_objects_discovery.pairs import PairScorer, load_model
from kasa.core.cost_objects_discovery.records import (
    SUBJECT_TYPE,
    DiscoveryRecord,
    load_records,
)
from kasa.core.extension import CandidateRecord, Extension, get_extension

SOURCE = "cost_objects_discovery"
LIVE_MEMBER = ("open", "accepted")


@dataclass
class DiscoveryReport:
    records: int = 0
    anchors: int = 0
    groups: int = 0
    created: list[UUID] = field(default_factory=list)
    updated: list[UUID] = field(default_factory=list)
    unchanged: int = 0
    members_added: int = 0
    not_qualified: int = 0
    skipped_collective: list[dict[str, Any]] = field(default_factory=list)
    skipped_rejected: int = 0
    model_version: int = 0

    def summary(self) -> str:
        return (f"Rekordy: {self.records}, kotwice: {self.anchors}, grupy: {self.groups}; kandydaci nowi: "
                f"{len(self.created)}, zmienieni: {len(self.updated)}, bez zmian: {self.unchanged}; "
                f"nowi członkowie: {self.members_added}; grupy bez drugiego typu rekordu: {self.not_qualified}; "
                f"pozycje zbiorcze pominięte: {len(self.skipped_collective)}; model par v{self.model_version}")


def candidate_record(record: DiscoveryRecord, *, group: int = 0, is_anchor: bool = False,
                     anchor_eligible: bool = False, link_strength: float | None = None) -> CandidateRecord:
    """The record as the extension sees it (no names of persons, texts are pseudonymized)."""
    attrs = {
        "group": group, "is_anchor": is_anchor, "anchor_eligible": anchor_eligible,
        "display_name": record.display_name, "client_name": record.client_name,
        "client_hint": record.facts.get("client_hint"), "window": record.window, "date_source": record.date_source,
        "city": record.city, "city_source": record.city_source, "direction": record.direction,
        "document_kind": record.document_kind, "mapping_key": record.facts.get("mapping_key"),
        "cost_object_ref": record.facts.get("cost_object_ref"), "month": record.facts.get("month"),
        "link_strength": link_strength, "entities": record.entities,
    }
    return CandidateRecord(record_type=SUBJECT_TYPE[record.record_type], record_id=record.record_id,
                           company_id=record.company_id, record_date=record.record_date,
                           amount_minor=record.amount_minor, text=record.text, attrs=attrs)


def evidence_row_ids(conn: psycopg.Connection, company_id: UUID) -> set[UUID]:
    """Rows of internal files that are evidence of a document (one cost, many pieces of evidence)."""
    from kasa.core.allocation.amounts import load_documents, load_internal_records
    from kasa.core.allocation.subjects import evidence_of_documents, load_matches

    return set(evidence_of_documents(load_internal_records(conn, company_id), load_documents(conn, company_id),
                                     load_matches(conn, company_id)))


def is_anchor_eligible(extension: Extension, record: DiscoveryRecord) -> bool:
    verdict = extension.is_anchor_eligible(candidate_record(record))
    return default_anchor_eligible(record) if verdict is None else bool(verdict)


def _existing(conn: psycopg.Connection, company_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT co.id, co.status::text, co.method::text, co.name, co.attrs,
               coalesce(json_agg(json_build_object('id', m.id, 'record_type', m.record_type, 'record_id', m.record_id,
                                                   'link_strength', m.link_strength, 'is_anchor', m.is_anchor,
                                                   'status', m.status, 'method', m.method))
                        FILTER (WHERE m.id IS NOT NULL), '[]')
          FROM l3_cost_object co
          LEFT JOIN l2_cost_object_member m ON m.candidate_id = co.id
         WHERE co.company_id = %s AND co.system_code IS NULL
         GROUP BY co.id
        """,
        (company_id,),
    ).fetchall()
    return [{"id": r[0], "status": r[1], "method": r[2], "name": r[3], "attrs": r[4], "members": r[5]} for r in rows]


def pair_verdicts(conn: psycopg.Connection, company_id: UUID) -> dict[tuple[UUID, UUID], str]:
    """The latest verdict of a person about each pair of cost objects."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (a_id, b_id) a_id, b_id, verdict::text FROM cost_object_pair_decision
         WHERE company_id = %s ORDER BY a_id, b_id, created_at DESC, id DESC
        """,
        (company_id,),
    ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def _member_key(member: dict[str, Any]) -> tuple[str, UUID]:
    rid = member["record_id"]
    return member["record_type"], rid if isinstance(rid, UUID) else UUID(str(rid))


def build_proposal(extension: Extension, candidate_attrs: dict[str, Any], name: str, anchor: DiscoveryRecord,
                   members: list[tuple[DiscoveryRecord, float]], evidence_rows: set[UUID] | None = None,
                   ) -> dict[str, Any]:
    """The card. A row of an internal file that is evidence of a document is not counted twice."""
    evidence_rows = evidence_rows or set()
    revenue = sum(r.amount_minor or 0 for r in (anchor, *[m for m, _ in members])
                  if r.record_type in ("document_line", "document") and r.direction == "sales")
    cost = sum(r.amount_minor or 0 for r in (anchor, *[m for m, _ in members])
               if (r.record_type in ("document_line", "document") and r.direction == "purchase")
               or (r.record_type == "internal_record" and r.record_id not in evidence_rows))
    ordered = sorted(members, key=lambda x: (-round(x[1], 4), x[0].record_type, str(x[0].record_id)))
    return {
        "name": name,
        "attrs": candidate_attrs,
        "anchor": {"record_type": anchor.record_type, "record_id": str(anchor.record_id)},
        "members": [{"record_type": r.record_type, "record_id": str(r.record_id), "link_strength": round(s, 4)}
                    for r, s in ordered],
        "weakest_link": round(min((s for _, s in members), default=1.0), 4),
        "revenue_minor": int(revenue),
        "cost_minor": int(cost),
    }


def _facts(extension: Extension, anchor: DiscoveryRecord, members: list[tuple[DiscoveryRecord, float]],
           attrs: dict[str, Any]) -> list[str]:
    kinds: dict[str, int] = {}
    for r, _ in members:
        kinds[r.record_type] = kinds.get(r.record_type, 0) + 1
    labels = {"document_line": "pozycji", "internal_record": "wierszy plików wewnętrznych", "transaction": "przelewów",
              "document": "dokumentów", "email_case": "spraw z maili"}
    anchor_label = {"document_line": "pozycja dokumentu", "internal_record": "wiersz pliku wewnętrznego",
                    "email_case": "sprawa z maila"}.get(anchor.record_type, anchor.record_type)
    facts = [f"kotwica: {anchor_label} „{anchor.display_name or anchor.text}”"
             + (f" ({anchor.facts['number']})" if anchor.facts.get("number") else "")]
    if kinds:
        facts.append("powiązane: " + ", ".join(f"{n} {labels.get(k, k)}" for k, n in sorted(kinds.items())))
    weakest = min(members, key=lambda x: x[1]) if members else None
    if weakest:
        strength = f"{weakest[1]:.2f}".replace(".", ",")
        facts.append(f"najsłabsze powiązanie {strength}: „{(weakest[0].display_name or weakest[0].text)[:60]}”")
    window = " - ".join(str(attrs[k]) for k in ("date_start", "date_end") if attrs.get(k))
    if window:
        facts.append(f"okno dat {window}" + (f", miasto {attrs['city']}" if attrs.get("city") else ""))
    return facts


def _upsert_suggestion(conn: psycopg.Connection, company_id: UUID, cost_object_id: UUID, proposal: dict[str, Any],
                       score: float, evidence: dict[str, Any], created_by: str, source: str = SOURCE) -> bool:
    """One open card per candidate; a changed proposal expires the old card. Returns True when written."""
    current = conn.execute(
        """
        SELECT id, proposal FROM l2_suggestion
         WHERE company_id = %s AND kind = 'cost_object_candidate' AND subject_type = 'cost_object'
           AND subject_id = %s AND status = 'open'
        """,
        (company_id, cost_object_id),
    ).fetchall()
    if len(current) == 1 and current[0][1] == proposal:
        return False
    if current:
        conn.execute(
            "UPDATE l2_suggestion SET status = 'expired', resolved_at = now() WHERE id = ANY(%s)",
            ([r[0] for r in current],),
        )
    conn.execute(
        """
        INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, score, rank,
                                   source, method, confidence, evidence, created_by)
        VALUES (%s, 'cost_object_candidate', 'cost_object', %s, %s, %s, 1, %s, 'code', %s, %s, %s)
        """,
        (company_id, cost_object_id, Jsonb(proposal), round(score, 4), source, round(score, 4), Jsonb(evidence),
         created_by),
    )
    return True


def insert_member(conn: psycopg.Connection, company_id: UUID, candidate_id: UUID, record: DiscoveryRecord,
                  strength: float, *, is_anchor: bool, method: str = "code", source: str = SOURCE,
                  created_by: str = "kasa", status: str = "open", evidence: dict | None = None) -> bool:
    """Insert a member (or refresh the strength of an open member written by code). True when changed."""
    row = conn.execute(
        """
        INSERT INTO l2_cost_object_member (company_id, candidate_id, record_type, record_id, link_strength,
                                           is_anchor, status, resolved_at, source, method, confidence, evidence,
                                           created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, CASE WHEN %s = 'open' THEN NULL ELSE now() END, %s, %s, %s, %s, %s)
        ON CONFLICT (candidate_id, record_type, record_id) DO UPDATE
           SET link_strength = EXCLUDED.link_strength, confidence = EXCLUDED.confidence
         WHERE l2_cost_object_member.status = 'open' AND l2_cost_object_member.method = 'code'
           AND l2_cost_object_member.link_strength IS DISTINCT FROM EXCLUDED.link_strength
        RETURNING id
        """,
        (company_id, candidate_id, record.record_type, record.record_id, round(strength, 4), is_anchor, status,
         status, source, method, round(strength, 4), Jsonb(evidence or {}), created_by),
    ).fetchone()
    return row is not None


def discover(conn: psycopg.Connection, company: Any, *, extension: Extension | None = None,
             created_by: str = "kasa") -> DiscoveryReport:
    """Group the company's records into cost object candidates (writes candidates, members, cards)."""
    extension = extension or get_extension(company.extension_slug)
    report = DiscoveryReport()
    records = load_records(conn, company.id)
    by_key = {r.key: r for r in records}
    model = load_model(conn, company.id)
    report.model_version = model.version
    scorer = PairScorer(model, records)
    report.records = len(records)
    evidence_rows = evidence_row_ids(conn, company.id)

    existing = _existing(conn, company.id)
    verdicts = pair_verdicts(conn, company.id)
    owner: dict[tuple[str, UUID], set[UUID]] = {}
    tombstones: set[tuple[str, UUID]] = set()
    groups: list[Group] = []
    for obj in existing:
        live = [m for m in obj["members"] if m["status"] in LIVE_MEMBER]
        for m in obj["members"]:
            if m["status"] != "expired":
                owner.setdefault(_member_key(m), set()).add(obj["id"])
        if obj["status"] == "rejected":
            tombstones |= {_member_key(m) for m in obj["members"] if m["status"] == "rejected"}
            continue
        if obj["status"] not in ("candidate", "approved"):
            continue
        group = Group(existing_id=obj["id"])
        for m in live:
            record = by_key.get(_member_key(m))
            if record is None:
                continue
            if m["is_anchor"] or is_anchor_eligible(extension, record):
                group.anchors.append(record)
            else:
                group.members[record.key] = (record, float(m["link_strength"]))
            group.fixed.add(record.key)
        if group.anchors:
            groups.append(group)

    def objects_of(group: Group) -> set[UUID]:
        out = {group.existing_id} if group.existing_id else set()
        for key in group.keys:
            out |= owner.get(key, set())
        return out

    def cannot_link(a: Group, b: Group) -> bool:
        for x in objects_of(a):
            for y in objects_of(b):
                if x != y and verdicts.get((min(x, y), max(x, y))) == "different":
                    return True
        return False

    # records of a candidate a person rejected (or left out of a split) never anchor a new one
    tombstones -= {k for g in groups for k in g.keys}
    eligible = [r for r in records if is_anchor_eligible(extension, r)]
    anchors = [r for r in eligible if r.key not in tombstones]
    report.anchors = len(anchors)
    report.skipped_rejected = len(eligible) - len(anchors)
    groups = cluster_anchors(anchors, groups, scorer, cannot_link)
    report.skipped_collective = attach_members(records, groups, scorer)
    groups = [g for g in groups if g.anchors]
    report.groups = len(groups)

    existing_by_id = {obj["id"]: obj for obj in existing}
    for index, group in enumerate(groups):
        obj = existing_by_id.get(group.existing_id) if group.existing_id else None
        anchor_member = None
        if obj:
            anchor_member = next((by_key.get(_member_key(m)) for m in obj["members"]
                                  if m["is_anchor"] and m["status"] in LIVE_MEMBER), None)
        primary = anchor_member or primary_anchor(group)
        if obj is None:
            if not qualifies(group, scorer, primary):
                report.not_qualified += 1
                continue
        others = [(a, link(a, group, scorer)) for a in group.anchors if a.key != primary.key]
        others += list(group.members.values())
        cand_records = [candidate_record(primary, group=index, is_anchor=True, anchor_eligible=True, link_strength=1.0)]
        cand_records += [candidate_record(r, group=index, anchor_eligible=r in group.anchors, link_strength=s)
                         for r, s in others]
        derived = extension.derive_cost_object_candidates(cand_records)
        if not derived and obj is None:
            report.not_qualified += 1
            continue

        if obj is None:
            candidate = derived[0]
            clean = extension.validate_cost_object_attrs(candidate.attrs)
            cost_object_id = create_candidate(
                conn, extension, company_id=company.id, attrs=clean, source=SOURCE, created_by=created_by,
                name=candidate.name, confidence=round(min((s for _, s in others), default=1.0), 4),
                evidence={**candidate.evidence, "model_version": model.version},
            )
            obj = {"id": cost_object_id, "status": "candidate", "method": "code", "name": candidate.name,
                   "attrs": clean, "members": []}
            report.created.append(cost_object_id)
            changed = True
        else:
            cost_object_id = obj["id"]
            changed = False
            if derived and obj["status"] == "candidate" and obj["method"] == "code":
                clean = extension.validate_cost_object_attrs(derived[0].attrs)
                if clean != obj["attrs"] or derived[0].name != obj["name"]:
                    conn.execute(
                        "UPDATE l3_cost_object SET attrs = %s, name = %s, evidence = evidence || %s WHERE id = %s",
                        (Jsonb(clean), derived[0].name, Jsonb({**derived[0].evidence, "model_version": model.version}),
                         cost_object_id),
                    )
                    obj["attrs"], obj["name"] = clean, derived[0].name
                    changed = True

        known = {_member_key(m) for m in obj["members"]}
        member_status = "accepted" if obj["status"] == "approved" else "open"
        if primary.key not in known:
            insert_member(conn, company.id, cost_object_id, primary, 1.0, is_anchor=True, created_by=created_by,
                          status=member_status)
            changed = True
        for record, strength in others:
            if record.key in group.fixed and record.key in known:
                continue
            if record.key in known and obj["status"] == "approved":
                continue
            levels = scorer.explain(record, primary)[1]
            if insert_member(conn, company.id, cost_object_id, record, strength, is_anchor=False,
                             created_by=created_by, status="open",
                             evidence={"levels_vs_anchor": levels, "model_version": model.version}):
                if record.key not in known:
                    report.members_added += 1
                changed = True

        if obj["status"] == "candidate":
            proposal = build_proposal(extension, obj["attrs"], obj["name"], primary, others, evidence_rows)
            score = sum(s for _, s in others) / len(others) if others else 1.0
            evidence = {"facts": _facts(extension, primary, others, obj["attrs"]), "badge": "medium",
                        "precedents": 0, "hard": False, "margin": None,
                        "tags": [f"pair_model:v{model.version}", "cost_object_candidate"]}
            if _upsert_suggestion(conn, company.id, cost_object_id, proposal, score, evidence, created_by):
                changed = True
        if changed and cost_object_id not in report.created:
            report.updated.append(cost_object_id)
        elif not changed:
            report.unchanged += 1
    return report
