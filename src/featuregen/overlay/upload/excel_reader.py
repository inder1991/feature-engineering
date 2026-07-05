from __future__ import annotations

import io

import openpyxl

from featuregen.overlay.upload._headers import build_row, field_map
from featuregen.overlay.upload.canonical import CanonicalRow


def read_excel_rows(data: bytes, *, source: str, sheet: str | None = None) -> list[CanonicalRow]:
    """Read the first (or named) sheet of an .xlsx: the first non-empty row is the header, the rest
    are data rows. Same header aliasing + canonical row shape as the CSV reader."""
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    try:
        rows = ws.iter_rows(values_only=True)
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
