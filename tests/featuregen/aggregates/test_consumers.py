from tests.featuregen._helpers import make_actor, make_cmd

from featuregen.aggregates._append import provenance_for
from featuregen.aggregates.activation import apply_activation
from featuregen.aggregates.consumers import (
    deprecate_command,
    deregister_consumer_command,
    finalize_deprecate_command,
    register_consumer_command,
    retier_command,
    supersede_command,
)
from featuregen.aggregates.feature_versions import mint_feature_version
from featuregen.events.store import load_stream


def _mint(db, feature_id, run, base=None, tier="low"):
    return mint_feature_version(
        db,
        feature_id=feature_id,
        produced_by_run=run,
        verification_stamp="DATA-CHECKED",
        risk_tier=tier,
        approval_type="PRODUCTION",
        approved_use_cases=("fraud",),
        blocked_use_cases=(),
        required_artifact_refs={},
        content_hash="sha256:" + run,
        actor=make_actor(),
        provenance=provenance_for(),
        base_feature_version_id=base,
    )


def test_register_then_deregister_consumer(db):
    v1 = _mint(db, "feat_a", "run1")
    register_consumer_command(
        db,
        make_cmd(
            "register_consumer",
            "feature",
            "feat_a",
            {"consumer_kind": "model", "consumer_ref": "model:churn", "feature_version_id": v1},
        ),
    )
    active = db.execute(
        "SELECT count(*) FROM consumers WHERE feature_id='feat_a' AND edge_status='active'"
    ).fetchone()[0]
    assert active == 1
    deregister_consumer_command(
        db,
        make_cmd(
            "deregister_consumer",
            "feature",
            "feat_a",
            {"consumer_kind": "model", "consumer_ref": "model:churn"},
        ),
    )
    active = db.execute(
        "SELECT count(*) FROM consumers WHERE feature_id='feat_a' AND edge_status='active'"
    ).fetchone()[0]
    assert active == 0


def test_deprecate_blocked_while_active_consumer_exists(db):
    v1 = _mint(db, "feat_b", "run1")
    apply_activation(
        db,
        feature_id="feat_b",
        feature_version_id=v1,
        use_case="fraud",
        base_feature_version_id=None,
        approval_type="PRODUCTION",
        actor=make_actor(),
    )
    register_consumer_command(
        db,
        make_cmd(
            "register_consumer",
            "feature",
            "feat_b",
            {"consumer_kind": "model", "consumer_ref": "model:churn"},
        ),
    )
    blocked = deprecate_command(
        db,
        make_cmd("deprecate", "feature", "feat_b", {"feature_version_id": v1, "use_case": "fraud"}),
    )
    assert blocked.accepted is False and "consumer" in blocked.denied_reason
    deregister_consumer_command(
        db,
        make_cmd(
            "deregister_consumer",
            "feature",
            "feat_b",
            {"consumer_kind": "model", "consumer_ref": "model:churn"},
        ),
    )
    ok = deprecate_command(
        db,
        make_cmd("deprecate", "feature", "feat_b", {"feature_version_id": v1, "use_case": "fraud"}),
    )
    assert ok.accepted
    state = db.execute(
        "SELECT activation_state FROM feature_active_versions "
        "WHERE feature_id='feat_b' AND use_case='fraud'"
    ).fetchone()[0]
    assert state == "DEPRECATED"


def test_supersede_updates_active_and_keeps_prior_immutable(db):
    v1 = _mint(db, "feat_c", "run1")
    apply_activation(
        db,
        feature_id="feat_c",
        feature_version_id=v1,
        use_case="fraud",
        base_feature_version_id=None,
        approval_type="PRODUCTION",
        actor=make_actor(),
    )
    v2 = _mint(db, "feat_c", "run2", base=v1)
    res = supersede_command(
        db,
        make_cmd(
            "supersede",
            "feature",
            "feat_c",
            {"feature_version_id": v2, "use_case": "fraud", "expected_prior": v1},
        ),
    )
    assert res.accepted
    row = db.execute(
        "SELECT feature_version_id FROM feature_active_versions "
        "WHERE feature_id='feat_c' AND use_case='fraud'"
    ).fetchone()
    assert row[0] == v2
    assert (
        db.execute(
            "SELECT immutable FROM feature_versions WHERE feature_version_id=%s", (v1,)
        ).fetchone()[0]
        is True
    )
    assert load_stream(db, "feature", "feat_c")[-1].type == "VERSION_SUPERSEDED"


def test_supersede_requires_expected_prior(db):
    """supersede must CAS on expected_prior; omitting it must be denied, not an unconditional
    overwrite of the active slot (review MAJOR #14 — parity with activate's mandatory CAS)."""
    v1 = _mint(db, "feat_g", "run1")
    apply_activation(
        db,
        feature_id="feat_g",
        feature_version_id=v1,
        use_case="fraud",
        base_feature_version_id=None,
        approval_type="PRODUCTION",
        actor=make_actor(),
    )
    v2 = _mint(db, "feat_g", "run2", base=v1)
    result = supersede_command(
        db,
        make_cmd(
            "supersede",
            "feature",
            "feat_g",
            {"feature_version_id": v2, "use_case": "fraud"},  # no expected_prior
        ),
    )
    assert result.accepted is False
    assert "expected_prior" in (result.denied_reason or "")
    # no clobber: the production-active slot still holds v1, and no event was appended.
    row = db.execute(
        "SELECT feature_version_id FROM feature_active_versions "
        "WHERE feature_id='feat_g' AND use_case='fraud'"
    ).fetchone()
    assert row[0] == v1
    assert load_stream(db, "feature", "feat_g")[-1].type != "VERSION_SUPERSEDED"


def test_supersede_stale_expected_prior_conflicts(db):
    v1 = _mint(db, "feat_h", "run1")
    apply_activation(
        db,
        feature_id="feat_h",
        feature_version_id=v1,
        use_case="fraud",
        base_feature_version_id=None,
        approval_type="PRODUCTION",
        actor=make_actor(),
    )
    v2 = _mint(db, "feat_h", "run2", base=v1)
    result = supersede_command(
        db,
        make_cmd(
            "supersede",
            "feature",
            "feat_h",
            {"feature_version_id": v2, "use_case": "fraud", "expected_prior": "fv_stale_nonmatch"},
        ),
    )
    assert result.accepted is False  # CAS mismatch, no clobber
    row = db.execute(
        "SELECT feature_version_id FROM feature_active_versions "
        "WHERE feature_id='feat_h' AND use_case='fraud'"
    ).fetchone()
    assert row[0] == v1
    assert load_stream(db, "feature", "feat_h")[-1].type != "VERSION_SUPERSEDED"


def test_retier_emits_event_without_mutating_version(db):
    v1 = _mint(db, "feat_d", "run1", tier="high")
    res = retier_command(
        db,
        make_cmd("retier", "feature", "feat_d", {"feature_version_id": v1, "new_risk_tier": "low"}),
    )
    assert res.accepted
    assert (
        db.execute(
            "SELECT risk_tier FROM feature_versions WHERE feature_version_id=%s", (v1,)
        ).fetchone()[0]
        == "high"
    )
    last = load_stream(db, "feature", "feat_d")[-1]
    assert last.type == "VERSION_RETIERED"
    assert last.payload == {**last.payload, "old_risk_tier": "high", "new_risk_tier": "low"}


def test_force_deprecate_quiesces_with_grace_not_immediate_deprecation(db):
    v1 = _mint(db, "feat_e", "run1")
    apply_activation(
        db,
        feature_id="feat_e",
        feature_version_id=v1,
        use_case="fraud",
        base_feature_version_id=None,
        approval_type="PRODUCTION",
        actor=make_actor(),
    )
    register_consumer_command(
        db,
        make_cmd(
            "register_consumer",
            "feature",
            "feat_e",
            {"consumer_kind": "model", "consumer_ref": "model:churn"},
        ),
    )
    res = deprecate_command(
        db,
        make_cmd(
            "deprecate",
            "feature",
            "feat_e",
            {
                "feature_version_id": v1,
                "use_case": "fraud",
                "force_quiesce": True,
                "grace_seconds": 3600,
            },
        ),
    )
    assert res.accepted
    quiesced = load_stream(db, "feature", "feat_e")[-1]
    assert quiesced.type == "VERSION_QUIESCED"
    assert quiesced.payload["impacted_consumers"] == ["model:churn"]  # impact analysis recorded
    # active version is NOT deprecated yet — it is quiescing through the grace window
    state = db.execute(
        "SELECT activation_state FROM feature_active_versions "
        "WHERE feature_id='feat_e' AND use_case='fraud'"
    ).fetchone()[0]
    assert state == "PRODUCTION"
    timer = db.execute(
        "SELECT kind, payload->>'handler' FROM timers "
        "WHERE aggregate='feature' AND aggregate_id='feat_e'"
    ).fetchone()
    assert timer == ("business_repair", "finalize_deprecate")


def test_finalize_deprecate_completes_after_grace_and_is_idempotent(db):
    v1 = _mint(db, "feat_f", "run1")
    apply_activation(
        db,
        feature_id="feat_f",
        feature_version_id=v1,
        use_case="fraud",
        base_feature_version_id=None,
        approval_type="PRODUCTION",
        actor=make_actor(),
    )
    register_consumer_command(
        db,
        make_cmd(
            "register_consumer",
            "feature",
            "feat_f",
            {"consumer_kind": "model", "consumer_ref": "model:churn"},
        ),
    )
    deprecate_command(
        db,
        make_cmd(
            "deprecate",
            "feature",
            "feat_f",
            {"feature_version_id": v1, "use_case": "fraud", "force_quiesce": True},
        ),
    )
    res = finalize_deprecate_command(
        db,
        make_cmd(
            "finalize_deprecate",
            "feature",
            "feat_f",
            {"feature_version_id": v1, "use_case": "fraud"},
        ),
    )
    assert res.accepted and len(res.produced_event_ids) == 1
    last = load_stream(db, "feature", "feat_f")[-1]
    assert last.type == "VERSION_DEPRECATED" and last.payload["via"] == "quiesce"
    state = db.execute(
        "SELECT activation_state FROM feature_active_versions "
        "WHERE feature_id='feat_f' AND use_case='fraud'"
    ).fetchone()[0]
    assert state == "DEPRECATED"
    # idempotent second fire: no further event
    again = finalize_deprecate_command(
        db,
        make_cmd(
            "finalize_deprecate",
            "feature",
            "feat_f",
            {"feature_version_id": v1, "use_case": "fraud"},
        ),
    )
    assert again.accepted and again.produced_event_ids == ()
