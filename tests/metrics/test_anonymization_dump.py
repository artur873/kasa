"""Anonymization of persons leaves NO personal data anywhere in the database (review of 4C/5, point 2).

On a copy of the test database after the whole pipeline and the person's clicks (``clicked_database``):
``python -m kasa retention anonymize --company odm --before <future> --shred``, then ``pg_dump`` of the whole
database (schema and data) and a search of every surname, IBAN, phone and home address of the persons of
``fixtures/odm/ground_truth.json`` (``pii.persons``) - 0 hits.

L0 is included, not excluded: the source files are kept only until the end of the contract (D6), so after
"contract + 1 year" ``--shred`` purges the files with persons (``kasa_purge_raw``, logged), and the metadata
of the files (an e-mail's sender, the attachment's name) is anonymized like any text. What is left of L0 is
also read as text (bytea of the dump is hex; PDF and XLSX are compressed - a grep of the dump alone would not
see inside them).

Business names with a person in them (``pii.business_names_with_person``: a sole trader, a bailiff's office)
stay as companies do (D6): they are not persons of the fixtures' ``pii.persons``.
"""

from __future__ import annotations

import binascii
import io
import re
import subprocess
import zipfile
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from kasa.__main__ import main
from kasa.core.checks import run_checks
from kasa.core.documents.pdf import pdf_text
from kasa.core.pseudonym import fold
from tests.conftest import drop_database
from tests.metrics.helpers import clicked_database, truth
from tests.queue.pipeline import copy_database, derived_name

HEX = re.compile(r"\\\\x([0-9a-f]+)")


def needles(pii: dict[str, Any]) -> list[tuple[str, Any]]:
    """(label, test(raw, folded, compact)) for every personal value of the persons of the fixtures."""
    out: list[tuple[str, Any]] = []
    for person in pii["persons"]:
        surname = fold(person["name"].split()[-1])
        out.append((f"nazwisko {person['name']}",
                    lambda raw, folded, compact, s=surname: re.search(rf"(?<![^\W\d_]){re.escape(s)}(?![^\W\d_])",
                                                                       folded)))
        for phone in {person["phone"], *person["phone_formats"]}:
            out.append((f"telefon {phone}", lambda raw, folded, compact, p=phone: p in raw))
        if person.get("iban"):
            out.append((f"IBAN {person['iban']}", lambda raw, folded, compact, n=person["iban"][2:]: n in compact))
        address = person.get("address")
        if address:
            out.append((f"kod {address['postal_code']}", lambda raw, folded, compact, c=address["postal_code"]: c in raw))
            street = fold(address["line1"].replace("ul. ", ""))
            out.append((f"adres {address['line1']}", lambda raw, folded, compact, s=street: s in folded))
    return out


def hits(text: str, tests: list[tuple[str, Any]]) -> set[str]:
    folded = fold(text)
    compact = re.sub(r"\s", "", text)
    return {label for label, test in tests if test(text, folded, compact)}


def dump_hits(url: str, tests: list[tuple[str, Any]]) -> dict[str, set[str]]:
    """``pg_dump`` of the whole database, searched table by table (bytea decoded from hex)."""
    dump = subprocess.run(["pg_dump", "--no-owner", f"--dbname={url}"], capture_output=True, text=True,
                          check=True).stdout
    found: dict[str, set[str]] = {}
    section = "schemat"
    for line in dump.splitlines():
        if line.startswith("COPY "):
            section = line.split()[1]
            continue
        if line == "\\.":
            section = "schemat"
            continue
        text = line
        for m in HEX.finditer(line):
            text += " " + binascii.unhexlify(m.group(1)).decode("utf-8", "replace")
        found_here = hits(text, tests)
        if found_here:
            found.setdefault(section, set()).update(found_here)
    return found


def raw_hits(conn: psycopg.Connection, tests: list[tuple[str, Any]]) -> dict[str, set[str]]:
    """What L0 still holds, read as text (PDF -> text layer, XLSX -> its XML parts)."""
    found: dict[str, set[str]] = {}
    for raw_id, content_type, external_ref, payload in conn.execute(
            "SELECT id, content_type, external_ref, payload FROM l0_raw WHERE purged_at IS NULL").fetchall():
        data = bytes(payload)
        if content_type == "application/pdf":
            text = pdf_text(data)
        elif "spreadsheetml" in content_type:
            archive = zipfile.ZipFile(io.BytesIO(data))
            text = " ".join(archive.read(n).decode("utf-8", "replace") for n in archive.namelist())
        else:
            text = data.decode("utf-8", "replace")
        found_here = hits(text, tests)
        if found_here:
            found[f"l0_raw {external_ref or raw_id}"] = found_here
    return found


@pytest.fixture
def copy_url() -> Iterator[str]:
    url = copy_database(clicked_database(), derived_name("anonymized"))
    yield url
    drop_database(url)


def test_after_anonymization_pg_dump_has_no_personal_data_of_the_fixtures(copy_url, capsys):
    tests = needles(truth()["pii"])
    # the search finds them before (the facts are complete until the retention says otherwise)
    before = dump_hits(copy_url, tests)
    with psycopg.connect(copy_url) as conn:
        before_raw = raw_hits(conn, tests)
    assert {"public.l1_bank_transaction", "public.l1_counterparty", "public.queue_state",
            "public.l1_document_observation", "public.l2_suggestion"} <= set(before)
    assert before_raw
    # retention: the CLI as the user runs it
    assert main(["--database-url", copy_url, "retention", "anonymize", "--company", "odm", "--before", "2100-01-01",
                 "--shred", "--by", "pytest"]) == 0
    out = capsys.readouterr().out
    assert "anonymization_log" in out and "l0_raw" in out
    after = dump_hits(copy_url, tests)
    with psycopg.connect(copy_url) as conn:
        after_raw = raw_hits(conn, tests)
        counts = conn.execute("SELECT counts FROM anonymization_log ORDER BY anonymized_at DESC LIMIT 1").fetchone()[0]
        purged = conn.execute("SELECT count(*) FROM l0_purge_log WHERE reason LIKE 'anonimizacja osób%%'").fetchone()[0]
        shredded = conn.execute("SELECT count(*) FILTER (WHERE value_encrypted IS NOT NULL) FROM pseudonym_key "
                                "WHERE kind = 'person'").fetchone()[0]
    assert after == {} and after_raw == {}, {**after, **after_raw}
    assert counts["l0_raw"] == purged > 0 and shredded == 0
    for key in ("l1_counterparty", "l1_document_observation", "queue_state", "l2_suggestion", "control_set_item"):
        assert counts[key] > 0, key
    # idempotent, and the database is still whole: every check green
    assert main(["--database-url", copy_url, "retention", "anonymize", "--company", "odm", "--before", "2100-01-01",
                 "--shred"]) == 0
    assert "Nic do anonimizacji" in capsys.readouterr().out
    failed = [r for r in run_checks(copy_url) if not r.ok]
    assert not failed, [(r.name, r.detail) for r in failed]
