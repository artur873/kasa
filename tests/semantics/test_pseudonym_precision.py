"""Pseudonymization precision (phase 2 review, blocking point 1): names of products, streets and companies
stay readable; every real person of the fixtures is still coded. Runs on the fresh test database."""

from __future__ import annotations

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
