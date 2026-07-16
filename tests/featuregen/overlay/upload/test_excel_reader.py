import io

import openpyxl
import pytest

from featuregen.overlay.upload import excel_reader
from featuregen.overlay.upload.excel_reader import read_excel_rows


def _xlsx(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_reads_aliased_headers_types_and_flags():
    data = _xlsx([
        ["Table Name", "Attribute", "SQL Type", "Grain", "As Of", "Description", "Sensitivity"],
        ["accounts", "id", "integer", "Y", "", "Account id", ""],
        ["accounts", "posted_at", "timestamp", "", "yes", "As-of date", ""],
        ["accounts", "ssn_hash", "text", "", "", "Hashed SSN", "PII"],
    ])
    rows = read_excel_rows(data, source="deposits")
    assert len(rows) == 3
    assert rows[0].source == "deposits" and rows[0].table == "accounts"
    assert rows[0].column == "id" and rows[0].type == "integer" and rows[0].is_grain is True
    assert rows[1].as_of is True and rows[1].is_grain is False
    assert rows[2].sensitivity == "pii" and rows[2].definition == "Hashed SSN"


def test_conflicting_alias_headers_rejected():
    # Two headers aliasing to the same canonical field (`table` + `tablename`) are ambiguous; reject
    # the file rather than silently last-write-winning one identity column (#17).
    data = _xlsx([["table", "tablename", "column", "type"],
                  ["orders", "ORDERS", "id", "integer"]])
    with pytest.raises(ValueError, match="table"):
        read_excel_rows(data, source="deposits")


def test_skips_leading_blank_rows_and_source_column():
    data = _xlsx([
        [None, None, None],
        ["source", "table", "column", "type"],
        ["cards", "card_accounts", "acct_id", "integer"],
    ])
    rows = read_excel_rows(data, source="fallback")
    assert len(rows) == 1
    assert rows[0].source == "cards" and rows[0].column == "acct_id"


# ── Decompression-bomb bound (round-3 #27) ───────────────────────────────────────────────────────
# The API edge caps the COMPRESSED upload bytes (uploads._read_capped), but a small zip can expand
# to an enormous sheet. The reader itself must bound what it READS and reject an over-budget sheet
# (a clean parse error -> 400), never silently truncate it. Caps are patched small so the tests
# stay fast; production values live in excel_reader.MAX_SHEET_ROWS / MAX_SHEET_CELLS.


def test_sheet_over_row_cap_rejected(monkeypatch):
    monkeypatch.setattr(excel_reader, "MAX_SHEET_ROWS", 5)
    data = _xlsx([["table", "column", "type"]]
                 + [["accounts", f"c{i}", "text"] for i in range(6)])
    with pytest.raises(ValueError, match="exceeds the .* read budget"):
        read_excel_rows(data, source="deposits")


def test_sheet_over_cell_budget_rejected(monkeypatch):
    # Wide rows blow the rows*width budget even under the row cap — the bomb shape.
    monkeypatch.setattr(excel_reader, "MAX_SHEET_CELLS", 40)
    data = _xlsx([["table", "column", "type"] + [f"x{i}" for i in range(17)],
                  ["accounts", "id", "integer"] + ["v"] * 17,
                  ["accounts", "b", "integer"] + ["v"] * 17])
    with pytest.raises(ValueError, match="exceeds the .* read budget"):
        read_excel_rows(data, source="deposits")


def test_blank_row_flood_counts_toward_budget(monkeypatch):
    # A bomb of blank rows before any header must hit the budget too (the header scan would
    # otherwise spin through them unbounded).
    monkeypatch.setattr(excel_reader, "MAX_SHEET_ROWS", 5)
    data = _xlsx([[None, None]] * 6 + [["table", "column", "type"],
                                       ["accounts", "id", "integer"]])
    with pytest.raises(ValueError, match="exceeds the .* read budget"):
        read_excel_rows(data, source="deposits")


def test_sheet_within_caps_still_reads(monkeypatch):
    monkeypatch.setattr(excel_reader, "MAX_SHEET_ROWS", 5)
    monkeypatch.setattr(excel_reader, "MAX_SHEET_CELLS", 40)
    data = _xlsx([["table", "column", "type"],
                  ["accounts", "id", "integer"],
                  ["accounts", "posted_at", "timestamp"]])
    rows = read_excel_rows(data, source="deposits")
    assert [r.column for r in rows] == ["id", "posted_at"]


def test_production_caps_are_sane():
    # The real caps must stay comfortably above a legitimate schema export (a row per catalog
    # column) while remaining finite — and wide enough for canonical's 200-column table bound.
    assert excel_reader.MAX_SHEET_ROWS >= 100_000
    assert excel_reader.MAX_SHEET_CELLS >= excel_reader.MAX_SHEET_ROWS
    assert excel_reader.MAX_SHEET_CELLS <= 50_000_000
