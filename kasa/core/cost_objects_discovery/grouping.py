"""Grouping records into cost object candidates (briefing, step 5).

1. Anchors: records that may start a candidate - by default a sales line with a proper name
   (not an advance correction, not a month-long collective line) and a row of an imported list
   that names its cost object; the extension may decide otherwise (``is_anchor_eligible``).
2. Anchors are merged agglomeratively (average linkage with a minimum link): two groups join
   when the mean pair probability reaches the ``merge`` threshold and no pair is below
   ``min_link``. Two existing cost objects are never merged, and a pair marked "different" by a
   person never joins (cannot-link).
3. Other records join the best group when their link (mean of the three best pair probabilities
   with the group's anchors) reaches ``member`` and beats the second group by ``margin``.
   A record whose text names two or more groups (a collective line, "Gdańsk, Wrocław, Łódź") or
   whose month-long window holds several groups of the same client joins none - a collective
   invoice is never an anchor and never a member.
4. A group is a candidate when it has an anchor and at least one record of another type linked
   with high probability (``high``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from uuid import UUID

from kasa.core.cost_objects_discovery.pairs import PairScorer
from kasa.core.cost_objects_discovery.records import DiscoveryRecord
from kasa.core.cost_objects_discovery.text import window_gap_days

Key = tuple[str, UUID]



def default_anchor_eligible(record: DiscoveryRecord) -> bool:
    if record.record_type == "document_line":
        return (record.direction == "sales" and bool(record.name_tokens) and not record.month_only
                and record.line_kind != "correction" and record.document_kind not in ("correction",))
    if record.record_type == "internal_record":
        return bool(record.facts.get("cost_object_ref")) and bool(record.name_tokens)
    if record.record_type == "email_case":
        return True
    return False


@dataclass
class Group:
    anchors: list[DiscoveryRecord] = field(default_factory=list)
    members: dict[Key, tuple[DiscoveryRecord, float]] = field(default_factory=dict)
    existing_id: UUID | None = None        # cost object this group continues
    fixed: set[Key] = field(default_factory=set)   # records a person already put here

    @property
    def keys(self) -> set[Key]:
        return {a.key for a in self.anchors} | set(self.members)

    def distinctive_tokens(self) -> set[str]:
        tokens: set[str] = set()
        for anchor in self.anchors:
            tokens |= set(anchor.name_tokens)
            if anchor.city_key:
                tokens.add(anchor.city_key)
        return tokens

    def client_tokens(self) -> set[str]:
        out: set[str] = set()
        for anchor in self.anchors:
            out |= set(anchor.client_tokens)
        return out

    def window(self):
        windows = [a.window for a in self.anchors if a.window and a.date_source in ("text", "record")]
        if not windows:
            return None
        return min(w[0] for w in windows), max(w[1] for w in windows)


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def link(record: DiscoveryRecord, group: Group, scorer: PairScorer, top: int = 3) -> float:
    probs = sorted((scorer.probability(record, a) for a in group.anchors if a.key != record.key), reverse=True)
    return _mean(probs[:top])


def cluster_anchors(anchors: list[DiscoveryRecord], groups: list[Group], scorer: PairScorer,
                    cannot_link: Callable[[Group, Group], bool]) -> list[Group]:
    """Agglomerative clustering of the free anchors into the existing and new groups."""
    merge_at = scorer.model.threshold("merge")
    min_link = scorer.model.threshold("min_link")
    assigned = {k for g in groups for k in g.keys}
    groups = list(groups) + [Group(anchors=[a]) for a in anchors if a.key not in assigned]
    while True:
        best: tuple[float, int, int] | None = None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                gi, gj = groups[i], groups[j]
                if gi.existing_id and gj.existing_id:
                    continue
                if not gi.anchors or not gj.anchors or cannot_link(gi, gj):
                    continue
                probs = [scorer.probability(a, b) for a in gi.anchors for b in gj.anchors]
                if min(probs) < min_link:
                    continue
                avg = _mean(probs)
                if avg >= merge_at and (best is None or avg > best[0]):
                    best = (avg, i, j)
        if best is None:
            return groups
        _, i, j = best
        keep, drop = (groups[i], groups[j]) if groups[i].existing_id or not groups[j].existing_id else (
            groups[j], groups[i])
        keep.anchors += drop.anchors
        keep.members.update(drop.members)
        keep.fixed |= drop.fixed
        groups.remove(drop)


def names_several_groups(record: DiscoveryRecord, groups: list[Group]) -> list[int]:
    """Indexes of the groups whose distinctive tokens (not shared with other groups) the text names."""
    tokens = record.text_tokens | record.name_tokens
    distinct = [g.distinctive_tokens() for g in groups]
    hits = []
    for i, own in enumerate(distinct):
        others = set().union(*(d for j, d in enumerate(distinct) if j != i)) if len(distinct) > 1 else set()
        unique = own - others
        if unique & tokens:
            hits.append(i)
    return hits


def covers_several_groups(record: DiscoveryRecord, groups: list[Group]) -> list[int]:
    """A month-long window without a name that holds several groups of the record's client."""
    if record.name_tokens and record.record_type != "transaction":
        return []
    if not record.window or (record.window[1] - record.window[0]).days < 7 or not record.client_tokens:
        return []
    hits = []
    for i, group in enumerate(groups):
        window = group.window()
        if window and window_gap_days(window, record.window) == 0 and record.client_tokens & group.client_tokens():
            if window[0] >= record.window[0] and window[1] <= record.window[1]:
                hits.append(i)
    return hits


def attach_members(records: list[DiscoveryRecord], groups: list[Group], scorer: PairScorer) -> list[dict]:
    """Attach non-anchor records to groups; returns the collective records skipped (for the report)."""
    member_at = scorer.model.threshold("member")
    margin = scorer.model.threshold("margin")
    anchored = {k for g in groups for k in g.keys}
    skipped = []
    live = [g for g in groups if g.anchors]
    for record in records:
        if record.key in anchored:
            continue
        several = names_several_groups(record, live)
        covering = covers_several_groups(record, live)
        if len(several) > 1 or len(covering) > 1:
            skipped.append({"record_type": record.record_type, "record_id": str(record.record_id),
                            "reason": "collective", "groups": max(len(several), len(covering))})
            continue
        scored = sorted(((link(record, g, scorer), i) for i, g in enumerate(live)), reverse=True)
        if not scored:
            continue
        best, index = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best >= member_at and best - second >= margin:
            live[index].members[record.key] = (record, best)
    return skipped


def qualifies(group: Group, scorer: PairScorer, primary: DiscoveryRecord) -> bool:
    """An anchor plus at least one record of another type linked with high probability."""
    high = scorer.model.threshold("high")
    for anchor in group.anchors:
        if anchor.record_type != primary.record_type and link(anchor, group, scorer) >= high:
            return True
    return any(r.record_type != primary.record_type and strength >= high
               for r, strength in group.members.values())


_PRIMARY_ORDER = {"email_case": 0, "document_line": 1, "internal_record": 2, "document": 3, "transaction": 4}
_KIND_ORDER = {"proforma": 0, "settlement": 1, "vat": 2, "advance": 3}


def primary_anchor(group: Group) -> DiscoveryRecord:
    """The anchor shown on the card: an offer, else a sales line (final invoice first), else a row."""
    return min(group.anchors, key=lambda a: (
        _PRIMARY_ORDER.get(a.record_type, 9), _KIND_ORDER.get(a.document_kind or "", 5),
        -(abs(a.amount_minor or 0)), str(a.record_id)))
