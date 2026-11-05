"""The gate: the ONLY way from kasa to a language model (CLAUDE.md rule 7; briefing, steps 4, 8, 9).

``guarded_complete(conn, company, purpose, text, schema, *, provider=None, ...)``
does, in this order, for every provider (the local mock too):

1. **sensitive words** (``kasa.core.sensitive``, table ``sensitive_term``): a hit -
   or a subject already flagged ``sensitive`` - returns ``Blocked('sensitive')``
   without any call; the subject gets an L2 suggestion ``category`` with
   ``{"needs_human": true, "reason": "sensitive"}`` (the review queue);
2. **pseudonymization** (``kasa.core.pseudonym.pseudonymize_text``): the module is
   missing or raises -> ``Blocked('gate_unavailable')``, no call;
3. **second line of defence**: the pseudonymized text must not contain anything
   that looks like an IBAN (mod-97), a phone number, a PESEL or an e-mail ->
   otherwise ``Blocked('leak_suspected')``, no call;
4. the call, with the prompt of ``prompts.PROMPTS[purpose]`` - the model gets the
   texts as ``{"untrusted_text": ...}`` and a few code facts, never a list of
   invoices or counterparties;
5. **log**: ``llm_call_log`` (every attempt, blocked ones too; the input only as a
   SHA-256 and a length) and ``llm_call_payload`` (full pseudonymized prompt and
   answer, 90 days);
6. the answer is NOT depseudonymized here (title semantics keep P-xxxx codes on
   purpose - briefing, step 9); a caller that needs values back (PDF facts) does it
   itself, only for codes that were in the input;
7. the answer is validated with the pydantic ``schema``.

Every log row is written in the caller's transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from kasa.core.documents import privacy
from kasa.core.normalize import is_valid_iban
from kasa.core.providers.llm import LlmProvider, get_llm_provider
from kasa.core.semantics.prompts import INVOICE_PROMPT, PROMPTS, build_prompt, envelope_json
from kasa.core.sensitive import Term, find_sensitive, load_terms

log = logging.getLogger(__name__)

PRICE_ENV = "KASA_LLM_PRICE_PER_1K"     # price of 1000 characters (prompt + answer), e.g. 0.0004
PDF_PURPOSE = "pdf_invoice"
CREATED_BY = "kasa.semantics"


def price_per_1k() -> Decimal:
    try:
        value = Decimal(os.environ.get(PRICE_ENV, "0").replace(",", ".") or "0")
    except InvalidOperation:
        log.warning("%s nie jest liczbą - przyjmuję 0", PRICE_ENV)
        return Decimal(0)
    return value if value >= 0 else Decimal(0)


def estimate_cost(chars: int, price: Decimal | None = None) -> Decimal:
    price = price_per_1k() if price is None else price
    return (Decimal(chars) * price / Decimal(1000)).quantize(Decimal("0.000001"))


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Blocked:
    """No answer: the call did not happen (sensitive, gate_unavailable, leak_suspected) or failed (provider_error)."""

    reason: str
    detail: str = ""
    call_id: UUID | None = None
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticsResult:
    data: BaseModel | None              # the answer validated with the schema (None: invalid answer)
    raw: dict[str, Any]                 # the answer as the model gave it (codes not reversed)
    model: str
    prompt_version: str
    provider: str
    input_pseudonymized: str            # exactly the data the model saw
    call_id: UUID
    hits: tuple[Any, ...] = ()
    validation_error: str | None = None
    method: str = "model"               # 'code' when the provider read it with code (mock regex)

    @property
    def codes(self) -> set[str]:
        """Pseudonym codes the model was given (the only ones a caller may reverse)."""
        from kasa.core.pseudonym import PSEUDONYM_RE

        return {m.group(0) for m in PSEUDONYM_RE.finditer(self.input_pseudonymized)}


# ---------------------------------------------------------------------------
# Second line of defence
# ---------------------------------------------------------------------------

_LEAK_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LEAK_IBAN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}(?![A-Z0-9])")
_LEAK_NRB = re.compile(r"(?<![\dA-Z])\d{2}(?:[ ]?\d{4}){6}(?!\d)")
_LEAK_PHONE = re.compile(r"(?<![\d+/.,\w-])(?:(?:\+|00)48[ -]?)?\d{3}[ -]?\d{3}[ -]?\d{3}(?![\d/,]|[.-]\d)")
_LEAK_PESEL = re.compile(r"(?<!\d)\d{11}(?!\d)")


def _pesel_ok(digits: str) -> bool:
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    check = (10 - sum(int(d) * w for d, w in zip(digits[:10], weights, strict=True)) % 10) % 10
    return check == int(digits[10]) and int(digits[2:4]) % 20 in range(1, 13) and 1 <= int(digits[4:6]) <= 31


def leak_scan(text: str) -> list[str]:
    """Kinds of personal data still visible in a text that passed pseudonymization (should be [])."""
    found: list[str] = []
    if _LEAK_EMAIL.search(text):
        found.append("email")
    for m in _LEAK_IBAN.finditer(text):
        candidate = m.group(0).replace(" ", "")
        if any(is_valid_iban(candidate[:n]) for n in range(len(candidate), 14, -1)):
            found.append("iban")
            break
    for m in _LEAK_NRB.finditer(text):
        if is_valid_iban("PL" + m.group(0).replace(" ", "")):
            found.append("iban")
            break
    for m in _LEAK_PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))[-9:]
        if digits[0] != "0":
            found.append("phone")
            break
    for m in _LEAK_PESEL.finditer(text):
        if _pesel_ok(m.group(0)):
            found.append("pesel")
            break
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _log_call(conn: psycopg.Connection, company_id: UUID, *, provider: LlmProvider, model: str | None,
              prompt_version: str | None, purpose: str, subject: tuple[str, UUID] | None, status: str,
              blocked_reason: str | None = None, prompt: str | None = None, response: str | None = None,
              input_chars: int = 0, latency_ms: int | None = None, error_kind: str | None = None) -> UUID:
    leaves = bool(getattr(provider, "leaves_machine", True))
    chars = (len(prompt) if prompt is not None else input_chars) + (len(response) if response else 0)
    cost = estimate_cost(chars) if (status != "blocked" and leaves) else Decimal(0)
    call_id = conn.execute(
        """
        INSERT INTO llm_call_log (company_id, provider, model, prompt_version, purpose, subject_type, subject_id,
                                  leaves_machine, input_hash, input_chars, output_hash, output_chars, latency_ms,
                                  cost_estimate, status, blocked_reason, error_kind, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (company_id, getattr(provider, "name", "unknown"), model, prompt_version, purpose,
         subject[0] if subject else None, subject[1] if subject else None, leaves,
         _sha(prompt) if prompt is not None else None,
         len(prompt) if prompt is not None else input_chars,
         _sha(response) if response is not None else None, len(response) if response is not None else None,
         latency_ms, cost, status, blocked_reason, error_kind, CREATED_BY),
    ).fetchone()[0]
    if prompt is not None:
        conn.execute(
            "INSERT INTO llm_call_payload (call_id, company_id, prompt_pseudonymized, response) "
            "VALUES (%s, %s, %s, %s)",
            (call_id, company_id, prompt, response),
        )
    return call_id


def flag_sensitive(conn: psycopg.Connection, company_id: UUID, subject: tuple[str, UUID], *, purpose: str,
                   categories: Sequence[str] = ()) -> UUID | None:
    """L2 suggestion "a person must categorize this" for a subject that bypassed the model (idempotent)."""
    subject_type, subject_id = subject
    existing = conn.execute(
        """
        SELECT id FROM l2_suggestion
         WHERE company_id = %s AND kind = 'category' AND subject_type = %s AND subject_id = %s
           AND proposal @> '{"needs_human": true, "reason": "sensitive"}'::jsonb
         LIMIT 1
        """,
        (company_id, subject_type, subject_id),
    ).fetchone()
    if existing:
        return existing[0]
    return conn.execute(
        """
        INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, source, method, evidence,
                                   created_by)
        VALUES (%s, 'category', %s, %s, %s, 'semantics', 'code', %s, %s)
        RETURNING id
        """,
        (company_id, subject_type, subject_id,
         Jsonb({"needs_human": True, "reason": "sensitive", "categories": sorted(set(categories))}),
         Jsonb({"purpose": purpose, "note": "słowo z listy wrażliwej - tekst nie trafił do modelu"}),
         CREATED_BY),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _pseudonymize(conn: psycopg.Connection, company_id: UUID, text: str, context: Any | None) -> tuple[str, list]:
    """Text after the pseudonymization gate; raises when the gate does not work."""
    module = privacy.load_pseudonym_module()
    function = getattr(module, "pseudonymize_text", None) if module is not None else None
    if function is None:
        raise privacy.PseudonymizationUnavailable(
            f"bramka pseudonimizacji niedostępna ({privacy.PSEUDONYM_MODULE}.pseudonymize_text)")
    with conn.transaction():
        result = function(conn, company_id, text) if context is None else function(conn, company_id, text,
                                                                                    context=context)
    hits = list(result[1]) if isinstance(result, tuple) and len(result) > 1 else []
    return privacy.as_text(result), hits


def model_name(provider: LlmProvider, purpose: str) -> str:
    """Model that will answer ``purpose`` (known before the call: idempotence by model)."""
    if purpose == PDF_PURPOSE:
        return provider.model
    return getattr(provider, "semantics_model", None) or provider.model


def guarded_complete(conn: psycopg.Connection, company: Any, purpose: str, text: str | None,
                     schema: type[BaseModel], *, provider: LlmProvider | None = None,
                     fields: Mapping[str, str | None] | None = None, facts: Mapping[str, Any] | None = None,
                     subject: tuple[str, UUID] | None = None, known_sensitive: bool = False,
                     pseudonym_context: Any | None = None, terms: Sequence[Term] | None = None,
                     source_name: str | None = None, source_sha256: str | None = None,
                     ) -> SemanticsResult | Blocked:
    """Ask a model about ``text`` (plus short untrusted ``fields``), through every check of the gate.

    ``facts`` are values known by code (e.g. direction, operation type) - never a list of
    invoices or counterparties. ``subject`` = (subject_type, id) for the log and the queue flag.
    ``known_sensitive``: the subject is flagged sensitive already (e.g. ``l1_bank_transaction.sensitive``)
    - its text is not even read.
    """
    if purpose not in PROMPTS:
        raise ValueError(f"nieznany cel wywołania modelu: {purpose!r} (znane: {sorted(PROMPTS)})")
    provider = provider or get_llm_provider()
    company_id = company.id
    text = text or ""
    extra = {k: v for k, v in (fields or {}).items() if v}
    prompt_version = PROMPTS[purpose]
    model = model_name(provider, purpose)
    raw_chars = len(text) + sum(len(v) for v in extra.values())

    def blocked(reason: str, detail: str, categories: Sequence[str] = ()) -> Blocked:
        call_id = _log_call(conn, company_id, provider=provider, model=model, prompt_version=prompt_version,
                            purpose=purpose, subject=subject, status="blocked", blocked_reason=reason,
                            input_chars=0 if known_sensitive else raw_chars)
        return Blocked(reason, detail, call_id, tuple(categories))

    # 1. sensitive words (the text of a subject already flagged sensitive is not read at all)
    if known_sensitive:
        if subject is not None:
            flag_sensitive(conn, company_id, subject, purpose=purpose)
        return blocked("sensitive", "podmiot oznaczony jako wrażliwy - bez modelu, do kolejki")
    if terms is None:
        terms = terms_for(conn, company)
    found = find_sensitive(" \n ".join([text, *extra.values()]), terms)
    if found:
        by_term = {t.term: t.category for t in terms}
        categories = sorted({by_term.get(term, "other") for term in found})
        if subject is not None:
            flag_sensitive(conn, company_id, subject, purpose=purpose, categories=categories)
        return blocked("sensitive", "słowo z listy wrażliwej - bez modelu, do kolejki", categories)

    # 2. pseudonymization: no gate, no call (the local mock included)
    try:
        pseudonymized, hits = _pseudonymize(conn, company_id, text, pseudonym_context)
        extra_ps: dict[str, str] = {}
        for name, value in extra.items():
            extra_ps[name], more = _pseudonymize(conn, company_id, value, pseudonym_context)
            hits += more
    except Exception as exc:  # any failure of the gate means: no call
        log.warning("Bramka pseudonimizacji nie działa (%s) - wywołania modelu %s nie ma.",
                    type(exc).__name__, getattr(provider, "name", "?"))
        return blocked("gate_unavailable", f"bramka pseudonimizacji niedostępna lub zgłosiła błąd "
                                           f"({type(exc).__name__}) - wywołania modelu nie ma")

    # 3. second line of defence
    leaks = leak_scan(" \n ".join([pseudonymized, *extra_ps.values()]))
    if leaks:
        return blocked("leak_suspected", f"po pseudonimizacji wciąż widać: {', '.join(leaks)} - wywołania nie ma")

    # 4. the call
    if purpose == PDF_PURPOSE:
        data_seen = pseudonymized
        prompt = INVOICE_PROMPT + pseudonymized
    else:
        envelope = {
            "purpose": purpose,
            "prompt_version": prompt_version,
            "input": {"untrusted_text": pseudonymized,
                      **{name: {"untrusted_text": value} for name, value in extra_ps.items()}},
            "facts": dict(facts or {}),
        }
        data_seen = envelope_json(envelope)
        prompt = build_prompt(purpose, envelope)
    started = time.monotonic()
    try:
        if purpose == PDF_PURPOSE:
            answer, answered_by, answered_version = provider.extract_invoice(
                pseudonymized, schema.model_json_schema(), source_name=source_name, source_sha256=source_sha256)
        else:
            answer, answered_by, answered_version = provider.complete(prompt, schema.model_json_schema())
    except Exception as exc:  # noqa: BLE001 - logged as an error call, the caller decides
        latency = int((time.monotonic() - started) * 1000)
        call_id = _log_call(conn, company_id, provider=provider, model=model, prompt_version=prompt_version,
                            purpose=purpose, subject=subject, status="error", prompt=prompt, latency_ms=latency,
                            error_kind=type(exc).__name__)
        log.warning("Wywołanie modelu %s nie powiodło się: %s", getattr(provider, "name", "?"), type(exc).__name__)
        return Blocked("provider_error", f"model zgłosił błąd ({type(exc).__name__})", call_id)
    latency = int((time.monotonic() - started) * 1000)
    answer = answer if isinstance(answer, dict) else {"answer": answer}
    response = json.dumps(answer, ensure_ascii=False, sort_keys=True, default=str)

    # 7. validation (before logging, so that an invalid answer is logged as such)
    validated: BaseModel | None = None
    problem: str | None = None
    try:
        validated = schema.model_validate(answer)
    except ValidationError as exc:
        problem = f"odpowiedź niezgodna ze schematem: {exc.errors()[:3]}"

    # 5. log (6. no depseudonymization here)
    call_id = _log_call(conn, company_id, provider=provider, model=answered_by, prompt_version=answered_version,
                        purpose=purpose, subject=subject, status="ok" if problem is None else "error",
                        prompt=prompt, response=response, latency_ms=latency,
                        error_kind=None if problem is None else "invalid_output")
    method = "code" if answer.get("_reader") == "regex" else "model"
    return SemanticsResult(validated, answer, answered_by, answered_version, getattr(provider, "name", "unknown"),
                           data_seen, call_id, tuple(hits), problem, method)


def terms_for(conn: psycopg.Connection, company: Any) -> list[Term]:
    from kasa.core.extension import ExtensionError, get_extension

    try:
        words = get_extension(company.extension_slug).sensitive_words
    except (ExtensionError, AttributeError):
        words = []
    return load_terms(conn, company.id, words)
