"""`source_row` is FTR's column, not every glossary's.

The shape check already treats `source_row` as optional — but the ROW READER still demanded a
non-empty integer in it, so the real second source (`CIB_Customer_Column_Mapping_final.csv`, which
has no such column) had all 111 rows quarantined with "invalid source_row ''". The file was accepted
and then entirely refused, which is the worst of both.

`source_row` exists for ONE purpose: so a data owner can find the offending row in their own file.
When the file does not carry the column, its spreadsheet row number serves that purpose exactly as
well — so it is DERIVED rather than demanded.

The strictness is kept where it still means something: a file that DECLARES the column and leaves it
blank, or repeats a value, is a file whose identity column is broken, and is still refused.
"""
from __future__ import annotations

from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary

_HDR = "schema.table.column,term_name,description_business_definition,data_domain,data_type"
_HDR_WITH_ROW = "source_row," + _HDR


def _rows(text: str, source: str = "cib"):
    return read_ftr_glossary(text, source=source)


# ── a file WITHOUT the column parses ─────────────────────────────────────────────────────────────

def test_a_glossary_without_source_row_is_read_not_quarantined():
    prepared = _rows(f"{_HDR}\nS.T.A,Alpha,The alpha,Customer,varchar(10)\n")
    assert prepared.quarantined == []
    assert len(prepared.rows) == 1


def test_the_derived_source_row_is_the_SPREADSHEET_row_number():
    """The point of the field is "find this row in your file", so the number must be the one the
    uploader sees in Excel — the header occupies row 1, so the first data row is row 2."""
    prepared = _rows(
        f"{_HDR}\nS.T.A,Alpha,The alpha,Customer,varchar(10)\nS.T.B,Beta,The beta,Customer,int\n")
    assert [r.source_row for r in prepared.rows] == ["2", "3"]


def test_derived_source_rows_cannot_collide():
    """Uniqueness is by construction, so the duplicate-source_row refusal can never fire spuriously
    on a file that never claimed the column."""
    body = "".join(f"S.T.C{i},Term {i},Definition {i},Customer,int\n" for i in range(40))
    prepared = _rows(f"{_HDR}\n{body}")
    assert prepared.quarantined == []
    assert len({r.source_row for r in prepared.rows}) == 40


# ── a file WITH the column keeps every existing rule ─────────────────────────────────────────────

def test_a_declared_source_row_is_used_verbatim_not_overwritten():
    """FTR's own numbering need not match the line number — gaps are legitimate — so a declared
    value must win."""
    prepared = _rows(f"{_HDR_WITH_ROW}\n21,S.T.A,Alpha,The alpha,Customer,varchar(10)\n")
    assert [r.source_row for r in prepared.rows] == ["21"]


def test_a_DECLARED_but_blank_source_row_is_still_refused():
    """THE boundary. Absence of the column is a different file shape; a blank cell in a column the
    file claims to have is a broken identity column, and must still fail closed."""
    prepared = _rows(f"{_HDR_WITH_ROW}\n,S.T.A,Alpha,The alpha,Customer,varchar(10)\n")
    assert prepared.rows == []
    assert "invalid source_row" in prepared.quarantined[0].message


def test_a_DECLARED_duplicate_source_row_is_still_refused():
    prepared = _rows(
        f"{_HDR_WITH_ROW}\n7,S.T.A,Alpha,The alpha,Customer,int\n7,S.T.B,Beta,The beta,Customer,int\n")
    assert prepared.rows == []
    assert all("duplicate source_row" in q.message for q in prepared.quarantined)


# ── the width diagnostic must not quote FTR's column count at a non-FTR file ─────────────────────

def test_the_malformed_width_message_reports_THIS_file_s_column_count():
    """Collateral of accepting a second layout: the message hard-coded FTR's 17. Telling the owner of
    a 5-column file that 17 were expected sends them to fix the wrong thing."""
    prepared = _rows(f"{_HDR}\nS.T.A,Alpha,A, definition, with commas,Customer,varchar(10)\n")
    assert len(prepared.quarantined) == 1
    message = prepared.quarantined[0].message
    assert "expected 5" in message, message
    assert "expected 17" not in message
