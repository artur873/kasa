"""Line semantics: proper name, client, dates, city and service kind of invoice lines and sheet rows."""

from __future__ import annotations

import psycopg

from kasa.core.semantics.lines import internal_record_text
from kasa.core.semantics.mock import extract_dates, proper_name
from kasa.core.semantics.run import run_semantics
from kasa.core.sensitive import fold

from .helpers import ground_truth, pm_sheet_people


def _same_name(candidate: str | None, event_name: str) -> bool:
    """'Gali ACME 2026' ~ 'Gala ACME 2026': every word of the candidate inflects a word of the event name."""
    if not candidate:
        return False

    def close(a: str, b: str) -> bool:
        return a == b or (min(len(a), len(b)) >= 3 and a[:max(3, min(len(a), len(b)) - 1)] ==
                          b[:max(3, min(len(a), len(b)) - 1)])

    words = [fold(w) for w in candidate.split()]
    target = [fold(w) for w in event_name.split()]
    return len(words) >= 2 and all(any(close(w, t) for t in target) for w in words)


def _line_readings(conn, company_id, direction: str) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        return cur.execute(
            """
            SELECT l.name, s.entities, s.method::text AS method, s.input_pseudonymized
              FROM l1_semantics s JOIN l1_document_line l ON l.id = s.subject_id
              JOIN l1_document d ON d.id = l.document_id
             WHERE s.company_id = %s AND s.subject_type = 'line' AND s.superseded_by IS NULL
               AND d.direction::text = %s
            """,
            (company_id, direction),
        ).fetchall()


def test_sales_lines_with_event_names_give_name_city_and_dates(conn, loaded):
    run_semantics(conn, loaded, "lines")
    events = ground_truth()["events"]
    readings = _line_readings(conn, loaded.id, "sales")
    assert len(readings) == 14
    checked = 0
    for reading in readings:
        words = fold(reading["name"]).replace(",", " ").split()
        # the event of a line: every word of its sheet label ("Gala ACME", "Nordis Gdańsk") starts a word of the line
        event = next((e for e in events
                      if all(any(w.startswith(fold(label)[:3]) for w in words)
                             for label in e["pm_sheet_label"].split())), None)
        if event is None:
            continue
        checked += 1
        entities = reading["entities"]
        candidate = (entities["candidate_name"] or {}).get("untrusted_text")
        assert _same_name(candidate, event["name"]), (reading["name"], candidate, event["name"])
        assert fold((entities["client_hint"] or {}).get("untrusted_text") or "") in fold(event["client"])
        dates = set(entities["dates"])
        assert dates and dates <= {event["date_from"], event["date_to"]} | set(
            d for d in dates if event["date_from"] <= d <= event["date_to"]), (reading["name"], dates)
        if entities["city"]:
            assert entities["city"] == event["city"], (reading["name"], entities["city"])
    assert checked == 8               # Gala ACME (FV 1, FV 3, FV 7 x2), Nordis Gdańsk (FV 2, KOR 1), Wrocław, Helios
    by_name = {r["name"]: r["entities"] for r in readings}
    gala = by_name["Organizacja Gali ACME 2026 (Kraków, 08-10.10.2026) - usługa kompleksowa wg umowy 14/2026"]
    assert gala["city"] == "Kraków" and gala["period"] == {"from": "2026-10-08", "to": "2026-10-10"}
    helios = by_name["Event management services - Helios Partner Summit 2026, Warsaw, 15-16.10.2026"]
    assert helios["city"] == "Warszawa" and helios["candidate_name"] == {"untrusted_text": "Helios Partner Summit 2026"}
    # a collective invoice line has no proper name - only the month
    collective = by_name["Obsługa eventów - październik 2026 (zamówienie ramowe NR/2026/04)"]
    assert collective["candidate_name"] is None and collective["month"] == "2026-10"
    # the core never calls it an event: no such key in the entities
    assert all("event" not in key for r in readings for key in r["entities"])


def test_purchase_lines_get_a_service_kind(conn, loaded):
    run_semantics(conn, loaded, "lines")
    kinds = {r["name"]: r["entities"]["service_kind"] for r in _line_readings(conn, loaded.id, "purchase")}
    assert kinds["Catering - Gala ACME 2026, kolacja galowa 09.10.2026"] == "catering"
    assert kinds["Nocleg w pokoju dwuosobowym, 14 pokoi (08-10.10.2026)"] == "hotel"
    assert kinds["Nagłośnienie, oświetlenie i multimedia - Gala ACME 2026, Kraków 08-10.10.2026"] == "technika"
    assert kinds["Prowadzenie konferansjerki podczas Gali ACME 2026 (09.10.2026, Kraków)"] == "personel"
    assert kinds["Wynajem hali B z zapleczem - Roadshow Nordis Łódź 24.10.2026"] == "sala"
    assert kinds["Google Workspace Business Standard (Flexible Plan)"] == "saas"
    assert kinds["ON EFECTA DIESEL"] == "transport"
    # the office address in a line goes to the model as an address code, not as text
    office = next(r for r in _line_readings(conn, loaded.id, "purchase") if r["name"].startswith("Najem lokalu"))
    assert "Chmielna" not in office["input_pseudonymized"] and "A-" in office["input_pseudonymized"]


def test_sheet_rows_go_without_the_person_and_the_amount(conn, loaded):
    report = run_semantics(conn, loaded, "internal")
    assert report.by_scope["internal"].calls == 20
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        rows = cur.execute(
            """
            SELECT r.fields, r.mapping_key, r.evidence, s.input_pseudonymized, s.entities
              FROM l1_semantics s JOIN l1_internal_record r ON r.id = s.subject_id WHERE s.company_id = %s
            """,
            (loaded.id,),
        ).fetchall()
    people = {fold(w) for p in pm_sheet_people() for w in p.split()}
    for row in rows:
        text = internal_record_text(loaded, row)
        assert str(row["fields"]["Osoba"]) not in text and str(row["fields"]["Kwota"]) not in text
        seen_words = set(fold(row["input_pseudonymized"]).replace("|", " ").split())
        assert not (seen_words & people), row["input_pseudonymized"]
        assert "tel. 500" not in row["input_pseudonymized"]
    hostess = next(r for r in rows if "hostessa" in (r["fields"].get("Uwagi") or ""))
    assert hostess["entities"]["service_kind"] == "personel" and hostess["entities"]["candidate_name"]


def test_dates_and_proper_names_by_code():
    assert extract_dates("Nocleg 11-15.06.2026 Kraków") == {
        "dates": ["2026-06-11", "2026-06-15"], "period": {"from": "2026-06-11", "to": "2026-06-15"}, "month": None}
    assert extract_dates("okres 15.09-14.10.2026")["period"] == {"from": "2026-09-15", "to": "2026-10-14"}
    assert extract_dates("abonament 09.2026")["month"] == "2026-09"
    assert proper_name("Obsługa Gali ACME 12.06 Kraków") == ("Gali ACME", "ACME")
    assert proper_name("Wynajem ekranu LED P3,9 (12 m²) z obsługą") == (None, None)
    assert proper_name("Event management services - Helios Partner Summit 2026, Warsaw") == (
        "Helios Partner Summit 2026", "Helios")
