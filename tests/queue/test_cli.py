"""``python -m kasa queue|rules|audit`` on a committed copy of the pipeline database."""

from __future__ import annotations

import pytest

from kasa.__main__ import main
from tests.conftest import drop_database
from tests.queue.pipeline import copy_database, derived_name


@pytest.fixture
def cli_url(pipeline_url):
    url = copy_database(pipeline_url, derived_name("queue_cli"))
    yield url
    drop_database(url)


def run(url, capsys, *args):
    code = main(["--database-url", url, *args])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_queue_rules_audit_commands(cli_url, capsys):
    code, out = run(cli_url, capsys, "queue", "build")
    assert code == 0 and "Kolejka odm: kart" in out and "Kandydaci na reguły" in out
    code, out = run(cli_url, capsys, "queue", "list", "--limit", "8")
    assert code == 0 and "kandydat na obiekt kosztowy" in out
    code, out = run(cli_url, capsys, "queue", "show", "6")
    assert code == 0 and "[1]" in out and "odznaka" in out and "%" not in out.split("\n", 1)[1]
    code, out = run(cli_url, capsys, "queue", "accept", "6", "--by", "artur")
    assert code == 0 and "Decyzje:" in out
    decision = out.split("Decyzje: ")[1].split()[0].strip(",")
    code, out = run(cli_url, capsys, "queue", "show", "6")
    assert code == 1 and "Nie ma karty na pozycji 6" in out            # decided; positions change at the next build
    code, out = run(cli_url, capsys, "queue", "other", "7", "--kind", "no_document", "--by", "artur")
    assert code == 1 and "podtyp" in out
    code, out = run(cli_url, capsys, "queue", "other", "7", "--kind", "no_document", "--subtype", "salary")
    assert code == 0
    code, out = run(cli_url, capsys, "queue", "defer", "8", "--until", "2030-01-01")
    assert code == 0 and "Odłożone" in out
    code, out = run(cli_url, capsys, "queue", "undo", decision, "--by", "artur")
    assert code == 0 and "Cofnięte" in out
    code, out = run(cli_url, capsys, "queue", "history")
    assert code == 0 and "cofnięta" in out
    code, out = run(cli_url, capsys, "queue", "stats")
    assert code == 0 and "Twoja praca" in out and "nie „procent przez AI”" in out
    code, out = run(cli_url, capsys, "rules", "mine", "--min-months", "1", "--verbose")
    assert code == 0 and "Kandydaci na reguły" in out
    code, out = run(cli_url, capsys, "rules", "list", "--all")
    assert code == 0 and "builtin:own_transfer" in out
    code, out = run(cli_url, capsys, "rules", "show", "builtin:own_transfer")
    assert code == 0 and "wzorzec" in out
    code, out = run(cli_url, capsys, "rules", "simulate", "builtin:own_transfer")
    assert code == 0 and "Symulacja" in out
    code, out = run(cli_url, capsys, "rules", "suspend", "builtin:own_transfer", "--reason", "przegląd")
    assert code == 0
    code, out = run(cli_url, capsys, "rules", "resume", "builtin:own_transfer", "--to", "auto")
    assert code == 0
    code, out = run(cli_url, capsys, "audit", "sample", "--month", "2026-10", "--seed", "1")
    assert code == 0 and "wylosowano 20" in out
    code, out = run(cli_url, capsys, "audit", "list", "--open")
    assert code == 0 and "Próbek: 20" in out
    sample = out.split()[0]
    code, out = run(cli_url, capsys, "audit", "verdict", sample, "confirmed")
    assert code == 0 and "confirmed" in out
