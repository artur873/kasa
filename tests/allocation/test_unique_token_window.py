"""Signal A "unique token" (review of 4B, point 8): uniqueness within ±90 days of the subject's dates; a subject
without any date gets a "high" suggestion, never the automation. Pure units - no database."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from kasa.core.allocation.objects import ObjectProfile, named_objects, unique_tokens_around
from kasa.core.allocation.run import _rows
from kasa.core.allocation.signals import AllocationContext, badge, proposals_for
from kasa.core.allocation.subjects import Subject
from kasa.core.cost_objects_discovery.text import city_key, client_tokens, token_set
from kasa.core.extension import get_extension


def _profile(name: str, city: str, window: tuple[date, date] | None, client: str = "ACME Polska sp. z o.o.",
             status: str = "approved") -> ObjectProfile:
    return ObjectProfile(id=uuid4(), kind="event", name=name, status=status,
                         attrs={"name": name, "city": city, "client": client}, name_tokens=token_set(name),
                         city_key=city_key(city), client_tokens=client_tokens(client), window=window)


def _unassigned() -> ObjectProfile:
    return ObjectProfile(id=uuid4(), kind="unassigned", name="UNASSIGNED", status="approved", attrs={},
                         system_code="UNASSIGNED")


def _profiles(*items: ObjectProfile) -> dict:
    out = {p.id: p for p in items}
    known = [p for p in items if p.system_code is None]
    for p in known:              # global uniqueness, as load_profiles computes it
        others = set().union(*(o.identity_tokens for o in known if o.id != p.id)) if len(known) > 1 else set()
        p.unique_tokens = frozenset(p.identity_tokens - others - p.client_tokens)
    return out


def _subject(text: str, window: tuple[date, date] | None) -> Subject:
    return Subject(subject_type="line", subject_id=uuid4(), text=text, tokens=token_set(text), window=window,
                   window_source="document" if window else None, direction="purchase")


def test_a_last_years_object_does_not_take_the_uniqueness_away():
    this_year = _profile("Gala Jubileuszowa", "Kraków", (date(2026, 10, 8), date(2026, 10, 10)))
    last_year = _profile("Gala Jubileuszowa", "Kraków", (date(2025, 10, 8), date(2025, 10, 10)))
    profiles = _profiles(this_year, last_year, _unassigned())
    tokens = token_set("Catering - Gala Jubileuszowa Kraków")
    assert not this_year.unique_tokens                         # globally: the same name twice, nothing unique
    assert named_objects(tokens, profiles) == []
    around = (date(2026, 10, 9), date(2026, 10, 9))
    assert unique_tokens_around(this_year, profiles, around)   # within ±90 days only this year's Gala
    named = named_objects(tokens, profiles, around=around)
    assert [p.id for p, _, _ in named] == [this_year.id]
    # two objects of the same season never share a unique token
    autumn = _profile("Gala Jubileuszowa", "Kraków", (date(2026, 11, 20), date(2026, 11, 21)))
    profiles = _profiles(this_year, last_year, autumn, _unassigned())
    assert named_objects(tokens, profiles, around=around) == []


def test_without_any_date_the_explicit_identifier_is_high_but_not_automatic():
    summit = _profile("Partner Summit Helios", "Warszawa", (date(2026, 10, 15), date(2026, 10, 16)),
                      client="Helios Software GmbH")
    gala = _profile("Gala Jubileuszowa", "Kraków", (date(2026, 10, 8), date(2026, 10, 10)))
    profiles = _profiles(summit, gala, _unassigned())
    ctx = AllocationContext(company_id=uuid4(), extension=get_extension("odm"), profiles=profiles, documents={},
                            records={})
    dated = _subject("Nagłośnienie - Partner Summit Helios", (date(2026, 10, 15), date(2026, 10, 15)))
    undated = _subject("Nagłośnienie - Partner Summit Helios", None)
    top = proposals_for(dated, ctx)[0]
    assert top.strength == "A" and top.auto and top.shares[0][0] == summit.id
    row = _rows(dated, proposals_for(dated, ctx), ctx)[0]
    assert row["evidence"]["auto_eligible"] is True
    top = proposals_for(undated, ctx)[0]
    assert top.strength == "A" and not top.auto and "no_date" in top.tags and badge(top) == "high"
    row = _rows(undated, proposals_for(undated, ctx), ctx)[0]
    assert row["evidence"]["auto_eligible"] is False and row["evidence"]["badge"] == "high"
