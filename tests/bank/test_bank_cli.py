"""``python -m kasa bank ...`` end to end on its own database."""

from __future__ import annotations

import psycopg

from bank_paths import DATA, FIXTURES
from kasa.__main__ import main


def test_bank_commands(cli_database_url, capsys, monkeypatch):
    monkeypatch.setenv("KASA_FIXTURES_DIR", str(FIXTURES))
    db = ["--database-url", cli_database_url]

    assert main([*db, "bank", "ingest", "--company", "odm", "--from", "2026-10-01", "--to", "2026-10-31",
                 "--provider", "mock"]) == 0
    out = capsys.readouterr().out
    assert "Utworzono firmę odm" in out
    assert out.count("[OK  ] ciągłość sald") == 3 and "[OK  ] MPP: bloki /VAT/ we wpływach 15180.00" in out

    assert main([*db, "bank", "ingest", "--company", "odm", "--from", "2026-10-01", "--to", "2026-10-31"]) == 0
    assert "nowe 0, bez zmian 69" in capsys.readouterr().out

    assert main([*db, "bank", "import-camt", str(FIXTURES / "bank/camt053/mbank_biezacy_2026-10.xml"),
                 "--company", "odm"]) == 0
    assert "nowe 0, bez zmian 66" in capsys.readouterr().out
    assert main([*db, "bank", "import-mt940", str(DATA / "millennium_2611.mt940"), "--company", "odm"]) == 0
    assert "zaksięgowane 5" in capsys.readouterr().out

    assert main([*db, "bank", "balance-check", "--company", "odm", "--month", "2026-10"]) == 0
    out = capsys.readouterr().out
    assert "PL80116022020000000412906733: otwarcie 38450.19 + operacje -21334.28 = 17115.91" in out

    assert main([*db, "bank", "accounts", "--company", "odm", "--provider", "mock"]) == 0
    assert "rachunek VAT do PL40114020040000350277812345" in capsys.readouterr().out
    assert main([*db, "bank", "consent", "--provider", "mock"]) == 0
    assert "status zgody: AUTHORIZED" in capsys.readouterr().out

    assert main([*db, "check"]) == 0
    out = capsys.readouterr().out
    assert "[OK  ] Kontrola prawdy 1: ciągłość sald" in out and "[OK  ] Kontrola prawdy 5: MPP" in out

    with psycopg.connect(cli_database_url) as conn:
        counts = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'booked'), count(*) FILTER (WHERE status = 'pending')
              FROM l1_bank_transaction WHERE superseded_by IS NULL
            """).fetchone()
        assert counts == (88, 4)                           # October (83) + the MT940 statement of November (5)
        assert conn.execute("SELECT count(*) FROM truth_check_result WHERE NOT ok").fetchone() == (0,)
