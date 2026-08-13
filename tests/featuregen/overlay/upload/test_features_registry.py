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
                                            derives_from=(("bank", "public.accounts.balance"),)), lifecycle_state="idea")


def test_register_persists_the_unverified_stamp(db):
    feat = get_feature(db, _feat(db))
    assert feat["verification"] == "UNVERIFIED"   # direct registration is honestly UNVERIFIED (finding #4)
    assert feat["derives_from"] == [{"catalog_source": "bank", "object_ref": "public.accounts.balance"}]


def test_register_with_default_spec_is_unverified(db):
    # a bare-default FeatureSpec (no verification arg) => the persisted row is UNVERIFIED, not a false stamp
    fid = register_feature(db, FeatureSpec(name="bare"), lifecycle_state="idea")
    assert get_feature(db, fid)["verification"] == "UNVERIFIED"


def test_verification_check_constraint_rejects_out_of_vocab(db):
    # 0973 adds a CHECK constraint: an out-of-vocabulary stamp is rejected at the DB.
    fid = register_feature(db, FeatureSpec(name="bad"), lifecycle_state="idea")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE feature SET verification = 'BOGUS' WHERE feature_id = %s", (fid,))


def test_list_features_returns_the_inventory(db):
    _feat(db, "a")
    _feat(db, "b")
    assert {"a", "b"} <= {f["name"] for f in list_features(db)}


def test_consumer_registration_links_model_and_feature_both_ways(db):
    fid = _feat(db)
    # A2: consumers register only against GOVERNED features; these tests exercise the
    # linking mechanics, not activation — flip the fixture deliberately.
    db.execute("UPDATE feature SET lifecycle_state = 'governed'")

    cid = register_consumer(db, model_ref="churn_model_v3", feature_id=fid, purpose="churn",
                            environment="prod", actor="user:ana")
    assert cid
    cons = consumers_of_feature(db, fid)
    assert cons and cons[0]["model_ref"] == "churn_model_v3" and cons[0]["environment"] == "prod"
    feats = features_for_consumer(db, "churn_model_v3")
    assert feats and feats[0]["feature_id"] == fid


def test_consumer_registration_is_idempotent_and_guards_unknown_feature(db):
    fid = _feat(db)
    # A2: consumers register only against GOVERNED features; these tests exercise the
    # linking mechanics, not activation — flip the fixture deliberately.
    db.execute("UPDATE feature SET lifecycle_state = 'governed'")

    a = register_consumer(db, model_ref="m", feature_id=fid, environment="prod")
    b = register_consumer(db, model_ref="m", feature_id=fid, environment="prod")   # same (model,feat,env)
    assert a == b and len(consumers_of_feature(db, fid)) == 1        # idempotent
    assert register_consumer(db, model_ref="m", feature_id="nope") is None   # unknown feature


# ── Remediation A2 slice 1: the idea/governed lifecycle is a fact, not a vibe ───────────────────

def test_direct_registration_is_an_idea_and_never_a_model_consumer(db):
    import pytest as _pytest

    from featuregen.overlay.upload.features import (
        FeatureSpec,
        IdeaNotConsumableError,
        list_features,
        register_consumer,
        register_feature,
    )

    fid = register_feature(db, FeatureSpec(
        name="sketch_balance_avg", aggregation="avg_90d",
        derives_from=(("deposits", "public.accounts.balance"),)), lifecycle_state="idea")
    row = db.execute("SELECT lifecycle_state FROM feature WHERE feature_id = %s",
                     (fid,)).fetchone()
    assert row[0] == "idea"
    listed = next(f for f in list_features(db) if f["feature_id"] == fid)
    assert listed["lifecycle_state"] == "idea" and listed["governed"] is False

    with _pytest.raises(IdeaNotConsumableError):
        register_consumer(db, model_ref="churn_v3", feature_id=fid, purpose="scoring")
    assert db.execute("SELECT COUNT(*) FROM feature_consumer WHERE feature_id = %s",
                      (fid,)).fetchone()[0] == 0               # refusal wrote NOTHING


def test_lifecycle_state_is_mandatory_and_closed(db):
    import pytest as _pytest

    from featuregen.overlay.upload.features import FeatureSpec, register_feature

    spec = FeatureSpec(name="no_default", aggregation="sum",
                       derives_from=(("deposits", "public.accounts.balance"),))
    with _pytest.raises(TypeError):
        register_feature(db, spec)                             # every writer states its intent
    with _pytest.raises(ValueError):
        register_feature(db, spec, lifecycle_state="published")
