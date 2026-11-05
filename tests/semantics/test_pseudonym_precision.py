"""Pseudonymization precision (phase 2 review, blocking point 1): names of products, streets and companies
stay readable; every real person of the fixtures is still coded. Runs on the fresh test database.

Phase 3 review (docs/PRZEGLADY.md, points 1, 2, 6): addresses without "ul." (Polish and foreign) are coded,
a mask (PESEL, IBAN, phone) bounds the window of a person, capitalised acronyms are never a name, and a
person's window is a first name with one or two surnames - the word before a number is the street."""

from __future__ import annotations

import pytest

from kasa.core.documents.pdf import pdf_text
from kasa.core.pseudonym import (
    PSEUDONYM_RE,
    _encryption_key,
    detect,
    fold,
    load_context,
    pseudonym_for,
    pseudonymize_text,
)

from .helpers import FIXTURES, ground_truth, real_persons, surnames

# (text, expected detections as (kind, value)) - the sentences of the phase 3 review and their traps
REVIEW_SENTENCES = [
    # 1. addresses without a prefix
    ("Kwiatowa 7, 05-500 Piaseczno", [("address", "Kwiatowa 7, 05-500 Piaseczno")]),
    ("Czynsz za lokal Kwiatowa 7, 05-500 Piaseczno - listopad", [("address", "Kwiatowa 7, 05-500 Piaseczno")]),
    ("Mazowiecka 12 m. 3 Warszawa", [("address", "Mazowiecka 12 m. 3 Warszawa")]),
    ("Kwiatowa 7/3", [("address", "Kwiatowa 7/3")]),
    ("ul. Kwiatowa 7/3", [("address", "ul. Kwiatowa 7/3")]),
    ("Aleje Jerozolimskie 54 m. 3", [("address", "Aleje Jerozolimskie 54 m. 3")]),
    ("Jana Pawła II 12/4, 00-001 Warszawa", [("address", "Jana Pawła II 12/4, 00-001 Warszawa")]),
    ("Gdańska 5/3", [("address", "Gdańska 5/3")]),
    ("70 Sir John Rogerson's Quay", [("address", "70 Sir John Rogerson's Quay")]),
    ("500 Market Street, Suite 900", [("address", "500 Market Street, Suite 900")]),
    ("1600 Amphitheatre Parkway, Mountain View", [("address", "1600 Amphitheatre Parkway")]),
    ("Kwiatowa 7, 05-500 Piaseczno Jan Kowalski",
     [("address", "Kwiatowa 7, 05-500 Piaseczno"), ("person", "Jan Kowalski")]),
    # ... but not invoice numbers, periods, amounts or dates
    ("Czynsz pazdziernik 2026 FV 7/10/2026", []),
    ("Faktura 214/09/2026", []),
    ("Faktura 2026/10/0193 catering Gala ACME", []),
    ("Rachunek 4/2026 - konferansjerka Gala ACME 09.10", []),
    ("Polecenie zaplaty L/10/2026/0815 rata 10/36", []),
    ("Komornik KM 1187/26 - zajecie wynagrodzenia", []),
    ("Kwota oryg. 150,00 USD, kurs 3,9090 | Karta 5375 **** **** 4417", []),
    ("Okres 9/10", []),
    ("Szpitalna 5", []),
    ("Dublin 2, Ireland", []),
    ("STACJA PETROVA 214 LODZ PL | Karta 5375 **** **** 4417", []),
    # 2. a mask is a boundary, an acronym is never a name
    ("PESEL 44051401359 Marek Nowak", [("pesel", "44051401359"), ("person", "Marek Nowak")]),
    ("Marek Nowak PESEL 44051401359", [("person", "Marek Nowak"), ("pesel", "44051401359")]),
    ("IBAN PL61109010140000071219812874 Anna Nowak",
     [("iban", "PL61109010140000071219812874"), ("person", "Anna Nowak")]),
    ("tel. 500 085 853 Maja Lewandowicz", [("phone", "500 085 853"), ("person", "Maja Lewandowicz")]),
    ("BLIK Jan Kowalski", [("person", "Jan Kowalski")]),
    ("NIP Jan Kowalski FV 3/2026", [("person", "Jan Kowalski")]),
    # 6. window = first name + 1-2 surnames; the word before a number is the address
    ("Jan Kowalski Kwiatowa 7", [("person", "Jan Kowalski"), ("address", "Kwiatowa 7")]),
    ("Kwiatowa 7 Jan Kowalski", [("address", "Kwiatowa 7"), ("person", "Jan Kowalski")]),
    ("Jan Kowalski, Kwiatowa 7/3, 05-500 Piaseczno",
     [("person", "Jan Kowalski"), ("address", "Kwiatowa 7/3, 05-500 Piaseczno")]),
    ("Jan Kowalski Mazowiecka 12 m. 3 Warszawa",
     [("person", "Jan Kowalski"), ("address", "Mazowiecka 12 m. 3 Warszawa")]),
    ("Zwrot dla Anna Nowak zam. Lipowa 5", [("person", "Anna Nowak"), ("address", "Lipowa 5")]),
    ("Anna Maria Nowak", [("person", "Anna Maria Nowak")]),
    ("hostessa Maja Nowak 03.10", [("person", "Maja Nowak")]),
    ("Nowak Jan 12.10.2026", [("person", "Nowak Jan")]),
    ("Jan Kowalski 7", [("person", "Jan Kowalski")]),
]


@pytest.mark.parametrize(("text", "expected"), REVIEW_SENTENCES, ids=[t for t, _ in REVIEW_SENTENCES])
def test_review_sentences(text, expected):
    assert [(d.kind, d.value) for d in detect(text)] == expected


def test_same_person_same_code_with_and_without_an_address(conn, odm):
    with_address, hits_a = pseudonymize_text(conn, odm.id, "Jan Kowalski Kwiatowa 7")
    without, hits_b = pseudonymize_text(conn, odm.id, "Zwrot kosztow Jan Kowalski")
    full, hits_c = pseudonymize_text(conn, odm.id, "Jan Kowalski, Kwiatowa 7/3, 05-500 Piaseczno")
    codes = {h.pseudonym for h in (*hits_a, *hits_b, *hits_c) if h.kind == "person"}
    assert len(codes) == 1
    assert {h.kind for h in hits_a} == {"person", "address"} and {h.kind for h in hits_c} == {"person", "address"}
    for coded in (with_address, without, full):
        assert "Kowalski" not in coded and "Kwiatowa" not in coded and "05-500" not in coded
    # the address has a code of its own, stable as well
    again, hits_d = pseudonymize_text(conn, odm.id, "czynsz Kwiatowa 7")
    assert hits_d == [] and "Kwiatowa 7" in again      # a bare "Name 7" without a person is not an address
    assert [h.pseudonym for h in hits_a if h.kind == "address"] == \
        [h.pseudonym for h in pseudonymize_text(conn, odm.id, "Kwiatowa 7 Jan Kowalski")[1] if h.kind == "address"]
    coded, _ = pseudonymize_text(conn, odm.id, "PESEL 44051401359 Marek Nowak")
    assert "Nowak" not in coded and "44051401359" not in coded and "PESEL" in coded


def test_addresses_of_the_fixtures(conn, loaded):
    """Only real addresses are coded: none in titles, parties and company names; in invoice lines ODM's
    own office; in PDFs the sellers', the buyer's and the home address of the person on his bill."""
    context = load_context(conn, loaded.id)
    found: dict[str, set[str]] = {}
    for group, texts in _texts(conn, loaded.id).items():
        for text in texts:
            for d in detect(text, context):
                if d.kind == "address":
                    found.setdefault(group, set()).add(" ".join(d.value.split()))
    assert found == {
        "lines": {"ul. Chmielna 73 lok. 4"},
        "pdfs": {"70 Sir John Rogerson's Quay", "500 Market Street, Suite 900", "548 Folsom Street",
                 "ul. Chmielna 73 lok. 4 00-801 Warszawa", "ul. Lipowa 12/3 31-216 Kraków",
                 "ul. Rokicińska 154 92-412 Łódź", "ul. Stradomska 21 31-068 Kraków"},
    }


def test_home_addresses_of_persons_are_coded(conn, loaded):
    """Every person of ``pii`` with an address: in a transfer title, with and without "ul.", the street and
    the postal code disappear and the person keeps one code."""
    persons = [p for p in ground_truth()["pii"]["persons"] if p.get("address")]
    assert len(persons) >= 2
    for person in persons:
        address = person["address"]
        bare = address["line1"].removeprefix("ul. ")
        codes = set()
        for text in (f"{person['name']}, {address['line1']}, {address['line2']}",
                     f"Czynsz {bare}, {address['line2']} - {person['name']}",
                     f"Zwrot kosztow {person['name']} {bare}",
                     f"{person['name']} zwrot"):
            coded, hits = pseudonymize_text(conn, loaded.id, text)
            assert address["street"] not in coded and address["postal_code"] not in coded, coded
            assert person["name"].split()[-1] not in coded, coded
            codes |= {h.pseudonym for h in hits if h.kind == "person"}
        assert len(codes) == 1, (person["name"], codes)


def _person_tokens(truth) -> set[str]:
    return {fold(w) for name in real_persons(truth) for w in name.split()}


def _texts(conn, company_id) -> dict[str, list[str]]:
    titles = [r[0] for r in conn.execute(
        "SELECT title_raw FROM l1_bank_transaction WHERE company_id = %s AND superseded_by IS NULL AND title_raw <> ''",
        (company_id,))]
    parties = [r[0] for r in conn.execute(
        """SELECT DISTINCT counterparty_name_raw FROM l1_bank_transaction
            WHERE company_id = %s AND counterparty_name_raw IS NOT NULL""", (company_id,))]
    lines = [r[0] for r in conn.execute(
        """SELECT l.name FROM l1_document_line l JOIN l1_document d ON d.id = l.document_id
            WHERE d.company_id = %s AND d.superseded_by IS NULL""", (company_id,))]
    companies = sorted({inv["counterparty_name"] for inv in ground_truth()["invoices"]}
                       | {doc["seller"] for doc in ground_truth()["email_documents"]})
    pdfs = [pdf_text(p.read_bytes()) for p in sorted((FIXTURES / "email_pdfs").glob("*.pdf"))]
    return {"titles": titles, "parties": parties, "lines": lines, "companies": companies, "pdfs": pdfs}


def test_no_person_codes_for_products_streets_and_companies(conn, loaded):
    truth = ground_truth()
    persons = _person_tokens(truth)
    context = load_context(conn, loaded.id)
    false_persons = []
    for group, texts in _texts(conn, loaded.id).items():
        for text in texts:
            for d in detect(text, context):
                if d.kind == "person" and not ({fold(w) for w in d.value.split()} & persons):
                    false_persons.append((group, d.value, d.method))
    assert false_persons == []
    # invoice lines and company names without a person stay as they are
    for line in _texts(conn, loaded.id)["lines"]:
        assert not [d for d in detect(line, context) if d.kind == "person"], line
    for name in ("Google Workspace Business Standard (Flexible Plan)", "70 Sir John Rogerson's Quay",
                 "500 Market Street, Suite 900", "Hotel Wawel Grand sp. z o.o.", "Studio Graficzne Kreska sp. z o.o.",
                 "Dwór Kalina Event sp. z o.o.", "Smaki Wisły Catering sp. z o.o.", "Scena Pro sp. z o.o."):
        assert not [d for d in detect(name, context) if d.kind == "person"], name


def test_every_real_person_is_still_coded(conn, loaded):
    truth = ground_truth()
    names = surnames(truth)
    for group, texts in _texts(conn, loaded.id).items():
        for text in texts:
            coded, _ = pseudonymize_text(conn, loaded.id, text)
            words = set(fold(coded).replace("-", " ").split())
            assert not (names & {w.strip(".,;:()|/") for w in words}), (group, coded)


def test_person_keys_are_only_real_persons(conn, loaded):
    rows = conn.execute(
        """
        SELECT pgp_sym_decrypt(value_encrypted, %s), basis FROM pseudonym_key
         WHERE company_id = %s AND kind = 'person'
        """,
        (_encryption_key(), loaded.id),
    ).fetchall()
    persons = _person_tokens(ground_truth())
    assert rows and all({fold(w) for w in value.split()} & persons for value, _ in rows), rows
    assert all(basis in ("registered", "first_name", "context", "known_person") for _, basis in rows)
    real = {" ".join(sorted(fold(w) for w in n.split())) for n in real_persons(ground_truth())}
    keys = {" ".join(sorted(fold(w) for w in value.split())) for value, _ in rows}
    assert keys <= real and len(keys) >= 20


def test_old_keys_without_evidence_are_not_used(conn, odm):
    # a key from before 0008 (no basis), e.g. a product taken for a person by the old detector
    legacy = pseudonym_for(conn, odm.id, "person", "Google Workspace")
    assert PSEUDONYM_RE.fullmatch(legacy)
    assert "google workspace" not in load_context(conn, odm.id).persons
    coded, hits = pseudonymize_text(conn, odm.id, "GOOGLE *Workspace odmpar | Google Workspace Business")
    assert hits == [] and "Google Workspace" in coded
    # a legacy key with a first name is still a person
    pseudonym_for(conn, odm.id, "person", "Hanna Drozdowska")
    assert "drozdowska hanna" in load_context(conn, odm.id).persons
