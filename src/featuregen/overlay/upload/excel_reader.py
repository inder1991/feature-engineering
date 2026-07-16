from __future__ import annotations

import io
from collections.abc import Iterator

import openpyxl

from featuregen.overlay.upload._headers import build_row, field_map
from featuregen.overlay.upload.canonical import CanonicalRow

# Decompression-bomb bound (round-3 #27): the API edge caps the COMPRESSED upload bytes
# (uploads._read_capped, 25 MiB), but a tiny .xlsx can expand to an enormous sheet — the reader
# must bound what it actually READS. A schema export is one row per catalog column, so these caps
# sit far above any legitimate upload: MAX_SHEET_ROWS is parity with what the CSV byte cap can
# carry (~100 bytes/row over 25 MiB), and MAX_SHEET_CELLS is the rows*width budget a bomb inflates
# first (a sheet declaring 16k-wide rows exhausts it in ~60 rows). Fail-closed: an over-budget
# sheet is REJECTED with a parse error (-> 400), never silently truncated — mirroring canonical's
# MAX_COLUMNS_PER_TABLE=200 downstream table-width reject (#29).
MAX_SHEET_ROWS = 250_000
MAX_SHEET_CELLS = 1_000_000


def _bounded_rows(ws, sheet_name: str) -> Iterator[tuple]:
    """Yield the sheet's rows while enforcing ONE read budget across the header scan and the data
    read (blank filler rows count too — a bomb of empty rows must not spin the header scan)."""
    n_rows = 0
    n_cells = 0
    for raw in ws.iter_rows(values_only=True):
        n_rows += 1
        n_cells += len(raw) if raw else 0
        if n_rows > MAX_SHEET_ROWS or n_cells > MAX_SHEET_CELLS:
            raise ValueError(
                f"sheet '{sheet_name}' exceeds the {MAX_SHEET_ROWS}-row / {MAX_SHEET_CELLS}-cell "
                "read budget — a schema export is one row per catalog column and never this large"
            )
        yield raw


def read_excel_rows(data: bytes, *, source: str, sheet: str | None = None) -> list[CanonicalRow]:
    """Read the first (or named) sheet of an .xlsx: the first non-empty row is the header, the rest
    are data rows. Same header aliasing + canonical row shape as the CSV reader."""
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    try:
        rows = _bounded_rows(ws, ws.title)
        headers: list[str] = []
        for raw in rows:
            if raw and any(c is not None and str(c).strip() for c in raw):
                headers = ["" if c is None else str(c) for c in raw]
                break
        if not headers:
            return []
        fmap = field_map(headers)
        out: list[CanonicalRow] = []
        for raw in rows:
            if raw is None or all(c is None for c in raw):
                continue  # skip blank rows
            rowdict = {headers[i]: raw[i] for i in range(min(len(headers), len(raw)))}
            out.append(build_row(fmap, rowdict, source))
        return out
    finally:
        wb.close()
