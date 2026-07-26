from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from featuregen.identity.current_principal import (
    CurrentPrincipalResolution,
    PrincipalResolutionStatus,
)
from featuregen.overlay.upload.recipe_formula_shadow import (
    build_capture_entries,
    declare_expected_run,
    write_manifest,
    write_work_item,
)
from featuregen.overlay.upload.recipe_formula_worker import (
    process_recipe_formula_shadow_once,
)
from featuregen.runtime.queue import enqueue_checked


def _seed_work(db, suffix: str = "1"):
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
        recipe_expectation_hash=f"expectation-hash-{suffix}",
        binding_envelope={"bindings": []},
        binding_envelope_hash=f"binding-hash-{suffix}",
        provider_input={
            "hypothesis": "merchant diversity",
            "prediction_goal": "identify merchant fraud",
            "target_entity": "customer",
            "formula_expectation": expectation,
        },
        provider_input_hash=f"provider-hash-{suffix}",
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


def test_revoked_principal_terminalizes_without_dispatch(db, monkeypatch) -> None:
    work_item_id, run_id = _seed_work(db, "revoked")
    called = False

    def _run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring", _run)
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
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration",
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
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring", _run)
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
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_worker.validate_recipe_provider_payload",
        lambda value: None,
    )
    frozen = SimpleNamespace(
        formula_facts=lambda proposal: ({}, {}),
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
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring", _run)
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
    assert received["facts_reader"] is frozen.formula_facts
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
        "featuregen.overlay.upload.recipe_formula_worker.verify_frozen_configuration",
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
        "featuregen.overlay.upload.recipe_formula_worker.run_authoring", _run)
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
