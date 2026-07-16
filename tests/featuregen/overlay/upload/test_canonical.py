from featuregen.overlay.upload.canonical import (
    MAX_COLUMNS_PER_TABLE,
    CanonicalRow,
    validate_rows,
)


def _row(**kw):
    base = dict(source="deposits", table="accounts", column="id", type="integer")
    base.update(kw)
    return CanonicalRow(**base)


def test_valid_rows_pass_through():
    rows = [_row(column="id", is_grain=True), _row(column="posted_at", type="timestamp", as_of=True)]
    result = validate_rows(rows)
    assert len(result.good) == 2
    assert result.quarantined == []
    assert result.structural_error is None


def test_missing_required_field_quarantines_that_row_only():
    rows = [_row(column="id"), _row(column="", type="text")]  # blank column
    result = validate_rows(rows)
    assert len(result.good) == 1
    assert len(result.quarantined) == 1
    assert result.quarantined[0].row_index == 1


def test_empty_upload_is_structural_error():
    result = validate_rows([])
    assert result.structural_error is not None
    assert result.good == []


def test_duplicate_same_type_dedups_conflicting_type_quarantines():
    rows = [_row(column="id", type="integer"), _row(column="id", type="integer"),
            _row(column="id", type="text")]
    result = validate_rows(rows)
    assert len(result.good) == 0               # conflicting column fails closed (not graphed)
    assert len(result.quarantined) == 2        # first + conflicting row both surfaced for review


def test_row_carries_definition():
    r = CanonicalRow("deposits", "accounts", "balance", "numeric", definition="ledger balance")
    assert r.definition == "ledger balance"


def test_unrecognized_sensitivity_is_quarantined():
    rows = [_row(column="ssn", sensitivity="confidential")]   # not pii/restricted
    result = validate_rows(rows)
    assert result.good == []
    assert len(result.quarantined) == 1
    assert "sensitivity" in result.quarantined[0].message


def test_whitespace_only_required_field_quarantined():
    # A required field that is only whitespace is effectively missing. Readers strip cells, but a
    # reviewer-edited row can carry "   "; reject it rather than graphing a blank-named object (#18).
    result = validate_rows([_row(table="   ")])
    assert result.good == []
    assert len(result.quarantined) == 1
    assert "table" in result.quarantined[0].message


def test_invalid_cardinality_quarantined():
    result = validate_rows([_row(cardinality="many-to-one")])   # a typo for "N:1"
    assert result.good == []
    assert "cardinality" in result.quarantined[0].message


def test_invalid_additivity_quarantined():
    result = validate_rows([_row(additivity="kinda")])
    assert result.good == []
    assert "additivity" in result.quarantined[0].message


def test_invalid_as_of_basis_quarantined_not_coerced():
    # An unrecognized as-of basis must surface for review, NOT silently become posted_at (#18).
    result = validate_rows([_row(as_of=True, as_of_basis="whenever")])
    assert result.good == []
    assert "as_of_basis" in result.quarantined[0].message


def test_valid_enums_and_empty_pass():
    rows = [_row(cardinality="N:1", additivity="semi_additive", as_of=True, as_of_basis="posted_at"),
            _row(column="c2", cardinality="", additivity="", as_of_basis="")]
    result = validate_rows(rows)
    assert len(result.good) == 2
    assert result.quarantined == []


def test_dotted_table_or_column_is_quarantined():
    # A '.' inside a table/column name would corrupt the "public.<table>.<column>" object ref: two
    # distinct rows can collide on the graph PK, or lineage/join-path mis-parses the segments. Fail
    # closed at validation so a dotted name never reaches normalize_ref/build_graph (#2).
    rows = [
        _row(table="orders.line", column="id"),      # dot in the table name
        _row(table="orders", column="customer.id"),  # dot in the column name
        _row(table="orders", column="ok"),           # a clean row still passes
    ]
    result = validate_rows(rows)
    assert [r.column for r in result.good] == ["ok"]
    assert len(result.quarantined) == 2
    assert all("." in q.message for q in result.quarantined)


def test_accepted_rows_carry_normalized_identity():
    # #1: validate_rows dedups on the NORMALIZED (strip+lower) key, so the rows it RETURNS must carry
    # that same normalized identity — otherwise graph nodes / drift snapshot / cache keys inherit the
    # raw casing and one physical column splits into case-variant twins across uploads.
    result = validate_rows([_row(source="Deposits ", table="Accounts ", column=" ID",
                                 joins_to="Customers.CUST_ID ")])
    assert len(result.good) == 1
    good = result.good[0]
    assert good.source == "deposits"
    assert good.table == "accounts"
    assert good.column == "id"
    assert good.joins_to == "customers.cust_id"   # the join TARGET is an identity too


def test_case_variant_duplicates_dedup_to_one_normalized_row():
    rows = [_row(table="Accounts", column="ID"), _row(table="accounts ", column="id ")]
    result = validate_rows(rows)
    assert len(result.good) == 1
    assert (result.good[0].table, result.good[0].column) == ("accounts", "id")
    assert result.quarantined == []


def test_conflicting_metadata_for_a_column_fails_closed():
    # A later duplicate with a pii tag must NOT be silently dropped (leaving the column world-readable).
    rows = [
        CanonicalRow("s", "t", "ssn", "text"),                     # untagged
        CanonicalRow("s", "t", "ssn", "text", sensitivity="pii"),  # same column, now pii
    ]
    result = validate_rows(rows)
    assert all(not (r.table == "t" and r.column == "ssn") for r in result.good)  # neither accepted
    assert sum(1 for q in result.quarantined if q.row and q.row.column == "ssn") == 2


def test_table_at_the_column_bound_ingests_normally():
    rows = [_row(column=f"c{i}") for i in range(MAX_COLUMNS_PER_TABLE)]
    result = validate_rows(rows)
    assert len(result.good) == MAX_COLUMNS_PER_TABLE
    assert result.quarantined == []


def test_100_column_table_ingests_under_the_lineage_bound():
    # #29 follow-up: the always-on bound is LINEAGE's structural assumption (MAX_NODES), not the
    # flags-on 64-profile LLM synthesis egress cap — denormalized/regulatory bank extracts routinely
    # exceed 64 columns and must ingest flag-off. table_synth enforces its own 64 cap separately
    # (enrich_llm._item_egress_ok), so an over-64 table simply gets no synthesis, not no ingest.
    rows = [_row(column=f"c{i}") for i in range(100)]
    result = validate_rows(rows)
    assert len(result.good) == 100
    assert result.quarantined == []


def test_column_bound_is_lineages_node_cap():
    # The bound and lineage.MAX_NODES move in LOCKSTEP: lineage installs a whole anchor table even
    # past its cap only because ingest keeps table widths within it. Raising one requires the other.
    from featuregen.overlay.upload.lineage import MAX_NODES
    assert MAX_COLUMNS_PER_TABLE == MAX_NODES


def test_table_over_the_column_bound_is_quarantined_whole():
    # Lineage installs a whole anchor table even past its node cap ("acceptable under upload
    # governance, where table widths are bounded" — lineage.py). Ingest must UPHOLD the width bound
    # it assumes (#29): an over-wide table is quarantined WHOLE — never a silent arbitrary prefix
    # of its columns.
    wide = [_row(column=f"c{i}") for i in range(MAX_COLUMNS_PER_TABLE + 1)]
    narrow = [_row(table="customers", column="id")]
    result = validate_rows(wide + narrow)
    assert len(result.good) == 1                               # the under-bound table still ingests
    assert result.good[0].table == "customers"
    assert len(result.quarantined) == MAX_COLUMNS_PER_TABLE + 1
    assert all("accounts" in q.message and str(MAX_COLUMNS_PER_TABLE) in q.message
               and "column" in q.message for q in result.quarantined)


def test_column_bound_counts_unique_columns_not_raw_rows():
    # Dedup happens first: bound-many unique columns plus duplicate rows is still AT the bound.
    rows = [_row(column=f"c{i}") for i in range(MAX_COLUMNS_PER_TABLE)]
    rows += [_row(column="c0"), _row(column="C1 ")]            # exact + case/space-variant dups
    result = validate_rows(rows)
    assert len(result.good) == MAX_COLUMNS_PER_TABLE
    assert result.quarantined == []
