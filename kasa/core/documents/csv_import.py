"""Import wizard for tabular files (CSV here, XLSX in ``xlsx_import``): office exports and internal sheets.

1. The file is read as it is: encoding (UTF-8 with or without BOM, else
   Windows-1250), separator (``;`` ``,`` tab ``|``) and the header row are detected.
2. Columns are mapped with the extension's ``import_mappings`` (``document`` for an
   office export, ``pm_sheet`` for the PM sheet ...). The mapping a person confirmed
   for files with these headers is saved in ``import_mapping_profile``; the next file
   with the same headers is imported without questions.
3. Rows become documents (``target: document`` -> :func:`upsert_document`, with
   deduplication) or internal records (``target: internal_record`` ->
   ``l1_internal_record`` with the whole row in ``fields``).
"""

from __future__ import annotations

import codecs
import csv
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.documents.facts import InvoiceFacts, Party
from kasa.core.documents.ingest import (
    IngestReport,
    Observation,
    capabilities,
    get_company_nip,
    relink_pending,
    upsert_document,
)
from kasa.core.documents.privacy import person_pseudonym
from kasa.core.extension import ColumnMapping, ColumnSpec, MappingError, get_extension, header_key
from kasa.core.normalize import normalize_nip
from kasa.core.raw_store import store_raw

PARSER_VERSION = 1
DELIMITERS = (";", "\t", ",", "|")
CONTENT_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

AskFn = Callable[[str, ColumnSpec, list[str]], str | None]


class MappingIncomplete(ValueError):
    """Required fields have no column in the file; the wizard must ask (or get ``answers``)."""

    def __init__(self, mapping_key: str, missing: list[str], headers: list[str]):
        self.mapping_key = mapping_key
        self.missing = missing
        self.headers = headers
        super().__init__(
            f"Mapowanie {mapping_key!r}: brak kolumn dla pól {missing}. Nagłówki pliku: {headers}. "
            "Wskaż kolumny (--map pole=Nagłówek) - wybór zostanie zapisany dla kolejnych plików."
        )


@dataclass
class TableFile:
    headers: list[str]
    rows: list[tuple[int, dict[str, Any]]]          # (row number in the file, header -> cell)
    file_format: dict[str, Any] = field(default_factory=dict)
    sheet: str | None = None

    @property
    def kind(self) -> str:
        return self.file_format.get("type", "csv")


# ---------------------------------------------------------------------------
# Reading CSV
# ---------------------------------------------------------------------------


def detect_encoding(payload: bytes) -> str:
    if payload.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return "cp1250"
    return "utf-8"


def detect_delimiter(header_line: str) -> str:
    counts = {d: header_line.count(d) for d in DELIMITERS}
    best = max(DELIMITERS, key=lambda d: (counts[d], -DELIMITERS.index(d)))
    return best if counts[best] else ";"


def read_csv(payload: bytes, *, header_row: int = 1) -> TableFile:
    encoding = detect_encoding(payload)
    text = payload.decode(encoding)
    lines = text.splitlines()
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return TableFile([], [], {"type": "csv", "encoding": encoding})
    header_index = header_row - 1
    delimiter = detect_delimiter(lines[header_index] if header_index < len(lines) else non_empty[0])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    headers: list[str] = []
    rows: list[tuple[int, dict[str, Any]]] = []
    for number, cells in enumerate(reader, start=1):
        if number < header_row:
            continue
        if number == header_row:
            headers = [cell.strip() for cell in cells]
            continue
        if not any(cell.strip() for cell in cells):
            continue
        rows.append((number, {headers[i] if i < len(headers) else f"_col{i + 1}": cell
                              for i, cell in enumerate(cells)}))
    return TableFile(headers, rows, {"type": "csv", "encoding": encoding, "delimiter": delimiter,
                                     "header_row": header_row,
                                     "line_ending": "CRLF" if "\r\n" in text else "LF"})


def read_table(payload: bytes, filename: str, mapping: ColumnMapping) -> TableFile:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix == "xlsx":
        from kasa.core.documents.xlsx_import import read_xlsx

        table = read_xlsx(payload, sheet=mapping.sheet, header_row=mapping.header_row)
    else:
        table = read_csv(payload, header_row=mapping.header_row)
    if table.kind not in mapping.file_types:
        raise MappingError(f"Mapowanie przyjmuje pliki {mapping.file_types}, a plik jest typu {table.kind}")
    return table


# ---------------------------------------------------------------------------
# Profiles (the wizard's memory)
# ---------------------------------------------------------------------------


def file_signature(headers: list[str]) -> str:
    """Identity of a kind of file: its normalized headers in order."""
    return "|".join(header_key(h) for h in headers if h and h.strip())


def _auto_map(mapping: ColumnMapping, headers: list[str]) -> dict[str, str]:
    by_key = {header_key(h): h for h in headers if h and h.strip()}
    found = {}
    for name, spec in mapping.columns.items():
        for candidate in spec.headers():
            if header_key(candidate) in by_key:
                found[name] = by_key[header_key(candidate)]
                break
    return found


def resolve_column_map(
    conn: psycopg.Connection,
    company_id: UUID,
    mapping_key: str,
    mapping: ColumnMapping,
    table: TableFile,
    *,
    answers: dict[str, str] | None = None,
    ask: AskFn | None = None,
    created_by: str = "import",
) -> tuple[dict[str, str], UUID, bool]:
    """Column map for this file: saved profile, else extension mapping + answers. Returns (map, profile, new)."""
    signature = file_signature(table.headers)
    profile = conn.execute(
        """
        SELECT id, column_map FROM import_mapping_profile
         WHERE company_id = %s AND mapping_key = %s AND file_signature = %s AND superseded_by IS NULL
        """,
        (company_id, mapping_key, signature),
    ).fetchone()
    if profile is not None and not answers:
        return dict(profile[1]), profile[0], False

    column_map = _auto_map(mapping, table.headers) if profile is None else dict(profile[1])
    by_key = {header_key(h): h for h in table.headers if h and h.strip()}
    for name, header in (answers or {}).items():
        if name not in mapping.columns:
            raise MappingError(f"Mapowanie {mapping_key!r} nie ma pola {name!r} (pola: {list(mapping.columns)})")
        if header_key(header) not in by_key:
            raise MappingError(f"Plik nie ma kolumny {header!r} (nagłówki: {table.headers})")
        column_map[name] = by_key[header_key(header)]
    missing = [name for name, spec in mapping.columns.items() if spec.required and name not in column_map]
    if missing and ask is not None:
        for name in list(missing):
            header = ask(name, mapping.columns[name], table.headers)
            if header and header_key(header) in by_key:
                column_map[name] = by_key[header_key(header)]
                missing.remove(name)
    if missing:
        raise MappingIncomplete(mapping_key, missing, table.headers)

    if profile is not None and dict(profile[1]) == column_map:
        return column_map, profile[0], False
    with conn.transaction():
        new_id = conn.execute(
            """
            INSERT INTO import_mapping_profile (company_id, mapping_key, file_signature, headers, file_format,
                                                column_map, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (company_id, mapping_key, signature + ("~new" if profile is not None else ""),
             Jsonb(table.headers), Jsonb(table.file_format), Jsonb(column_map), created_by),
        ).fetchone()[0]
        if profile is not None:
            conn.execute("UPDATE import_mapping_profile SET superseded_by = %s WHERE id = %s", (new_id, profile[0]))
            conn.execute("UPDATE import_mapping_profile SET file_signature = %s WHERE id = %s", (signature, new_id))
    return column_map, new_id, True


def apply_column_map(mapping: ColumnMapping, column_map: dict[str, str], row: dict[str, Any]):
    """Map a row with the profile: every field reads the header the profile names."""
    renamed = {mapping.columns[name].source: row.get(header) for name, header in column_map.items()
               if name in mapping.columns}
    return mapping.map_row(renamed)


# ---------------------------------------------------------------------------
# Rows -> facts
# ---------------------------------------------------------------------------


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return str(Decimal(repr(value)))
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _direction_and_kind(text: str | None, direction: str | None) -> tuple[str | None, str]:
    folded = (text or "").casefold()
    kind = "vat"
    if "korekt" in folded:
        kind = "correction"
    elif "zalicz" in folded:
        kind = "advance"
    elif "import" in folded:
        kind = "foreign"
    elif "paragon" in folded:
        kind = "receipt"
    elif "rachun" in folded:
        kind = "bill"
    elif "proforma" in folded:
        kind = "proforma"
    elif "wewn" in folded:
        kind = "internal"
    hint = (direction or "").casefold()
    if hint in ("sales", "sprzedaż", "sprzedaz", "s") or "sprzeda" in hint:
        return "sales", kind
    if hint in ("purchase", "zakup", "z", "koszt") or "zakup" in hint:
        return "purchase", kind
    if "sprzeda" in folded:
        return "sales", kind
    if "zakup" in folded or "import" in folded or "koszt" in folded:
        return "purchase", kind
    return None, kind


def _nip_or_foreign(value: str | None, issues: list[str]) -> tuple[str | None, str | None]:
    if not value or not str(value).strip():
        return None, None
    try:
        return normalize_nip(str(value)), None
    except ValueError:
        try:
            nip = normalize_nip(str(value), validate=False)
            issues.append(f"NIP {value!r} ma niepoprawną sumę kontrolną")
            return nip, None
        except ValueError:
            return None, str(value).strip().upper().replace(" ", "")


def row_to_facts(core: dict[str, Any], extra: dict[str, Any], raw_row: dict[str, Any], *, channel: str,
                 mapping_key: str, company_nip: str | None, company_name: str) -> InvoiceFacts:
    issues: list[str] = []
    direction, kind = _direction_and_kind(core.get("kind"), core.get("direction"))
    seller_nip, seller_foreign = _nip_or_foreign(core.get("seller_nip"), issues)
    buyer_nip, buyer_foreign = _nip_or_foreign(core.get("buyer_nip"), issues)
    if direction is None and company_nip:
        direction = "sales" if seller_nip == company_nip else "purchase" if buyer_nip == company_nip else None
    if direction is None:
        raise MappingError("nie da się ustalić kierunku (sprzedaż/zakup) - zmapuj kolumnę typu lub kierunku")
    other_nip, other_foreign = _nip_or_foreign(extra.get("counterparty_nip"), issues)
    other = Party(nip=other_nip, foreign_tax_id=other_foreign, name=extra.get("counterparty_name"),
                  country="PL" if other_nip else None)
    us = Party(nip=company_nip, name=company_name, country="PL")
    if direction == "sales":
        seller = Party(nip=seller_nip or company_nip, foreign_tax_id=seller_foreign,
                       name=core.get("seller_name") or company_name)
        buyer = Party(nip=buyer_nip, foreign_tax_id=buyer_foreign, name=core.get("buyer_name")) \
            if (buyer_nip or buyer_foreign or core.get("buyer_name")) else other
    else:
        buyer = Party(nip=buyer_nip or company_nip, foreign_tax_id=buyer_foreign,
                      name=core.get("buyer_name") or company_name) if (buyer_nip or core.get("buyer_name")) else us
        seller = Party(nip=seller_nip, foreign_tax_id=seller_foreign, name=core.get("seller_name")) \
            if (seller_nip or seller_foreign or core.get("seller_name")) else other
    net, vat, gross = core.get("net"), core.get("vat"), core.get("gross")
    if gross is None and net is not None:
        gross = net + (vat or 0)
    if gross is None:
        raise MappingError("brak kwoty brutto")
    if gross < 0 and kind != "correction":
        kind = "correction"
    checks: dict[str, Any] = {}
    if net is not None and vat is not None and net + vat != gross:
        issues.append(f"netto + VAT ({net + vat}) różni się od brutto ({gross})")
    if issues:
        checks["issues"] = issues
    ksef = core.get("ksef_number")
    return InvoiceFacts(
        channel=channel,  # type: ignore[arg-type]
        parser=f"table:{mapping_key}",
        parser_version=PARSER_VERSION,
        direction=direction,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        kind_certain=False,
        ksef_number=ksef or None,
        number_raw=core["number_raw"],
        issue_date=core["issue_date"],
        sale_date=core.get("sale_date"),
        due_date=core.get("due_date"),
        seller=seller,
        buyer=buyer,
        currency=core.get("currency") or "PLN",
        net_minor=net,
        vat_minor=vat,
        gross_minor=gross,
        lines_sum_ok=None,
        checks=checks,
        extra={"row": {k: json_safe(v) for k, v in raw_row.items()},
               "mapped_extra": {k: json_safe(v) for k, v in extra.items()
                                if k not in ("counterparty_nip", "counterparty_name")}},
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_table(
    conn: psycopg.Connection,
    company: Any,
    payload: bytes,
    *,
    filename: str,
    mapping_key: str,
    answers: dict[str, str] | None = None,
    ask: AskFn | None = None,
    created_by: str = "import",
) -> IngestReport:
    """Import one CSV/XLSX file with the extension mapping ``mapping_key``."""
    extension = get_extension(company.extension_slug)
    mapping = extension.import_mapping(mapping_key)
    table = read_table(payload, filename, mapping)
    column_map, profile_id, profile_created = resolve_column_map(
        conn, company.id, mapping_key, mapping, table, answers=answers, ask=ask, created_by=created_by
    )
    raw = store_raw(conn, company.id, "file", payload, content_type=CONTENT_TYPES[table.kind],
                    external_ref=filename,
                    meta={"filename": filename, "mapping": mapping_key, "file_format": table.file_format,
                          "profile_id": str(profile_id), "sheet": table.sheet})
    report = IngestReport(channel=table.kind)
    report.raw(raw.created)
    if profile_created:
        report.warnings.append(f"zapisano mapowanie kolumn {mapping_key!r} dla plików z tymi nagłówkami")
    if mapping.target == "document":
        _import_documents(conn, company, table, mapping, mapping_key, column_map, raw.id, filename,
                          created_by, report)
        report.versions_created += relink_pending(conn, company.id)
    else:
        _import_internal_records(conn, company, table, mapping, mapping_key, column_map, profile_id, raw.id,
                                 created_by, report)
    return report


def _locator(table: TableFile, row_no: int) -> str:
    return f"{table.sheet}!row:{row_no}" if table.sheet else f"row:{row_no}"


def _import_documents(conn, company, table, mapping, mapping_key, column_map, raw_id, filename, created_by,
                      report: IngestReport) -> None:
    caps = capabilities(conn)
    company_nip = get_company_nip(conn, company.id)
    for row_no, row in table.rows:
        try:
            mapped = apply_column_map(mapping, column_map, row)
            facts = row_to_facts(mapped.core, mapped.extra, row, channel=table.kind, mapping_key=mapping_key,
                                 company_nip=company_nip, company_name=company.name)
        except (MappingError, ValueError) as exc:
            report.errors.append(f"wiersz {row_no}: {exc}")
            continue
        result = upsert_document(conn, company.id, facts, Observation(
            raw_id=raw_id, source=table.kind, locator=_locator(table, row_no), method="code",
            evidence={"file": filename, "mapping": mapping_key, "column_map": column_map},
            created_by=created_by,
        ), caps=caps)
        report.add(result)


def _import_internal_records(conn, company, table, mapping, mapping_key, column_map, profile_id, raw_id,
                             created_by, report: IngestReport) -> None:
    for row_no, row in table.rows:
        exists = conn.execute(
            """
            SELECT id FROM l1_internal_record
             WHERE file_raw_id = %s AND coalesce(sheet, '') = %s AND row_no = %s AND superseded_by IS NULL
            """,
            (raw_id, table.sheet or "", row_no),
        ).fetchone()
        if exists:
            report.unchanged += 1
            continue
        try:
            mapped = apply_column_map(mapping, column_map, row)
        except MappingError as exc:
            report.errors.append(f"wiersz {row_no}: {exc}")
            continue
        core = mapped.core
        pseudonym, pseudonym_method = person_pseudonym(conn, company.id, core.get("person"))
        conn.execute(
            """
            INSERT INTO l1_internal_record (
                company_id, file_raw_id, mapping_key, sheet, row_no, fields, person_pseudonym, record_date,
                amount_minor, currency, cost_object_ref, settlement_type,
                source, method, evidence, schema_version, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'code', %s, 1, %s)
            """,
            (
                company.id, raw_id, mapping_key, table.sheet, row_no,
                Jsonb({k: json_safe(v) for k, v in row.items()}),
                pseudonym, core.get("record_date"), core.get("amount"), core.get("currency") or mapping.currency,
                core.get("cost_object_ref"), core.get("settlement_type"),
                table.kind,
                Jsonb({"mapping": mapping_key, "profile_id": str(profile_id), "column_map": column_map,
                       "pseudonym_method": pseudonym_method,
                       "extra": {k: json_safe(v) for k, v in mapped.extra.items()}}),
                created_by,
            ),
        )
        report.internal_records_created += 1
