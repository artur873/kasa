"""The metric layer (briefing, step 8): one definition of "revenue", "receivables", "margin" for the agent,
the dashboard and the export - two margins are impossible technically, not only forbidden.

A metric is a class with ``key``, ``version`` and ``compute``; the registry (``registry.py``) wraps every
result with what the briefing requires of each answer: the metric with its version (and the SHA-256 of its
code), the state of the data on a day, the share of amounts that are still only in open suggestions, the
provenance (decisions and suggestions behind the number) and the warnings of the truth checks that are red
in the period.

The code of a metric reads facts with unqualified table names: the dashboard (owner role) reads the
tables of ``public``, the agent (role ``kasa_agent_ro``, ``search_path = evidence, ...``) reads the
per-company mirrors of the same tables in ``evidence`` - the same code, the same number.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, ClassVar
from uuid import UUID

import psycopg

GRAINS = ("company", "cost_object", "counterparty", "document", "day")
UNITS = ("pln_minor", "days", "ratio", "count")
ID_SAMPLE = 25          # how many decision / suggestion ids an answer lists (the counts are always complete)


class MetricError(ValueError):
    """A request the metric cannot answer as asked (bad period, unknown dimension or filter)."""


class UnknownMetric(LookupError):
    """No such metric (or version) in the layer."""


class DefinitionChanged(RuntimeError):
    """The code of a definition differs from the one recorded for its (key, version)."""


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str

    @property
    def months(self) -> list[str]:
        out, day = [], date(self.start.year, self.start.month, 1)
        while day <= self.end:
            out.append(f"{day.year:04d}-{day.month:02d}")
            day = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
        return out

    def as_dict(self) -> dict[str, str]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat(), "label": self.label}


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(?:ytd:)?(\d{4})$")
_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:\.\.|/|:)\s*(\d{4}-\d{2}-\d{2})$")


def parse_period(value: str | None, *, as_of: date | None = None) -> Period | None:
    """'2026-10' (month), '2026' or 'ytd:2026' (from 1 January to as_of / year end), 'YYYY-MM-DD..YYYY-MM-DD'."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().lower()
    if m := _MONTH_RE.match(text):
        year, month = int(m[1]), int(m[2])
        if not 1 <= month <= 12:
            raise MetricError(f"zły miesiąc: {value!r}")
        return Period(date(year, month, 1), date(year, month, monthrange(year, month)[1]), text)
    if m := _YEAR_RE.match(text):
        year = int(m[1])
        end = date(year, 12, 31)
        if as_of is not None and as_of.year == year:
            end = as_of
        return Period(date(year, 1, 1), end, f"{year} (od początku roku do {end.isoformat()})")
    if m := _RANGE_RE.match(text):
        start, end = date.fromisoformat(m[1]), date.fromisoformat(m[2])
        if end < start:
            raise MetricError(f"koniec okresu przed początkiem: {value!r}")
        return Period(start, end, f"{start.isoformat()}..{end.isoformat()}")
    raise MetricError(f"nieznany format okresu {value!r} (miesiąc 2026-10, rok 2026, zakres 2026-10-01..2026-10-31)")


def parse_day(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise MetricError(f"zła data {value!r} (RRRR-MM-DD)") from exc


def month_period(day: date) -> Period:
    return Period(date(day.year, day.month, 1), date(day.year, day.month, monthrange(day.year, day.month)[1]),
                  f"{day.year:04d}-{day.month:02d}")


def days(n: int) -> timedelta:
    return timedelta(days=n)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def fmt_pln(minor: int | None) -> str | None:
    """12 345,67 zł (Polish grouping and decimal comma)."""
    if minor is None:
        return None
    sign = "-" if minor < 0 else ""
    whole, cents = divmod(abs(int(minor)), 100)
    return f"{sign}{whole:,}".replace(",", " ") + f",{cents:02d} zł"


def fmt_amount(minor: int | None, currency: str | None = "PLN") -> str | None:
    """12 345,67 zł, or 150,00 USD for an amount in a foreign currency."""
    text = fmt_pln(minor)
    if text is None or not currency or currency.strip().upper() == "PLN":
        return text
    return text[: -len(" zł")] + f" {currency.strip().upper()}"


def fmt_share(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value * 100:.1f}".replace(".", ",") + "%"


def share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


@dataclass
class Computed:
    """What a metric's ``compute`` returns; the registry adds the metadata of the answer."""

    value: Any
    values: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    confirmed_minor: int = 0          # amounts behind the number resting on decisions (or facts)
    unconfirmed_minor: int = 0        # amounts resting only on open suggestions
    decisions: list[UUID] = field(default_factory=list)
    suggestions: list[UUID] = field(default_factory=list)
    as_of: date | None = None
    period: Period | None = None
    notes: list[str] = field(default_factory=list)
    metrics_used: list[str] = field(default_factory=list)


@dataclass
class MetricResult:
    key: str
    version: int
    name_pl: str
    unit: str
    grain: str
    code_sha256: str
    value: Any
    values: list[dict[str, Any]]
    detail: dict[str, Any]
    as_of: date | None
    period: Period | None
    data_state: dict[str, Any]
    unconfirmed_share: float
    unconfirmed_minor: int
    provenance: dict[str, Any]
    warnings: list[str]
    traps_pl: list[str]
    metrics_used: list[dict[str, Any]]
    dimensions: list[str]
    filters: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        out = {
            "metric": {"key": self.key, "version": self.version, "ref": f"{self.key}@{self.version}",
                       "name_pl": self.name_pl, "unit": self.unit, "grain": self.grain,
                       "code_sha256": self.code_sha256},
            "value": self.value,
            "values": self.values,
            "detail": self.detail,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "period": self.period.as_dict() if self.period else None,
            "dimensions": self.dimensions,
            "filters": self.filters,
            "data_state": self.data_state,
            "unconfirmed_share": self.unconfirmed_share,
            "unconfirmed_minor": self.unconfirmed_minor,
            "provenance": self.provenance,
            "warnings": self.warnings,
            "traps_pl": self.traps_pl,
            "metrics_used": self.metrics_used,
        }
        if self.unit == "pln_minor" and isinstance(self.value, int):
            out["value_pl"] = fmt_pln(self.value)
        elif self.unit == "days" and self.value is not None:
            out["value_pl"] = f"{self.value} dni".replace(".", ",")
        elif self.unit == "ratio" and self.value is not None:
            out["value_pl"] = fmt_share(self.value)
        elif self.unit == "count" and self.value is not None:
            out["value_pl"] = str(self.value)
        if self.value is None:
            out["value_pl"] = "brak liczby (zob. ostrzeżenia)"
        out["unconfirmed_share_pl"] = fmt_share(self.unconfirmed_share)
        out["unconfirmed_pl"] = fmt_pln(self.unconfirmed_minor)
        return with_pl(out)


def with_pl(obj: Any) -> Any:
    """Every ``*_minor`` amount gets a ``*_pl`` sibling (12 345,67 zł), every ``*_share`` a ``*_share_pl`` -
    the agent quotes numbers from the answer, never formats them itself.

    Currency: a key with ``pln`` is PLN; ``*_doc_minor`` is in the row's ``doc_currency`` (or ``currency``);
    any other amount is in the row's ``currency`` (a document of a view), PLN when the row has none."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            out[key] = with_pl(value)
        currency = obj.get("currency") if isinstance(obj.get("currency"), str) else None
        doc_currency = obj.get("doc_currency") if isinstance(obj.get("doc_currency"), str) else None
        for key, value in obj.items():
            if key.endswith("_minor") and isinstance(value, int) and not isinstance(value, bool):
                if "pln" in key:
                    unit = "PLN"
                elif key.endswith("_doc_minor"):
                    unit = doc_currency or currency or "PLN"
                else:
                    unit = currency or "PLN"
                out.setdefault(key[: -len("_minor")] + "_pl", fmt_amount(value, unit))
            elif (key.endswith("_share") or key == "share") and isinstance(value, float):
                out.setdefault(key + "_pl", fmt_share(value))
        return out
    if isinstance(obj, list):
        return [with_pl(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# The definition
# ---------------------------------------------------------------------------


class MetricContext:
    """One request: the connection (owner or the agent's read-only role) and a per-request memo."""

    def __init__(self, conn: psycopg.Connection, company_id: UUID):
        self.conn = conn
        self.company_id = company_id
        self.memo: dict[Any, Any] = {}

    def cached(self, key: Any, fn):
        if key not in self.memo:
            self.memo[key] = fn()
        return self.memo[key]


class Metric:
    """A metric definition. Subclasses set the class attributes and implement ``compute``.

    ``period_mode``: ``month`` - needs a period (default: the month of the data state), ``as_of`` - a day
    (default: the day of the data state), ``optional`` - a period may narrow the answer, ``none``.
    ``uses``: helper functions whose source is part of the definition (and of its SHA-256).
    """

    key: ClassVar[str]
    version: ClassVar[int] = 1
    name_pl: ClassVar[str]
    description_pl: ClassVar[str]
    grain: ClassVar[str] = "company"
    unit: ClassVar[str] = "pln_minor"
    dimensions: ClassVar[tuple[str, ...]] = ()
    filters: ClassVar[dict[str, str]] = {}
    traps_pl: ClassVar[tuple[str, ...]] = ()
    period_mode: ClassVar[str] = "month"
    uses: ClassVar[tuple[Any, ...]] = ()

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:  # pragma: no cover - abstract
        raise NotImplementedError

    @classmethod
    def ref(cls) -> str:
        return f"{cls.key}@{cls.version}"

    @classmethod
    def implementation(cls) -> str:
        return f"python:{cls.__module__}:{cls.__qualname__}"


def text_filter(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.casefold() if text else None


def matches_text(value: str | None, wanted: str | None) -> bool:
    if wanted is None:
        return True
    if value is None:
        return False
    options = [w.strip() for w in wanted.split("|") if w.strip()]
    folded = value.casefold()
    return any(o in folded for o in options)
