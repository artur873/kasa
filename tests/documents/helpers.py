"""Helpers shared by the documents tests and the gate 2 documents test (no fixtures here)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures" / "odm"
ODM_NIP = "5342650778"


def ground_truth() -> dict[str, Any]:
    return json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))


def ksef_index() -> dict[str, Any]:
    return json.loads((FIXTURES / "ksef" / "index.json").read_text(encoding="utf-8"))


def expected_pdfs() -> dict[str, Any]:
    return json.loads((FIXTURES / "email_pdfs" / "expected.json").read_text(encoding="utf-8"))["documents"]


def load_nbp_rates(conn: psycopg.Connection) -> None:
    """Fixture NBP table A as shared reference data (company_id NULL, source nbp_a)."""
    rates = json.loads((FIXTURES / "reference" / "nbp_rates.json").read_text(encoding="utf-8"))["rates"]
    for currency, items in rates.items():
        for item in items:
            conn.execute(
                """
                INSERT INTO l1_fx_rate (rate_date, currency, rate, table_ref, source, method, created_by)
                VALUES (%s, %s, %s, %s, 'nbp_a', 'code', 'pytest')
                ON CONFLICT DO NOTHING
                """,
                (item["date"], currency, item["mid"], item["table"]),
            )


def current_documents(conn: psycopg.Connection, company_id) -> list[tuple]:
    return conn.execute(
        "SELECT id, number_raw, source FROM l1_document WHERE company_id = %s AND superseded_by IS NULL",
        (company_id,),
    ).fetchall()


def observations_of(conn: psycopg.Connection, document_id) -> list[tuple]:
    """(source, linked_by, locator) of every observation in the document's lineage."""
    return conn.execute(
        """
        SELECT o.source, o.linked_by::text, o.locator
          FROM document_lineage l JOIN l1_document_observation o ON o.document_id = l.document_id
         WHERE l.head_id = (SELECT head_id FROM document_lineage WHERE document_id = %s)
         ORDER BY o.created_at, o.source
        """,
        (document_id,),
    ).fetchall()


ROW_COUNT_TABLES = (
    "l0_raw", "l1_document", "l1_document_line", "l1_document_observation", "l1_internal_record",
    "l1_pdf_extraction", "l2_suggestion", "import_mapping_profile",
)


def row_counts(conn: psycopg.Connection, company_id) -> dict[str, int]:
    counts = {}
    for table in ROW_COUNT_TABLES:
        counts[table] = conn.execute(f"SELECT count(*) FROM {table} WHERE company_id = %s", (company_id,)).fetchone()[0]
    if conn.execute("SELECT to_regclass('l1_counterparty') IS NOT NULL").fetchone()[0]:
        counts["l1_counterparty"] = conn.execute(
            "SELECT count(*) FROM l1_counterparty WHERE company_id = %s", (company_id,)
        ).fetchone()[0]
    return counts
