"""Sensitive words: stems with Polish inflection (review point 15), kept in the database (sensitive_term)."""

from __future__ import annotations

import contextlib

import psycopg
import pytest

from kasa.core.extension import get_extension
from kasa.core.sensitive import (
    add_human_term,
    code_terms,
    find_sensitive,
    load_terms,
    phrase_stem,
    stem,
    sync_sensitive_terms,
)


@contextlib.contextmanager
def db_error(conn: psycopg.Connection, sqlstate: str):
    with pytest.raises(psycopg.Error) as excinfo:
        with conn.transaction():
            yield
    assert excinfo.value.sqlstate == sqlstate, f"{excinfo.value.sqlstate}: {excinfo.value}"


def test_inflected_forms_are_caught_and_ordinary_words_are_not():
    terms = code_terms()                                   # the core list alone
    for word in ("alimentów", "komornicze", "związkowej", "chorobowego", "zdrowotna",
                 "ZWIAZKOWEJ", "komorniczego", "dłużnika", "leczenia", "Alimenty"):
        assert find_sensitive(word, terms), word
    for word in ("zapłata", "faktura", "Zaplata za FV 5/09/2026", "Wynagrodzenie 09/2026", "Skladki ZUS za 09/2026",
                 "w związku z fakturą", "związany z eventem", "Prowadzenie konferansjerki", "Catering - Gala ACME"):
        assert not find_sensitive(word, terms), word


def test_stemmer_endings():
    assert stem("alimentów") == stem("alimenty") == "aliment"
    assert stem("zdrowie") == "zdrow" and stem("zdrowotna").startswith("zdrow")
    assert phrase_stem("zajęcie wynagrodzenia") == "zajec wynagrodzeni"
    assert stem("faktura") == "faktur" and stem("kot") == "kot"      # at least 4 letters stay


def test_phrases_match_consecutive_words_only():
    terms = code_terms(get_extension("odm").sensitive_words)
    assert "zajęcie wynagrodzenia" in find_sensitive("Komornik KM 1187/26 - zajecie wynagrodzenia dluznika", terms)
    assert "zajęcie wynagrodzenia" not in find_sensitive("zajęcie sali, wynagrodzenie hostess", terms)
    assert "zwolnienie lekarskie" in find_sensitive("zwolnienia lekarskiego 3 dni", terms)


def test_terms_live_in_the_database_and_are_seeded_once(conn, make):
    company_id = make.company(extension_slug="odm")
    words = get_extension("odm").sensitive_words
    first = sync_sensitive_terms(conn, [(company_id, words)])
    assert first > 0 and sync_sensitive_terms(conn, [(company_id, words)]) == 0
    rows = conn.execute(
        "SELECT source::text, count(*) FROM sensitive_term WHERE company_id IS NULL OR company_id = %s GROUP BY 1",
        (company_id,),
    ).fetchall()
    assert dict(rows)["core"] >= 4 and dict(rows).get("extension", 0) >= 1
    categories = dict(conn.execute(
        "SELECT term, category FROM sensitive_term WHERE term IN ('komornik', 'zdrowie', 'alimenty')").fetchall())
    assert categories == {"komornik": "enforcement", "zdrowie": "health", "alimenty": "family"}


def test_a_word_added_by_a_person_is_used_and_can_only_be_retired(conn, make):
    company_id = make.company(extension_slug="odm")
    assert not find_sensitive("Konsultacji psychologicznej 10/2026", load_terms(conn, company_id, []))
    add_human_term(conn, company_id, "konsultacja psychologiczna", created_by="artur")
    terms = load_terms(conn, company_id, [])
    assert "konsultacja psychologiczna" in find_sensitive("Konsultacji psychologicznej 10/2026", terms)
    other = make.company(extension_slug="odm")
    assert "konsultacja psychologiczna" not in [t.term for t in load_terms(conn, other, [])]
    with db_error(conn, "KS002"):
        conn.execute("DELETE FROM sensitive_term WHERE company_id = %s", (company_id,))
    with db_error(conn, "KS011"):
        conn.execute("UPDATE sensitive_term SET term = 'x' WHERE company_id = %s", (company_id,))
    conn.execute("UPDATE sensitive_term SET retired_at = now() WHERE company_id = %s", (company_id,))
    assert "konsultacja psychologiczna" not in [t.term for t in load_terms(conn, company_id, [])]


def test_extension_uses_the_same_stems():
    odm = get_extension("odm")
    assert odm.is_sensitive("Komornik Sądowy przy Sądzie Rejonowym") and odm.is_sensitive("składki związkowej")
    assert not odm.is_sensitive("Zaplata za FV 5/09/2026")
