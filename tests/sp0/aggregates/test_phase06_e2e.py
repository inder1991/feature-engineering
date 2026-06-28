# tests/sp0/aggregates/test_phase06_e2e.py
import pytest

from sp0.events.store import load_stream
from sp0.commands.api import execute_command
from sp0.commands.registry import clear_registry
from sp0.aggregates.commands import register_phase06_commands
from sp0.aggregates._append import provenance_for
from sp0.aggregates.feature_versions import mint_feature_version
from tests.sp0._helpers import make_cmd, make_actor


@pytest.fixture(autouse=True)
def _registered():
    clear_registry()
    register_phase06_commands()
    yield
    clear_registry()


def test_multi_candidate_request_flow_through_execute_command(db):
    req = execute_command(db, make_cmd("create_request", "request", None,
        {"feature_concept": "salary irregularity"})).aggregate_id
    a = execute_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    b = execute_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    res = execute_command(db, make_cmd("select_candidate", "request", req,
        {"selections": ({"run_id": a},), "candidates_explored_count": 5}))
    assert res.accepted
    assert any(e.type == "RUN_REJECTED" for e in load_stream(db, "run", b))
    sel = [e for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"][0]
    assert sel.payload["candidates_explored_count"] == 5


def test_activation_cas_oracle_through_execute_command(db):
    def mint(feature_id, run, base=None):
        return mint_feature_version(
            db, feature_id=feature_id, produced_by_run=run, verification_stamp="USEFULNESS-CHECKED",
            risk_tier="low", approval_type="PRODUCTION", approved_use_cases=("fraud",),
            blocked_use_cases=(), required_artifact_refs={}, content_hash="sha256:" + run,
            actor=make_actor(), provenance=provenance_for(),
            base_feature_version_id=base)
    v1 = mint("feat_z", "r1")
    execute_command(db, make_cmd("activate", "feature", "feat_z",
        {"feature_version_id": v1, "use_case": "fraud", "base_feature_version_id": None,
         "approval_type": "PRODUCTION"}))
    v2 = mint("feat_z", "r2", base=v1)
    v3 = mint("feat_z", "r3", base=v1)
    execute_command(db, make_cmd("activate", "feature", "feat_z",
        {"feature_version_id": v2, "use_case": "fraud", "base_feature_version_id": v1,
         "approval_type": "PRODUCTION"}))
    execute_command(db, make_cmd("activate", "feature", "feat_z",
        {"feature_version_id": v3, "use_case": "fraud", "base_feature_version_id": v1,
         "approval_type": "PRODUCTION"}))
    active = db.execute("SELECT feature_version_id FROM feature_active_versions "
                        "WHERE feature_id='feat_z' AND use_case='fraud'").fetchone()[0]
    assert active == v2
    assert load_stream(db, "feature", "feat_z")[-1].type == "ACTIVATION_CONFLICT"


def test_command_double_submit_is_idempotent(db):
    cmd = make_cmd("create_request", "request", None, {"feature_concept": "double"}, idem="dup-key")
    first = execute_command(db, cmd)
    second = execute_command(db, cmd)
    assert first == second
    requests = db.execute("SELECT count(*) FROM events WHERE type='REQUEST_CREATED' "
                          "AND aggregate_id=%s", (first.aggregate_id,)).fetchone()[0]
    assert requests == 1


def test_every_catalog_action_is_registered():
    from sp0.commands.registry import get_command
    for action in [
        "create_request", "create_run", "duplicate_of", "select_candidate", "cancel",
        "withdraw", "reject", "park", "unpark", "reopen_as_new_run", "resolve_degraded",
        "fact_confirmed_resume", "source_changed_revalidate", "activate", "supersede",
        "deprecate", "finalize_deprecate", "retier", "register_consumer", "deregister_consumer",
        "raise_monitoring_alert", "require_revalidation", "record_revalidation_outcome",
        "deactivate_expired_version",
    ]:
        assert callable(get_command(action))


def test_resolve_degraded_unblocks_run_through_execute_command(db):
    db.execute(
        "INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version, "
        "degraded, degraded_reason) VALUES ('run_rd', 'req_rd', 'DRAFT', 1, true, 'boom')"
    )
    # a normal command on a degraded run is blocked...
    blocked = execute_command(db, make_cmd("park", "run", "run_rd", {"owner": "o"}))
    assert blocked.accepted is False and "degraded" in blocked.denied_reason
    # ...resolve_degraded bypasses the block and clears the flag...
    cleared = execute_command(db, make_cmd("resolve_degraded", "run", "run_rd", {}))
    assert cleared.accepted
    # ...and the run accepts commands again.
    ok = execute_command(db, make_cmd("park", "run", "run_rd", {"owner": "o"}))
    assert ok.accepted
