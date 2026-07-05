from featuregen.overlay.upload.csv_reader import read_csv_rows


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
