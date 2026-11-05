"""Rentease hooks: the cost object is a room in a month (room x month).

The same core grouping as for ODM events: an anchor is a row of the rent roll (room, tenant,
month, rent), members are the rent transfers and the costs of the room. The room comes from the
row (``cost_object_ref``) or from a description ("czynsz P3 listopad"), the month from the row
(column ``miesiąc``) or the dates of the records.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from kasa.core.extension import CandidateRecord, CostObjectCandidate

_ROOM_RE = re.compile(r"\b(?:pok(?:[oó]j|oju)?\.?\s*)?([A-Z]{1,2}\d{1,3})\b")


def is_anchor_eligible(extension, record: CandidateRecord) -> bool | None:
    """Only a row of the rent roll anchors a room-month."""
    if record.record_type == "internal_record":
        return record.attrs.get("mapping_key") == "rent_roll" and bool(record.attrs.get("cost_object_ref"))
    return False


def _room(record: CandidateRecord) -> str | None:
    ref = record.attrs.get("cost_object_ref")
    if ref:
        return str(ref).strip()
    match = _ROOM_RE.search(record.text or "")
    return match.group(1) if match else None


def _month(record: CandidateRecord) -> str | None:
    month = record.attrs.get("month")
    if month:
        return str(month)
    if record.record_date:
        return f"{record.record_date.year:04d}-{record.record_date.month:02d}"
    return None


def derive_cost_object_candidates(extension, records: Sequence[CandidateRecord]) -> list[CostObjectCandidate]:
    groups: dict[Any, list[CandidateRecord]] = {}
    for record in records:
        groups.setdefault(record.attrs.get("group", 0), []).append(record)
    out = []
    for group in groups.values():
        anchors = [r for r in group if r.attrs.get("is_anchor") or r.attrs.get("anchor_eligible")] or group
        rooms = Counter(x for x in (_room(r) for r in anchors) if x)
        months = Counter(x for x in (_month(r) for r in anchors) if x)
        if not rooms or not months:
            continue
        attrs = {"room": rooms.most_common(1)[0][0], "month": months.most_common(1)[0][0]}
        anchor = next((r for r in group if r.attrs.get("is_anchor")), anchors[0])
        out.append(CostObjectCandidate(
            kind=extension.cost_object_kind, name=extension.cost_object_name(attrs), attrs=attrs,
            anchor=(anchor.record_type, anchor.record_id),
            supporting=[(r.record_type, r.record_id) for r in group if r is not anchor],
            evidence={"rule": "pokój × miesiąc z listy czynszów"},
        ))
    return out
