"""Intent of a transfer title (briefing, step 4, layer 4: "semantyka tytułu, zapisana raz").

Code already extracted the identifiers (``l1_bank_transaction.extracted``: invoice
numbers, KSeF numbers, MPP block, periods); the model adds what code cannot read:
the intent ("zapłata za fakturę", "czynsz", "zwrot"), persons (as P-xxxx codes),
the place, a company named in the title, a memo and one sentence of summary.

* The model never sees a list of invoices or counterparties - only this title
  (pseudonymized), the bank's counterparty name (pseudonymized), the direction
  and the operation type.
* Code has priority: invoice numbers found by code are kept as they are; the
  model may confirm them or read more (``source = 'model'``).
* Codes P-xxxx stay codes (no depseudonymization - briefing, step 9); only codes
  that were in the input are kept (a model cannot invent a person).
* Every text copied from the title is ``{"untrusted_text": ...}``.
* An empty title, or one with nothing but personal data, KSeF numbers, digits and
  punctuation, gets ``intent = unknown`` without any call (``model = 'code'``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kasa.core.normalize import normalize_invoice_number
from kasa.core.pseudonym import PSEUDONYM_RE, detect
from kasa.core.semantics.gate import Blocked, SemanticsResult, guarded_complete, model_name
from kasa.core.semantics.prompts import PROMPTS
from kasa.core.semantics.store import already_read, store_reading, untrusted
from kasa.core.semantics.vocab import INTENTS

PURPOSE = "title_intent"
SUBJECT = "transaction"
_KSEF_RE = re.compile(r"\b\d{10}-\d{8}-[0-9A-F]{12}-[0-9A-F]{2}\b")
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class TitleAnswer(BaseModel):
    """What the model must return (also sent to it as the JSON schema)."""

    model_config = ConfigDict(extra="ignore")

    intent: str = Field("unknown", description=" | ".join(INTENTS))
    invoice_numbers: list[str] = Field(default_factory=list)
    period: str | None = Field(None, description="RRRR-MM")
    person_codes: list[str] = Field(default_factory=list)
    place: str | None = None
    counterparty_hint: str | None = None
    memo: str | None = None
    summary: str | None = None
    confidence: float | None = None

    @field_validator("intent", mode="before")
    @classmethod
    def _known_intent(cls, value: Any) -> str:
        text = str(value or "unknown").strip().lower()
        return text if text in INTENTS else "unknown"

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return None

    @field_validator("invoice_numbers", "person_codes", mode="before")
    @classmethod
    def _list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value if v is not None]


@dataclass(frozen=True)
class TitleOutcome:
    status: str                         # read | code | done | blocked
    semantics_id: UUID | None = None
    blocked: Blocked | None = None
    call_id: UUID | None = None         # llm_call_log row (also of a blocked attempt)


def only_codes(title: str | None, context: Any = None) -> bool:
    """Nothing for a model to read: empty, or only personal data, KSeF numbers, digits and punctuation."""
    if not title or not title.strip():
        return True
    masked = list(title)
    for d in detect(title, context):
        masked[d.start:d.end] = " " * (d.end - d.start)
    rest = _KSEF_RE.sub(" ", PSEUDONYM_RE.sub(" ", "".join(masked)))
    return not re.search(r"[^\W\d_]", rest)


def _code_numbers(extracted: dict[str, Any] | None) -> list[dict[str, Any]]:
    numbers = []
    for item in (extracted or {}).get("invoice_numbers") or []:
        raw = item.get("raw") if isinstance(item, dict) else str(item)
        if not raw:
            continue
        normalized = item.get("normalized") if isinstance(item, dict) else normalize_invoice_number(raw)
        alt = list(item.get("alt") or []) if isinstance(item, dict) else []
        numbers.append({"raw": untrusted(raw), "normalized": normalized, "alt": alt, "source": "code",
                        "confirmed_by_model": False})
    return numbers


def _code_period(extracted: dict[str, Any] | None) -> str | None:
    for item in (extracted or {}).get("periods") or []:
        if isinstance(item, dict) and item.get("month"):
            return item["month"]
    return None


def build_entities(answer: TitleAnswer | None, extracted: dict[str, Any] | None, seen: str) -> dict[str, Any]:
    """Entities as stored: code first, model additions marked, texts wrapped as untrusted."""
    numbers = _code_numbers(extracted)
    if answer is not None:
        for raw in answer.invoice_numbers:
            try:
                normalized = normalize_invoice_number(raw)
            except (TypeError, ValueError):
                continue
            same = [n for n in numbers if normalized == n["normalized"] or normalized in n["alt"]]
            if same:
                for n in same:
                    n["confirmed_by_model"] = True
                continue
            numbers.append({"raw": untrusted(raw), "normalized": normalized, "alt": [], "source": "model",
                            "confirmed_by_model": True})
    input_codes = {m.group(0) for m in PSEUDONYM_RE.finditer(seen)}
    period = _code_period(extracted)
    if period is None and answer is not None and answer.period and _PERIOD_RE.match(answer.period):
        period = answer.period
    return {
        "invoice_numbers": numbers,
        "period": period,
        "person_codes": sorted(c for c in (answer.person_codes if answer else []) if c in input_codes
                               and c.startswith("P-")),
        "place": untrusted(answer.place) if answer else None,
        "counterparty_hint": untrusted(answer.counterparty_hint) if answer else None,
        "memo": untrusted(answer.memo) if answer else None,
    }


def read_title(conn: psycopg.Connection, company: Any, tx: dict[str, Any], *, provider: Any,
               pseudonym_context: Any = None, terms: Any = None) -> TitleOutcome:
    """Read one transaction title (once per prompt version and model)."""
    prompt_version = PROMPTS[PURPOSE]
    model = model_name(provider, PURPOSE)
    if already_read(conn, SUBJECT, tx["id"], PURPOSE, prompt_version, model):
        return TitleOutcome("done")
    subject = (SUBJECT, tx["id"])
    if tx.get("sensitive"):
        # flagged at ingestion: the title is not even read (no text goes to the gate)
        outcome = guarded_complete(conn, company, PURPOSE, "", TitleAnswer, provider=provider, subject=subject,
                                   known_sensitive=True)
        return TitleOutcome("blocked", blocked=outcome, call_id=outcome.call_id)
    title = tx.get("title_raw") or ""
    if only_codes(title, pseudonym_context):
        semantics_id = store_reading(
            conn, company.id, subject_type=SUBJECT, subject_id=tx["id"], purpose=PURPOSE,
            prompt_version=prompt_version, model="code", method="code", intent="unknown",
            entities=build_entities(None, tx.get("extracted"), ""), summary=None, input_pseudonymized="",
            confidence=None, evidence={"reason": "pusty tytuł albo same kody - bez wywołania modelu"})
        return TitleOutcome("code", semantics_id)
    facts = {"direction": "in" if (tx.get("amount_minor") or 0) > 0 else "out",
             "op_type": tx.get("op_type")}
    outcome = guarded_complete(conn, company, PURPOSE, title, TitleAnswer, provider=provider,
                               fields={"counterparty": tx.get("counterparty_name_raw")}, facts=facts,
                               subject=subject, pseudonym_context=pseudonym_context, terms=terms)
    if isinstance(outcome, Blocked):
        return TitleOutcome("blocked", blocked=outcome, call_id=outcome.call_id)
    return TitleOutcome("read", _store(conn, company, tx, outcome), call_id=outcome.call_id)


def _store(conn: psycopg.Connection, company: Any, tx: dict[str, Any], outcome: SemanticsResult) -> UUID:
    answer = outcome.data if isinstance(outcome.data, TitleAnswer) else None
    entities = build_entities(answer, tx.get("extracted"), outcome.input_pseudonymized)
    summary = (answer.summary or "").strip()[:300] if answer and answer.summary else None
    return store_reading(
        conn, company.id, subject_type=SUBJECT, subject_id=tx["id"], purpose=PURPOSE,
        prompt_version=PROMPTS[PURPOSE], model=outcome.model, method="model",
        intent=answer.intent if answer else "unknown", entities=entities, summary=summary,
        input_pseudonymized=outcome.input_pseudonymized, confidence=answer.confidence if answer else None,
        llm_call_id=outcome.call_id,
        evidence={"provider": outcome.provider, "validation_error": outcome.validation_error,
                  "pseudonym_hits": len(outcome.hits)})

