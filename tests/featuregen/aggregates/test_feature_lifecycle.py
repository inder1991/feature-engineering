from featuregen.events.store import load_stream
from featuregen.aggregates.feature_lifecycle import (
    raise_monitoring_alert_command, require_revalidation_command,
    record_revalidation_outcome_command,
)
from tests.featuregen._helpers import make_cmd, make_actor


def _svc():
    return make_actor(subject="service:monitoring", actor_kind="service", roles=("monitoring",))


def test_revalidated_returns_to_production(db):
    raise_monitoring_alert_command(db, make_cmd("raise_monitoring_alert", "feature", "feat_a",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    require_revalidation_command(db, make_cmd("require_revalidation", "feature", "feat_a",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    res = record_revalidation_outcome_command(db, make_cmd("record_revalidation_outcome",
        "feature", "feat_a", {"feature_version_id": "fv_1", "outcome": "revalidated"}, actor=_svc()))
    assert res.accepted
    last = load_stream(db, "feature", "feat_a")[-1]
    assert last.type == "REVALIDATION_OUTCOME_RECORDED" and last.payload["outcome"] == "revalidated"


def test_require_revalidation_rejected_without_prior_alert(db):
    res = require_revalidation_command(db, make_cmd("require_revalidation", "feature", "feat_b",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    assert res.accepted is False and "MONITORING_ALERT" in res.denied_reason


def test_requires_change_spawns_new_run(db):
    raise_monitoring_alert_command(db, make_cmd("raise_monitoring_alert", "feature", "feat_c",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    require_revalidation_command(db, make_cmd("require_revalidation", "feature", "feat_c",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    res = record_revalidation_outcome_command(db, make_cmd("record_revalidation_outcome",
        "feature", "feat_c", {"feature_version_id": "fv_1", "outcome": "requires_change"}, actor=_svc()))
    outcome = load_stream(db, "feature", "feat_c")[-1]
    new_run = outcome.payload["new_run_id"]
    assert new_run and new_run.startswith("run_")
    created = load_stream(db, "run", new_run)[0]
    assert created.type == "RUN_CREATED" and created.feature_id == "feat_c"


def test_deprecate_outcome_sets_active_map_deprecated(db):
    db.execute("INSERT INTO feature_versions (feature_version_id, feature_id, produced_by_run, "
               "verification_stamp, risk_tier, approval_type, content_hash) "
               "VALUES ('fv_x','feat_d','run_x','DATA-CHECKED','low','PRODUCTION','sha256:1')")
    db.execute("INSERT INTO feature_active_versions (feature_id, use_case, feature_version_id, "
               "activation_state, activated_seq) VALUES ('feat_d','fraud','fv_x','PRODUCTION',1)")
    raise_monitoring_alert_command(db, make_cmd("raise_monitoring_alert", "feature", "feat_d",
        {"feature_version_id": "fv_x"}, actor=_svc()))
    require_revalidation_command(db, make_cmd("require_revalidation", "feature", "feat_d",
        {"feature_version_id": "fv_x"}, actor=_svc()))
    record_revalidation_outcome_command(db, make_cmd("record_revalidation_outcome",
        "feature", "feat_d", {"feature_version_id": "fv_x", "outcome": "deprecate"}, actor=_svc()))
    state = db.execute("SELECT activation_state FROM feature_active_versions "
                       "WHERE feature_id='feat_d'").fetchone()[0]
    assert state == "DEPRECATED"
