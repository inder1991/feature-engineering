from __future__ import annotations

from types import SimpleNamespace

import psycopg
import pytest

from featuregen.overlay.upload.recipe_formula_shadow import (
    ShadowIntegrityError,
    build_capture_entries,
    declare_expected_run,
    finalize_manifest,
    reconcile_run,
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
        "recipe_expectation_hash": "expectation-hash",
        "binding_envelope": {"bindings": []},
        "binding_envelope_hash": "binding-hash",
        "provider_input": {"hypothesis": "h"},
        "provider_input_hash": "input-hash",
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
    with pytest.raises(ShadowIntegrityError):
        write_work_item(db, **{**values, "provider_input_hash": "changed"})
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute(
            "DELETE FROM recipe_formula_shadow_work_item WHERE work_item_id='work-1'")
