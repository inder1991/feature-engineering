"""Slice 3A-ii — the honest validation state carried end-to-end and persisted on the contract row.

Task 1 covers the table shape (migration 1002): `contract.validation_status` (CHECK-constrained to
the underscore VALIDATION_STATES vocab — a NEW axis, separate from the hyphenated `verification`
stamp) and `contract.requirements` (jsonb). Later tasks extend this file with the draft/confirm
round-trip; per RF-I3 only symbols that exist at this task are imported here.
"""
import psycopg
import pytest


def _seed_feature(db, feature_id: str, name: str) -> None:
    """RF-I4: contract.feature_id is FK-constrained (contract_feature_id_fk, migration 0972) —
    every contract insert must reference a REAL feature row, never a bogus id."""
    db.execute(
        "INSERT INTO feature (feature_id, name) VALUES (%s, %s)", (feature_id, name))


def test_contract_has_validation_status_and_requirements_columns(db):
    cols = dict(db.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'contract' "
        "AND column_name IN ('validation_status', 'requirements')").fetchall())
    assert cols.get("validation_status") == "text"
    assert cols.get("requirements") == "jsonb"


def test_contract_validation_status_check_rejects_unknown_value(db):
    _seed_feature(db, "f-check", "fx")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO contract "
            "(contract_id, feature_id, feature_name, version, validation_status) "
            "VALUES ('c-bogus', 'f-check', 'fx', 1, 'BOGUS')")


def test_contract_validation_status_defaults_to_design_checked(db):
    _seed_feature(db, "f-default", "fd")
    db.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, version) "
        "VALUES ('c-default', 'f-default', 'fd', 1)")
    row = db.execute(
        "SELECT validation_status, requirements FROM contract WHERE contract_id = 'c-default'"
    ).fetchone()
    assert row[0] == "DESIGN_CHECKED"
    assert row[1] == []
