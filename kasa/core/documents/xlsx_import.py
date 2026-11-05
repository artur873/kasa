"""XLSX files for the import wizard: the sheet becomes the same ``TableFile`` as a CSV.

Cells keep their spreadsheet types (dates as ``datetime``, numbers as ``int``/``float``);
the extension mapping converts them (``kasa.core.extension.ColumnMapping.map_row``),
so an XLSX and a CSV export of the same sheet give the same facts.
"""

from __future__ import annotations

import io

from kasa.core.documents.csv_import import TableFile


def read_xlsx(payload: bytes, *, sheet: str | None = None, header_row: int = 1) -> TableFile:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    try:
        worksheet = workbook[sheet] if sheet else workbook.worksheets[0]
        headers: list[str] = []
        rows = []
        for number, cells in enumerate(worksheet.iter_rows(values_only=True), start=1):
            if number < header_row:
                continue
            if number == header_row:
                headers = ["" if c is None else str(c).strip() for c in cells]
                continue
            if all(c is None or (isinstance(c, str) and not c.strip()) for c in cells):
                continue
            rows.append((number, {headers[i] if i < len(headers) and headers[i] else f"_col{i + 1}": c
                                  for i, c in enumerate(cells) if not (c is None and i >= len(headers))}))
        while headers and not headers[-1]:
            headers.pop()
        return TableFile(headers, rows, {"type": "xlsx", "sheet": worksheet.title, "header_row": header_row},
                         sheet=worksheet.title)
    finally:
        workbook.close()
