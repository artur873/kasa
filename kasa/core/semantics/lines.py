"""Entities of an invoice line or an internal sheet row (briefing, step 5: "Pozycja to miejsce,
gdzie widać Gala ACME 12.06 Kraków").

From "Obsługa Gali ACME 12.06 Kraków" or "Nocleg 11-15.06 Kraków" the reading gives:
``candidate_name`` (a proper name), ``client_hint``, ``dates`` / ``period`` / ``month``,
``city`` and ``service_kind`` (catering, hotel, technika, personel, sala, transport,
saas, other). The core gives them no meaning - these are entities; the extension
decides what they are (phase 4: anchors of cost object candidates).

A row of an internal sheet is sent without the person's column (and without the
amount): only the cost object reference as written, dates, form and notes, pseudonymized.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kasa.core.extension import ExtensionError, get_extension, header_key
from kasa.core.semantics.gate import Blocked, SemanticsResult, guarded_complete, model_name
from kasa.core.semantics.prompts import PROMPTS
from kasa.core.semantics.store import already_read, store_reading, untrusted
from kasa.core.semantics.vocab import SERVICE_KINDS, canonical_city

PURPOSE = "line_entities"


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


class LineAnswer(BaseModel):
    """What the model must return (also sent to it as the JSON schema)."""

    model_config = ConfigDict(extra="ignore")

    candidate_name: str | None = None
    client_hint: str | None = None
    dates: list[str] = Field(default_factory=list)
    period_from: str | None = None
    period_to: str | None = None
    month: str | None = Field(None, description="RRRR-MM")
    city: str | None = None
    service_kind: str = Field("other", description=" | ".join(SERVICE_KINDS))
    summary: str | None = None
    confidence: float | None = None

    @field_validator("service_kind", mode="before")
    @classmethod
    def _known_kind(cls, value: Any) -> str:
        text = str(value or "other").strip().lower()
        return text if text in SERVICE_KINDS else "other"

    @field_validator("dates", mode="before")
    @classmethod
    def _dates(cls, value: Any) -> list[str]:
        values = value if isinstance(value, list) else ([value] if value else [])
        return sorted({d for d in (_iso_or_none(v) for v in values) if d})

    @field_validator("period_from", "period_to", mode="before")
    @classmethod
    def _date(cls, value: Any) -> str | None:
        return _iso_or_none(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> float | None:
        try:
            return None if value is None else min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class LineOutcome:
    status: str                         # read | code | done | blocked
    semantics_id: UUID | None = None
    blocked: Blocked | None = None
    call_id: UUID | None = None         # llm_call_log row (also of a blocked attempt)


def build_entities(answer: LineAnswer | None) -> dict[str, Any]:
    if answer is None:
        return {"candidate_name": None, "client_hint": None, "dates": [], "period": None, "month": None,
                "city": None, "service_kind": "other"}
    period = None
    if answer.period_from and answer.period_to and answer.period_from <= answer.period_to:
        period = {"from": answer.period_from, "to": answer.period_to}
    month = answer.month if answer.month and len(answer.month) == 7 and answer.month[4] == "-" else None
    city = canonical_city(answer.city) or untrusted(answer.city)
    return {
        "candidate_name": untrusted(answer.candidate_name),
        "client_hint": untrusted(answer.client_hint),
        "dates": answer.dates,
        "period": period,
        "month": month,
        "city": city,
        "service_kind": answer.service_kind,
    }


def _read(conn: psycopg.Connection, company: Any, subject_type: str, subject_id: UUID, text: str,
          facts: dict[str, Any], *, provider: Any, pseudonym_context: Any, terms: Any) -> LineOutcome:
    prompt_version = PROMPTS[PURPOSE]
    if already_read(conn, subject_type, subject_id, PURPOSE, prompt_version, model_name(provider, PURPOSE)):
        return LineOutcome("done")
    if not text.strip():
        semantics_id = store_reading(
            conn, company.id, subject_type=subject_type, subject_id=subject_id, purpose=PURPOSE,
            prompt_version=prompt_version, model="code", method="code", intent=None,
            entities=build_entities(None), summary=None, input_pseudonymized="",
            evidence={"reason": "pusty opis - bez wywołania modelu"})
        return LineOutcome("code", semantics_id)
    outcome = guarded_complete(conn, company, PURPOSE, text, LineAnswer, provider=provider, facts=facts,
                               subject=(subject_type, subject_id), pseudonym_context=pseudonym_context,
                               terms=terms)
    if isinstance(outcome, Blocked):
        return LineOutcome("blocked", blocked=outcome, call_id=outcome.call_id)
    return LineOutcome("read", _store(conn, company, subject_type, subject_id, outcome), call_id=outcome.call_id)


def _store(conn: psycopg.Connection, company: Any, subject_type: str, subject_id: UUID,
           outcome: SemanticsResult) -> UUID:
    answer = outcome.data if isinstance(outcome.data, LineAnswer) else None
    summary = (answer.summary or "").strip()[:300] if answer and answer.summary else None
    return store_reading(
        conn, company.id, subject_type=subject_type, subject_id=subject_id, purpose=PURPOSE,
        prompt_version=PROMPTS[PURPOSE], model=outcome.model, method="model", intent=None,
        entities=build_entities(answer), summary=summary, input_pseudonymized=outcome.input_pseudonymized,
        confidence=answer.confidence if answer else None, llm_call_id=outcome.call_id,
        evidence={"provider": outcome.provider, "validation_error": outcome.validation_error,
                  "pseudonym_hits": len(outcome.hits)})


def read_line(conn: psycopg.Connection, company: Any, line: dict[str, Any], *, provider: Any,
              pseudonym_context: Any = None, terms: Any = None) -> LineOutcome:
    """Read one document line (``l1_document_line`` + direction of its document)."""
    facts = {"source": "document_line", "direction": line.get("direction"), "line_kind": line.get("line_kind")}
    return _read(conn, company, "line", line["id"], line.get("name") or "", facts, provider=provider,
                 pseudonym_context=pseudonym_context, terms=terms)


def internal_record_text(company: Any, record: dict[str, Any]) -> str:
    """Values of a sheet row without the person's and the amount's columns (they never go to a model)."""
    column_map = (record.get("evidence") or {}).get("column_map") or {}
    excluded = {header_key(str(column_map[name])) for name in ("person", "amount") if column_map.get(name)}
    try:
        mapping = get_extension(company.extension_slug).import_mapping(record["mapping_key"])
        for name in ("person", "amount"):
            spec = mapping.columns.get(name)
            if spec is not None:
                excluded |= {header_key(h) for h in spec.headers()}
    except (ExtensionError, KeyError, AttributeError):
        pass
    values = []
    for header, value in (record.get("fields") or {}).items():
        if header_key(str(header)) in excluded or value in (None, ""):
            continue
        values.append(str(value).strip())
    return " | ".join(v for v in values if v)


def read_internal_record(conn: psycopg.Connection, company: Any, record: dict[str, Any], *, provider: Any,
                         pseudonym_context: Any = None, terms: Any = None) -> LineOutcome:
    """Read one internal sheet row (``l1_internal_record``)."""
    facts = {"source": "internal_record", "mapping": record.get("mapping_key")}
    return _read(conn, company, "internal_record", record["id"], internal_record_text(company, record), facts,
                 provider=provider, pseudonym_context=pseudonym_context, terms=terms)
