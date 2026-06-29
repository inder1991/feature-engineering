from datetime import datetime, timezone

from featuregen.governance.attributes import (
    GovernanceAttributes,
    from_feature_version_row,
    to_feature_version_row,
)


def test_governance_attributes_round_trip_through_feature_versions(db):
    attrs = GovernanceAttributes(
        feature_version_id="fv_rt", feature_id="feat_rt", produced_by_run="run_rt",
        base_feature_version_id="fv_base",
        verification_stamp="USEFULNESS-CHECKED", risk_tier="high", approval_type="PRODUCTION",
        approved_use_cases=("churn", "fraud"), blocked_use_cases=("credit_decisioning",),
        required_artifact_refs={"evaluation_report": "doc_e", "monitoring_spec": "doc_m"},
        dsl_operation_catalog_version="ops@v9",
        conditions=("review quarterly",),
        expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        max_uses=100, reviewed_evidence_refs=("doc_r",),
    )
    # Satisfy the real feature_versions.base_feature_version_id FK by seeding the parent row.
    base_attrs = GovernanceAttributes(
        feature_version_id="fv_base", feature_id="feat_rt", produced_by_run="run_rt",
        verification_stamp="DESIGN-CHECKED", risk_tier="low", approval_type="EXPERIMENTAL",
    )
    base_row = to_feature_version_row(base_attrs, content_hash="sha256:base")
    base_cols = list(base_row.keys())
    db.execute(
        f"INSERT INTO feature_versions ({', '.join(base_cols)}) "
        f"VALUES ({', '.join(['%s'] * len(base_cols))})",
        [base_row[c] for c in base_cols],
    )

    row = to_feature_version_row(attrs, content_hash="sha256:abc")
    cols = list(row.keys())
    db.execute(
        f"INSERT INTO feature_versions ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )
    fetched = db.execute(
        "SELECT feature_version_id, feature_id, produced_by_run, base_feature_version_id, "
        "verification_stamp, risk_tier, approval_type, approved_use_cases, blocked_use_cases, "
        "required_artifact_refs, dsl_operation_catalog_version, approval, expires_at, immutable "
        "FROM feature_versions WHERE feature_version_id = %s",
        ("fv_rt",),
    ).fetchone()
    keys = (
        "feature_version_id", "feature_id", "produced_by_run", "base_feature_version_id",
        "verification_stamp", "risk_tier", "approval_type", "approved_use_cases", "blocked_use_cases",
        "required_artifact_refs", "dsl_operation_catalog_version", "approval", "expires_at", "immutable",
    )
    back = from_feature_version_row(dict(zip(keys, fetched)))
    assert back == attrs
