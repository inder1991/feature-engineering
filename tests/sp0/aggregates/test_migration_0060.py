import pytest

EXPECTED_TABLES = [
    "feature_versions", "feature_active_versions",
    "consumers", "concept_claims", "command_idempotency",
]

@pytest.mark.parametrize("table", EXPECTED_TABLES)
def test_table_exists(db, table):
    row = db.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchone()
    assert row is not None, f"missing table {table}"

def test_feature_active_versions_pk_is_feature_id_use_case(db):
    cols = db.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'feature_active_versions'::regclass AND i.indisprimary "
        "ORDER BY a.attname"
    ).fetchall()
    assert [c[0] for c in cols] == ["feature_id", "use_case"]

def test_concept_claims_concept_key_is_pk(db):
    row = db.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'concept_claims'::regclass AND i.indisprimary"
    ).fetchone()
    assert row[0] == "concept_key"


def _seed_feature_version(db):
    db.execute(
        "INSERT INTO feature_versions (feature_version_id, feature_id, produced_by_run, "
        "verification_stamp, risk_tier, approval_type, content_hash) "
        "VALUES ('fv_im','feat_im','run_im','DATA-CHECKED','low','PRODUCTION','sha256:1')"
    )


def test_feature_versions_reject_update(db):
    _seed_feature_version(db)
    with pytest.raises(Exception):  # plpgsql RAISE EXCEPTION from feature_versions_no_mutation
        db.execute("UPDATE feature_versions SET risk_tier='high' WHERE feature_version_id='fv_im'")


def test_feature_versions_reject_delete(db):
    _seed_feature_version(db)
    with pytest.raises(Exception):  # plpgsql RAISE EXCEPTION from feature_versions_no_mutation
        db.execute("DELETE FROM feature_versions WHERE feature_version_id='fv_im'")
