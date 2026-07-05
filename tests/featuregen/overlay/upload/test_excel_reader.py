import io

import openpyxl

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


def test_skips_leading_blank_rows_and_source_column():
    data = _xlsx([
        [None, None, None],
        ["source", "table", "column", "type"],
        ["cards", "card_accounts", "acct_id", "integer"],
    ])
    rows = read_excel_rows(data, source="fallback")
    assert len(rows) == 1
    assert rows[0].source == "cards" and rows[0].column == "acct_id"
