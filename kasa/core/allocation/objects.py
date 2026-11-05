"""What allocation knows about the cost objects of a company.

A profile holds the tokens of the name and its aliases (names of the members as written on
lines and sheet rows), the city, the client, the date window, the people of the internal files
allocated to the object, the member records, and the *unique tokens*: tokens of the name and the
city that no other known cost object has (candidates count too - a token shared with a not yet
approved object is not unique). Only approved objects are allocation targets; UNASSIGNED is the
system object of the company.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.cost_objects_discovery.text import city_key, client_tokens, token_set


@dataclass
class ObjectProfile:
    id: UUID
    kind: str
    name: str
    status: str
    attrs: dict[str, Any]
    system_code: str | None = None
    name_tokens: frozenset[str] = frozenset()
    city_key: str | None = None
    client_tokens: frozenset[str] = frozenset()
    window: tuple[date, date] | None = None
    aliases: list[str] = field(default_factory=list)
    unique_tokens: frozenset[str] = frozenset()
    members: dict[tuple[str, UUID], float] = field(default_factory=dict)
    persons: set[str] = field(default_factory=set)
    merged_into: UUID | None = None

    @property
    def is_target(self) -> bool:
        return self.status == "approved" and self.system_code is None

    @property
    def identity_tokens(self) -> frozenset[str]:
        return self.name_tokens | ({self.city_key} if self.city_key else frozenset())


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _window(attrs: dict[str, Any]) -> tuple[date, date] | None:
    """The date window of a cost object from its attributes (any extension: start/end or a month)."""
    start = next((_date(attrs[k]) for k in ("date_start", "start", "from") if attrs.get(k)), None)
    end = next((_date(attrs[k]) for k in ("date_end", "end", "to") if attrs.get(k)), None)
    if start and end:
        return start, end
    month = attrs.get("month")
    if isinstance(month, str) and len(month) == 7:
        from kasa.core.cost_objects_discovery.text import month_window

        return month_window(month)
    if start:
        return start, start
    return None


def load_profiles(conn: psycopg.Connection, company_id: UUID) -> dict[UUID, ObjectProfile]:
    """Approved and candidate cost objects (plus UNASSIGNED) with tokens, members and people."""
    rows = conn.execute(
        """
        SELECT id, kind, name, status::text, attrs, system_code, evidence, merged_into
          FROM l3_cost_object WHERE company_id = %s
        """,
        (company_id,),
    ).fetchall()
    profiles: dict[UUID, ObjectProfile] = {}
    for oid, kind, name, status, attrs, system_code, evidence, merged_into in rows:
        attrs = attrs or {}
        aliases = [a for a in (evidence or {}).get("aliases", []) if isinstance(a, str)]
        tokens = token_set(name) | token_set(attrs.get("name"))
        for alias in aliases:
            tokens |= token_set(alias)
        profiles[oid] = ObjectProfile(
            id=oid, kind=kind, name=name, status=status, attrs=attrs, system_code=system_code,
            name_tokens=tokens, city_key=city_key(attrs.get("city")),
            client_tokens=client_tokens(attrs.get("client")), window=_window(attrs), aliases=aliases,
            merged_into=merged_into,
        )
    for candidate_id, record_type, record_id, strength in conn.execute(
        """
        SELECT candidate_id, record_type::text, record_id, link_strength FROM l2_cost_object_member
         WHERE company_id = %s AND status IN ('open', 'accepted')
        """,
        (company_id,),
    ).fetchall():
        if candidate_id in profiles:
            profiles[candidate_id].members[(record_type, record_id)] = float(strength)
    for cost_object_id, person in conn.execute(
        """
        SELECT a.cost_object_id, r.person_pseudonym FROM l3_allocation a
          JOIN l1_internal_record r ON r.id = a.subject_id
         WHERE a.company_id = %s AND a.status = 'active' AND a.subject_type = 'internal_record'
           AND r.person_pseudonym IS NOT NULL
        UNION
        SELECT m.candidate_id, r.person_pseudonym FROM l2_cost_object_member m
          JOIN l1_internal_record r ON r.id = m.record_id
         WHERE m.company_id = %s AND m.status = 'accepted' AND m.record_type = 'internal_record'
           AND r.person_pseudonym IS NOT NULL
        """,
        (company_id, company_id),
    ).fetchall():
        if cost_object_id in profiles:
            profiles[cost_object_id].persons.add(person)
    known = [p for p in profiles.values() if p.system_code is None and p.status in ("approved", "candidate")]
    for profile in known:
        others: set[str] = set()
        for other in known:
            if other.id != profile.id:
                others |= set(other.identity_tokens)
        profile.unique_tokens = frozenset(profile.identity_tokens - others - profile.client_tokens)
    return profiles


def resolve(profiles: dict[UUID, ObjectProfile], cost_object_id: UUID) -> ObjectProfile | None:
    """Follow ``merged_into`` to the object that is in force."""
    seen = set()
    profile = profiles.get(cost_object_id)
    while profile is not None and profile.merged_into and profile.id not in seen:
        seen.add(profile.id)
        profile = profiles.get(profile.merged_into)
    return profile


def unassigned(profiles: dict[UUID, ObjectProfile]) -> ObjectProfile:
    for profile in profiles.values():
        if profile.system_code == "UNASSIGNED":
            return profile
    raise LookupError("Firma nie ma obiektu UNASSIGNED")


UNIQUE_WINDOW_DAYS = 90


def _near(profile: ObjectProfile, around: tuple[date, date], days: int) -> bool:
    """The object's window is within ``days`` of the subject's dates (an object without dates always counts)."""
    if profile.window is None:
        return True
    start, end = around
    return profile.window[1] >= start - timedelta(days=days) and profile.window[0] <= end + timedelta(days=days)


def unique_tokens_around(profile: ObjectProfile, profiles: dict[UUID, ObjectProfile], around: tuple[date, date],
                         days: int = UNIQUE_WINDOW_DAYS) -> frozenset[str]:
    """Tokens of the object that no OTHER known object within ±``days`` of the subject's dates has.

    "Gala ACME" of last year does not take the uniqueness of this year's "Gala ACME" away from an invoice
    of this year - and two objects of the same season never share a unique token (review of 4B, point 8).
    """
    others: set[str] = set()
    for other in profiles.values():
        if other.id == profile.id or other.system_code or other.status not in ("approved", "candidate"):
            continue
        if _near(other, around, days):
            others |= set(other.identity_tokens)
    return frozenset(profile.identity_tokens - others - profile.client_tokens)


def named_objects(tokens: frozenset[str], profiles: dict[UUID, ObjectProfile], *,
                  targets_only: bool = True, around: tuple[date, date] | None = None,
                  days: int = UNIQUE_WINDOW_DAYS) -> list[tuple[ObjectProfile, frozenset[str], frozenset[str]]]:
    """Objects a text names: a unique token of the object and at least two tokens of its name/city.

    ``around`` = the dates of the subject: the uniqueness of a token is counted among the objects within
    ±``days`` of them; without dates - among all known objects. Returns (profile, hits, unique hits); a
    text naming several objects lists them all.
    """
    out = []
    for profile in profiles.values():
        if profile.system_code or profile.status not in ("approved", "candidate"):
            continue
        if targets_only and not profile.is_target:
            continue
        hits = tokens & profile.identity_tokens
        unique = unique_tokens_around(profile, profiles, around, days) if around else profile.unique_tokens
        unique_hits = tokens & unique
        if unique_hits and len(hits) >= 2:
            out.append((profile, frozenset(hits), frozenset(unique_hits)))
    return out


def resolve_reference(reference: str | None, profiles: dict[UUID, ObjectProfile]) -> list[ObjectProfile]:
    """Approved objects an internal file names by name or alias ("Nordis Gdańsk" -> Roadshow Nordis Gdańsk)."""
    ref = token_set(reference)
    if not ref:
        return []
    found = []
    for profile in profiles.values():
        if not profile.is_target:
            continue
        if ref <= profile.identity_tokens and ref & profile.unique_tokens:
            found.append(profile)
    return found
