"""KSeF provider (mock + configuration of the real client) and truth check nr 4: KSeF completeness."""

from __future__ import annotations

import logging
import shutil
from datetime import date
from decimal import Decimal

import pytest

from kasa.core.checks import check_ksef_completeness
from kasa.core.documents.fa3 import parse_fa3
from kasa.core.documents.ingest import Observation, ingest_ksef, upsert_document
from kasa.core.providers.ksef import (
    Ksef,
    KsefProvider,
    MockKsef,
    ProviderNotConfigured,
    get_ksef_provider,
)
from kasa.core.raw_store import store_raw

from .conftest import FIXTURES, ODM_NIP, ground_truth, ksef_index

WINDOW = (date(2026, 9, 1), date(2026, 10, 31))


class _Company:
    slug = "odm"


def test_mock_serves_the_fixture_window():
    mock = MockKsef()
    assert isinstance(mock, KsefProvider) and mock.subject_nip(_Company()) == ODM_NIP
    invoices = mock.fetch_invoices(_Company(), *WINDOW)
    assert len(invoices) == 42 and len({i.ksef_number for i in invoices}) == 42
    assert [i.accepted_at for i in invoices] == sorted(i.accepted_at for i in invoices)
    assert len(mock.fetch_invoices(_Company(), *WINDOW, direction="sales")) == 13
    october = mock.fetch_invoices(_Company(), date(2026, 10, 1), date(2026, 10, 31))
    assert len(october) == 38 and all(i.accepted_at.date() >= date(2026, 10, 1) for i in october)
    refs = mock.list_invoice_numbers(_Company(), *WINDOW)
    truth = ground_truth()["checks"]["ksef_completeness"]
    assert len(refs) == truth["count"]
    assert sum(r.direction == "sales" for r in refs) == truth["sales_count"]
    assert sum(r.gross_minor for r in refs if r.direction == "purchase") == int(
        Decimal(truth["purchase_gross_pln"]) * 100)


def test_mock_refuses_a_tampered_file(tmp_path):
    shutil.copytree(FIXTURES / "ksef", tmp_path / "odm" / "ksef")
    entry = ksef_index()["invoices"][0]
    path = tmp_path / "odm" / "ksef" / entry["file"]
    path.write_bytes(path.read_bytes().replace(b"ODM Partners", b"ODM Partnerz"))
    with pytest.raises(ValueError, match="SHA-256"):
        MockKsef(root=tmp_path).fetch_invoices(_Company(), *WINDOW)


def test_missing_configuration_falls_back_to_mock(monkeypatch, caplog):
    for name in ("KASA_KSEF_TOKEN", "KASA_KSEF_CERT_PATH", "KASA_KSEF_NIP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KASA_KSEF_PROVIDER", "ksef")
    with caplog.at_level(logging.WARNING):
        provider = get_ksef_provider()
    assert isinstance(provider, MockKsef) and "mock" in caplog.text
    with pytest.raises(ProviderNotConfigured):
        Ksef.from_env()
    monkeypatch.setenv("KASA_KSEF_NIP", ODM_NIP)
    monkeypatch.setenv("KASA_KSEF_TOKEN", "token-tylko-odczyt")
    monkeypatch.setenv("KASA_KSEF_ENV", "test")
    client = get_ksef_provider()                                      # configured: no network at construction
    assert isinstance(client, Ksef) and client.base_url == "https://ksef-test.mf.gov.pl/api/v2"
    assert client.subject_nip(_Company()) == ODM_NIP
    monkeypatch.setenv("KASA_KSEF_PROVIDER", "mock")
    assert isinstance(get_ksef_provider(), MockKsef)


def test_completeness_is_green_after_a_full_sync(conn, odm):
    report = ingest_ksef(conn, odm, MockKsef(), *WINDOW)
    assert report.documents_created == 42 and not report.errors and not report.warnings
    result = check_ksef_completeness(conn, odm, MockKsef(), *WINDOW)
    assert result.ok, result.detail()
    assert result.ksef_count == result.kasa_count == 42
    assert result.kasa_totals["sales/EUR"] == {"count": 1, "gross_minor": 1_250_000}
    has_table = conn.execute("SELECT to_regclass('truth_check_result') IS NOT NULL").fetchone()[0]
    assert result.recorded == has_table
    if has_table:
        row = conn.execute(
            "SELECT check_name, ok, period_start, period_end, detail ->> 'kasa_count' FROM truth_check_result "
            "WHERE company_id = %s", (odm.id,),
        ).fetchone()
        assert row == ("ksef_completeness", True, *WINDOW, "42")


def test_completeness_is_red_both_ways(conn, odm):
    ingest_ksef(conn, odm, MockKsef(), *WINDOW, direction="sales")
    missing = check_ksef_completeness(conn, odm, MockKsef(), *WINDOW, record=False)
    assert not missing.ok and len(missing.missing_in_kasa) == 29 and not missing.missing_in_ksef

    # A document claiming a KSeF number that KSeF does not list (other direction of the check).
    entry = ksef_index()["invoices"][0]
    xml = (FIXTURES / "ksef" / entry["file"]).read_bytes()
    raw = store_raw(conn, odm.id, "file", xml + b"\n", content_type="application/xml")
    fake = "5342650778-20261003-0000000000AB-00"
    facts = parse_fa3(xml + b"\n", company_nip=ODM_NIP, ksef_number=fake)   # another file, another number
    facts.number_raw, facts.number_normalized = "FV 99/10/2026", "FV99102026"
    upsert_document(conn, odm.id, facts, Observation(raw_id=raw.id, source="ksef"))
    ingest_ksef(conn, odm, MockKsef(), *WINDOW)
    extra = check_ksef_completeness(conn, odm, MockKsef(), *WINDOW, record=False)
    assert not extra.ok and extra.missing_in_kasa == [] and extra.missing_in_ksef == [fake]
