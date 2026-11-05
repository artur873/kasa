"""Import wizard: CSV/XLSX detection, mapping profiles, office export and the PM sheet."""

from __future__ import annotations

from datetime import date

import pytest

from kasa.core.documents.csv_import import (
    MappingIncomplete,
    detect_delimiter,
    detect_encoding,
    file_signature,
    import_table,
    read_csv,
)
from kasa.core.documents.xlsx_import import read_xlsx

from .conftest import FIXTURES, current_documents, ground_truth, row_counts

OFFICE_CSV = FIXTURES / "history" / "biuro_export_2025-10.csv"
PM_XLSX = FIXTURES / "internal" / "arkusz_pm_2026-10.xlsx"
PM_CSV = FIXTURES / "internal" / "arkusz_pm_2026-10.csv"


def test_office_export_is_read_as_cp1250_with_semicolons():
    payload = OFFICE_CSV.read_bytes()
    assert detect_encoding(payload) == "cp1250"
    table = read_csv(payload)
    assert table.file_format["delimiter"] == ";" and table.file_format["line_ending"] == "CRLF"
    assert table.headers == ["numer", "data", "kontrahent", "NIP", "netto", "VAT", "brutto", "typ"]
    assert len(table.rows) == 23
    smaki = next(row for _, row in table.rows if row["numer"] == "SMA/891/10/2025")
    assert smaki["kontrahent"] == "Smaki Wisły Catering sp. z o.o."
    assert {row["typ"] for _, row in table.rows} == {"zakup", "sprzedaż", "import usług", "korekta sprzedaży"}


def test_pm_csv_has_bom_and_the_same_rows_as_xlsx():
    payload = PM_CSV.read_bytes()
    assert detect_encoding(payload) == "utf-8-sig"
    csv_table, xlsx_table = read_csv(payload), read_xlsx(PM_XLSX.read_bytes())
    assert csv_table.headers == xlsx_table.headers == ["Osoba", "Data", "Event", "Kwota", "Forma",
                                                       "Typ rozliczenia", "Uwagi"]
    assert len(csv_table.rows) == len(xlsx_table.rows) == 20
    assert [n for n, _ in xlsx_table.rows] == list(range(2, 22))
    assert file_signature(csv_table.headers) == file_signature(xlsx_table.headers)
    assert detect_delimiter("a,b,c") == "," and detect_delimiter("a\tb") == "\t"


def test_office_export_becomes_documents(conn, odm):
    report = import_table(conn, odm, OFFICE_CSV.read_bytes(), filename=OFFICE_CSV.name, mapping_key="document")
    assert not report.errors
    assert report.documents_created == 23
    rows = {r[0]: r for r in conn.execute(
        """
        SELECT number_raw, direction::text, kind::text, seller_nip, buyer_nip, gross_minor, net_minor, vat_minor,
               issue_date, source, corrects_ref
          FROM l1_document WHERE company_id = %s AND superseded_by IS NULL
        """,
        (odm.id,),
    ).fetchall()}
    assert rows["SCE/801/10/2025"][1:6] == ("purchase", "vat", "9516127975", None, 119798)
    assert rows["FV 1/10/2025"][1:6] == ("sales", "vat", None, "5867422344", 2709690)
    assert rows["5091772410"][1:3] == ("purchase", "foreign")          # "import usług", no NIP
    korekta = rows["KOR 1/10/2025"]
    assert korekta[1:3] == ("sales", "correction") and korekta[5] == -119802 and korekta[10]
    assert rows["FV 1/10/2025"][8] == date(2025, 10, 5) and rows["FV 1/10/2025"][9] == "csv"
    # a mapping profile was saved for these headers
    [profile] = conn.execute(
        "SELECT mapping_key, column_map FROM import_mapping_profile WHERE company_id = %s", (odm.id,)
    ).fetchall()
    assert profile[0] == "document" and profile[1]["number_raw"] == "numer" and profile[1]["counterparty_nip"] == "NIP"


def test_second_import_is_idempotent(conn, odm):
    import_table(conn, odm, OFFICE_CSV.read_bytes(), filename=OFFICE_CSV.name, mapping_key="document")
    before = row_counts(conn, odm.id)
    report = import_table(conn, odm, OFFICE_CSV.read_bytes(), filename=OFFICE_CSV.name, mapping_key="document")
    assert report.documents_created == 0 and report.observations_created == 0 and report.unchanged == 23
    assert row_counts(conn, odm.id) == before


def _renamed_export(rows: slice) -> bytes:
    text = OFFICE_CSV.read_bytes().decode("cp1250").splitlines()
    header = "Nr dok.;data;kontrahent;NIP;netto;VAT;Do zapłaty;typ"
    return "\r\n".join([header, *text[1:][rows]]).encode("cp1250")


def test_wizard_asks_once_and_remembers_the_mapping(conn, odm):
    first = _renamed_export(slice(0, 5))
    with pytest.raises(MappingIncomplete) as excinfo:
        import_table(conn, odm, first, filename="export_a.csv", mapping_key="document")
    assert set(excinfo.value.missing) == {"number_raw", "gross"}
    asked = []

    def ask(name, spec, headers):
        asked.append(name)
        return {"number_raw": "Nr dok.", "gross": "Do zapłaty"}[name]

    report = import_table(conn, odm, first, filename="export_a.csv", mapping_key="document", ask=ask)
    assert sorted(asked) == ["gross", "number_raw"] and report.documents_created == 5
    # the next file with the same headers: no questions (ask would fail the test)
    second = _renamed_export(slice(5, 9))
    report = import_table(conn, odm, second, filename="export_b.csv", mapping_key="document",
                          ask=lambda *a: pytest.fail("wizard asked again"))
    assert report.documents_created == 4 and not report.errors
    assert len(current_documents(conn, odm.id)) == 9
    # answers given up front (CLI --map) give the same profile
    profiles = conn.execute(
        "SELECT column_map FROM import_mapping_profile WHERE company_id = %s AND superseded_by IS NULL", (odm.id,)
    ).fetchall()
    assert len(profiles) == 1 and profiles[0][0]["number_raw"] == "Nr dok."


def test_changed_answers_create_a_new_profile_version(conn, odm):
    payload = _renamed_export(slice(0, 2))
    answers = {"number_raw": "Nr dok.", "gross": "Do zapłaty"}
    import_table(conn, odm, payload, filename="a.csv", mapping_key="document", answers=answers)
    import_table(conn, odm, payload, filename="a.csv", mapping_key="document",
                 answers={**answers, "sale_date": "data"})
    rows = conn.execute(
        "SELECT superseded_by IS NULL, column_map ? 'sale_date' FROM import_mapping_profile WHERE company_id = %s",
        (odm.id,),
    ).fetchall()
    assert sorted(rows) == [(False, False), (True, True)]


def test_pm_sheet_rows_become_internal_records(conn, odm):
    report = import_table(conn, odm, PM_XLSX.read_bytes(), filename=PM_XLSX.name, mapping_key="pm_sheet")
    assert report.internal_records_created == 20 and not report.errors
    records = conn.execute(
        """
        SELECT row_no, sheet, fields, person_pseudonym, record_date, amount_minor, currency, cost_object_ref,
               settlement_type, mapping_key, source
          FROM l1_internal_record WHERE company_id = %s ORDER BY row_no
        """,
        (odm.id,),
    ).fetchall()
    assert len(records) == 20
    truth = {r["sheet_row"]: r for r in ground_truth()["pm_sheet_rows"]}
    for row_no, sheet, fields, pseudonym, record_date, amount, currency, ref, settlement, key, source in records:
        expected = truth[row_no]
        assert record_date.isoformat() == expected["date"]
        assert amount == int(expected["amount"].replace(".", ""))
        assert ref == expected["event_label"]                       # the core does not interpret it
        assert settlement == expected["settlement_type"]
        assert (currency, key, source, sheet) == ("PLN", "pm_sheet", "xlsx", "Rozliczenia 10.2026")
        assert set(fields) == {"Osoba", "Data", "Event", "Kwota", "Forma", "Typ rozliczenia", "Uwagi"}
        assert pseudonym and pseudonym.startswith("P-") and fields["Osoba"] not in pseudonym
    zawadzki = next(r for r in records if r[2]["Osoba"] == "Zawadzki Marek")
    assert zawadzki[5] == 180000 and zawadzki[8] == "faktura"


def test_same_person_same_pseudonym_in_csv_and_xlsx(conn, odm):
    import_table(conn, odm, PM_XLSX.read_bytes(), filename=PM_XLSX.name, mapping_key="pm_sheet")
    report = import_table(conn, odm, PM_CSV.read_bytes(), filename=PM_CSV.name, mapping_key="pm_sheet")
    assert report.internal_records_created == 20
    assert not any("zapisano mapowanie" in w for w in report.warnings)   # same headers: no new profile
    pairs = conn.execute(
        """
        SELECT x.person_pseudonym = c.person_pseudonym, x.amount_minor = c.amount_minor,
               x.record_date = c.record_date
          FROM l1_internal_record x JOIN l1_internal_record c ON c.row_no = x.row_no AND c.source = 'csv'
         WHERE x.company_id = %s AND x.source = 'xlsx' AND c.company_id = %s
        """,
        (odm.id, odm.id),
    ).fetchall()
    assert len(pairs) == 20 and all(all(p) for p in pairs)
    before = row_counts(conn, odm.id)
    import_table(conn, odm, PM_CSV.read_bytes(), filename=PM_CSV.name, mapping_key="pm_sheet")
    assert row_counts(conn, odm.id) == before


def test_mapping_for_wrong_file_type_is_refused():
    from kasa.core.documents.csv_import import read_table
    from kasa.core.extension import ColumnMapping, MappingError

    only_xlsx = ColumnMapping.model_validate({
        "target": "internal_record", "file_types": ["xlsx"], "columns": {"person": {"source": "osoba"}},
    })
    with pytest.raises(MappingError, match="pliki"):
        read_table(PM_CSV.read_bytes(), PM_CSV.name, only_xlsx)
    assert read_table(PM_XLSX.read_bytes(), PM_XLSX.name, only_xlsx).kind == "xlsx"
