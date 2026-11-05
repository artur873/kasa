"""PDF documents from e-mails (foreign invoices, receipts, bills of persons) read by a model.

The most expensive and least certain channel (briefing, step 1), so:

* the PDF is stored in L0 first; its text (pypdf) goes to the model only through
  ``kasa.core.semantics.gate.guarded_complete`` (sensitive words bypass the model,
  pseudonymization, leak check, call log) - when the gate does not work, **no model
  is called**, the local mock included;
* codes in the answer are reversed only when they were in the text sent (a model
  could answer with a code it never saw); a regex reading of the mock is ``code``;
* the model reads a file once per prompt version: the reading is kept in
  ``l1_pdf_extraction`` and reused on the next import (no second call, no cost);
* code checks that the lines add up to the totals (``lines_sum_ok``); a mismatch
  keeps the observation but marks the document ``needs_review`` (review queue);
* the document then goes through the same deduplication as every channel
  (the hotel invoice from KSeF and from the e-mail is one document).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kasa.core.documents.facts import KSEF_NUMBER_RE, InvoiceFacts, InvoiceLine, Party
from kasa.core.documents.ingest import (
    IngestReport,
    Observation,
    capabilities,
    get_company_nip,
    upsert_document,
)
from kasa.core.documents.privacy import load_pseudonym_module
from kasa.core.normalize import normalize_iban, normalize_nip, parse_amount, to_minor
from kasa.core.providers.llm import LlmProvider, get_llm_provider
from kasa.core.raw_store import store_raw
from kasa.core.semantics.gate import Blocked, guarded_complete

log = logging.getLogger(__name__)

PARSER_NAME = "pdf-llm"
PARSER_VERSION = 1


def pdf_text(payload: bytes) -> str:
    """Text layer of a PDF (no OCR: a scan without text gives an empty string)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(payload))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


# ---------------------------------------------------------------------------
# What the model must return
# ---------------------------------------------------------------------------


class PdfParty(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    tax_id: str | None = None
    country: str | None = None


class PdfLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    quantity: str | None = None
    unit: str | None = None
    unit_price: str | None = None
    vat_rate: str | None = None
    net: str | None = None
    vat: str | None = None
    gross: str | None = None


class PdfTotals(BaseModel):
    model_config = ConfigDict(extra="ignore")

    net: str | None = None
    vat: str | None = None
    gross: str | None = None


class PdfPayment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    method: str | None = None
    card_last4: str | None = None
    bank_account: str | None = None
    due_date: date | None = None
    status: str | None = None


class PdfExtraction(BaseModel):
    """Schema of the model's answer (also sent to the model as JSON schema)."""

    model_config = ConfigDict(extra="ignore")

    doc_kind: str = Field("invoice", description="invoice | receipt | bill | correction | proforma | other")
    seller: PdfParty = Field(default_factory=PdfParty)
    buyer: PdfParty = Field(default_factory=PdfParty)
    number: str | None = None
    ksef_number: str | None = None
    issue_date: date | None = None
    sale_date: date | None = None
    period_from: date | None = None
    period_to: date | None = None
    currency: str | None = "PLN"
    lines: list[PdfLine] = Field(default_factory=list)
    totals: PdfTotals = Field(default_factory=PdfTotals)
    vat_mode: str | None = Field(None, description="standard | reverse | included | none")
    payment: PdfPayment | None = None
    confidence: float | None = None


EXTRACTION_SCHEMA: dict[str, Any] = PdfExtraction.model_json_schema()


def _amount(value: str | None) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    return parse_amount(str(value).replace("$", "").replace("€", "").replace("EUR", "").replace("USD", ""))


def lines_sum_check(extraction: PdfExtraction) -> tuple[bool, dict[str, Any]]:
    """Code check, never trusted from the model: lines add up to the totals (net, VAT, gross)."""
    if not extraction.lines:
        return False, {"reason": "brak pozycji"}
    totals_gross = _amount(extraction.totals.gross)
    if totals_gross is None:
        return False, {"reason": "brak kwoty razem"}
    details: dict[str, Any] = {}
    ok = True
    for name in ("net", "vat", "gross"):
        total = _amount(getattr(extraction.totals, name))
        values = [_amount(getattr(line, name)) for line in extraction.lines]
        if total is None:
            continue
        if any(v is None for v in values):
            if name == "gross":
                ok = False
                details[name] = {"reason": "pozycja bez kwoty"}
            continue
        lines_total = sum(values, Decimal(0))
        details[name] = {"lines": str(lines_total), "total": str(total)}
        if lines_total != total:
            ok = False
    return ok, details


# ---------------------------------------------------------------------------
# Extraction -> facts
# ---------------------------------------------------------------------------


class PdfNotADocument(ValueError):
    """The reading has no number, date or amount - no document is built from it."""


def _party(raw: PdfParty) -> Party:
    tax = re.sub(r"[\s\-.]", "", raw.tax_id or "").upper()
    nip = foreign = None
    if tax:
        candidate = tax.removeprefix("PL") if re.fullmatch(r"(PL)?[0-9]{10}", tax) else None
        if candidate:
            try:
                nip = normalize_nip(candidate)
            except ValueError:
                nip = normalize_nip(candidate, validate=False)
        else:
            foreign = tax
    country = (raw.country or ("PL" if nip else (foreign[:2] if foreign and foreign[:2].isalpha() else None)))
    return Party(nip=nip, foreign_tax_id=foreign, name=raw.name, country=country)


KIND_BY_DOC = {"receipt": "receipt", "bill": "bill", "correction": "correction", "proforma": "proforma"}


def extraction_to_facts(extraction: PdfExtraction, *, company_nip: str | None, company_name: str,
                        file_hash: str, lines_ok: bool, line_details: dict[str, Any]) -> InvoiceFacts:
    if not extraction.number or extraction.issue_date is None:
        raise PdfNotADocument("odczyt bez numeru albo daty wystawienia")
    currency = (extraction.currency or "PLN").upper()
    gross = _amount(extraction.totals.gross)
    if gross is None:
        raise PdfNotADocument("odczyt bez kwoty do zapłaty")
    seller, buyer = _party(extraction.seller), _party(extraction.buyer)
    direction = "sales" if company_nip and seller.nip == company_nip else "purchase"
    if direction == "purchase" and not buyer.nip and not buyer.name:
        buyer = Party(nip=company_nip, name=company_name, country="PL")
    doc_kind = (extraction.doc_kind or "invoice").lower()
    if doc_kind == "invoice":
        foreign_seller = not seller.nip and (seller.foreign_tax_id or (seller.country and seller.country != "PL"))
        kind = "foreign" if foreign_seller else "vat"
    elif doc_kind in KIND_BY_DOC:
        kind = KIND_BY_DOC[doc_kind]
    else:
        raise PdfNotADocument(f"rodzaj dokumentu {doc_kind!r} nie jest fakturą ani rachunkiem")
    lines = []
    for number, line in enumerate(extraction.lines, start=1):
        price = _amount(line.unit_price)
        lines.append(InvoiceLine(
            line_no=number,
            name=line.name,
            quantity=_amount(line.quantity),
            unit=line.unit,
            unit_price=price,
            unit_price_minor=to_minor(price, currency, rounding=ROUND_HALF_UP) if price is not None else None,
            net_minor=_minor(line.net, currency),
            vat_rate=line.vat_rate,
            vat_minor=_minor(line.vat, currency),
            gross_minor=_minor(line.gross, currency),
        ))
    payment = extraction.payment.model_dump(mode="json", exclude_none=True) if extraction.payment else {}
    iban = None
    if extraction.payment and extraction.payment.bank_account:
        try:
            iban = normalize_iban(extraction.payment.bank_account)
        except ValueError:
            iban = normalize_iban(extraction.payment.bank_account, validate=False)
    ksef = extraction.ksef_number if extraction.ksef_number and KSEF_NUMBER_RE.match(extraction.ksef_number) else None
    return InvoiceFacts(
        channel="pdf",
        parser=PARSER_NAME,
        parser_version=PARSER_VERSION,
        direction=direction,
        kind=kind,
        kind_certain=True,
        ksef_number=ksef,
        number_raw=extraction.number,
        file_hash=file_hash,
        issue_date=extraction.issue_date,
        sale_date=extraction.sale_date if extraction.sale_date else (
            None if extraction.period_from else extraction.issue_date),
        service_period_start=extraction.period_from,
        service_period_end=extraction.period_to,
        due_date=extraction.payment.due_date if extraction.payment else None,
        seller=seller,
        buyer=buyer,
        payment_iban=iban if direction == "purchase" else None,
        payment=payment,
        currency=currency,
        net_minor=_minor(extraction.totals.net, currency),
        vat_minor=_minor(extraction.totals.vat, currency),
        gross_minor=to_minor(gross, currency),
        lines=lines,
        reverse_charge=(extraction.vat_mode or "").lower() == "reverse",
        lines_sum_ok=lines_ok,
        checks={"lines": line_details},
        extra={"vat_mode": extraction.vat_mode, "doc_kind": doc_kind} if extraction.vat_mode else {"doc_kind": doc_kind},
    )


def _minor(value: str | None, currency: str) -> int | None:
    amount = _amount(value)
    return to_minor(amount, currency) if amount is not None else None


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass
class PdfOutcome:
    filename: str
    status: str                         # document | blocked | sensitive | not_a_document | error
    document_id: Any = None
    needs_review: bool = False
    detail: str = ""


@dataclass
class PdfReport(IngestReport):
    outcomes: list[PdfOutcome] = field(default_factory=list)


def load_email_meta(path: Path | None) -> dict[str, dict[str, Any]]:
    """``index.json`` of an e-mail archive -> {attachment file name or sha256: message metadata}."""
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    found: dict[str, dict[str, Any]] = {}
    for message in data.get("messages", []):
        info = {k: message.get(k) for k in ("id", "mailbox", "message_id", "from", "to", "date", "subject")}
        for attachment in message.get("attachments", []):
            entry = {**info, "attachment": attachment}
            found[attachment["filename"]] = entry
            if attachment.get("sha256"):
                found[attachment["sha256"]] = entry
    return found


def _depseudonymize(conn: psycopg.Connection, company_id: Any, value: Any, codes: set[str]) -> Any:
    """Codes in the model's answer back to values (the parser is an internal role) - only ``codes``,
    the ones present in the text sent to the model; any other code stays as it is."""
    module = load_pseudonym_module()
    reverse = getattr(module, "depseudonymize", None) if module is not None else None
    if reverse is None or not codes:
        return value
    if isinstance(value, str):
        return reverse(conn, company_id, value, role="operator", only=codes)
    if isinstance(value, list):
        return [_depseudonymize(conn, company_id, v, codes) for v in value]
    if isinstance(value, dict):
        return {k: _depseudonymize(conn, company_id, v, codes) for k, v in value.items()}
    return value


def import_pdf(conn: psycopg.Connection, company: Any, payload: bytes, *, filename: str,
               email: dict[str, Any] | None = None, llm: LlmProvider | None = None,
               report: PdfReport | None = None, created_by: str = "import-pdf") -> PdfReport:
    """Import one PDF: L0, model reading (once), lines check, deduplicated document."""
    llm = llm or get_llm_provider()
    report = report or PdfReport(channel="pdf")
    digest = hashlib.sha256(payload).hexdigest()
    raw = store_raw(conn, company.id, "pdf", payload, content_type="application/pdf",
                    external_ref=(email or {}).get("message_id") or filename,
                    meta={"filename": filename, "email": email} if email else {"filename": filename})
    report.raw(raw.created)
    previous = conn.execute(
        """
        SELECT id, extracted, model, prompt_version, lines_sum_ok, pseudonymized, confidence, method::text
          FROM l1_pdf_extraction
         WHERE raw_id = %s AND superseded_by IS NULL AND evidence ->> 'provider' = %s
         ORDER BY created_at DESC LIMIT 1
        """,
        (raw.id, llm.name),
    ).fetchone()

    pseudonymized = False
    input_sha = None
    method = "model"
    if previous is not None:
        _, data, model, prompt_version, _, pseudonymized, _, method = previous
    else:
        gated = guarded_complete(conn, company, "pdf_invoice", pdf_text(payload), PdfExtraction, provider=llm,
                                 source_name=filename, source_sha256=digest)
        if isinstance(gated, Blocked):
            if gated.reason == "sensitive":
                report.skipped.append(f"{filename}: słowo wrażliwe - bez modelu, do kolejki")
                report.outcomes.append(PdfOutcome(filename, "sensitive", detail=", ".join(gated.categories)))
            else:
                report.errors.append(f"{filename}: {gated.detail}")
                report.outcomes.append(PdfOutcome(filename, "blocked", detail=gated.detail))
            return report
        pseudonymized = True
        input_sha = hashlib.sha256(gated.input_pseudonymized.encode()).hexdigest()
        data, model, prompt_version, method = gated.raw, gated.model, gated.prompt_version, gated.method
        data = _depseudonymize(conn, company.id, data, gated.codes)

    problem = ""
    try:
        extraction = PdfExtraction.model_validate(data)
    except ValidationError as exc:
        extraction = None
        problem = f"odpowiedź modelu niezgodna ze schematem: {exc.errors()[:3]}"
    lines_ok, line_details = lines_sum_check(extraction) if extraction else (False, {"reason": "schemat"})
    document_id = None
    outcome = PdfOutcome(filename, "document", needs_review=not lines_ok)
    if extraction is not None:
        try:
            facts = extraction_to_facts(
                extraction, company_nip=get_company_nip(conn, company.id), company_name=company.name,
                file_hash=digest, lines_ok=lines_ok, line_details=line_details,
            )
        except (PdfNotADocument, ValueError) as exc:
            facts = None
            problem = str(exc)
    else:
        facts = None
    confidence = extraction.confidence if extraction and extraction.confidence is not None else None
    if facts is not None:
        evidence = {"model": model, "provider": llm.name, "pseudonymized": pseudonymized,
                    "lines_check": line_details, "needs_review": not lines_ok}
        if email:
            evidence["email"] = {k: email.get(k) for k in ("id", "message_id", "date", "subject")}
        result = upsert_document(conn, company.id, facts, Observation(
            raw_id=raw.id, source="pdf", method=method, prompt_version=prompt_version,
            confidence=Decimal(str(confidence)).quantize(Decimal("0.0001")) if confidence is not None else None,
            evidence=evidence, created_by=f"{created_by}:{model}",
        ), caps=capabilities(conn))
        report.add(result)
        document_id = result.document_id
        outcome.document_id = document_id
        outcome.detail = result.action + (f" ({result.linked_by})" if result.linked_by not in (None, "created") else "")
    else:
        outcome.status = "not_a_document"
        outcome.needs_review = True
        outcome.detail = problem
        report.skipped.append(f"{filename}: {problem}")
    if outcome.needs_review:
        report.needs_review += 1
    if previous is None:
        conn.execute(
            """
            INSERT INTO l1_pdf_extraction (company_id, raw_id, document_id, model, input_sha256, pseudonymized,
                                           extracted, lines_sum_ok, needs_review, source, method, confidence,
                                           evidence, prompt_version, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pdf', %s, %s, %s, %s, %s)
            ON CONFLICT (raw_id, model, prompt_version) DO NOTHING
            """,
            (company.id, raw.id, document_id, model, input_sha, pseudonymized, Jsonb(data), lines_ok,
             outcome.needs_review, method,
             Decimal(str(confidence)).quantize(Decimal("0.0001")) if confidence is not None else None,
             Jsonb({"provider": llm.name, "filename": filename, "lines_check": line_details,
                    "outcome": outcome.status}),
             prompt_version, created_by),
        )
    report.outcomes.append(outcome)
    return report


def import_pdfs(conn: psycopg.Connection, company: Any, paths: list[Path], *, meta_path: Path | None = None,
                llm: LlmProvider | None = None) -> PdfReport:
    """Import PDF files (or every *.pdf in the given directories)."""
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.glob("*.pdf")) if path.is_dir() else [path])
    meta = load_email_meta(meta_path)
    llm = llm or get_llm_provider()
    report = PdfReport(channel="pdf")
    for path in files:
        payload = path.read_bytes()
        email = meta.get(hashlib.sha256(payload).hexdigest()) or meta.get(path.name)
        with conn.transaction():
            import_pdf(conn, company, payload, filename=path.name, email=email, llm=llm, report=report)
    return report
