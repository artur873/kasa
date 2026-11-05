"""``python -m kasa documents ...`` end to end on a fresh database (the CLI commits)."""

from __future__ import annotations

import pytest

from kasa.__main__ import main

from .conftest import FIXTURES


@pytest.fixture
def cli(scratch_database_url, monkeypatch):
    monkeypatch.setenv("KASA_KSEF_PROVIDER", "mock")
    monkeypatch.setenv("KASA_LLM_PROVIDER", "mock")
    assert main(["--database-url", scratch_database_url, "init-db"]) == 0

    def run(*args: str) -> int:
        return main(["--database-url", scratch_database_url, "documents", *args])

    return run


def test_documents_commands(cli, capsys):
    assert cli("ksef-sync", "--company", "odm", "--from", "2026-09-01", "--to", "2026-10-31") == 0
    out = capsys.readouterr().out
    assert "dokumenty nowe: 42" in out and "Kontrola kompletności KSeF: kompletna: 42" in out

    assert cli("import-jpk", str(FIXTURES / "history" / "JPK_V7M_2026-09.xml")) == 0
    assert "dopięte: nip_kind_number: 4" in capsys.readouterr().out

    office = str(FIXTURES / "history" / "biuro_export_2025-10.csv")
    assert cli("import", "--company", "odm", "--mapping", "document", "--no-input", office) == 0
    assert "dokumenty nowe: 23" in capsys.readouterr().out
    assert cli("import", "--company", "odm", "--mapping", "pm_sheet", "--no-input",
               str(FIXTURES / "internal" / "arkusz_pm_2026-10.xlsx")) == 0
    assert "wiersze wewnętrzne nowe: 20" in capsys.readouterr().out
    assert cli("import-pdf", "--company", "odm", "--meta", str(FIXTURES / "email_pdfs" / "index.json"),
               str(FIXTURES / "email_pdfs")) == 0
    out = capsys.readouterr().out
    assert "dokumenty nowe: 5" in out and "linked (nip_kind_number)" in out

    assert cli("list", "--company", "odm", "--month", "2026-10", "--direction", "purchase") == 0
    out = capsys.readouterr().out
    assert "FV/2026/10/0412" in out and "ksef+pdf" in out
    assert cli("show", "FV/2026/10/0412") == 0
    out = capsys.readouterr().out
    assert "obserwacje (2)" in out and "EM-06_hotel_wawel_grand_FV-2026-10-0412.pdf" in out
    assert "pozycje (2)" in out and "odczyt PDF: model mock-expected" in out
    assert cli("show", "KOR 1/10/2026") == 0
    assert "koryguje: FV 2/10/2026" in capsys.readouterr().out
    assert cli("ksef-check", "--company", "odm", "--from", "2026-09-01", "--to", "2026-10-31") == 0
    assert cli("show", "nie-ma-takiego") == 1

    # a second run of every channel adds nothing
    assert cli("ksef-sync", "--company", "odm", "--from", "2026-09-01", "--to", "2026-10-31") == 0
    assert "dokumenty nowe: 0; nowe wersje: 0; obserwacje nowe: 0" in capsys.readouterr().out


def test_import_without_mapping_answers_fails_cleanly(cli, tmp_path, capsys):
    odd = tmp_path / "eksport.csv"
    odd.write_text("Nr dok.;data;Do zapłaty\nFV 1;01.10.2026;100,00\n", encoding="utf-8")
    assert cli("import", "--company", "odm", "--mapping", "document", "--no-input", str(odd)) == 2
    assert "--map" in capsys.readouterr().err
    assert cli("import", "--company", "odm", "--mapping", "document", "--no-input",
               "--map", "number_raw=Nr dok.", "--map", "gross=Do zapłaty", str(odd)) == 1   # no direction column
    assert "kierunku" in capsys.readouterr().err
