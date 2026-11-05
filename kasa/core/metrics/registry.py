"""The registry of metrics: implementations from ``definitions/*.py``, definitions in ``metrics.metric_definition``.

* ``code_hash(metric)`` - SHA-256 of the source of the definition: its class (and the base classes defined in
  the definitions package) plus the helpers it declares in ``uses``. The lock file ``definitions.lock.json``
  records it for every (key, version): a change of the code without a new version fails the test
  ``tests/metrics/test_registry.py``; the database refuses it too (``sync_definitions`` raises
  ``DefinitionChanged``, ``verify`` refuses to answer).
* ``sync_definitions(conn)`` (owner) - writes the definitions missing in the database and refreshes the
  agent's ``evidence`` mirrors (a column added by a later migration reaches the agent with the same masks).
* ``compute(conn, company_id, key, ...)`` - THE way to a number for the agent and the dashboard: verifies the
  definition (key, version, hash) against the database, computes, and wraps the result with the metadata of
  every answer (``MetricResult``).
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import pkgutil
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.metrics import definitions as definitions_package
from kasa.core.metrics.base import (
    GRAINS,
    ID_SAMPLE,
    UNITS,
    Computed,
    DefinitionChanged,
    Metric,
    MetricContext,
    MetricError,
    MetricResult,
    UnknownMetric,
    month_period,
    parse_day,
    parse_period,
    share,
)
from kasa.core.metrics.state import data_as_of, data_state

LOCK_FILE = Path(__file__).resolve().parent / "definitions.lock.json"


# ---------------------------------------------------------------------------
# implementations
# ---------------------------------------------------------------------------


@cache
def implementations() -> dict[tuple[str, int], type[Metric]]:
    found: dict[tuple[str, int], type[Metric]] = {}
    for info in sorted(pkgutil.iter_modules(definitions_package.__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{definitions_package.__name__}.{info.name}")
        for cls in getattr(module, "METRICS", ()):
            if not (isinstance(cls, type) and issubclass(cls, Metric)):
                raise TypeError(f"{module.__name__}.METRICS: {cls!r} nie jest metryką")
            if cls.grain not in GRAINS or cls.unit not in UNITS:
                raise TypeError(f"{cls.ref()}: ziarno {cls.grain!r} albo jednostka {cls.unit!r} spoza listy")
            if (cls.key, cls.version) in found:
                raise TypeError(f"dwie implementacje {cls.ref()}")
            found[(cls.key, cls.version)] = cls
    return found


def latest(key: str) -> type[Metric]:
    versions = [cls for (k, _), cls in implementations().items() if k == key]
    if not versions:
        raise UnknownMetric(key)
    return max(versions, key=lambda c: c.version)


def get(key: str, version: int | None = None) -> type[Metric]:
    if version is None:
        return latest(key)
    cls = implementations().get((key, int(version)))
    if cls is None:
        raise UnknownMetric(f"{key}@{version}")
    return cls


def keys() -> list[str]:
    return sorted({k for k, _ in implementations()})


def code_hash(cls: type[Metric]) -> str:
    parts = []
    for base in cls.__mro__:
        if base is Metric or base is object or not base.__module__.startswith(definitions_package.__name__):
            continue
        parts.append(inspect.getsource(base))
    for helper in cls.uses:
        parts.append(inspect.getsource(helper))
    parts.append(json.dumps({"key": cls.key, "version": cls.version, "grain": cls.grain, "unit": cls.unit,
                             "dimensions": list(cls.dimensions), "filters": dict(cls.filters),
                             "traps": list(cls.traps_pl), "period_mode": cls.period_mode},
                            ensure_ascii=False, sort_keys=True))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def describe(cls: type[Metric]) -> dict[str, Any]:
    return {"key": cls.key, "version": cls.version, "ref": cls.ref(), "name_pl": cls.name_pl,
            "description_pl": cls.description_pl, "grain": cls.grain, "unit": cls.unit,
            "period": {"month": "okres wymagany (domyślnie miesiąc stanu danych)",
                       "as_of": "dzień (domyślnie dzień stanu danych)",
                       "optional": "okres opcjonalny", "none": "bez okresu"}[cls.period_mode],
            "dimensions": list(cls.dimensions), "filters": dict(cls.filters), "traps_pl": list(cls.traps_pl),
            "implementation": cls.implementation(), "code_sha256": code_hash(cls)}


# ---------------------------------------------------------------------------
# the lock file and the database
# ---------------------------------------------------------------------------


def lock_entries() -> dict[str, str]:
    return {cls.ref(): code_hash(cls) for cls in sorted(implementations().values(), key=lambda c: c.ref())}


def read_lock() -> dict[str, str]:
    return json.loads(LOCK_FILE.read_text(encoding="utf-8")) if LOCK_FILE.exists() else {}


def write_lock() -> dict[str, str]:
    """Record the hashes of the definitions (after a NEW version; never to hide a change of an old one)."""
    old = read_lock()
    new = lock_entries()
    changed = [ref for ref, digest in new.items() if ref in old and old[ref] != digest]
    if changed:
        raise DefinitionChanged("kod definicji zmieniony bez nowej wersji: " + ", ".join(changed)
                                + " - dodaj nową wersję (klasa z version+1), starej nie zmieniaj")
    merged = {**old, **new}
    LOCK_FILE.write_text(json.dumps(merged, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return merged


def sync_definitions(conn: psycopg.Connection, *, refresh_views: bool = True) -> dict[str, list[str]]:
    """Write the missing definitions (owner role); a recorded (key, version) with another hash -> error."""
    report: dict[str, list[str]] = {"added": [], "unchanged": [], "changed": []}
    for cls in sorted(implementations().values(), key=lambda c: c.ref()):
        digest = code_hash(cls)
        row = conn.execute("SELECT code_sha256 FROM metrics.metric_definition WHERE key = %s AND version = %s",
                           (cls.key, cls.version)).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO metrics.metric_definition (key, version, name_pl, description_pl, grain, unit,
                                                       implementation, dimensions, filters, traps_pl, code_sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (cls.key, cls.version, cls.name_pl, cls.description_pl, cls.grain, cls.unit, cls.implementation(),
                 Jsonb(list(cls.dimensions)), Jsonb(dict(cls.filters)), list(cls.traps_pl), digest),
            )
            report["added"].append(cls.ref())
        elif row[0] == digest:
            report["unchanged"].append(cls.ref())
        else:
            report["changed"].append(cls.ref())
    if report["changed"]:
        raise DefinitionChanged("definicja zmieniona bez nowej wersji: " + ", ".join(report["changed"]))
    report["deprecated"] = []
    for key in keys():
        newest = latest(key).version
        for (old_key, old_version) in conn.execute(
            "UPDATE metrics.metric_definition SET status = 'deprecated' WHERE key = %s AND version < %s "
            "AND status = 'active' RETURNING key, version", (key, newest)
        ).fetchall():
            report["deprecated"].append(f"{old_key}@{old_version}")
    if refresh_views:
        refresh_agent_views(conn)
    return report


def refresh_agent_views(conn: psycopg.Connection) -> int:
    """Rebuild the ``evidence`` mirrors when a table got a column the mirror does not have (owner role)."""
    missing = conn.execute(
        """
        SELECT count(*) FROM information_schema.columns c
         WHERE c.table_schema = 'public'
           AND c.table_name IN (SELECT table_name FROM information_schema.views WHERE table_schema = 'evidence')
           AND c.table_name NOT IN ('company', 'l0_raw')
           AND NOT (c.table_name = 'l1_semantics' AND c.column_name = 'input_pseudonymized')
           AND NOT EXISTS (SELECT 1 FROM information_schema.columns e
                            WHERE e.table_schema = 'evidence' AND e.table_name = c.table_name
                              AND e.column_name = c.column_name)
        """
    ).fetchone()[0]
    if missing:
        return conn.execute("SELECT kasa_refresh_agent_views()").fetchone()[0]
    return 0


def verify(conn: psycopg.Connection, cls: type[Metric]) -> dict[str, Any]:
    """The recorded definition of (key, version) - with the hash of the running code (both roles read it)."""
    row = conn.execute(
        "SELECT code_sha256, status FROM metrics.metric_definition WHERE key = %s AND version = %s",
        (cls.key, cls.version),
    ).fetchone()
    if row is None:
        raise DefinitionChanged(f"definicja {cls.ref()} nie jest zapisana w bazie (uruchom: python -m kasa metrics "
                                "sync)")
    if row[0] != code_hash(cls):
        raise DefinitionChanged(f"kod definicji {cls.ref()} różni się od zapisanej wersji - zmiana definicji "
                                "wymaga nowej wersji")
    return {"status": row[1]}


def recorded(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT key, version, name_pl, grain, unit, status, code_sha256, created_at FROM metrics.metric_definition "
        "ORDER BY key, version"
    ).fetchall()
    return [{"key": r[0], "version": r[1], "name_pl": r[2], "grain": r[3], "unit": r[4], "status": r[5],
             "code_sha256": r[6], "created_at": r[7].isoformat()} for r in rows]


# ---------------------------------------------------------------------------
# compute
# ---------------------------------------------------------------------------


def _ids(values: list[UUID]) -> list[str]:
    unique = sorted({str(v) for v in values if v})
    return unique[:ID_SAMPLE]


def compute(conn: psycopg.Connection, company_id: UUID, key: str, *, version: int | None = None,
            period: str | None = None, as_of: str | date | None = None, dimensions: list[str] | None = None,
            filters: dict[str, Any] | None = None) -> MetricResult:
    cls = get(key, version)
    definition = verify(conn, cls)
    dimensions = list(dimensions or [])
    filters = dict(filters or {})
    unknown_dims = [d for d in dimensions if d not in cls.dimensions]
    if unknown_dims:
        raise MetricError(f"{cls.ref()}: nieznany wymiar {unknown_dims} (dostępne: {list(cls.dimensions)})")
    unknown_filters = [f for f in filters if f not in cls.filters]
    if unknown_filters:
        raise MetricError(f"{cls.ref()}: nieznany filtr {unknown_filters} (dostępne: {sorted(cls.filters)})")
    state_day = data_as_of(conn, company_id) or date.today()
    day = parse_day(as_of) or state_day
    parsed = parse_period(period, as_of=day)
    if cls.period_mode == "month" and parsed is None:
        parsed = month_period(day)
    if cls.period_mode == "as_of" and parsed is not None:
        day = min(parsed.end, day) if as_of is None else day
        parsed = None
    if cls.period_mode == "none":
        parsed = None
    ctx = MetricContext(conn, company_id)
    computed: Computed = cls().compute(ctx, period=parsed, as_of=day, dimensions=dimensions, filters=filters)
    window = (parsed.start, parsed.end) if parsed else (day, day)
    state = data_state(conn, company_id, *window)
    warnings = list(state["warnings"]) + list(computed.notes)
    if definition["status"] == "deprecated":
        warnings.append(f"definicja {cls.ref()} jest wycofana (deprecated) - użyj nowszej wersji")
    if parsed is not None and state["as_of"] and parsed.end > date.fromisoformat(state["as_of"]):
        warnings.append(f"okres kończy się {parsed.end.isoformat()}, a dane są na dzień {state['as_of']} - "
                        "liczby za resztę okresu jeszcze nie istnieją")
    unconfirmed_share = share(computed.unconfirmed_minor, computed.confirmed_minor + computed.unconfirmed_minor)
    used = [{"key": cls.key, "version": cls.version}]
    for ref in computed.metrics_used:
        k, v = ref.split("@")
        used.append({"key": k, "version": int(v)})
    return MetricResult(
        key=cls.key, version=cls.version, name_pl=cls.name_pl, unit=cls.unit, grain=cls.grain,
        code_sha256=code_hash(cls), value=computed.value, values=computed.values, detail=computed.detail,
        as_of=computed.as_of or day, period=computed.period or parsed,
        data_state={k: state[k] for k in ("as_of", "coverage_start", "last_imports", "truth_checks_green",
                                          "red_checks")},
        unconfirmed_share=unconfirmed_share, unconfirmed_minor=computed.unconfirmed_minor,
        provenance={"decisions": len({d for d in computed.decisions if d}),
                    "suggestions": len({s for s in computed.suggestions if s}),
                    "decision_ids": _ids(computed.decisions), "suggestion_ids": _ids(computed.suggestions),
                    "note": "liczba z decyzji (L3) i faktów (L1); kwoty z otwartych sugestii (L2) są tylko w "
                            "udziale niepotwierdzonych"},
        warnings=warnings, traps_pl=list(cls.traps_pl), metrics_used=used, dimensions=dimensions, filters=filters)
