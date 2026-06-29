from tests.featuregen._helpers import make_actor

from featuregen.aggregates._append import provenance_for
from featuregen.aggregates.feature_versions import mint_feature_version
from featuregen.events.store import load_stream


def test_mint_freezes_version_and_emits_event(db):
    fv = mint_feature_version(
        db,
        feature_id="feat_1",
        produced_by_run="run_1",
        verification_stamp="USEFULNESS-CHECKED",
        risk_tier="medium",
        approval_type="PRODUCTION",
        approved_use_cases=("fraud",),
        blocked_use_cases=("credit",),
        required_artifact_refs={"evaluation_report": "doc_1"},
        content_hash="sha256:abc",
        actor=make_actor(),
        provenance=provenance_for(),
    )
    assert fv.startswith("fv_")
    row = db.execute(
        "SELECT feature_id, approval_type, immutable, approved_use_cases "
        "FROM feature_versions WHERE feature_version_id = %s",
        (fv,),
    ).fetchone()
    assert row[0] == "feat_1" and row[1] == "PRODUCTION" and row[2] is True
    assert row[3] == ["fraud"]
    minted = load_stream(db, "feature", "feat_1")[-1]
    assert minted.type == "VERSION_MINTED" and minted.payload["feature_version_id"] == fv


def test_base_version_fk_chain(db):
    base = mint_feature_version(
        db,
        feature_id="feat_2",
        produced_by_run="run_a",
        verification_stamp="DATA-CHECKED",
        risk_tier="low",
        approval_type="PRODUCTION",
        approved_use_cases=(),
        blocked_use_cases=(),
        required_artifact_refs={},
        content_hash="sha256:1",
        actor=make_actor(),
        provenance=provenance_for(),
    )
    child = mint_feature_version(
        db,
        feature_id="feat_2",
        produced_by_run="run_b",
        verification_stamp="DATA-CHECKED",
        risk_tier="low",
        approval_type="PRODUCTION",
        approved_use_cases=(),
        blocked_use_cases=(),
        required_artifact_refs={},
        content_hash="sha256:2",
        actor=make_actor(),
        provenance=provenance_for(),
        base_feature_version_id=base,
    )
    row = db.execute(
        "SELECT base_feature_version_id FROM feature_versions WHERE feature_version_id = %s",
        (child,),
    ).fetchone()
    assert row[0] == base
