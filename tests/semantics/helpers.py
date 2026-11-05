"""Helpers of the phase 3 tests: the full ODM fixture month, recording providers, leak checks."""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from kasa.core.bank.fx import FixtureNbpSource, load_rates
from kasa.core.bank.ingest import ingest_company
from kasa.core.companies import Company, ensure_company
from kasa.core.documents.csv_import import import_table
from kasa.core.documents.ingest import ingest_jpk, ingest_ksef
from kasa.core.documents.pdf import import_pdfs
from kasa.core.extension import get_extension
from kasa.core.normalize import is_valid_iban
from kasa.core.providers.bank import MockEnableBanking
from kasa.core.providers.ksef import MockKsef
from kasa.core.providers.llm import MockLlm
from kasa.core.sensitive import fold

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures" / "odm"
OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


def ground_truth() -> dict[str, Any]:
    return json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))


def odm_company(conn: psycopg.Connection) -> Company:
    company, _ = ensure_company(conn, get_extension("odm"))
    return company


def load_month(conn: psycopg.Connection, company: Company, *, llm: Any = None) -> None:
    """Every channel of the October fixtures: bank, KSeF, JPK, PM sheet, PDFs (through the gate)."""
    load_rates(conn, FixtureNbpSource(FIXTURES / "reference" / "nbp_rates.json"), ["EUR", "USD"],
               date(2026, 9, 1), OCTOBER[1])
    ingest_company(conn, company, MockEnableBanking(FIXTURES), *OCTOBER)
    ingest_ksef(conn, company, MockKsef(), date(2026, 9, 1), OCTOBER[1])
    jpk = FIXTURES / "history" / "JPK_V7M_2026-09.xml"
    ingest_jpk(conn, company, jpk.read_bytes(), filename=jpk.name)
    pm = FIXTURES / "internal" / "arkusz_pm_2026-10.xlsx"
    import_table(conn, company, pm.read_bytes(), filename=pm.name, mapping_key="pm_sheet")
    pdfs = FIXTURES / "email_pdfs"
    import_pdfs(conn, company, [pdfs], meta_path=pdfs / "index.json", llm=llm or MockLlm())


class RecordingLlm(MockLlm):
    """Answers like the mock but behaves like a model that leaves the machine; records every input."""

    name = "recording"
    leaves_machine = True

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def complete(self, prompt, schema=None):
        self.prompts.append(prompt)
        return super().complete(prompt, schema)

    def extract_invoice(self, content, schema, **kwargs):
        self.prompts.append(content if isinstance(content, str) else repr(content))
        return super().extract_invoice(content, schema, **kwargs)


class CountingMock(MockLlm):
    """The local mock, counting calls."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def complete(self, prompt, schema=None):
        self.calls += 1
        return super().complete(prompt, schema)

    def extract_invoice(self, content, schema, **kwargs):
        self.calls += 1
        return super().extract_invoice(content, schema, **kwargs)


# ---------------------------------------------------------------------------
# What must never reach a model
# ---------------------------------------------------------------------------


def pm_sheet_people() -> list[str]:
    with (FIXTURES / "internal" / "arkusz_pm_2026-10.csv").open(encoding="utf-8-sig") as handle:
        return [row["Osoba"] for row in csv.DictReader(handle, delimiter=";")]


def real_persons(truth: dict[str, Any]) -> list[str]:
    """Every natural person of the fixtures (PM sheet "Nazwisko Imię", pii persons, persons in business names)."""
    people = [" ".join(reversed(p.split())) for p in pm_sheet_people()]
    people += [p["name"] for p in truth["pii"]["persons"]]
    people += [p["person"] for p in truth["pii"]["business_names_with_person"]]
    return list(dict.fromkeys(people))


def surnames(truth: dict[str, Any]) -> set[str]:
    return {fold(name.split()[-1]) for name in real_persons(truth)}


def phones(truth: dict[str, Any]) -> set[str]:
    found = {re.sub(r"\D", "", f)[-9:] for p in truth["pii"]["persons"] for f in p["phone_formats"]}
    with (FIXTURES / "internal" / "arkusz_pm_2026-10.csv").open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            for m in re.finditer(r"\d{3}[ -]?\d{3}[ -]?\d{3}", row.get("Uwagi") or ""):
                found.add(re.sub(r"\D", "", m.group(0)))
    return found


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IBAN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}")
_NRB = re.compile(r"(?<!\d)\d{2}(?:[ ]?\d{4}){6}(?!\d)")


def leaks(text: str | None, truth: dict[str, Any]) -> list[str]:
    """Personal data visible in a text a model saw (must be empty)."""
    if not text:
        return []
    found: list[str] = []
    words = set(re.findall(r"[^\W\d_]+", fold(text)))
    found += [f"surname:{s}" for s in surnames(truth) if s in words]
    digits = re.sub(r"\D", "", text)
    found += [f"phone:{p}" for p in phones(truth) if p in digits]
    found += [f"email:{m.group(0)}" for m in _EMAIL.finditer(text)]
    for m in _IBAN.finditer(text):
        candidate = m.group(0).replace(" ", "")
        if any(is_valid_iban(candidate[:n]) for n in range(len(candidate), 14, -1)):
            found.append(f"iban:{candidate}")
    for m in _NRB.finditer(text):
        if is_valid_iban("PL" + m.group(0).replace(" ", "")):
            found.append(f"nrb:{m.group(0)}")
    return found
