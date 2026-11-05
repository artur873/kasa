"""LLM provider: reading documents (PDF invoices) and general structured completion.

Two implementations, chosen by ``KASA_LLM_PROVIDER``:

* ``mock`` (default) - :class:`MockLlm` answers from ``fixtures/<slug>/email_pdfs/expected.json``
  (matched by file name or SHA-256 of the PDF); for an unknown file it runs a simple
  regex reading of the PDF text. It never leaves the machine (``leaves_machine = False``).
* ``vertex`` - :class:`VertexLlm`, Google Vertex AI in an EU region (Gemini and Claude
  under one contract, decision D5). Needs ``KASA_VERTEX_PROJECT``,
  ``KASA_VERTEX_LOCATION`` (``europe-*``) and ``GOOGLE_APPLICATION_CREDENTIALS``.

Nothing calls a provider directly: the only way to a model is
``kasa.core.semantics.gate.guarded_complete`` (sensitive words, pseudonymization,
leak check, call log). A provider with ``leaves_machine = True`` refuses raw PDF
bytes, because bytes cannot be pseudonymized.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple, Protocol, runtime_checkable

from kasa.core.providers import provider_name
from kasa.core.providers.ksef import ProviderNotConfigured, fixtures_dir

# Prompt texts and versions live in kasa.core.semantics.prompts (one place for every prompt).
from kasa.core.semantics.prompts import INVOICE_PROMPT, INVOICE_PROMPT_VERSION

log = logging.getLogger(__name__)

COMPLETE_PROMPT_VERSION = "complete-v1"


class LlmResult(NamedTuple):
    data: dict[str, Any]
    model: str
    prompt_version: str


@runtime_checkable
class LlmProvider(Protocol):
    name: str
    model: str
    leaves_machine: bool

    def extract_invoice(self, content: str | bytes, schema: dict[str, Any], *,
                        source_name: str | None = None, source_sha256: str | None = None) -> LlmResult:
        """Read one document into JSON matching ``schema``. Returns (json, model, prompt_version)."""

    def complete(self, prompt: str, schema: dict[str, Any] | None = None) -> LlmResult:
        """General completion; with ``schema`` the answer is JSON matching it."""


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------

EXTRACTION_KEYS = (
    "doc_kind", "seller", "buyer", "number", "ksef_number", "issue_date", "sale_date", "period_from",
    "period_to", "currency", "lines", "totals", "vat_mode", "payment",
)


class MockLlm:
    """Answers from fixtures; regex reading for files it does not know. Never leaves the machine."""

    name = "mock"
    model = "mock-expected"
    semantics_model = "mock-heuristic"     # model name of title/line readings (kasa.core.semantics.mock)
    leaves_machine = False

    def __init__(self, root: Path | None = None):
        self.root = root
        self._by_name: dict[str, dict[str, Any]] | None = None
        self._by_sha: dict[str, dict[str, Any]] = {}

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._by_name is not None:
            return self._by_name
        self._by_name = {}
        base = self.root or fixtures_dir()
        for expected_path in sorted(base.glob("*/email_pdfs/expected.json")):
            documents = json.loads(expected_path.read_text(encoding="utf-8")).get("documents", {})
            for doc_id, document in documents.items():
                self._by_name[document["file"]] = {**document, "_id": doc_id}
            index_path = expected_path.with_name("index.json")
            if index_path.is_file():
                for message in json.loads(index_path.read_text(encoding="utf-8")).get("messages", []):
                    for attachment in message.get("attachments", []):
                        document = self._by_name.get(attachment["filename"])
                        if document is not None and attachment.get("sha256"):
                            self._by_sha[attachment["sha256"]] = document
        return self._by_name

    def known(self, source_name: str | None, source_sha256: str | None) -> dict[str, Any] | None:
        by_name = self._load()
        if source_sha256 and source_sha256 in self._by_sha:
            return self._by_sha[source_sha256]
        if source_name:
            return by_name.get(Path(source_name).name)
        return None

    def extract_invoice(self, content: str | bytes, schema: dict[str, Any], *,
                        source_name: str | None = None, source_sha256: str | None = None) -> LlmResult:
        if isinstance(content, bytes) and source_sha256 is None:
            source_sha256 = hashlib.sha256(content).hexdigest()
        document = self.known(source_name, source_sha256)
        if document is not None:
            data = {key: document.get(key) for key in EXTRACTION_KEYS if key in document}
            data["confidence"] = 0.97
            return LlmResult(data, "mock-expected", INVOICE_PROMPT_VERSION)
        text = content if isinstance(content, str) else _pdf_text(content)
        return LlmResult(regex_extract_invoice(text), "mock-regex", INVOICE_PROMPT_VERSION)

    def complete(self, prompt: str, schema: dict[str, Any] | None = None) -> LlmResult:
        """Semantics prompts (title intent, line entities) get a heuristic answer in Polish."""
        from kasa.core.semantics.mock import answer  # lazy: providers do not depend on semantics at import
        from kasa.core.semantics.prompts import parse_envelope

        envelope = parse_envelope(prompt)
        if envelope is None:
            return LlmResult({"answer": None, "note": "mock - brak odpowiedzi modelu"}, "mock",
                             COMPLETE_PROMPT_VERSION)
        return LlmResult(answer(envelope), self.semantics_model,
                         str(envelope.get("prompt_version") or COMPLETE_PROMPT_VERSION))


def _pdf_text(payload: bytes) -> str:
    from kasa.core.documents.pdf import pdf_text  # lazy: providers do not depend on documents at import

    return pdf_text(payload)


_DATE_PATTERNS = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b"), lambda m: date(int(m[3]), int(m[2]), int(m[1]))),
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_EN_DATE = re.compile(r"\b([A-Z][a-z]{2})[a-z]*\.? (\d{1,2}), (\d{4})\b")
_AMOUNT = r"(-?\d{1,3}(?:[  ]?\d{3})*(?:[.,]\d{2}))"


def _first_date(text: str) -> date | None:
    found: list[tuple[int, date]] = []
    for pattern, build in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                found.append((match.start(), build(match)))
            except ValueError:
                continue
    for match in _EN_DATE.finditer(text):
        month = _MONTHS.get(match[1].lower())
        if month:
            found.append((match.start(), date(int(match[3]), month, int(match[2]))))
    return min(found)[1] if found else None


def _labelled(text: str, labels: str) -> str | None:
    match = re.search(rf"(?:{labels})\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9/\-_.]*)", text, re.IGNORECASE)
    return match.group(1).rstrip(".") if match else None


def _tax_id(block: str) -> str | None:
    match = re.search(r"(?:NIP|VAT(?: \(OSS\))?|EU VAT(?: \(OSS\))?)\s*:?\s*([A-Z]{2})?\s*([0-9A-Z][0-9A-Z\- ]{6,14}[0-9A-Z])",
                      block)
    if not match:
        return None
    return f"{match[1] or ''}{re.sub(r'[- ]', '', match[2])}"


def _name_after(block: str, label: str) -> str | None:
    match = re.search(rf"{label}[^\n]*\n([^\n]+)", block)
    return match.group(1).strip() if match else None


def regex_extract_invoice(text: str) -> dict[str, Any]:
    """Best-effort reading without a model (mock for unknown files). Lines are not read."""
    from kasa.core.normalize import parse_amount

    upper = text.upper()
    if "PARAGON" in upper:
        kind = "receipt"
    elif "RACHUNEK" in upper and "FAKTURA" not in upper:
        kind = "bill"
    elif "KOREKT" in upper:
        kind = "correction"
    else:
        kind = "invoice"
    split = re.split(r"Nabywca|Buyer", text, maxsplit=1)
    seller_block, buyer_block = (split[0], split[1]) if len(split) == 2 else (text, "")
    gross = None
    for label in (r"Razem do zap[łl]aty", r"Do zap[łl]aty", r"Total", r"SUMA PLN", r"Razem"):
        match = re.search(rf"{label}\s*\n?\s*[$€]?\s*{_AMOUNT}", text, re.IGNORECASE)
        if match:
            gross = str(parse_amount(match.group(1)))
            break
    currency = "EUR" if "EUR" in text else "USD" if ("USD" in text or "$" in text) else "PLN"
    number = _labelled(text, r"Numer faktury|Invoice number|Faktura(?: VAT)? nr|RACHUNEK nr|Nr wydr\.|Numer")
    seller_name = _name_after(seller_block, r"Sprzedawca")
    if seller_name is None and seller_block.strip():
        seller_name = seller_block.strip().splitlines()[0]
    return {
        "doc_kind": kind,
        "seller": {"name": seller_name, "tax_id": _tax_id(seller_block), "country": None},
        "buyer": {"name": _name_after("Nabywca" + buyer_block, r"Nabywca"), "tax_id": _tax_id(buyer_block)},
        "number": number,
        "issue_date": (d := _first_date(text)) and d.isoformat(),
        "sale_date": None,
        "period_from": None,
        "period_to": None,
        "currency": currency,
        "lines": [],
        "totals": {"net": None, "vat": None, "gross": gross},
        "vat_mode": None,
        "payment": None,
        "confidence": 0.3,
        "_reader": "regex",
    }


# ---------------------------------------------------------------------------
# Vertex AI (skeleton, no network tests)
# ---------------------------------------------------------------------------


class VertexLlm:
    """Gemini or Claude on Vertex AI in an EU region. Skeleton - not exercised by tests."""

    name = "vertex"
    leaves_machine = True

    def __init__(self, *, project: str, location: str, credentials_path: str, model: str, timeout: float = 120):
        if not project:
            raise ProviderNotConfigured("brak KASA_VERTEX_PROJECT")
        if not location or not location.startswith("europe-"):
            raise ProviderNotConfigured(f"KASA_VERTEX_LOCATION musi być regionem UE (europe-*), jest {location!r}")
        if not credentials_path or not Path(credentials_path).expanduser().is_file():
            raise ProviderNotConfigured("brak pliku GOOGLE_APPLICATION_CREDENTIALS")
        self.project = project
        self.location = location
        self.credentials_path = credentials_path
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> VertexLlm:
        env = os.environ
        return cls(
            project=env.get("KASA_VERTEX_PROJECT", ""),
            location=env.get("KASA_VERTEX_LOCATION", "europe-west1"),
            credentials_path=env.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
            model=env.get("KASA_VERTEX_MODEL", "gemini-2.5-flash"),
        )

    def _access_token(self) -> str:
        try:
            import google.auth
            import google.auth.transport.requests
        except ImportError as exc:
            raise ProviderNotConfigured("brak biblioteki google-auth (uv pip install google-auth)") from exc
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(google.auth.transport.requests.Request())
        return credentials.token

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        import urllib.request

        request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
        request.add_header("Authorization", f"Bearer {self._access_token()}")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def _call(self, prompt: str, schema: dict[str, Any] | None) -> dict[str, Any]:
        base = (f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}"
                f"/locations/{self.location}/publishers")
        if self.model.startswith("claude"):
            instruction = prompt if schema is None else f"{prompt}\n\nSchemat JSON:\n{json.dumps(schema)}"
            answer = self._post(f"{base}/anthropic/models/{self.model}:rawPredict", {
                "anthropic_version": "vertex-2023-10-16",
                "max_tokens": 4096,
                "temperature": 0,
                "messages": [{"role": "user", "content": instruction}],
            })
            text = "".join(part.get("text", "") for part in answer.get("content", []))
        else:
            config: dict[str, Any] = {"temperature": 0}
            if schema is not None:
                config |= {"responseMimeType": "application/json", "responseSchema": schema}
            answer = self._post(f"{base}/google/models/{self.model}:generateContent", {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": config,
            })
            text = answer["candidates"][0]["content"]["parts"][0]["text"]
        if schema is None:
            return {"text": text}
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(match.group(0) if match else text)

    def extract_invoice(self, content: str | bytes, schema: dict[str, Any], *,
                        source_name: str | None = None, source_sha256: str | None = None) -> LlmResult:
        if not isinstance(content, str):
            raise TypeError("Vertex dostaje tylko tekst po pseudonimizacji - nigdy surowy PDF")
        return LlmResult(self._call(INVOICE_PROMPT + content, schema), self.model, INVOICE_PROMPT_VERSION)

    def complete(self, prompt: str, schema: dict[str, Any] | None = None) -> LlmResult:
        return LlmResult(self._call(prompt, schema), self.model, COMPLETE_PROMPT_VERSION)


def get_llm_provider(name: str | None = None) -> LlmProvider:
    """Provider configured by KASA_LLM_PROVIDER; never raises for missing keys."""
    name = (name or provider_name("llm")).lower()
    if name == "vertex":
        try:
            return VertexLlm.from_env()
        except ProviderNotConfigured as exc:
            log.warning("Vertex AI nie jest skonfigurowany (%s) - używam modelu testowego (mock).", exc)
            return MockLlm()
    if name != "mock":
        log.warning("Nieznany dostawca LLM %r - używam modelu testowego (mock).", name)
    return MockLlm()


REGISTRY = {"mock": MockLlm, "vertex": VertexLlm}
