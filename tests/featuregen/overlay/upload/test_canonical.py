from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows


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
    assert len(result.good) == 1               # deduped identical
    assert len(result.quarantined) == 1        # conflicting type


def test_row_carries_definition():
    r = CanonicalRow("deposits", "accounts", "balance", "numeric", definition="ledger balance")
    assert r.definition == "ledger balance"


def test_unrecognized_sensitivity_is_quarantined():
    rows = [_row(column="ssn", sensitivity="confidential")]   # not pii/restricted
    result = validate_rows(rows)
    assert result.good == []
    assert len(result.quarantined) == 1
    assert "sensitivity" in result.quarantined[0].message
