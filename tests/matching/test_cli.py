"""``python -m kasa match ...`` on a committed copy of the loaded month."""

from __future__ import annotations

import psycopg

from kasa.__main__ import main


def test_match_commands(copy_url, capsys):
    db = ["--database-url", copy_url]
    assert main([*db, "match", "run", "--company", "odm", "--month", "2026-10", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "SYMULACJA" in out and "automat: 45" in out and "remisy: 1" in out
    with psycopg.connect(copy_url) as conn:
        assert conn.execute("SELECT count(*) FROM l2_suggestion WHERE kind = 'match'").fetchone() == (0,)

    assert main([*db, "match", "calibrate", "--company", "odm", "--control-set", "ground-truth-transaction"]) == 0
    assert "fałszywych 0" in capsys.readouterr().out
    assert main([*db, "match", "run", "--company", "odm", "--month", "2026-10"]) == 0
    out = capsys.readouterr().out
    assert "automat: 45" in out and "do kolejki: 35" in out and "kontrola prawdy 2 (2026-10): zielona" in out
    assert main([*db, "match", "run", "--company", "odm"]) == 0
    assert "nowe 0, bez zmian 35" in capsys.readouterr().out

    assert main([*db, "match", "status", "--company", "odm", "--month", "2026-10"]) == 0
    out = capsys.readouterr().out
    # 45 automatic decisions close 48 transfers (three split payments have a VAT leg each)
    assert "z decyzją: 48" in out and "kontrola prawdy 2: zielona" in out and "w kolejce (otwarte sugestie): 35" in out

    assert main([*db, "match", "explain", "202610277149922608"]) == 0
    out = capsys.readouterr().out
    assert "remis: " in out and "automat: nie" in out and "czeka w kolejce" in out
    assert main([*db, "match", "explain", "202610232774376085", "--company", "odm"]) == 0
    out = capsys.readouterr().out
    assert "noga VAT split payment" in out and "builtin:match_mpp_block" in out and "dopasowanie [active]" in out

    assert main([*db, "check"]) == 0
    assert "[OK  ] Kontrola prawdy 2: pokrycie dopasowań" in capsys.readouterr().out
