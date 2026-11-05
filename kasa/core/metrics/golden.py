"""The golden result of the metric layer (review of 4C/5, point 8): the value of EVERY metric (every key and
version) on the fixtures after the person's clicks, recorded in ``golden.lock.json``.

The SHA-256 of a definition (``registry.code_hash``) covers its class, its helpers and the shared modules it
declares; the golden result covers what no hash can - a change of behaviour anywhere below (a SQL view, a
loader that is not declared, the data model). Gate 5 (``tests/gates/test_gate5.py``) computes every metric on
the clicked fixtures database and compares it with this file: a number that moved without a new version fails.

After a deliberate change (a NEW version of a metric, new fixtures): ``python -m kasa metrics golden --write``
(adds the new versions; a changed number of a recorded version is refused unless ``--force`` - that is a
change of a definition without a new version, or a change of the fixtures).

What is compared: ``value``, ``unconfirmed_share``, ``values`` and ``detail`` - normalized: identifiers
(uuid) and moments (timestamps of imports and checks) are placeholders, ``*_pl`` texts (formatting of the
numbers next to them) are left out, rows of a list are compared as a set (their order may follow ids).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.metrics import registry
from kasa.core.metrics.base import DefinitionChanged

GOLDEN_FILE = Path(__file__).resolve().parent / "golden.lock.json"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MOMENT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def normalize(value: Any) -> Any:
    """A comparable form: no ids, no moments, no formatted texts, rows of a list as a sorted set."""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in sorted(value.items()) if not str(k).endswith("_pl")}
    if isinstance(value, (list, tuple)):
        items = [normalize(v) for v in value]
        if items and all(isinstance(v, dict) for v in items):
            return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False, default=str))
        return items
    if isinstance(value, UUID):
        return "<id>"
    if isinstance(value, str):
        if _UUID.match(value):
            return "<id>"
        if _MOMENT.match(value):
            return "<czas>"
        return value
    if isinstance(value, float):
        return round(value, 6)
    return value


def golden_values(conn: psycopg.Connection, company_id: UUID) -> dict[str, dict[str, Any]]:
    """Every metric (every key@version) of the company, with the default period / day of the data state."""
    out: dict[str, dict[str, Any]] = {}
    for (key, version), _cls in sorted(registry.implementations().items()):
        result = registry.compute(conn, company_id, key, version=version).as_dict()
        out[f"{key}@{version}"] = normalize({"value": result["value"], "unconfirmed_share": result["unconfirmed_share"],
                                             "values": result["values"], "detail": result.get("detail")})
    return out


def read_golden() -> dict[str, dict[str, Any]]:
    return json.loads(GOLDEN_FILE.read_text(encoding="utf-8")) if GOLDEN_FILE.exists() else {}


def compare(current: dict[str, dict[str, Any]], golden: dict[str, dict[str, Any]]) -> list[str]:
    """Differences in Polish (empty = the layer gives the recorded numbers)."""
    problems = []
    for ref in sorted(set(current) - set(golden)):
        problems.append(f"{ref}: brak złotego wyniku (nowa wersja? python -m kasa metrics golden --write)")
    for ref in sorted(set(golden) - set(current)):
        problems.append(f"{ref}: złoty wynik bez definicji w kodzie")
    for ref in sorted(set(current) & set(golden)):
        if current[ref] == golden[ref]:
            continue
        parts = [part for part in ("value", "unconfirmed_share", "values", "detail")
                 if current[ref].get(part) != golden[ref].get(part)]
        problems.append(f"{ref}: inny wynik niż złoty ({', '.join(parts)}; wartość {golden[ref].get('value')!r} -> "
                        f"{current[ref].get('value')!r}) - zmiana definicji wymaga nowej wersji")
    return problems


def write_golden(current: dict[str, dict[str, Any]], *, force: bool = False) -> dict[str, list[str]]:
    """Record the golden result: new refs are added; a recorded ref with another result is refused (a change
    without a new version) unless ``force`` (a deliberate change of the fixtures)."""
    golden = read_golden()
    changed = [ref for ref in sorted(set(current) & set(golden)) if current[ref] != golden[ref]]
    if changed and not force:
        raise DefinitionChanged("wynik metryki zmieniony bez nowej wersji: " + ", ".join(changed)
                                + " - dodaj nową wersję definicji (albo --force po świadomej zmianie danych fixtures)")
    added = sorted(set(current) - set(golden))
    GOLDEN_FILE.write_text(json.dumps(dict(sorted({**golden, **current}.items())), indent=1, ensure_ascii=False,
                                      sort_keys=True) + "\n", encoding="utf-8")
    return {"added": added, "changed": changed if force else []}
