"""ODM hooks: rules about events that YAML cannot express.

An event = client + a compact window of dates + city + name (briefing, step 5). The core groups
records (lines, rows of the PM sheet, transfers) with the pair model and calls
``derive_cost_object_candidates`` once per group; this hook says what the event is.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

from kasa.core.extension import CandidateRecord, CostObjectCandidate

# where the name of the event is read from, most trusted first
_NAME_SOURCES = ("sales_line", "purchase_line", "internal_record", "transaction")
# a window longer than this is not "a compact window of an event" (a month of rent, a framework order)
_MAX_EVENT_DAYS = 14


def validate_cost_object_attrs(attrs: dict[str, Any]) -> None:
    """An event ends on or after the day it starts."""
    if attrs["date_end"] < attrs["date_start"]:
        raise ValueError("Koniec eventu (date_end) nie może być wcześniej niż początek (date_start)")


def is_anchor_eligible(extension, record: CandidateRecord) -> bool | None:
    """Anchors of an event: a sales line with a proper name, a PM sheet row naming the event.

    The core default says the same; ODM keeps it explicit (an e-mail case with an offer joins
    when the mail archive is imported).
    """
    return None


def _source(record: CandidateRecord) -> str:
    if record.record_type == "line":
        return "sales_line" if record.attrs.get("direction") == "sales" else "purchase_line"
    return record.record_type


def _pick_name(records: Sequence[CandidateRecord]) -> tuple[str | None, list[str]]:
    names: list[tuple[str, str]] = [(_source(r), r.attrs["display_name"])
                                    for r in records if r.attrs.get("display_name")]
    aliases = list(dict.fromkeys(n for _, n in names))
    for source in _NAME_SOURCES:
        tier = [n for s, n in names if s == source]
        if tier:
            overall = Counter(n for _, n in names)
            tier_count = Counter(tier)
            best = max(tier_count, key=lambda n: (tier_count[n], overall[n], len(n), -aliases.index(n)))
            return best, aliases
    return None, aliases


def _pick_client(records: Sequence[CandidateRecord]) -> str | None:
    sales = Counter(r.attrs["client_name"] for r in records
                    if r.attrs.get("client_name") and _source(r) == "sales_line")
    if sales:
        return sales.most_common(1)[0][0]
    resolved = Counter(r.attrs["client_name"] for r in records if r.attrs.get("client_name"))
    if resolved:
        return resolved.most_common(1)[0][0]
    hints = Counter(r.attrs["client_hint"] for r in records if r.attrs.get("client_hint"))
    return hints.most_common(1)[0][0] if hints else None


def _pick_window(records: Sequence[CandidateRecord]) -> tuple[date, date] | None:
    def windows(pool):
        out = []
        for r in pool:
            w = r.attrs.get("window")
            if w and r.attrs.get("date_source") in ("text", "record") and (w[1] - w[0]).days < _MAX_EVENT_DAYS:
                out.append(w)
        return out

    anchors = [r for r in records if r.attrs.get("is_anchor") or r.attrs.get("anchor_eligible")]
    lines = [r for r in records if r.record_type in ("line", "internal_record")]
    for pool in (anchors, lines):
        found = windows(pool)
        if found:
            start, end = min(w[0] for w in found), max(w[1] for w in found)
            if (end - start).days < _MAX_EVENT_DAYS:
                return start, end
            # not compact: keep the dates most records agree on (the median day +- the shortest window)
            days = sorted(w[0] for w in found)
            mid = days[len(days) // 2]
            near = [w for w in found if abs((w[0] - mid).days) < _MAX_EVENT_DAYS]
            return min(w[0] for w in near), max(w[1] for w in near)
    return None


def _pick_city(records: Sequence[CandidateRecord]) -> str | None:
    cities = Counter(r.attrs["city"] for r in records
                     if r.attrs.get("city") and r.attrs.get("city_source") == "text" and r.record_type != "transaction")
    return cities.most_common(1)[0][0] if cities else None


def derive_cost_object_candidates(extension, records: Sequence[CandidateRecord]) -> list[CostObjectCandidate]:
    """One event per group of records (``attrs['group']``); a group without a name or dates is no event."""
    groups: dict[Any, list[CandidateRecord]] = {}
    for record in records:
        groups.setdefault(record.attrs.get("group", 0), []).append(record)
    out = []
    for group in groups.values():
        name, aliases = _pick_name(group)
        window = _pick_window(group)
        client = _pick_client(group)
        if not name or not window or not client:
            continue
        attrs = {"name": name, "client": client, "date_start": window[0], "date_end": window[1]}
        city = _pick_city(group)
        if city:
            attrs["city"] = city
        anchor = next((r for r in group if r.attrs.get("is_anchor")), group[0])
        out.append(CostObjectCandidate(
            kind=extension.cost_object_kind, name=name, attrs=attrs,
            anchor=(anchor.record_type, anchor.record_id),
            supporting=[(r.record_type, r.record_id) for r in group if r is not anchor],
            evidence={"aliases": aliases, "rule": "event = klient + zwarte okno dat + miasto + nazwa"},
        ))
    return out
