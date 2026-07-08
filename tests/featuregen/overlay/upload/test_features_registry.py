"""Registry read surface + UNVERIFIED stamp persistence + model<->feature consumer registration.

Direct registration (register_feature / POST /features) is honestly UNVERIFIED — DESIGN-CHECKED is
EARNED only via the governed contract flow (confirm_contract). See test_govern.py for that path."""
import psycopg
import pytest

from featuregen.overlay.upload.features import (
    FeatureSpec,
    consumers_of_feature,
    features_for_consumer,
    get_feature,
    list_features,
    register_consumer,
    register_feature,
)


def _feat(db, name="f", agg="avg_90d"):
    return register_feature(db, FeatureSpec(name=name, aggregation=agg,
                                            derives_from=(("bank", "public.accounts.balance"),)))


def test_register_persists_the_unverified_stamp(db):
    feat = get_feature(db, _feat(db))
    assert feat["verification"] == "UNVERIFIED"   # direct registration is honestly UNVERIFIED (finding #4)
    assert feat["derives_from"] == [{"catalog_source": "bank", "object_ref": "public.accounts.balance"}]


def test_register_with_default_spec_is_unverified(db):
    # a bare-default FeatureSpec (no verification arg) => the persisted row is UNVERIFIED, not a false stamp
    fid = register_feature(db, FeatureSpec(name="bare"))
    assert get_feature(db, fid)["verification"] == "UNVERIFIED"


def test_verification_check_constraint_rejects_out_of_vocab(db):
    # 0973 adds a CHECK constraint: an out-of-vocabulary stamp is rejected at the DB.
    fid = register_feature(db, FeatureSpec(name="bad"))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE feature SET verification = 'BOGUS' WHERE feature_id = %s", (fid,))


def test_list_features_returns_the_inventory(db):
    _feat(db, "a")
    _feat(db, "b")
    assert {"a", "b"} <= {f["name"] for f in list_features(db)}


def test_consumer_registration_links_model_and_feature_both_ways(db):
    fid = _feat(db)
    cid = register_consumer(db, model_ref="churn_model_v3", feature_id=fid, purpose="churn",
                            environment="prod", actor="user:ana")
    assert cid
    cons = consumers_of_feature(db, fid)
    assert cons and cons[0]["model_ref"] == "churn_model_v3" and cons[0]["environment"] == "prod"
    feats = features_for_consumer(db, "churn_model_v3")
    assert feats and feats[0]["feature_id"] == fid


def test_consumer_registration_is_idempotent_and_guards_unknown_feature(db):
    fid = _feat(db)
    a = register_consumer(db, model_ref="m", feature_id=fid, environment="prod")
    b = register_consumer(db, model_ref="m", feature_id=fid, environment="prod")   # same (model,feat,env)
    assert a == b and len(consumers_of_feature(db, fid)) == 1        # idempotent
    assert register_consumer(db, model_ref="m", feature_id="nope") is None   # unknown feature
