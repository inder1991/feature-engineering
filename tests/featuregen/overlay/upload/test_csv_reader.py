import pytest

from featuregen.overlay.upload.csv_reader import read_csv_rows


def test_duplicate_header_rejected():
    # A repeated column (here two `sensitivity` columns) is silently collapsed last-write-wins by
    # csv.DictReader, which can drop a PII tag (the first `pii` overwritten by the trailing blank).
    # Reject the structurally-broken file loudly instead of graphing an untagged SSN column (#17).
    text = "sensitivity,table,column,type,sensitivity\npii,orders,ssn,varchar,\n"
    with pytest.raises(ValueError, match="sensitivity"):
        read_csv_rows(text, source="deposits")


def test_invalid_boolean_token_rejected():
    # An unrecognized boolean token (here is_grain='maybe') must not be silently coerced to False;
    # surface it as a parse error rather than dropping a possible grain declaration (#18).
    text = "table,column,type,is_grain\norders,id,integer,maybe\n"
    with pytest.raises(ValueError, match="boolean"):
        read_csv_rows(text, source="deposits")


def test_conflicting_alias_headers_rejected():
    # `table` and `tablename` both alias to the canonical `table` field; last-write-wins would
    # silently pick one identity column. Reject the ambiguous file (#17).
    text = "table,tablename,column,type\norders,ORDERS,id,integer\n"
    with pytest.raises(ValueError, match="table"):
        read_csv_rows(text, source="deposits")


def test_reads_aliased_headers_and_booleans():
    text = (
        "Table Name,Attribute,SQL Type,Grain,As Of\n"
        "accounts,id,integer,Y,\n"
        "accounts,posted_at,timestamp,,yes\n")
    rows = read_csv_rows(text, source="deposits")
    assert len(rows) == 2
    assert rows[0].source == "deposits"
    assert rows[0].table == "accounts" and rows[0].column == "id"
    assert rows[0].type == "integer" and rows[0].is_grain is True and rows[0].as_of is False
    assert rows[1].as_of is True and rows[1].is_grain is False


def test_source_column_overrides_argument():
    text = "source,table,column,type\ncards,card_accounts,acct_id,integer\n"
    rows = read_csv_rows(text, source="fallback")
    assert rows[0].source == "cards"


def test_reads_definition_alias():
    text = "table,column,type,Description\naccounts,balance,numeric,Ledger balance\n"
    rows = read_csv_rows(text, source="deposits")
    assert rows[0].definition == "Ledger balance"


def test_reads_headers_with_utf8_bom():
    # Excel-exported UTF-8 CSVs prefix the first header with a BOM.
    text = "﻿source,table,column,type\ncards,card_accounts,acct_id,integer\n"
    rows = read_csv_rows(text, source="fallback")
    assert rows[0].source == "cards" and rows[0].table == "card_accounts"
