from tests.featuregen._helpers import make_actor, make_cmd

from featuregen.aggregates.feature_lifecycle import (
    raise_monitoring_alert_command,
    record_revalidation_outcome_command,
    require_revalidation_command,
)
from featuregen.events.store import load_stream


def _svc():
    return make_actor(subject="service:monitoring", actor_kind="service", roles=("monitoring",))


def test_revalidated_returns_to_production(db):
    raise_monitoring_alert_command(
        db,
        make_cmd(
            "raise_monitoring_alert",
            "feature",
            "feat_a",
            {"feature_version_id": "fv_1"},
            actor=_svc(),
        ),
    )
    require_revalidation_command(
        db,
        make_cmd(
            "require_revalidation",
            "feature",
            "feat_a",
            {"feature_version_id": "fv_1"},
            actor=_svc(),
        ),
    )
    res = record_revalidation_outcome_command(
        db,
        make_cmd(
            "record_revalidation_outcome",
            "feature",
            "feat_a",
            {"feature_version_id": "fv_1", "outcome": "revalidated"},
            actor=_svc(),
        ),
    )
    assert res.accepted
    last = load_stream(db, "feature", "feat_a")[-1]
    assert last.type == "REVALIDATION_OUTCOME_RECORDED" and last.payload["outcome"] == "revalidated"


def test_require_revalidation_rejected_without_prior_alert(db):
    res = require_revalidation_command(
        db,
        make_cmd(
            "require_revalidation",
            "feature",
            "feat_b",
            {"feature_version_id": "fv_1"},
            actor=_svc(),
        ),
    )
    assert res.accepted is False and "MONITORING_ALERT" in res.denied_reason


def test_requires_change_spawns_new_run(db):
    raise_monitoring_alert_command(
        db,
        make_cmd(
            "raise_monitoring_alert",
            "feature",
            "feat_c",
            {"feature_version_id": "fv_1"},
            actor=_svc(),
        ),
    )
    require_revalidation_command(
        db,
        make_cmd(
            "require_revalidation",
            "feature",
            "feat_c",
            {"feature_version_id": "fv_1"},
            actor=_svc(),
        ),
    )
    record_revalidation_outcome_command(
        db,
        make_cmd(
            "record_revalidation_outcome",
            "feature",
            "feat_c",
            {"feature_version_id": "fv_1", "outcome": "requires_change"},
            actor=_svc(),
        ),
    )
    outcome = load_stream(db, "feature", "feat_c")[-1]
    new_run = outcome.payload["new_run_id"]
    assert new_run and new_run.startswith("run_")
    created = load_stream(db, "run", new_run)[0]
    assert created.type == "RUN_CREATED" and created.feature_id == "feat_c"


def test_deprecate_outcome_sets_active_map_deprecated(db):
    db.execute(
        "INSERT INTO feature_versions (feature_version_id, feature_id, produced_by_run, "
        "verification_stamp, risk_tier, approval_type, content_hash) "
        "VALUES ('fv_x','feat_d','run_x','DATA-CHECKED','low','PRODUCTION','sha256:1')"
    )
    db.execute(
        "INSERT INTO feature_active_versions (feature_id, use_case, feature_version_id, "
        "activation_state, activated_seq) VALUES ('feat_d','fraud','fv_x','PRODUCTION',1)"
    )
    raise_monitoring_alert_command(
        db,
        make_cmd(
            "raise_monitoring_alert",
            "feature",
            "feat_d",
            {"feature_version_id": "fv_x"},
            actor=_svc(),
        ),
    )
    require_revalidation_command(
        db,
        make_cmd(
            "require_revalidation",
            "feature",
            "feat_d",
            {"feature_version_id": "fv_x"},
            actor=_svc(),
        ),
    )
    record_revalidation_outcome_command(
        db,
        make_cmd(
            "record_revalidation_outcome",
            "feature",
            "feat_d",
            {"feature_version_id": "fv_x", "outcome": "deprecate"},
            actor=_svc(),
        ),
    )
    state = db.execute(
        "SELECT activation_state FROM feature_active_versions WHERE feature_id='feat_d'"
    ).fetchone()[0]
    assert state == "DEPRECATED"


def test_deprecate_outcome_without_version_is_rejected_loudly(db):
    # A "deprecate" outcome with a NULL feature_version_id would build an UPDATE ... WHERE
    # feature_version_id = NULL, which matches ZERO rows (SQL three-valued logic) — silently
    # deprecating NOTHING while claiming success. That under-deprecation trap leaves a version that
    # should be gone still in PRODUCTION. The command MUST fail loudly instead of no-oping.
    db.execute(
        "INSERT INTO feature_versions (feature_version_id, feature_id, produced_by_run, "
        "verification_stamp, risk_tier, approval_type, content_hash) "
        "VALUES ('fv_z','feat_f','run_f','DATA-CHECKED','low','PRODUCTION','sha256:1')"
    )
    db.execute(
        "INSERT INTO feature_active_versions (feature_id, use_case, feature_version_id, "
        "activation_state, activated_seq) VALUES ('feat_f','fraud','fv_z','PRODUCTION',1)"
    )
    raise_monitoring_alert_command(
        db,
        make_cmd(
            "raise_monitoring_alert",
            "feature",
            "feat_f",
            {"feature_version_id": "fv_z"},
            actor=_svc(),
        ),
    )
    require_revalidation_command(
        db,
        make_cmd(
            "require_revalidation",
            "feature",
            "feat_f",
            {"feature_version_id": "fv_z"},
            actor=_svc(),
        ),
    )
    res = record_revalidation_outcome_command(
        db,
        make_cmd(
            "record_revalidation_outcome",
            "feature",
            "feat_f",
            {"outcome": "deprecate"},  # NO feature_version_id — must be rejected, not no-oped
            actor=_svc(),
        ),
    )
    # Loud failure: a denial, never a false success while deprecating nothing.
    assert res.accepted is False and "feature_version_id" in res.denied_reason
    # No REVALIDATION_OUTCOME_RECORDED event may be emitted on the rejected path.
    assert load_stream(db, "feature", "feat_f")[-1].type == "REVALIDATION_REQUIRED"
    # The slot the operator failed to name stays untouched — it was NOT silently left half-handled.
    state = db.execute(
        "SELECT activation_state FROM feature_active_versions WHERE feature_id='feat_f'"
    ).fetchone()[0]
    assert state == "PRODUCTION"


def test_deprecate_outcome_scopes_to_revalidated_version(db):
    # Two slots under the SAME feature but DIFFERENT use_cases + versions. A revalidation
    # "deprecate" outcome for ONE version must deprecate only that slot — never every use_case
    # of the feature (feature_active_versions grain is (feature_id, use_case)).
    for fv in ("fv_p", "fv_q"):
        db.execute(
            "INSERT INTO feature_versions (feature_version_id, feature_id, produced_by_run, "
            "verification_stamp, risk_tier, approval_type, content_hash) "
            "VALUES (%s,'feat_e','run_e','DATA-CHECKED','low','PRODUCTION','sha256:1')",
            (fv,),
        )
    db.execute(
        "INSERT INTO feature_active_versions (feature_id, use_case, feature_version_id, "
        "activation_state, activated_seq) VALUES "
        "('feat_e','fraud','fv_p','PRODUCTION',1),"
        "('feat_e','credit','fv_q','PRODUCTION',2)"
    )
    raise_monitoring_alert_command(
        db,
        make_cmd(
            "raise_monitoring_alert",
            "feature",
            "feat_e",
            {"feature_version_id": "fv_p"},
            actor=_svc(),
        ),
    )
    require_revalidation_command(
        db,
        make_cmd(
            "require_revalidation",
            "feature",
            "feat_e",
            {"feature_version_id": "fv_p"},
            actor=_svc(),
        ),
    )
    record_revalidation_outcome_command(
        db,
        make_cmd(
            "record_revalidation_outcome",
            "feature",
            "feat_e",
            {"feature_version_id": "fv_p", "outcome": "deprecate"},
            actor=_svc(),
        ),
    )
    states = dict(
        db.execute(
            "SELECT use_case, activation_state FROM feature_active_versions WHERE feature_id='feat_e'"
        ).fetchall()
    )
    assert states["fraud"] == "DEPRECATED"  # the revalidated version's slot
    assert states["credit"] == "PRODUCTION"  # the unrelated version's slot must survive


def test_deprecate_outcome_no_active_slot_is_rejected_not_falsely_recorded(db):
    # A "deprecate" outcome whose version is no longer the active slot must UPDATE 0 rows and
    # be REJECTED (accepted=False) — it must NOT emit REVALIDATION_OUTCOME_RECORDED and claim
    # success while deprecating nothing (SP-0.5 round-2: revalidation rowcount).
    db.execute(
        "INSERT INTO feature_versions (feature_version_id, feature_id, produced_by_run, "
        "verification_stamp, risk_tier, approval_type, content_hash) "
        "VALUES ('fv_g','feat_g','run_g','DATA-CHECKED','low','PRODUCTION','sha256:9')"
    )
    db.execute(
        "INSERT INTO feature_active_versions (feature_id, use_case, feature_version_id, "
        "activation_state, activated_seq) VALUES ('feat_g','fraud','fv_g','PRODUCTION',1)"
    )
    raise_monitoring_alert_command(
        db,
        make_cmd("raise_monitoring_alert", "feature", "feat_g", {"feature_version_id": "fv_g"},
                 actor=_svc()),
    )
    require_revalidation_command(
        db,
        make_cmd("require_revalidation", "feature", "feat_g", {"feature_version_id": "fv_g"},
                 actor=_svc()),
    )
    # The active slot is gone by the time the outcome is recorded (already superseded/removed),
    # so the scoped deprecate UPDATE matches 0 rows.
    db.execute("DELETE FROM feature_active_versions WHERE feature_id='feat_g'")

    res = record_revalidation_outcome_command(
        db,
        make_cmd("record_revalidation_outcome", "feature", "feat_g",
                 {"feature_version_id": "fv_g", "outcome": "deprecate"}, actor=_svc()),
    )
    assert res.accepted is False  # nothing deprecated -> not a success
    # No false audit trail: the last feature event is still REVALIDATION_REQUIRED, not OUTCOME.
    assert load_stream(db, "feature", "feat_g")[-1].type == "REVALIDATION_REQUIRED"
