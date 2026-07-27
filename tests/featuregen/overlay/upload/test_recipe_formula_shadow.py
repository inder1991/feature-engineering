from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.rows import dict_row

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula.recipe_egress import (
    RecipeEgressViolation,
    build_recipe_authoring_egress,
)
from featuregen.formula.schema import (
    AggregateFunction,
    EmptyWindowResult,
    FinalOperation,
    Inclusivity,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    WindowBasis,
    WindowUnit,
)
from featuregen.overlay.upload import recipe_formula_shadow as shadow_module
from featuregen.overlay.upload.recipe_formula_contracts import (
    BoundExpressionExpectationV1,
    BoundRecipeFormulaExpectationV1,
    DecimalPolicyExpectationV1,
    WindowExpectationV1,
)
from featuregen.overlay.upload.recipe_formula_shadow import (
    RankedCaptureEntryV1,
    ShadowIntegrityError,
    build_capture_entries,
    capture_ranked_shadow,
    content_hash,
    declare_expected_run,
    finalize_manifest,
    reconcile_run,
    verify_expected_run_payload,
    verify_manifest_payload,
    verify_observation_payload,
    verify_work_item_payload,
    write_manifest,
    write_observation,
    write_work_item,
)


def _seed_lineage(db, suffix: str = "1"):
    intent_id = f"intent-shadow-{suffix}"
    run_id = f"run-shadow-{suffix}"
    scope_id = f"scope-shadow-{suffix}"
    revision_id = f"revision-shadow-{suffix}"
    considered_hash = f"considered-hash-{suffix}"
    db.execute(
        "INSERT INTO contract_intent "
        "(intent_id, hypothesis, intake_mode, redacted_hypothesis) "
        "VALUES (%s, 'h', 'hypothesis', 'h')",
        (intent_id,),
    )
    db.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
        "VALUES (%s, %s, '{}'::jsonb)",
        (run_id, intent_id),
    )
    db.execute(
        "INSERT INTO confirmed_generation_scope "
        "(scope_id, intent_id, generation_run_id, expansion, scope_mode, "
        "confirmation_source, confirmed_by) "
        "VALUES (%s, %s, %s, 'strict', 'scoped', 'user', 'user:test')",
        (scope_id, intent_id, run_id),
    )
    db.execute(
        "INSERT INTO contract_considered_revision "
        "(considered_revision_id, intent_id, generation_run_id, considered_json, "
        "considered_content_hash, canonicalization_version) "
        "VALUES (%s, %s, %s, '{}'::jsonb, %s, 'test-v1')",
        (revision_id, intent_id, run_id, considered_hash),
    )
    return intent_id, run_id, scope_id, revision_id, considered_hash


def _ranked():
    return (
        SimpleNamespace(
            recipe_id="merchant_mcc_diversity",
            canonical_rank=1,
            selected_for_initial_view=True,
            rank_reasons=("primary_use_case_match",),
            initial_view_reasons=("selected_initial_view",),
        ),
        SimpleNamespace(
            recipe_id="obligor_facility_count",
            canonical_rank=2,
            selected_for_initial_view=False,
            rank_reasons=("supporting_match",),
            initial_view_reasons=("family_cap_not_in_initial_view",),
        ),
    )


def _declare(db, suffix: str = "1"):
    intent_id, run_id, scope_id, revision_id, considered_hash = _seed_lineage(db, suffix)
    manifest_id = declare_expected_run(
        db,
        generation_run_id=run_id,
        intent_id=intent_id,
        confirmed_scope_id=scope_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        ranking_flag=True,
    )
    return intent_id, run_id, revision_id, considered_hash, manifest_id


def _capture_entry_and_common(db, suffix: str):
    intent_id, run_id, _revision_id, considered_hash, _manifest_id = _declare(db, suffix)
    revision_id = f"revision-shadow-{suffix}"
    entry = RankedCaptureEntryV1(
        capture_entry_id=f"entry-{suffix}",
        recipe_id="merchant_mcc_diversity",
        canonical_rank=1,
        selected_for_initial_view=True,
        rank_reasons=("primary",),
        initial_view_reasons=("selected",),
        recipe_candidate_key="candidate-1",
        candidate_resolution="EXACT",
        capture_required=True,
        capture_reason="selected_initial_view",
    )
    common = {
        "observation_id": f"observation-{suffix}",
        "idempotency_key": f"idempotency-{suffix}",
        "capture_entry_id": entry.capture_entry_id,
        "generation_run_id": run_id,
        "intent_id": intent_id,
        "considered_revision_id": revision_id,
        "considered_content_hash": considered_hash,
        "metadata_snapshot_id": "snapshot-test",
        "metadata_snapshot_content_hash": "snapshot-hash-test",
        "recipe_id": entry.recipe_id,
        "recipe_candidate_key": entry.recipe_candidate_key,
    }
    return entry, common


def _bound_expectation() -> BoundRecipeFormulaExpectationV1:
    window = WindowExpectationV1(
        event_time_role="event_ts",
        basis=WindowBasis.TRAILING,
        length_parameter="window",
        unit=WindowUnit.DAY,
        start_inclusive=Inclusivity.INCLUSIVE,
        end_inclusive=Inclusivity.EXCLUSIVE,
        timezone="Asia/Dubai",
        empty_window=EmptyWindowResult.NULL,
        null_input=NullInput.IGNORE,
    )
    return BoundRecipeFormulaExpectationV1(
        recipe_candidate_key="candidate-1",
        recipe_id="merchant_mcc_diversity",
        semantic_parameter_binding_hash="semantic-hash",
        final_operation=FinalOperation.IDENTITY,
        expressions=(
            BoundExpressionExpectationV1(
                expression_path="body.expr",
                aggregation=AggregateFunction.COUNT_DISTINCT,
                operand_ref="bank::public.txn.mcc",
                source_relation_ref="bank::public.txn",
                event_time_ref="bank::public.txn.event_ts",
                window_length=90,
                window=window,
            ),
        ),
        grain_entity="merchant",
        grain_key_refs=("bank::public.txn.merchant_id",),
        decimal=DecimalPolicyExpectationV1(
            precision=38,
            scale=6,
            rounding=RoundingMode.HALF_EVEN,
            overflow=OverflowBehavior.ERROR,
        ),
        blueprint_content_hash="blueprint-hash",
        policy_version=1,
    )


def test_expected_run_detects_wholly_missing_manifest(db):
    _intent, run_id, _revision, _hash, _manifest = _declare(db)
    result = reconcile_run(db, run_id)
    assert result.status == "INCOMPLETE"
    assert result.reason == "CAPTURE_MANIFEST_MISSING"


def test_manifest_and_observations_reconcile_exact_expected_population(db):
    intent_id, run_id, revision_id, considered_hash, manifest_id = _declare(db)
    ranked = _ranked()
    entries = build_capture_entries(
        generation_run_id=run_id,
        ranking_version="rank-v1",
        ranked=ranked,
        candidate_keys_by_recipe_id={
            "merchant_mcc_diversity": ("candidate-1",),
            "obligor_facility_count": (),
        },
    )
    assert entries[0].candidate_resolution == "EXACT"
    assert entries[1].candidate_resolution == "MISSING"
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
    before = reconcile_run(db, run_id)
    assert before.status == "INCOMPLETE"
    assert before.expected_observations == 1
    write_observation(
        db,
        observation_id="observation-1",
        idempotency_key="observation-key-1",
        capture_entry_id=entries[0].capture_entry_id,
        generation_run_id=run_id,
        intent_id=intent_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        recipe_id=entries[0].recipe_id,
        recipe_candidate_key=entries[0].recipe_candidate_key,
        capture_axis="CAPTURE_INPUT_INCOMPLETE",
        technical_axis="CAPTURE_NOT_WIRED",
    )
    complete = finalize_manifest(db, run_id)
    assert complete.status == "COMPLETE"
    assert complete.actual_observations == 1

    with db.cursor(row_factory=dict_row) as cursor:
        declaration = cursor.execute(
            "SELECT * FROM recipe_formula_shadow_expected_run WHERE generation_run_id=%s",
            (run_id,),
        ).fetchone()
        manifest = cursor.execute(
            "SELECT * FROM recipe_formula_shadow_run_manifest WHERE generation_run_id=%s",
            (run_id,),
        ).fetchone()
    assert verify_expected_run_payload(declaration) is None
    assert verify_manifest_payload(manifest) is None

    bad_declaration = dict(declaration)
    bad_declaration["ranking_flag"] = False
    assert verify_expected_run_payload(bad_declaration) == (
        "EXPECTED_RUN_DECLARATION_HASH_MISMATCH")
    bad_ranking = dict(manifest)
    bad_ranking["ranking_hash"] = "forged"
    assert verify_manifest_payload(bad_ranking) == "MANIFEST_RANKING_HASH_MISMATCH"
    bad_reconciliation = dict(manifest)
    bad_reconciliation["reconciliation_hash"] = "forged"
    assert verify_manifest_payload(bad_reconciliation) == (
        "MANIFEST_RECONCILIATION_HASH_MISMATCH")
    row = db.execute(
        "SELECT status, actual_observation_count FROM recipe_formula_shadow_run_manifest "
        "WHERE generation_run_id=%s",
        (run_id,),
    ).fetchone()
    assert row == ("COMPLETE", 1)
    assert finalize_manifest(db, run_id) == complete


def test_expected_manifest_and_observation_replays_are_content_checked(db):
    intent_id, run_id, scope_id, revision_id, considered_hash = _seed_lineage(db)
    manifest_id = declare_expected_run(
        db,
        generation_run_id=run_id,
        intent_id=intent_id,
        confirmed_scope_id=scope_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        ranking_flag=True,
    )
    assert declare_expected_run(
        db,
        generation_run_id=run_id,
        intent_id=intent_id,
        confirmed_scope_id=scope_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        ranking_flag=True,
    ) == manifest_id
    with pytest.raises(ShadowIntegrityError):
        declare_expected_run(
            db,
            generation_run_id=run_id,
            intent_id=intent_id,
            confirmed_scope_id=scope_id,
            considered_revision_id=revision_id,
            considered_content_hash=considered_hash,
            ranking_flag=False,
        )


def test_shadow_population_rows_are_write_once(db):
    intent_id, run_id, revision_id, considered_hash, _manifest_id = _declare(db)
    write_observation(
        db,
        observation_id="observation-worm",
        idempotency_key="observation-worm-key",
        capture_entry_id="entry-worm",
        generation_run_id=run_id,
        intent_id=intent_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        recipe_id="merchant_mcc_diversity",
        capture_axis="CAPTURE_INPUT_INCOMPLETE",
    )
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute(
            "UPDATE recipe_formula_shadow_observation SET technical_axis='tampered' "
            "WHERE observation_id='observation-worm'")
    cursor = db.execute(
        "SELECT * FROM recipe_formula_shadow_observation "
        "WHERE observation_id='observation-worm'"
    )
    columns = [description.name for description in cursor.description]
    stored = dict(zip(columns, cursor.fetchone(), strict=True))
    assert verify_observation_payload(stored) is None
    stored["technical_axis"] = "tampered"
    assert (
        verify_observation_payload(stored)
        == "OBSERVATION_PAYLOAD_HASH_MISMATCH"
    )


def test_unscoped_run_is_not_a_missing_manifest(db):
    result = reconcile_run(db, "never-enrolled")
    assert result.status == "NOT_IN_SHADOW_POPULATION"
    assert result.reason is None


def test_work_item_and_outbox_are_atomic_and_content_checked(db):
    intent_id, run_id, revision_id, considered_hash, _manifest_id = _declare(db)
    values = {
        "work_item_id": "work-1",
        "idempotency_key": "work-key-1",
        "capture_entry_id": "entry-work-1",
        "generation_run_id": run_id,
        "intent_id": intent_id,
        "considered_revision_id": revision_id,
        "considered_content_hash": considered_hash,
        "metadata_snapshot_id": None,
        "metadata_snapshot_content_hash": None,
        "recipe_id": "merchant_mcc_diversity",
        "recipe_candidate_key": "candidate-1",
        "recipe_expectation": {"recipe": "merchant_mcc_diversity"},
        "recipe_expectation_hash": content_hash(
            {"recipe": "merchant_mcc_diversity"}),
        "binding_envelope": {"bindings": []},
        "binding_envelope_hash": content_hash({"bindings": []}),
        "provider_input": {"hypothesis": "h"},
        "provider_input_hash": content_hash({"hypothesis": "h"}),
        "frozen_configuration": {"configuration_hash": "config-hash"},
        "frozen_configuration_hash": "config-hash",
        "request_identity": {"subject": "user:test"},
        "request_read_scope_hash": "scope-hash",
    }
    write_work_item(db, **values)
    write_work_item(db, **values)
    work = db.execute(
        "SELECT work_item_id,payload_hash FROM recipe_formula_shadow_work_item "
        "WHERE idempotency_key='work-key-1'",
    ).fetchone()
    outbox = db.execute(
        "SELECT topic,payload FROM outbox WHERE message_id='formula-shadow:work-1'",
    ).fetchone()
    assert work[0] == "work-1" and work[1]
    assert outbox == (
        "recipe_formula_shadow.requested.v1",
        {"work_item_id": "work-1"},
    )
    stored = db.execute(
        "SELECT * FROM recipe_formula_shadow_work_item "
        "WHERE work_item_id='work-1'",
    ).fetchone()
    columns = [
        description.name
        for description in db.execute(
            "SELECT * FROM recipe_formula_shadow_work_item LIMIT 0").description
    ]
    assert verify_work_item_payload(dict(zip(columns, stored, strict=True))) is None
    with pytest.raises(ShadowIntegrityError):
        write_work_item(db, **{**values, "provider_input_hash": "changed"})
    with pytest.raises(ShadowIntegrityError):
        write_work_item(
            db,
            **{
                **values,
                "request_identity": {"subject": "different-user"},
            },
        )
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute(
            "DELETE FROM recipe_formula_shadow_work_item WHERE work_item_id='work-1'")


def test_formula_capture_rejects_missing_sealed_generation_input_before_work_insert(db):
    entry, common = _capture_entry_and_common(db, "egress-missing")
    shadow_module._capture_selected_entry(
        db,
        index=0,
        entry=entry,
        common=common,
        grounding_context_by_candidate_key={"candidate-1": object()},
        metadata_snapshot_id="snapshot-test",
        metadata_snapshot_content_hash="snapshot-hash-test",
        identity=IdentityEnvelope(
            subject="user:test",
            actor_kind="human",
            authenticated=True,
            auth_method="password",
            role_claims=("analyst",),
        ),
        request_read_scope_hash="scope-hash",
    )
    assert db.execute(
        "SELECT delivery_axis,authoring_axis,technical_axis "
        "FROM recipe_formula_shadow_observation WHERE observation_id=%s",
        (common["observation_id"],),
    ).fetchone() == ("EGRESS_REJECTED", "NOT_RUN", "OK")
    assert db.execute(
        "SELECT count(*) FROM recipe_formula_shadow_work_item "
        "WHERE generation_run_id=%s",
        (common["generation_run_id"],),
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT count(*) FROM outbox WHERE message_id LIKE 'formula-shadow:%'",
    ).fetchone()[0] == 0


def test_formula_redactor_failure_persists_no_raw_prose_or_work(
    db, monkeypatch, caplog
):
    entry, common = _capture_entry_and_common(db, "egress-failure")
    raw_hypothesis = "customer named Never Persist This"
    raw_goal = "card 4111 1111 1111 1111 must never persist"
    monkeypatch.setattr(
        shadow_module,
        "generation_input_for_run",
        lambda _conn, _run: SimpleNamespace(
            intent_id=common["intent_id"],
            redacted_hypothesis=raw_hypothesis,
            redacted_prediction_goal=raw_goal,
        ),
    )
    monkeypatch.setattr(
        shadow_module, "bind_formula_expectation", lambda _context, _blueprint: object())
    monkeypatch.setattr(
        shadow_module,
        "build_recipe_authoring_egress",
        lambda **_kwargs: (_ for _ in ()).throw(
            RecipeEgressViolation("hypothesis prose redaction failed closed")),
    )
    shadow_module._capture_selected_entry(
        db,
        index=0,
        entry=entry,
        common=common,
        grounding_context_by_candidate_key={"candidate-1": object()},
        metadata_snapshot_id="snapshot-test",
        metadata_snapshot_content_hash="snapshot-hash-test",
        identity=IdentityEnvelope(
            subject="user:test",
            actor_kind="human",
            authenticated=True,
            auth_method="password",
            role_claims=("analyst",),
        ),
        request_read_scope_hash="scope-hash",
    )
    durable = "\n".join(
        str(row[0])
        for table in (
            "recipe_formula_shadow_expected_run",
            "recipe_formula_shadow_observation",
            "recipe_formula_shadow_work_item",
            "llm_call",
        )
        for row in db.execute(f"SELECT row_to_json(t)::text FROM {table} t").fetchall()
    )
    assert raw_hypothesis not in durable
    assert raw_goal not in durable
    assert raw_hypothesis not in caplog.text
    assert raw_goal not in caplog.text
    assert db.execute(
        "SELECT delivery_axis FROM recipe_formula_shadow_observation "
        "WHERE observation_id=%s",
        (common["observation_id"],),
    ).fetchone()[0] == "EGRESS_REJECTED"
    assert db.execute(
        "SELECT count(*) FROM recipe_formula_shadow_work_item "
        "WHERE generation_run_id=%s",
        (common["generation_run_id"],),
    ).fetchone()[0] == 0


def test_successful_formula_capture_persists_only_safe_prose_and_span_audit(db):
    intent_id, run_id, revision_id, considered_hash, _manifest_id = _declare(
        db, "egress-safe")
    raw_hypothesis = (
        "Customer named Alice Johnson emailed alice@example.com. "
        "Representative values such as PRIVATE01; PRIVATE02"
    )
    raw_goal = "Predict fraud for card 4111 1111 1111 1111"
    egress = build_recipe_authoring_egress(
        hypothesis=raw_hypothesis,
        prediction_goal=raw_goal,
        expectation=_bound_expectation(),
    )
    provider_input = egress.provider_payload()
    expectation_json = asdict(_bound_expectation())
    values = {
        "work_item_id": "work-egress-safe",
        "idempotency_key": "work-egress-safe-key",
        "capture_entry_id": "entry-egress-safe",
        "generation_run_id": run_id,
        "intent_id": intent_id,
        "considered_revision_id": revision_id,
        "considered_content_hash": considered_hash,
        "metadata_snapshot_id": None,
        "metadata_snapshot_content_hash": None,
        "recipe_id": "merchant_mcc_diversity",
        "recipe_candidate_key": "candidate-1",
        "recipe_expectation": expectation_json,
        "recipe_expectation_hash": content_hash(expectation_json),
        "binding_envelope": {"bindings": []},
        "binding_envelope_hash": content_hash({"bindings": []}),
        "provider_input": provider_input,
        "provider_input_hash": egress.content_hash,
        "frozen_configuration": {"configuration_hash": "config-hash"},
        "frozen_configuration_hash": "config-hash",
        "request_identity": {"subject": "user:test"},
        "request_read_scope_hash": "scope-hash",
    }
    write_work_item(db, **values)
    write_observation(
        db,
        observation_id="observation-egress-safe",
        idempotency_key="observation-egress-safe-key",
        capture_entry_id="observation-entry-egress-safe",
        generation_run_id=run_id,
        intent_id=intent_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        recipe_id="merchant_mcc_diversity",
        recipe_candidate_key="candidate-1",
        provider_input=provider_input,
        provider_input_hash=egress.content_hash,
        capture_axis="CAPTURED",
        delivery_axis="NOT_DISPATCHED",
    )
    durable = "\n".join(
        row[0]
        for table in (
            "recipe_formula_shadow_expected_run",
            "recipe_formula_shadow_observation",
            "recipe_formula_shadow_work_item",
            "outbox",
            "llm_call",
        )
        for row in db.execute(f"SELECT row_to_json(t)::text FROM {table} t").fetchall()
    )
    for raw in (
        raw_hypothesis,
        raw_goal,
        "Alice Johnson",
        "alice@example.com",
        "PRIVATE01",
        "PRIVATE02",
        "4111 1111 1111 1111",
    ):
        assert raw not in durable
    assert "[REDACTED:PERSON_NAME]" in durable
    assert "[REDACTED:EMAIL]" in durable
    assert "[REDACTED:PAN]" in durable
    assert '"type": "SAMPLE_VALUE"' in durable


def test_one_capture_failure_does_not_erase_other_ranked_entries(
    db, monkeypatch
) -> None:
    intent_id, run_id, scope_id, revision_id, considered_hash = _seed_lineage(
        db, "isolated")
    ranked = tuple(
        SimpleNamespace(
            recipe_id=recipe_id,
            canonical_rank=index,
            selected_for_initial_view=True,
            rank_reasons=("primary",),
            initial_view_reasons=("selected",),
        )
        for index, recipe_id in enumerate(
            ("merchant_mcc_diversity", "obligor_facility_count"), start=1)
    )

    def _capture(conn, *, index, common, **kwargs):
        del kwargs
        if index == 0:
            raise RuntimeError("candidate write failed")
        write_observation(
            conn,
            **common,
            capture_axis="CAPTURE_INPUT_INCOMPLETE",
            technical_axis="SECOND_ENTRY_RECORDED",
        )

    monkeypatch.setattr(shadow_module, "_capture_selected_entry", _capture)
    result = capture_ranked_shadow(
        db,
        generation_run_id=run_id,
        intent_id=intent_id,
        confirmed_scope_id=scope_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        metadata_snapshot_id="snapshot-isolated",
        metadata_snapshot_content_hash="snapshot-hash-isolated",
        ranked=ranked,
        ranking_version="rank-v1",
        ranking_enabled=True,
        candidate_keys_by_recipe_id={
            "merchant_mcc_diversity": ("candidate-merchant",),
            "obligor_facility_count": ("candidate-obligor",),
        },
        grounding_context_by_candidate_key={},
        identity=IdentityEnvelope(
            subject="user:test",
            actor_kind="human",
            authenticated=True,
            auth_method="password",
            role_claims=("analyst",),
        ),
        request_read_scope_hash="scope-hash",
    )
    assert result.status == "COMPLETE"
    assert db.execute(
        "SELECT technical_axis FROM recipe_formula_shadow_observation "
        "WHERE generation_run_id=%s ORDER BY recipe_id",
        (run_id,),
    ).fetchall() == [
        ("CAPTURE_PERSIST_FAILED",),
        ("SECOND_ENTRY_RECORDED",),
    ]
