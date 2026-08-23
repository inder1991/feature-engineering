from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from featuregen.formula.control import RecoveryRequiresReconciliation
from featuregen.identity.current_principal import (
    CurrentPrincipalResolution,
    PrincipalResolutionStatus,
)
from featuregen.overlay.upload.recipe_formula_shadow import (
    build_capture_entries,
    content_hash,
    declare_expected_run,
    write_manifest,
    write_work_item,
)
from featuregen.overlay.upload.recipe_formula_worker import (
    process_recipe_formula_shadow_once,
)
from featuregen.runtime.queue import enqueue_checked

_V2_WINDOW = {
    "event_time_role": "clock", "length_parameter": "window_days", "basis": "trailing",
    "unit": "day", "start_inclusive": "inclusive", "end_inclusive": "exclusive",
    "timezone": "UTC", "empty_window": "null", "null_input": "ignore", "offset_periods": 0,
}

#: The plain projection of a bound ``BoundRecipeFormulaExpectationV2`` — what a captured v2 work
#: item actually carries on ``provider_input_json.formula_expectation`` (A4 increment 1's arm).
_V2_EXPECTATION = {
    "formula_schema_version": "formula-v2",
    "final_operation": "identity",
    "grain_entity": "customer",
    "grain_key_refs": ["ftr::public.tx.customer_id"],
    "expressions": [{
        "expression_path": "body.expr",
        "aggregation": "count_distinct",
        "operand_ref": "ftr::public.tx.merchant_category",
        "second_operand_ref": None,
        "source_relation_ref": "ftr::public.tx",
        "event_time_ref": "ftr::public.tx.tran_date",
        "window_length": 90,
        "window": dict(_V2_WINDOW),
        "aggregation_argument": None,
        "authority_refs": None,
        "term_name": "",
        "term_sign": 0,
    }],
    "decimal": {"precision": 18, "scale": 6, "rounding": "half_even", "overflow": "error"},
    "policy_version": 1,
}


#: What a REAL work item declares. Defaulted here because the producer defaults it too:
#: `build_recipe_authoring_egress` writes `formula-v2` into every v2 payload and every capturable
#: recipe is v2. A test seeding an UNDECLARED item is testing the producer-bug path, and says so.
_DECLARED_BY_REAL_PRODUCERS = "formula-v2"


def _seed_work(db, suffix: str = "1", *,
               declared_schema: str | None = _DECLARED_BY_REAL_PRODUCERS):
    intent_id = f"intent-worker-{suffix}"
    run_id = f"run-worker-{suffix}"
    scope_id = f"scope-worker-{suffix}"
    revision_id = f"revision-worker-{suffix}"
    considered_hash = f"considered-worker-{suffix}"
    db.execute(
        "INSERT INTO contract_intent "
        "(intent_id,hypothesis,intake_mode,redacted_hypothesis) "
        "VALUES (%s,'h','hypothesis','h')",
        (intent_id,),
    )
    db.execute(
        "INSERT INTO feature_generation_run (generation_run_id,intent_id,actor) "
        "VALUES (%s,%s,'{}'::jsonb)",
        (run_id, intent_id),
    )
    db.execute(
        "INSERT INTO confirmed_generation_scope "
        "(scope_id,intent_id,generation_run_id,expansion,scope_mode,"
        "confirmation_source,confirmed_by) "
        "VALUES (%s,%s,%s,'strict','scoped','user','user:test')",
        (scope_id, intent_id, run_id),
    )
    db.execute(
        "INSERT INTO contract_considered_revision "
        "(considered_revision_id,intent_id,generation_run_id,considered_json,"
        "considered_content_hash,canonicalization_version) "
        "VALUES (%s,%s,%s,'{}'::jsonb,%s,'test-v1')",
        (revision_id, intent_id, run_id, considered_hash),
    )
    manifest_id = declare_expected_run(
        db,
        generation_run_id=run_id,
        intent_id=intent_id,
        confirmed_scope_id=scope_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        ranking_flag=True,
    )
    ranked = (SimpleNamespace(
        recipe_id="merchant_mcc_diversity",
        canonical_rank=1,
        selected_for_initial_view=True,
        rank_reasons=("primary",),
        initial_view_reasons=("selected",),
    ),)
    entries = build_capture_entries(
        generation_run_id=run_id,
        ranking_version="rank-v1",
        ranked=ranked,
        candidate_keys_by_recipe_id={"merchant_mcc_diversity": ("candidate-1",)},
    )
    write_manifest(
        db,
        manifest_id=manifest_id,
        generation_run_id=run_id,
        intent_id=intent_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        ranking_version="rank-v1",
        ranked=ranked,
        entries=entries,
        ranking_enabled=True,
    )
    expectation = {
        "final_operation": "identity",
        "grain_entity": "customer",
        "grain_key_refs": ["ftr::public.tx.customer_id"],
        "expressions": [{
            "aggregation": "count_distinct",
            "operand_ref": "ftr::public.tx.merchant_category",
            "source_relation_ref": "ftr::public.tx",
            "event_time_ref": "ftr::public.tx.tran_date",
            "window_length": 90,
        }],
        "decimal": {
            "precision": 18,
            "scale": 6,
            "rounding": "half_even",
            "overflow": "error",
        },
    }
    if declared_schema == "formula-v2":
        # A real v2 payload, not a v1 one with a version key bolted on: the worker hands this
        # dict straight to `recipe_expectation_validator_v2`.
        expectation = deepcopy(_V2_EXPECTATION)
    elif declared_schema is not None:
        expectation["formula_schema_version"] = declared_schema
    work_item_id = f"work-worker-{suffix}"
    write_work_item(
        db,
        work_item_id=work_item_id,
        idempotency_key=f"work-key-{suffix}",
        capture_entry_id=entries[0].capture_entry_id,
        generation_run_id=run_id,
        intent_id=intent_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        metadata_snapshot_id=f"snapshot-worker-{suffix}",
        metadata_snapshot_content_hash=f"snapshot-hash-{suffix}",
        recipe_id="merchant_mcc_diversity",
        recipe_candidate_key="candidate-1",
        recipe_expectation=expectation,
        recipe_expectation_hash=content_hash(expectation),
        binding_envelope={"bindings": []},
        binding_envelope_hash=content_hash({"bindings": []}),
        provider_input={
            "hypothesis": "merchant diversity",
            "prediction_goal": "identify merchant fraud",
            "target_entity": "customer",
            "formula_expectation": expectation,
        },
        provider_input_hash=content_hash({
            "hypothesis": "merchant diversity",
            "prediction_goal": "identify merchant fraud",
            "target_entity": "customer",
            "formula_expectation": expectation,
        }),
        frozen_configuration={"configuration_hash": f"config-hash-{suffix}"},
        frozen_configuration_hash=f"config-hash-{suffix}",
        request_identity={
            "subject": "user:test",
            "actor_kind": "human",
            "authenticated": True,
            "auth_method": "password",
            "role_claims": ["analyst"],
        },
        request_read_scope_hash=f"scope-hash-{suffix}",
    )
    enqueue_checked(
        db,
        message_id=f"formula-test-{suffix}",
        partition_key=run_id,
        handler="recipe_formula_shadow.author.v1",
        payload={"work_item_id": work_item_id},
    )
    return work_item_id, run_id


class _IdentityResolver:
    def __init__(self, status: PrincipalResolutionStatus):
        self.status = status

    def resolve_current_principal(self, conn, subject, tenant, observed_at):
        del conn, subject, tenant, observed_at
        principal = (
            SimpleNamespace(role_claims=("analyst",))
            if self.status is PrincipalResolutionStatus.CURRENT
            else None
        )
        return CurrentPrincipalResolution(self.status, principal)


@dataclass(frozen=True)
class _AuthoringResult:
    authoring_disposition: str
    technical_status: str
    authoring_run_id: str


def _prepare_dispatch_ready(monkeypatch, suffix: str, _dsn: str):
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker._current_read_scope_hash",
        lambda *args: f"scope-hash-{suffix}",
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.compare_snapshot_to_current",
        lambda *args: SimpleNamespace(
            status="current", current_content_hash=f"snapshot-hash-{suffix}"),
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_formula_authority_envelope",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.load_frozen_configuration_json",
        lambda value: object(),
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration_v2",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.validate_recipe_provider_payload",
        lambda value: None,
    )
    frozen = SimpleNamespace(
        formula_facts=lambda proposal: ({}, {}),
        formula_facts_v2=lambda proposal: ({}, ()),
        get_column_metadata=lambda ref: {"found": True, "logical_ref": ref},
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.FrozenRecipeReadContext.load",
        lambda *args: frozen,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration_v2",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.renew_recipe_formula_shadow",
        lambda *args, **kwargs: True,
    )
    return frozen


def _authoring_run_id(work_item_id: str) -> str:
    return "far_" + hashlib.sha256(work_item_id.encode()).hexdigest()[:24]


@pytest.mark.parametrize("declared", ["formula-v7", "formula-v1.5", "2", ""])
def test_an_unknown_expectation_declaration_never_reaches_any_orchestrator(
    db, monkeypatch, declared
) -> None:
    """The half of A4 increment 2's gate whose cause is still real. A declaration THIS BUILD has
    never heard of is a fact about US: authoring never ran, nothing was dispatched, and no verdict
    about the recipe was written anywhere. ``authoring_axis`` is NOT_RUN rather than UNSUPPORTED,
    because UNSUPPORTED is a capability verdict about a proposal and here none exists.

    (The ``formula-v2`` case that used to sit in this parametrization is gone with its cause: the
    replay-shaped v2 orchestrator landed, and the test below asserts the routing that replaced it.)
    """
    work_item_id, run_id = _seed_work(db, f"schema-{declared or 'blank'}",
                                      declared_schema=declared)
    reached = []

    def _never(name):
        def _run(*args, **kwargs):
            reached.append(name)
            raise AssertionError(f"the {name} orchestrator must never see an unknown declaration")
        return _run

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay",
        _never("v2"))
    # Everything downstream of the gate is left REAL: if the gate were absent the run would
    # proceed, not be saved by a stub.
    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-1",
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT),
    )
    assert outcome.status == "completed"
    assert outcome.work_item_id == work_item_id
    assert reached == []
    row = db.execute(
        "SELECT capture_axis,authorization_axis,authority_axis,drift_axis,configuration_axis,"
        "delivery_axis,authoring_axis,technical_axis,authoring_run_id,authoring_result_json "
        "FROM recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone()
    assert row == ("CAPTURED", "NOT_EVALUATED", "NOT_EVALUATED", "NOT_EVALUATED",
                   "NOT_EVALUATED", "NOT_DISPATCHED", "NOT_RUN",
                   "EXPECTATION_SCHEMA_UNKNOWN", None, None)
    assert db.execute("SELECT count(*) FROM llm_dispatch").fetchone()[0] == 0
    assert db.execute(
        "SELECT status FROM recipe_formula_shadow_run_manifest WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone()[0] == "COMPLETE"


def test_a_declared_v2_work_item_is_authored_by_the_REPLAY_SHAPED_v2_orchestrator(
    db, monkeypatch, _dsn
) -> None:
    """THE ROUTING. A4 increment 2 terminalized this work item ``V2_AUTHORING_UNAVAILABLE``
    because no replay-shaped v2 orchestrator existed; it does now, so the guard is gone and the
    item is authored.

    Every seam handed over is asserted to be the **v2** one — a v2 proposal validated by the v1
    validator, or resolved over a body-path-keyed bundle, would produce a confident verdict out of
    the wrong evidence. The v1 orchestrator is monkeypatched to RAISE, so no stub can hide a
    mis-route."""
    work_item_id, run_id = _seed_work(db, "v2-routed", declared_schema="formula-v2")
    frozen = _prepare_dispatch_ready(monkeypatch, "v2-routed", _dsn)
    received: dict = {}

    def _v1(*args, **kwargs):
        raise AssertionError("a formula-v2 work item must never reach the v1 orchestrator")

    def _v2(*args, **kwargs):
        received.update(kwargs)
        kwargs["progress_callback"]()
        return _AuthoringResult("RESOLVED", "ok", _authoring_run_id(work_item_id))

    # The v1 arm is GONE, so "routed to v2 and not to v1" is no longer two stubs — there is one
    # orchestrator, and that it is reached with the V2 SEAMS is what
    # `test_the_v2_route_uses_the_v2_validator_and_the_v2_tools` proves.
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay", _v2)
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.formula_dispatches_reconciled",
        lambda *args: True)

    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-1",
        author_client=object(),
        critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT),
    )
    assert outcome.status == "completed", db.execute(
        "SELECT last_error FROM queue WHERE message_id='formula-test-v2-routed'").fetchone()
    assert received["facts_reader"] is frozen.formula_facts_v2, (
        "v1's bundle is keyed by body path and would resolve every v2 operand to empty facts")
    assert received["critic_metadata_loader"] is frozen.get_column_metadata
    assert received["authoring_run_id"] == _authoring_run_id(work_item_id)
    assert received["lease_fence"] is not None
    assert db.execute(
        "SELECT delivery_axis,authoring_axis,technical_axis "
        "FROM recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone() == ("DISPATCHED_AUDITED", "RESOLVED", "OK")


def test_the_v2_route_uses_the_v2_validator_and_the_v2_tools(db, monkeypatch, _dsn) -> None:
    """The two remaining seams, proved by BEHAVIOUR rather than by identity: the validator that
    arrives understands a v2 proposal (v1's answers ``FINAL_OPERATION_NOT_PRESERVED`` to anything
    that is not a ``UnaryBody``), and the tool runner answers ``list_supported_operations`` in the
    v2 grammar (v1's answers out of the v1 enum)."""
    from featuregen.formula.schema_v2 import AggregateFunctionV2

    _seed_work(db, "v2-seams", declared_schema="formula-v2")
    _prepare_dispatch_ready(monkeypatch, "v2-seams", _dsn)
    received: dict = {}

    def _v2(*args, **kwargs):
        received.update(kwargs)
        return _AuthoringResult("RESOLVED", "ok", "far_seams")

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay", _v2)
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.formula_dispatches_reconciled",
        lambda *args: True)
    process_recipe_formula_shadow_once(
        db, owner="worker-1", author_client=object(), critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT))

    answer = received["tool_runner"](object(), "list_supported_operations", {})
    assert {item["name"] for item in answer["aggregate_functions"]} == {
        fn.value for fn in AggregateFunctionV2}

    from featuregen.formula.parse_v2 import parse_proposal_v2

    proposal = parse_proposal_v2({
        "formula_schema_version": 2, "operation_grammar_version": 1,
        "canonicalization_version": 1,
        "grain": {"entity": "customer", "keys": ["ftr::public.tx.customer_id"]},
        "body": {"final_operation": "identity", "expr": {
            "aggregation": "count_distinct", "operand": "ftr::public.tx.merchant_category",
            "source_relation": {"table_ref": "ftr::public.tx"}, "filter": None,
            "window": {"event_time_ref": "ftr::public.tx.tran_date", "basis": "trailing",
                       "length": 90, "unit": "day", "start_inclusive": "inclusive",
                       "end_inclusive": "exclusive", "timezone": "UTC", "empty_window": "null",
                       "null_input": "ignore", "offset_periods": 0},
            "aggregation_argument": None, "second_operand": None, "authority_refs": None}},
        "parameters": [], "expected_output": None, "allocation_policy_ref": "",
        "decimal": {"precision": 18, "scale": 6, "rounding": "half_even", "overflow": "error"}})
    # The proposal PRESERVES the seeded v2 expectation exactly, so the v2 validator passes it.
    # This used to also assert v1's validator returned FINAL_OPERATION_NOT_PRESERVED on the same
    # proposal — "which is why the sibling exists". That validator is deleted along with the v1
    # arm, so the claim moved to
    # `test_THERE_IS_NO_V1_VALIDATOR_OR_TOOL_RUNNER_LEFT_TO_MIS_HAND_A_PROPOSAL_TO`, which fails if
    # it comes back. What is still asserted here is the half that is about THIS route: the worker
    # wired the V2 validator, and it passes a proposal that preserves the expectation.
    assert received["proposal_validator"](proposal) == ()


def test_a_v2_work_item_verifies_the_V2_frozen_configuration(db, monkeypatch, _dsn) -> None:
    """A v2 work item frozen under the v1 author identity is DRIFT, and the worker must ask the v2
    question. Asserted by which verifier is called, and by the v1 one being poisoned."""
    _seed_work(db, "v2-config", declared_schema="formula-v2")
    _prepare_dispatch_ready(monkeypatch, "v2-config", _dsn)
    asked: list[str] = []
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration_v2",
        lambda *a, **k: asked.append("v1"))
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration_v2",
        lambda *a, **k: asked.append("v2"))
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay",
        lambda *a, **k: _AuthoringResult("RESOLVED", "ok", "far_config"))
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.formula_dispatches_reconciled",
        lambda *args: True)
    process_recipe_formula_shadow_once(
        db, owner="worker-1", author_client=object(), critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT))
    assert asked == ["v2"]


def test_a_second_operand_reaches_the_frozen_read_context(db, monkeypatch, _dsn) -> None:
    """The forward gap A4 increment 3 recorded, now REACHABLE and closed. A two-operand v2 body
    (``date_diff_avg``-shaped) needs its second column's governed facts as much as its first;
    without this the tool runner would refuse to read it and the facts reader would resolve it to
    nothing."""
    from featuregen.overlay.upload.recipe_formula_worker import _formula_refs

    two_operand = {
        "final_operation": "identity",
        "grain_entity": "customer",
        "grain_key_refs": ["ftr::public.tx.customer_id"],
        "expressions": [{
            "aggregation": "date_diff_avg",
            "operand_ref": "ftr::public.tx.opened_dt",
            "second_operand_ref": "ftr::public.tx.closed_dt",
            "source_relation_ref": "ftr::public.tx",
            "event_time_ref": "ftr::public.tx.tran_date",
            "window_length": 90,
        }],
    }
    assert _formula_refs(two_operand) == frozenset({
        "ftr::public.tx.customer_id", "ftr::public.tx.opened_dt",
        "ftr::public.tx.closed_dt", "ftr::public.tx.tran_date"})

    # ...and the SAME widening leaves every v1 expectation byte-identical, because a v1 bound
    # expectation has no such key at all.
    _work_item_id, _run_id = _seed_work(db, "v1-refs-unchanged")
    row = db.execute(
        "SELECT provider_input_json FROM recipe_formula_shadow_work_item "
        "WHERE work_item_id='work-worker-v1-refs-unchanged'").fetchone()[0]
    assert _formula_refs(row["formula_expectation"]) == frozenset({
        "ftr::public.tx.customer_id", "ftr::public.tx.merchant_category",
        "ftr::public.tx.tran_date"})


def test_a_work_item_DECLARING_NOTHING_IS_TERMINAL(db, monkeypatch, _dsn) -> None:
    """The inversion this retirement is FOR, and the reason it is safe now.

    Absence used to mean `formula-v1` — correct while the v1 payload shape genuinely carried no
    version key. Every capturable recipe now declares `formula-v2`, so absence can only mean a
    PRODUCER that failed to declare, and routing that down a lane which no longer exists would
    author a bug instead of reporting one.

    UNDECLARED, not UNKNOWN: unknown is a work item from a newer build — somebody else's deploy —
    and undeclared is ours.
    """
    _work_item_id, _run_id = _seed_work(db, "undeclared", declared_schema=None)
    _prepare_dispatch_ready(monkeypatch, "undeclared", _dsn)
    reached = False

    def _never_run(*args, **kwargs):
        nonlocal reached
        reached = True
        raise AssertionError("an undeclared work item must never reach the orchestrator")

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay", _never_run)
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.formula_dispatches_reconciled",
        lambda *args: True)
    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-1",
        author_client=object(),
        critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT),
    )
    # "completed" is the QUEUE's word — handled, not to be retried. The verdict is the axis; a
    # retry here would re-run a producer bug forever.
    assert outcome.status == "completed"
    assert reached is False
    observed = db.execute(
        "SELECT technical_axis FROM recipe_formula_shadow_observation "
        "WHERE generation_run_id = %s", (_run_id,)).fetchone()
    assert observed is not None and observed[0] == "EXPECTATION_SCHEMA_UNDECLARED"


def test_revoked_principal_terminalizes_without_dispatch(db, monkeypatch) -> None:
    work_item_id, run_id = _seed_work(db, "revoked")
    called = False

    def _run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay", _run)
    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-1",
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.REVOKED),
    )
    assert outcome.status == "completed"
    assert outcome.work_item_id == work_item_id
    assert not called
    row = db.execute(
        "SELECT authorization_axis,delivery_axis,status "
        "FROM recipe_formula_shadow_observation o "
        "JOIN recipe_formula_shadow_run_manifest m USING (generation_run_id) "
        "WHERE o.generation_run_id=%s",
        (run_id,),
    ).fetchone()
    assert row == ("AUTHORIZATION_REVOKED", "NOT_DISPATCHED", "COMPLETE")


def test_configuration_drift_is_terminal_not_retryable(db, monkeypatch) -> None:
    _work_item_id, run_id = _seed_work(db, "config")
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker._current_read_scope_hash",
        lambda *args: "scope-hash-config",
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.compare_snapshot_to_current",
        lambda *args: SimpleNamespace(
            status="current", current_content_hash="snapshot-hash-config"),
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_formula_authority_envelope",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.load_frozen_configuration_json",
        lambda value: object(),
    )

    def _drift(*args, **kwargs):
        from featuregen.formula.frozen_configuration import ConfigurationDrifted

        raise ConfigurationDrifted("changed")

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration_v2",
        _drift,
    )
    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-1",
        author_client=object(),
        critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT),
    )
    assert outcome.status == "completed"
    row = db.execute(
        "SELECT configuration_axis,delivery_axis FROM "
        "recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone()
    assert row == ("DRIFTED", "NOT_DISPATCHED")


def test_authority_drift_terminalizes_before_provider_dispatch(db, monkeypatch) -> None:
    _work_item_id, run_id = _seed_work(db, "authority")
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker._current_read_scope_hash",
        lambda *args: "scope-hash-authority",
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.compare_snapshot_to_current",
        lambda *args: SimpleNamespace(
            status="current", current_content_hash="snapshot-hash-authority"),
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_formula_authority_envelope",
        lambda *args: "EVENT_TIME_AUTHORITY_DRIFT",
    )
    called = False

    def _run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay", _run)
    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-1",
        author_client=object(),
        critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT),
    )
    assert outcome.status == "completed"
    assert not called
    row = db.execute(
        "SELECT authority_axis,drift_axis,delivery_axis "
        "FROM recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone()
    assert row == (
        "EVENT_TIME_AUTHORITY_DRIFT",
        "AUTHORITY_DRIFTED",
        "NOT_DISPATCHED",
    )


def test_success_uses_frozen_readers_and_completes_fenced_work(
    db, monkeypatch, _dsn
) -> None:
    work_item_id, run_id = _seed_work(db, "success")
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker._current_read_scope_hash",
        lambda *args: "scope-hash-success",
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.compare_snapshot_to_current",
        lambda *args: SimpleNamespace(
            status="current", current_content_hash="snapshot-hash-success"),
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_formula_authority_envelope",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.load_frozen_configuration_json",
        lambda value: object(),
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration_v2",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.validate_recipe_provider_payload",
        lambda value: None,
    )
    frozen = SimpleNamespace(
        formula_facts=lambda proposal: ({}, {}),
        formula_facts_v2=lambda proposal: ({}, ()),
        get_column_metadata=lambda ref: {"found": True, "logical_ref": ref},
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.FrozenRecipeReadContext.load",
        lambda *args: frozen,
    )
    received = {}

    def _run(*args, **kwargs):
        received.update(kwargs)
        kwargs["progress_callback"]()
        return _AuthoringResult("RESOLVED", "ok", "authoring-success")

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay", _run)
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.formula_dispatches_reconciled",
        lambda *args: True,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.renew_recipe_formula_shadow",
        lambda *args, **kwargs: True,
    )
    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-1",
        author_client=object(),
        critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT),
    )
    assert outcome.status == "completed", db.execute(
        "SELECT last_error FROM queue WHERE message_id='formula-test-success'"
    ).fetchone()
    # The V2 reader: `formula_facts` is body-path-keyed and `formula_facts_v2` is not, so handing
    # the v2 orchestrator the v1 bundle would resolve a v2 proposal over evidence keyed for another
    # shape and still look confident.
    assert received["facts_reader"] is frozen.formula_facts_v2
    assert received["critic_metadata_loader"] is frozen.get_column_metadata
    assert db.execute(
        "SELECT status FROM queue WHERE message_id='formula-test-success'"
    ).fetchone()[0] == "done"
    row = db.execute(
        "SELECT delivery_axis,authoring_axis,technical_axis "
        "FROM recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone()
    assert row == ("DISPATCHED_AUDITED", "RESOLVED", "OK")
    assert work_item_id == outcome.work_item_id


def test_missing_durable_audit_store_terminalizes_without_dispatch(
    db, monkeypatch
) -> None:
    _work_item_id, run_id = _seed_work(db, "no-audit-store")
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker._current_read_scope_hash",
        lambda *args: "scope-hash-no-audit-store",
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.compare_snapshot_to_current",
        lambda *args: SimpleNamespace(
            status="current",
            current_content_hash="snapshot-hash-no-audit-store",
        ),
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_formula_authority_envelope",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.load_frozen_configuration_json",
        lambda value: object(),
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration_v2",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.validate_recipe_provider_payload",
        lambda value: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.FrozenRecipeReadContext.load",
        lambda *args: object(),
    )
    called = False

    def _run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay", _run)
    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-1",
        author_client=object(),
        critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT),
    )
    assert outcome.status == "completed"
    assert not called
    assert db.execute(
        "SELECT delivery_axis,authoring_axis,technical_axis "
        "FROM recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone() == (
        "NOT_DISPATCHED",
        "NOT_RUN",
        "AUDIT_STORE_UNAVAILABLE",
    )


def test_completed_durable_authoring_is_delegated_to_verified_replay(
    db, monkeypatch, _dsn
) -> None:
    work_item_id, run_id = _seed_work(db, "recover-complete")
    _prepare_dispatch_ready(monkeypatch, "recover-complete", _dsn)
    authoring_run_id = _authoring_run_id(work_item_id)
    db.execute(
        "INSERT INTO formula_authoring_run "
        "(authoring_run_id,intent_hash,versions,actor) "
        "VALUES (%s,'intent-hash','{}'::jsonb,NULL)",
        (authoring_run_id,),
    )
    terminal = {
        "authoring_disposition": "RESOLVED",
        "candidate_formula_hash": "formula-hash",
        "structural_status": "ok",
        "capability_status": "ok",
        "output_status": "resolved",
        "expectation_status": "match",
        "critic_status": "clean",
        "technical_status": "ok",
    }
    db.execute(
        "INSERT INTO formula_authoring_trace_event "
        "(authoring_run_id,seq,kind,idempotency_key,payload) "
        "VALUES (%s,1,'completed',%s,%s)",
        (authoring_run_id, f"{authoring_run_id}:terminal", Jsonb(terminal)),
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.formula_dispatches_reconciled",
        lambda *args: True,
    )
    orchestration_calls = 0

    def _run(*args, **kwargs):
        nonlocal orchestration_calls
        orchestration_calls += 1
        return _AuthoringResult("RESOLVED", "ok", authoring_run_id)

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay", _run)
    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-recovery",
        author_client=object(),
        critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT),
    )
    assert outcome.status == "completed", db.execute(
        "SELECT last_error FROM queue WHERE message_id='formula-test-recover-complete'"
    ).fetchone()
    assert orchestration_calls == 1
    assert db.execute(
        "SELECT delivery_axis,authoring_axis "
        "FROM recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone() == ("DISPATCHED_AUDITED", "RESOLVED")


def test_incomplete_prior_dispatch_is_not_automatically_reissued(
    db, monkeypatch, _dsn
) -> None:
    work_item_id, run_id = _seed_work(db, "recover-ambiguous")
    _prepare_dispatch_ready(monkeypatch, "recover-ambiguous", _dsn)
    authoring_run_id = _authoring_run_id(work_item_id)
    db.execute(
        "INSERT INTO formula_authoring_run "
        "(authoring_run_id,intent_hash,versions,actor) "
        "VALUES (%s,'intent-hash','{}'::jsonb,NULL)",
        (authoring_run_id,),
    )
    called = False

    def _run(*args, **kwargs):
        nonlocal called
        called = True
        raise RecoveryRequiresReconciliation("ambiguous pre-dispatch record")

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring_v2_replay", _run)
    outcome = process_recipe_formula_shadow_once(
        db,
        owner="worker-recovery",
        author_client=object(),
        critic_client=object(),
        identity_resolver=_IdentityResolver(PrincipalResolutionStatus.CURRENT),
    )
    assert outcome.status == "completed", db.execute(
        "SELECT last_error FROM queue WHERE message_id='formula-test-recover-ambiguous'"
    ).fetchone()
    assert called
    assert db.execute(
        "SELECT delivery_axis,technical_axis "
        "FROM recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone() == (
        "PRIOR_DISPATCH_UNRECONCILED",
        "RECOVERY_REQUIRES_RECONCILIATION",
    )


def test_the_read_scope_recomputation_excludes_the_semantic_context_pin(db) -> None:
    """THE DEFECT E0's authored step found, in isolation.

    ``gate1`` seals ``request_read_scope_hash`` over the CANDIDATES' ``(catalog_source,
    object_ref)`` pairs. SE-2 then seals one extra snapshot item per catalog run — the frozen
    Layer-A context's identity PIN, whose ``graph_ref`` is a read-scope key (``context:<…>``),
    not a catalog object. The worker re-hashed **every** snapshot item, so on any run carrying a
    semantic context — which is the live path — the two could never be equal and EVERY formula
    shadow work item terminalized ``AUTHORIZATION_SCOPE_CHANGED`` without authoring anything.

    Nothing is unverified by the exclusion: the pin has its own freshness comparator and is
    checked by ``compare_snapshot_to_current`` a few lines later in the same worker.
    """
    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.recipe_formula_worker import _current_read_scope_hash

    snapshot_id = "snap-read-scope"
    db.execute(
        "INSERT INTO contract_intent (intent_id,hypothesis,intake_mode,redacted_hypothesis) "
        "VALUES ('intent-read-scope','h','hypothesis','h')")
    db.execute(
        "INSERT INTO feature_generation_run (generation_run_id,intent_id,actor) "
        "VALUES ('run-read-scope','intent-read-scope','{}'::jsonb)")
    rows = [
        ("bank", "public.txns.txn_amt", "column_field", "h1"),
        ("bank", "public.txns.event_ts", "column_field", "h2"),
        ("bank", "context:bank|public.txns", "generation_semantic_context", "h3"),
        ("bank", "public.txns.acct_id", "column_field", "h4"),
    ]
    for source, graph_ref, item_kind, item_hash in rows:
        db.execute(
            "INSERT INTO catalog_metadata_snapshot_item "
            "(snapshot_id, catalog_source, graph_ref, item_kind, field_or_fact_type, item_hash) "
            "VALUES (%s, %s, %s, %s, 'unit', %s)",
            (snapshot_id, source, graph_ref, item_kind, item_hash))
    # The header is written AFTER the items: the store seals an item set the moment its header
    # exists, which is the same discipline the real snapshot builder follows.
    db.execute(
        "INSERT INTO catalog_metadata_snapshot (snapshot_id, generation_run_id, "
        "read_scope_hash, isolation_level, content_hash) "
        "VALUES (%s, 'run-read-scope', 'rs', 'repeatable read', 'ch')",
        (snapshot_id,))

    expected = canonical_hash({
        "refs": [["bank", "public.txns.acct_id"], ["bank", "public.txns.event_ts"],
                 ["bank", "public.txns.txn_amt"]],
        "roles": ["platform_admin"],
    })
    assert _current_read_scope_hash(db, snapshot_id, ("platform_admin",)) == expected

    # ``item_kind`` is NOT NULL, so the exclusion is total and no row can slip past it as a NULL.
    # The worker still spells the filter ``IS DISTINCT FROM``; that is defence in depth, and this
    # is where the assumption behind it is written down.
    assert db.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'catalog_metadata_snapshot_item' AND column_name = 'item_kind'"
    ).fetchone()[0] == "NO"
