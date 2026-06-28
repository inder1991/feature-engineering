from datetime import datetime, timedelta, timezone

from sp0.contracts import Disposition, HandlerContext
from sp0.events.store import load_stream
from sp0.runtime.step import commit_step
from sp0.aggregates._append import current_version, provenance_for
from sp0.aggregates.feature_versions import mint_feature_version
from sp0.aggregates.activation import (
    apply_activation, activate_command, request_activation, deactivate_expired_version_command,
    on_run_approved, _cas_claim_slot, ACTIVATE_VERSION_HANDLER,
)
from tests.sp0._helpers import make_actor, make_cmd


def _mint(db, feature_id, run, base=None, approval="PRODUCTION", expires=None):
    return mint_feature_version(
        db, feature_id=feature_id, produced_by_run=run, verification_stamp="USEFULNESS-CHECKED",
        risk_tier="low", approval_type=approval, approved_use_cases=("fraud",),
        blocked_use_cases=(), required_artifact_refs={}, content_hash="sha256:" + run,
        actor=make_actor(), provenance=provenance_for(),
        base_feature_version_id=base, expires_at=expires)


def test_first_activation_from_null_base_succeeds(db):
    v1 = _mint(db, "feat_a", "run1")
    res = apply_activation(db, feature_id="feat_a", feature_version_id=v1, use_case="fraud",
                           base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    assert res.activated and not res.conflict
    row = db.execute("SELECT feature_version_id, activation_state FROM feature_active_versions "
                     "WHERE feature_id='feat_a' AND use_case='fraud'").fetchone()
    assert row[0] == v1 and row[1] == "PRODUCTION"


def test_two_runs_from_v1_later_activation_fails_cas(db):
    v1 = _mint(db, "feat_b", "run1")
    apply_activation(db, feature_id="feat_b", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    v2 = _mint(db, "feat_b", "run2", base=v1)
    v3 = _mint(db, "feat_b", "run3", base=v1)
    ok = apply_activation(db, feature_id="feat_b", feature_version_id=v2, use_case="fraud",
                          base_feature_version_id=v1, approval_type="PRODUCTION", actor=make_actor())
    lose = apply_activation(db, feature_id="feat_b", feature_version_id=v3, use_case="fraud",
                            base_feature_version_id=v1, approval_type="PRODUCTION", actor=make_actor())
    assert ok.activated and lose.conflict
    row = db.execute("SELECT feature_version_id FROM feature_active_versions "
                     "WHERE feature_id='feat_b' AND use_case='fraud'").fetchone()
    assert row[0] == v2  # no silent overwrite
    assert load_stream(db, "feature", "feat_b")[-1].type == "ACTIVATION_CONFLICT"


def test_activation_is_idempotent(db):
    v1 = _mint(db, "feat_c", "run1")
    a = apply_activation(db, feature_id="feat_c", feature_version_id=v1, use_case="fraud",
                         base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    b = apply_activation(db, feature_id="feat_c", feature_version_id=v1, use_case="fraud",
                         base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    assert a.activated and b.activated
    activations = [e for e in load_stream(db, "feature", "feat_c") if e.type == "VERSION_ACTIVATED"]
    assert len(activations) == 1


def test_use_case_scoped_coexistence(db):
    v1 = _mint(db, "feat_d", "run1")
    v2 = _mint(db, "feat_d", "run2")
    apply_activation(db, feature_id="feat_d", feature_version_id=v1, use_case="credit",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    apply_activation(db, feature_id="feat_d", feature_version_id=v2, use_case="fraud",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    rows = dict(db.execute("SELECT use_case, feature_version_id FROM feature_active_versions "
                           "WHERE feature_id='feat_d'").fetchall())
    assert rows == {"credit": v1, "fraud": v2}


def test_experimental_activation_schedules_expiry_timer(db):
    exp = datetime.now(timezone.utc) + timedelta(days=30)
    v1 = _mint(db, "feat_e", "run1", approval="EXPERIMENTAL", expires=exp)
    apply_activation(db, feature_id="feat_e", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="EXPERIMENTAL",
                     actor=make_actor(), expires_at=exp)
    state = db.execute("SELECT activation_state FROM feature_active_versions "
                       "WHERE feature_id='feat_e' AND use_case='fraud'").fetchone()[0]
    assert state == "ACTIVE_EXPERIMENTAL"
    timer = db.execute("SELECT kind, payload->>'handler' FROM timers "
                       "WHERE aggregate='feature' AND aggregate_id='feat_e'").fetchone()
    assert timer == ("experiment_expiry", "deactivate_expired_version")


def test_request_activation_enqueues_feature_partition_and_appends_run_event(db):
    v1 = _mint(db, "feat_f", "run1")
    mid = request_activation(db, feature_id="feat_f", feature_version_id=v1, use_case="fraud",
                             base_feature_version_id=None, approval_type="PRODUCTION",
                             produced_by_run="run1", actor=make_actor())
    row = db.execute("SELECT partition_key, handler, payload FROM queue WHERE message_id=%s",
                     (mid,)).fetchone()
    assert row[0] == "feature:feat_f" and row[1] == "activate_version"
    # the queue payload lets the Phase-04 worker rebuild HandlerContext from the run stream
    assert row[2]["run_id"] == "run1" and "event_id" in row[2]
    req = load_stream(db, "run", "run1")[-1]
    assert req.type == "ACTIVATION_REQUESTED"
    assert req.payload["feature_version_id"] == v1 and req.payload["use_case"] == "fraud"


def test_cas_claim_slot_first_writer_wins_no_silent_overwrite(db):
    # Two writers that BOTH passed a stale current==base(None) precheck race on the slot.
    # The active-map write is the atomic gate: the first wins, the second loses (no overwrite).
    v1 = _mint(db, "feat_cas", "run1")
    v2 = _mint(db, "feat_cas", "run2")
    won1 = _cas_claim_slot(db, feature_id="feat_cas", use_case="fraud", new_fv=v1,
                           base=None, state="PRODUCTION", activated_seq=1)
    won2 = _cas_claim_slot(db, feature_id="feat_cas", use_case="fraud", new_fv=v2,
                           base=None, state="PRODUCTION", activated_seq=2)
    assert won1 is True and won2 is False
    row = db.execute("SELECT feature_version_id FROM feature_active_versions "
                     "WHERE feature_id='feat_cas' AND use_case='fraud'").fetchone()
    assert row[0] == v1  # later null-base writer did NOT silently overwrite the first


def test_saga_step1_mints_version_and_enqueues_in_one_tx(db):
    res = on_run_approved(
        db, feature_id="feat_saga", produced_by_run="run_appr", use_case="fraud",
        approval_type="PRODUCTION", actor=make_actor(), provenance=provenance_for(),
        verification_stamp="USEFULNESS-CHECKED", risk_tier="low", approved_use_cases=("fraud",),
        blocked_use_cases=(), required_artifact_refs={}, content_hash="sha256:saga",
        base_feature_version_id=None)
    assert res.feature_version_id.startswith("fv_") and res.activation_message_id
    # version frozen in the run tx (step 1a)
    assert db.execute("SELECT count(*) FROM feature_versions WHERE feature_version_id=%s",
                      (res.feature_version_id,)).fetchone()[0] == 1
    assert load_stream(db, "feature", "feat_saga")[-1].type == "VERSION_MINTED"
    # activation request enqueued + ACTIVATION_REQUESTED on the run stream (step 1b)
    q = db.execute("SELECT partition_key, handler FROM queue WHERE message_id=%s",
                   (res.activation_message_id,)).fetchone()
    assert q == ("feature:feat_saga", "activate_version")
    assert load_stream(db, "run", "run_appr")[-1].type == "ACTIVATION_REQUESTED"


def test_activate_version_handler_executes_feature_side_activation(db):
    # §5.8 saga step 2: the registered handler the Phase-04 worker dispatches. The handler is
    # PURE — it only DECLARES the activation; commit_step applies it on the step-tx conn.
    v1 = _mint(db, "feat_hdl", "run_h")
    request_activation(db, feature_id="feat_hdl", feature_version_id=v1, use_case="fraud",
                       base_feature_version_id=None, approval_type="PRODUCTION",
                       produced_by_run="run_h", actor=make_actor())
    req = load_stream(db, "run", "run_h")[-1]
    assert req.type == "ACTIVATION_REQUESTED"
    ctx = HandlerContext(run_id="run_h", triggering_event=req, documents={}, read_conn=db)
    result = ACTIVATE_VERSION_HANDLER.handle(ctx)
    assert result.disposition == Disposition.OK
    assert result.new_events == ()  # no run-stream events; commit_step writes only the ledger
    # handler is pure: it declares the effect and writes NOTHING itself.
    assert len(result.activations) == 1 and result.activations[0].feature_version_id == v1
    assert db.execute("SELECT count(*) FROM feature_active_versions "
                      "WHERE feature_id='feat_hdl'").fetchone()[0] == 0
    # commit_step applies the declared activation on the step-transaction conn.
    commit_step(db, ctx, result, message_id="msg_hdl",
                expected_version=current_version(db, "run", "run_h"), table_version=1)
    row = db.execute("SELECT feature_version_id, activation_state FROM feature_active_versions "
                     "WHERE feature_id='feat_hdl' AND use_case='fraud'").fetchone()
    assert row == (v1, "PRODUCTION")
    assert load_stream(db, "feature", "feat_hdl")[-1].type == "VERSION_ACTIVATED"


def test_activate_version_handler_is_idempotent(db):
    v1 = _mint(db, "feat_hdl2", "run_h2")
    request_activation(db, feature_id="feat_hdl2", feature_version_id=v1, use_case="fraud",
                       base_feature_version_id=None, approval_type="PRODUCTION",
                       produced_by_run="run_h2", actor=make_actor())
    req = load_stream(db, "run", "run_h2")[-1]
    ctx = HandlerContext(run_id="run_h2", triggering_event=req, documents={}, read_conn=db)
    # two deliveries; each goes handler -> commit_step; apply_activation no-ops the second time.
    commit_step(db, ctx, ACTIVATE_VERSION_HANDLER.handle(ctx), message_id="msg_h2a",
                expected_version=current_version(db, "run", "run_h2"), table_version=1)
    commit_step(db, ctx, ACTIVATE_VERSION_HANDLER.handle(ctx), message_id="msg_h2b",
                expected_version=current_version(db, "run", "run_h2"), table_version=1)
    activations = [e for e in load_stream(db, "feature", "feat_hdl2") if e.type == "VERSION_ACTIVATED"]
    assert len(activations) == 1  # idempotent: one effect


def test_activation_is_atomic_with_step_rollback(db):
    # A failure anywhere in the step rolls back the ENTIRE step: no orphan active-map row,
    # no VERSION_ACTIVATED event, no expiry timer. Proves apply_activation ran on the step-tx
    # conn (not an autocommit handler conn).
    exp = datetime.now(timezone.utc) + timedelta(days=30)
    v1 = _mint(db, "feat_atom", "run_atom", approval="EXPERIMENTAL", expires=exp)
    request_activation(db, feature_id="feat_atom", feature_version_id=v1, use_case="fraud",
                       base_feature_version_id=None, approval_type="EXPERIMENTAL",
                       produced_by_run="run_atom", actor=make_actor(), expires_at=exp)
    req = load_stream(db, "run", "run_atom")[-1]
    ctx = HandlerContext(run_id="run_atom", triggering_event=req, documents={}, read_conn=db)
    result = ACTIVATE_VERSION_HANDLER.handle(ctx)
    try:
        with db.transaction():  # mirrors process_one's per-step savepoint
            commit_step(db, ctx, result, message_id="msg_atom",
                        expected_version=current_version(db, "run", "run_atom"), table_version=1)
            raise RuntimeError("boom: forced failure after commit_step, before savepoint release")
    except RuntimeError:
        pass
    assert db.execute("SELECT count(*) FROM feature_active_versions "
                      "WHERE feature_id='feat_atom'").fetchone()[0] == 0
    assert [e for e in load_stream(db, "feature", "feat_atom")
            if e.type == "VERSION_ACTIVATED"] == []
    assert db.execute("SELECT count(*) FROM timers WHERE aggregate_id='feat_atom'").fetchone()[0] == 0


def test_deactivate_expired_version_removes_active_entry(db):
    exp = datetime.now(timezone.utc) + timedelta(days=1)
    v1 = _mint(db, "feat_g", "run1", approval="EXPERIMENTAL", expires=exp)
    apply_activation(db, feature_id="feat_g", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="EXPERIMENTAL",
                     actor=make_actor(), expires_at=exp)
    res = deactivate_expired_version_command(
        db, make_cmd("deactivate_expired_version", "feature", "feat_g",
                     {"feature_version_id": v1, "use_case": "fraud"}))
    assert res.accepted
    assert db.execute("SELECT count(*) FROM feature_active_versions "
                      "WHERE feature_id='feat_g'").fetchone()[0] == 0
    assert load_stream(db, "feature", "feat_g")[-1].type == "VERSION_EXPIRED"
    # idempotent second fire
    again = deactivate_expired_version_command(
        db, make_cmd("deactivate_expired_version", "feature", "feat_g",
                     {"feature_version_id": v1, "use_case": "fraud"}))
    assert again.accepted and again.produced_event_ids == ()


def test_activate_command_wraps_apply_activation(db):
    v1 = _mint(db, "feat_h", "run1")
    res = activate_command(db, make_cmd("activate", "feature", "feat_h",
        {"feature_version_id": v1, "use_case": "fraud", "base_feature_version_id": None,
         "approval_type": "PRODUCTION"}))
    assert res.accepted and len(res.produced_event_ids) == 1
