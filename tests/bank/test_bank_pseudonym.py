"""Pseudonymization gate: no surname, IBAN or phone of the fixtures survives; stable codes; sensitive words."""

from __future__ import annotations

import contextlib
import csv
import re

import psycopg
import pytest

from bank_paths import FIXTURES
from kasa.core.extension import get_extension
from kasa.core.pseudonym import (
    PseudonymContext,
    depseudonymize,
    detect,
    fold,
    is_sensitive,
    load_context,
    pseudonym_for,
    pseudonymize_text,
    register_person,
    sensitive_hits,
    sensitive_words_for,
    value_hash,
)


@contextlib.contextmanager
def db_error(conn: psycopg.Connection, sqlstate: str):
    with pytest.raises(psycopg.Error) as excinfo:
        with conn.transaction():
            yield
    assert excinfo.value.sqlstate == sqlstate, f"{excinfo.value.sqlstate}: {excinfo.value}"


def _pm_sheet_people() -> list[str]:
    with (FIXTURES / "internal" / "arkusz_pm_2026-10.csv").open(encoding="utf-8-sig") as handle:
        return [row["Osoba"] for row in csv.DictReader(handle, delimiter=";")]


def _forbidden(ground_truth) -> tuple[set[str], set[str], set[str]]:
    surnames = {fold(name.split()[0]) for name in _pm_sheet_people()}            # "Nazwisko Imię"
    surnames |= {fold(p["name"].split()[-1]) for p in ground_truth["pii"]["persons"]}
    surnames |= {fold(p["person"].split()[-1]) for p in ground_truth["pii"]["business_names_with_person"]}
    ibans = {p["iban"] for p in ground_truth["pii"]["persons"]}
    phones = {re.sub(r"\D", "", f)[-9:] for p in ground_truth["pii"]["persons"] for f in p["phone_formats"]}
    return surnames, ibans, phones


def _leaks(text: str | None, surnames: set[str], ibans: set[str], phones: set[str]) -> list[str]:
    if not text:
        return []
    folded = fold(text)
    words = set(re.findall(r"[^\W\d_]+", folded))
    digits = re.sub(r"\D", "", text)
    leaks = [s for s in surnames if s in words]
    leaks += [i for i in ibans if i in re.sub(r"\s", "", text) or i[2:] in digits]
    leaks += [p for p in phones if p in digits]
    return leaks


def test_no_title_or_counterparty_leaks_after_ingestion(conn, ingested, rows_of, ground_truth):
    company, _ = ingested
    surnames, ibans, phones = _forbidden(ground_truth)
    rows = rows_of(company.id)
    assert len(rows) == 87
    leaks = {}
    for row in rows:
        for column in ("title_pseudonymized", "counterparty_pseudonym", "counterparty_iban_pseudonym"):
            found = _leaks(row[column], surnames, ibans, phones)
            if found:
                leaks[(row["entry_reference"], column)] = (row[column], found)
    assert not leaks, leaks
    # ...while the raw facts keep everything (the facts are complete, only the model view is reduced)
    raw_leaks = sum(bool(_leaks(r["title_raw"], surnames, ibans, phones)) for r in rows)
    assert raw_leaks >= 17                                   # BLIK phones and the bailiff title


def test_pseudonyms_are_stable_across_formats_and_word_order(conn, company):
    a = pseudonym_for(conn, company.id, "phone", "+48 500 085 853")
    assert a.startswith("T-")
    assert pseudonym_for(conn, company.id, "phone", "500-085-853") == a
    assert pseudonym_for(conn, company.id, "phone", "500085853") == a
    p = register_person(conn, company.id, "Lewandowicz Maja")
    assert p.startswith("P-") and pseudonym_for(conn, company.id, "person", "MAJA LEWANDOWICZ") == p
    iban = pseudonym_for(conn, company.id, "iban", "PL52 1023 0000 6941 1437 5921 4465")
    assert pseudonym_for(conn, company.id, "iban", "PL52102300006941143759214465") == iban
    assert conn.execute(
        "SELECT count(*) FROM pseudonym_key WHERE company_id = %s", (company.id,)).fetchone() == (3,)
    # the key stores only the HMAC and the encrypted value, never the plain value
    row = conn.execute(
        "SELECT value_hash, value_encrypted FROM pseudonym_key WHERE company_id = %s AND kind = 'person'",
        (company.id,)).fetchone()
    assert row[0] == value_hash("person", "lewandowicz maja")
    assert b"Lewandowicz" not in bytes(row[1])


def test_sheet_persons_are_found_in_any_form(conn, company):
    for person in _pm_sheet_people():
        register_person(conn, company.id, person)
    text = ("Lewandowicz, 03.10, Nordis, 400 | hostessa Maja Lewandowicz tel. 500-085-853 | "
            "rozliczenie Kuczyńskiej i WESOŁOWSKA PAULA, kontakt jan.kowalski@example.com, "
            "PESEL 44051401359, ul. Długa 12/4, 00-238 Warszawa, konto 52 1023 0000 6941 1437 5921 4465")
    pseudonymized, hits = pseudonymize_text(conn, company.id, text)
    kinds = sorted({h.kind for h in hits})
    assert kinds == ["address", "email", "iban", "person", "pesel", "phone"]
    for leaked in ("Lewandowicz", "Kuczyńskiej", "WESOŁOWSKA", "500-085-853", "kowalski", "44051401359",
                   "Długa", "6941"):
        assert leaked not in pseudonymized, (leaked, pseudonymized)
    maja = register_person(conn, company.id, "Maja Lewandowicz")
    assert pseudonymized.count(maja) == 2                     # surname alone and full name: one person
    assert "Nordis" in pseudonymized and "03.10" in pseudonymized and "400" in pseudonymized


def test_detector_knows_company_words_and_safe_words():
    context = PseudonymContext()
    context.add_company_name("Kadr Studio Paweł Wierzbicki")
    context.add_company_name("Dwór Kalina Event sp. z o.o.")
    assert "kadr" in context.company_tokens and "pawel" not in context.company_tokens
    assert {"dwor", "event"} <= context.company_tokens         # a capital company name is taken whole
    assert "kalina" not in context.company_tokens             # ...but a first name never shields a surname
    for text in ("Nordis Gdansk hostessa 03.10", "Prowizja za przelew Express Elixir",
                 "rezerwacja Dwor Kalina, termin 14.11.2026", "Gala ACME koordynacja"):
        assert [d for d in detect(text, context) if d.kind == "person"] == [], text
    persons = [d.value for d in detect("zajecie wynagrodzenia dluznika Tomasz Grabowiecki za 09/2026", context)]
    assert persons == ["Tomasz Grabowiecki"]
    assert [d.value for d in detect("zwrot dla Jan Kowalski", context)] == ["Jan Kowalski"]
    assert [d.method for d in detect("przelew Zenobiusz Brzęczyszczykiewicz", context)] == ["first_name"]
    # two capitalized words without evidence are not a person (product, street, company names)...
    for text in ("Google Workspace Business Standard", "70 Sir John Rogerson's Quay", "Scena Pro technika",
                 "przelew Grzmot Wichura"):
        assert [d for d in detect(text, context) if d.kind == "person"] == [], text
    # ...a first name in another case, an honorific, BLIK or a phone/PESEL after the name are evidence
    assert [d.value for d in detect("dla Pana Tomasza Grabowieckiego", context)] == ["Tomasza Grabowieckiego"]
    assert [(d.value, d.method) for d in detect("zwrot - Pani Wichura", context)] == [("Wichura", "context")]
    assert [d.method for d in detect("BLIK Grzmot Wichura", context)] == ["context"]
    assert [d.method for d in detect("Grzmot Wichura, tel. 500 111 222", context) if d.kind == "person"] == [
        "context"]
    assert [d.value for d in detect("Kadr Studio Paweł Wierzbicki", context)] == ["Paweł Wierzbicki"]


def test_depseudonymize_only_for_internal_roles(conn, company):
    text, _ = pseudonymize_text(conn, company.id, "Zwrot dla Anna Mazur, tel. 500 975 639")
    assert "Mazur" not in text
    assert depseudonymize(conn, company.id, text, role="owner") == "Zwrot dla Anna Mazur, tel. 500 975 639"
    with pytest.raises(PermissionError):
        depseudonymize(conn, company.id, text, role="agent")


def test_context_is_rebuilt_from_the_database(conn, ingested):
    company, _ = ingested
    context = load_context(conn, company.id)
    assert len(context.persons) >= 22 and "scena" in context.company_tokens
    assert "lewandowicz maja" in context.persons


def test_sensitive_words_match_by_stem():
    words = sensitive_words_for(get_extension("odm").sensitive_words)
    assert "komornik" in words and "zajęcie wynagrodzenia" in words
    hits = sensitive_hits("Komornik KM 1187/26 - zajecie wynagrodzenia dluznika", words)
    assert {"komornik", "zajęcie wynagrodzenia"} <= set(hits)
    for text in ("zajęcie komornicze 10/2026", "Alimenty za październik", "zwolnienia lekarskiego 3 dni",
                 "składka związku zawodowego", "koszty leczenia", "ZDROWIE PLUS abonament"):
        assert is_sensitive(text, words), text
    for text in ("Przelew wlasny - zasilenie rachunku Millennium", "LP/118/2026 Helios Partner Summit",
                 "Karta 5375 **** **** 4417", "Wynagrodzenie 09/2026", "Skladki ZUS za 09/2026"):
        assert not is_sensitive(text, words), text


def test_only_the_bailiff_transfer_is_sensitive(conn, ingested, rows_of, ground_truth):
    company, _ = ingested
    sensitive = {r["entry_reference"] for r in rows_of(company.id, "t.sensitive")}
    assert sensitive == set(ground_truth["pii"]["sensitive_transactions"])
    [row] = rows_of(company.id, "t.sensitive")
    assert "komornik" in row["extracted"]["sensitive_words"]


def test_pseudonym_keys_are_never_deleted_or_rewritten(conn, company):
    pseudonym_for(conn, company.id, "email", "jan@example.com")
    with db_error(conn, "KS002"):
        conn.execute("DELETE FROM pseudonym_key WHERE company_id = %s", (company.id,))
    with db_error(conn, "KS008"):
        conn.execute("UPDATE pseudonym_key SET pseudonym = 'E-0000' WHERE company_id = %s", (company.id,))
    shredded = conn.execute(
        "UPDATE pseudonym_key SET value_encrypted = NULL, shredded_at = now() WHERE company_id = %s",
        (company.id,)).rowcount
    assert shredded == 1
    text, _ = pseudonymize_text(conn, company.id, "kontakt jan@example.com")
    assert depseudonymize(conn, company.id, text, role="owner") == text   # nobody can reverse a shredded key
