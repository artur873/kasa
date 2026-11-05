"""Dashboard (briefing, step 8): built from the same metrics as the agent - a card is "metric@version +
dimensions + filters", declared by the extension (``kasa/extensions/<firma>/dashboard.yaml``); the core only
renders. Every card shows the day of the data, the version of the metric, the share of amounts still in
open suggestions and whether the truth checks of the period are green. There is no field for a formula: a
layout with any key outside ``CardSpec`` is refused, and no request parameter can define a number.

  GET /dashboard[?month=RRRR-MM]       cards (mobile first, inline SVG bars, no JavaScript)
  GET /api/dashboard[?month=RRRR-MM]   the same cards as JSON (the values the agent gets from get_metric)
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import psycopg
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kasa.core.companies import Company
from kasa.core.extension import ExtensionError, get_extension
from kasa.core.metrics import registry
from kasa.core.metrics.base import MetricError, fmt_pln, fmt_share
from kasa.core.metrics.state import data_as_of
from kasa.web.app import db, templates

Conn = Annotated[psycopg.Connection, Depends(db)]
router = APIRouter(tags=["dashboard"])
api = APIRouter(prefix="/dashboard", tags=["dashboard (API)"])

LAYOUT_FILE = "dashboard.yaml"
METRIC_REF = re.compile(r"^([a-z][a-z0-9_]*)@([1-9][0-9]*)$")
MONTH = re.compile(r"^\d{4}-\d{2}$")


class CardSpec(BaseModel):
    """A card: a metric with its version, dimensions and filters - and nothing that could compute."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    title: str
    dimensions: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    period: str = "data_month"
    chart: str = "table"
    bar_field: str | None = None
    label_field: str | None = None

    @field_validator("metric")
    @classmethod
    def _ref(cls, value: str) -> str:
        if not METRIC_REF.match(value):
            raise ValueError("karta wskazuje metrykę jako klucz@wersja, np. receivables_open@1")
        return value

    @field_validator("chart")
    @classmethod
    def _chart(cls, value: str) -> str:
        if value not in ("number", "table", "aging", "bars"):
            raise ValueError("chart: number | table | aging | bars")
        return value

    @field_validator("period")
    @classmethod
    def _period(cls, value: str) -> str:
        if value not in ("data_month", "previous_month", "data_day") and not MONTH.match(value):
            raise ValueError("period: data_month | previous_month | data_day | RRRR-MM")
        return value


class Layout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = "Dashboard"
    cards: list[CardSpec]


def load_layout(extension_slug: str) -> Layout:
    try:
        directory = Path(get_extension(extension_slug).directory)
    except (ExtensionError, AttributeError) as exc:
        raise LookupError(f"brak rozszerzenia {extension_slug!r}") from exc
    path = directory / LAYOUT_FILE
    if not path.exists():
        return Layout(title="Dashboard", cards=[CardSpec(metric=f"{key}@{registry.latest(key).version}",
                                                         title=registry.latest(key).name_pl)
                                                for key in registry.keys()])
    return parse_layout(path.read_text(encoding="utf-8"))


def parse_layout(text: str) -> Layout:
    try:
        return Layout.model_validate(yaml.safe_load(text) or {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"zły układ dashboardu ({problems})") from None


def _month(day: date, offset: int = 0) -> str:
    year, month = day.year, day.month + offset
    while month < 1:
        year, month = year - 1, month + 12
    return f"{year:04d}-{month:02d}"


def _bars(rows: list[dict[str, Any]], value_key: str, label_key: str) -> list[dict[str, Any]]:
    values = [r for r in rows if isinstance(r.get(value_key), int)]
    peak = max((abs(r[value_key]) for r in values), default=0) or 1
    negative = any(r[value_key] < 0 for r in values)
    bars = []
    for r in values:
        width = round(abs(r[value_key]) / peak * (50 if negative else 100), 2)
        start = (50 - width if r[value_key] < 0 else 50) if negative else 0
        bars.append({"label": str(r.get(label_key) or "-"), "value_pl": fmt_pln(r[value_key]), "x": start,
                     "width": max(width, 0.5), "negative": r[value_key] < 0})
    return bars


def card_view(conn: psycopg.Connection, company_id: UUID, spec: CardSpec, *, month: str | None,
              day: date) -> dict[str, Any]:
    key, version = METRIC_REF.match(spec.metric).groups()
    period = None
    if spec.period == "data_month":
        period = month or _month(day)
    elif spec.period == "previous_month":
        period = _month(date.fromisoformat((month or _month(day)) + "-01"), -1)
    elif MONTH.match(spec.period):
        period = spec.period
    cls = registry.get(key, int(version))
    if cls.period_mode == "as_of":
        period = None
    try:
        result = registry.compute(conn, company_id, key, version=int(version), period=period,
                                  dimensions=spec.dimensions, filters=spec.filters).as_dict()
    except (MetricError, registry.DefinitionChanged, registry.UnknownMetric) as exc:
        return {"spec": spec.model_dump(), "error": str(exc), "title": spec.title}
    state = result["data_state"]
    view: dict[str, Any] = {
        "spec": spec.model_dump(), "title": spec.title, "ref": result["metric"]["ref"], "result": result,
        "value_pl": result.get("value_pl"), "as_of": state["as_of"], "period": (result.get("period") or {}).get("label"),
        "unconfirmed_pl": fmt_share(result["unconfirmed_share"]), "checks_green": state["truth_checks_green"],
        "red_checks": state["red_checks"], "warnings": result["warnings"], "rows": [], "bars": [], "aging": []}
    values = [v for v in result.get("values") or [] if isinstance(v, dict)]
    if spec.chart == "aging":
        buckets = result["detail"].get("by_bucket_minor") or {}
        view["aging"] = _bars([{"bucket": b, "amount_minor": v} for b, v in buckets.items()], "amount_minor", "bucket")
        view["rows"] = values[:8]
    elif spec.chart == "bars":
        view["bars"] = _bars(values, spec.bar_field or "amount_minor", spec.label_field or "label")
    elif spec.chart == "table":
        view["rows"] = values[:12]
    return view


def dashboard_data(conn: psycopg.Connection, company: Company, month: str | None = None) -> dict[str, Any]:
    if month is not None and not MONTH.match(month):
        raise ValueError("month: RRRR-MM")
    try:
        with conn.transaction():
            registry.sync_definitions(conn, refresh_views=False)
    except registry.DefinitionChanged:
        pass                                  # every card of a changed definition shows the refusal of verify()
    layout = load_layout(company.extension_slug)
    day = data_as_of(conn, company.id) or date.today()
    cards = [card_view(conn, company.id, spec, month=month, day=day) for spec in layout.cards]
    proposals = conn.execute(
        "SELECT question, requested_key, created_at FROM metric_proposal WHERE company_id = %s AND status = 'open' "
        "ORDER BY created_at DESC LIMIT 10", (company.id,)).fetchall()
    drafts = conn.execute(
        "SELECT proposal ->> 'document_number', proposal -> 'draft' ->> 'untrusted_text', created_at FROM l2_suggestion "
        "WHERE company_id = %s AND kind::text = 'payment_reminder' AND status = 'open' ORDER BY created_at DESC LIMIT 5",
        (company.id,)).fetchall()
    return {"title": layout.title, "company": company.slug, "month": month or _month(day), "as_of": day.isoformat(),
            "cards": cards,
            "metric_proposals": [{"question": q, "key": k, "created_at": c.isoformat()} for q, k, c in proposals],
            "reminder_drafts": [{"document": n, "draft": t, "created_at": c.isoformat()} for n, t, c in drafts]}


def _company(conn: psycopg.Connection, request: Request, slug: str | None) -> Company:
    from kasa.web.routers.queue import _company as queue_company

    return queue_company(conn, request, slug)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, conn: Conn, month: str | None = None, company: str | None = None) -> HTMLResponse:
    owner = _company(conn, request, company)
    try:
        data = dashboard_data(conn, owner, month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return templates.TemplateResponse(request, "dashboard/index.html", data)


@api.get("")
def dashboard_api(request: Request, conn: Conn, month: str | None = None, company: str | None = None) -> dict[str, Any]:
    owner = _company(conn, request, company)
    try:
        data = dashboard_data(conn, owner, month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    for card in data["cards"]:
        card.pop("bars", None)
        card.pop("aging", None)
    return data
