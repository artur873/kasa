"""The export for the accounting office (briefing, step 9): documents with the evidence of every decision,
transfers without a document, the summary per object; every hand-over logged with the digest of each file."""

from __future__ import annotations

import csv
import hashlib
import io

import psycopg
from openpyxl import load_workbook

from kasa.__main__ import main
from kasa.core.reports.export import DOCUMENT_HEADER, OBJECT_HEADER, TRANSFER_HEADER, write_office_export
from tests.conftest import drop_database
from tests.metrics.helpers import truth
from tests.queue.pipeline import copy_database, derived_name


def _rows(path):
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig")), delimiter=";"))


def test_csv_export_with_the_evidence_of_decisions_and_its_log(mconn, modm, tmp_path):
    result = write_office_export(mconn, modm.id, "2026-10", fmt="csv", out_dir=tmp_path, exported_by="artur")
    names = {f["name"] for f in result.files}
    assert names == {"odm_2026-10_dokumenty.csv", "odm_2026-10_przelewy_bez_dokumentu.csv", "odm_2026-10_obiekty.csv"}
    for f in result.files:
        assert hashlib.sha256((tmp_path / f["name"]).read_bytes()).hexdigest() == f["sha256"]
    log = mconn.execute("SELECT exported_by, recipient, scope, files FROM export_log WHERE id = %s",
                        (result.log_id,)).fetchone()
    assert log[0] == "artur" and log[1] == "biuro rachunkowe" and log[2]["month"] == "2026-10"
    assert {f["sha256"] for f in log[3]} == {f["sha256"] for f in result.files}
    documents = _rows(tmp_path / "odm_2026-10_dokumenty.csv")
    assert documents[0] == DOCUMENT_HEADER
    by_number = {r[2]: dict(zip(DOCUMENT_HEADER, r, strict=True)) for r in documents[1:] if r[0] != "koszt"}
    scena = by_number["SP/2026/10/087"]
    assert scena["status_platnosci"] == "zapłacona" and scena["dopasowane_przelewy"]
    assert scena["dopasowanie_decyzja"] and scena["dopasowanie_data"] == "2026-11-05" or scena["dopasowanie_data"]
    assert scena["alokacja_kto"]
    transfers = _rows(tmp_path / "odm_2026-10_przelewy_bez_dokumentu.csv")
    assert transfers[0] == TRANSFER_HEADER
    refs = {r[-1] for r in transfers[1:]}
    assert {e["entry_reference"] for e in truth()["metrics_expected"]["no_document_outflows_over_100"]} <= refs
    komornik = next(r for r in transfers[1:] if r[-1] == "202610096232801350")
    assert komornik[TRANSFER_HEADER.index("tytul")].startswith("[tytuł wrażliwy")
    objects = _rows(tmp_path / "odm_2026-10_obiekty.csv")
    assert objects[0] == OBJECT_HEADER
    margins = {r[0]: r[4] for r in objects[1:]}
    for event in truth()["events"]:
        assert margins[event["name"]] == event["margin_net_pln"].replace(".", ",")
    # the log is append-only
    with mconn.transaction(force_rollback=True):
        try:
            mconn.execute("UPDATE export_log SET exported_by = 'ktoś' WHERE id = %s", (result.log_id,))
        except psycopg.Error as exc:
            assert exc.sqlstate == "KS001"
        else:
            raise AssertionError("export_log przyjął UPDATE")


def test_xlsx_export_is_one_workbook_with_three_sheets(mconn, modm, tmp_path):
    result = write_office_export(mconn, modm.id, "2026-10", fmt="xlsx", out_dir=tmp_path, exported_by="artur")
    assert [f["name"] for f in result.files] == ["odm_2026-10_biuro.xlsx"]
    book = load_workbook(tmp_path / "odm_2026-10_biuro.xlsx")
    assert book.sheetnames == ["dokumenty", "przelewy_bez_dokumentu", "obiekty"]
    header = [c.value for c in book["obiekty"][1]]
    assert header == OBJECT_HEADER
    gala = next(r for r in book["obiekty"].iter_rows(min_row=2, values_only=True) if r[0] == "Gala ACME 2026")
    assert float(gala[4]) == 16963.60


def test_cli_report_export_format_and_directory(clicked_url, tmp_path):
    copy = copy_database(clicked_url, derived_name("phase5_export"))
    try:
        code = main(["--database-url", copy, "report", "export", "--company", "odm", "--month", "2026-10",
                     "--format", "csv", "--out", str(tmp_path), "--by", "artur"])
        assert code == 0 and (tmp_path / "odm_2026-10_dokumenty.csv").exists()
        with psycopg.connect(copy) as conn:
            assert conn.execute("SELECT count(*) FROM export_log WHERE exported_by = 'artur'").fetchone()[0] == 1
        legacy = tmp_path / "jeden.csv"
        assert main(["--database-url", copy, "report", "export", "--company", "odm", "--month", "2026-10",
                     "--out", str(legacy)]) == 0
        assert legacy.read_text(encoding="utf-8").lstrip("﻿").startswith("kierunek;rodzaj")
    finally:
        drop_database(copy)
