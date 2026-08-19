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
from featuregen.formula.schema import AggregateFunction, FinalOperation, WindowBasis
from featuregen.formula.schema_leaves import (
    EmptyWindowResult,
    Inclusivity,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    WindowUnit,
)
from featuregen.overlay.upload import recipe_formula_shadow as shadow_module
from featuregen.overlay.upload.recipe_formula_contracts import (
    BoundExpressionExpectationV1,
    BoundRecipeFormulaExpectationV1,
    DecimalPolicyExpectationV1,
    WindowExpectationV1,
)
from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
    RecipeFormulaExpectationBlueprintV2,
)
from featuregen.overlay.upload.recipe_formula_expectations import (
    RECIPE_FORMULA_EXPECTATIONS,
)
from featuregen.overlay.upload.recipe_formula_shadow import (
    FORMULA_SCHEMA_V1,
    FORMULA_SCHEMA_V2,
    MAX_RECIPE_FORMULA_CAPTURES_PER_RUN,
    RankedCaptureEntryV1,
    ShadowIntegrityError,
    build_capture_entries,
    capture_blueprint_for,
    capture_ranked_shadow,
    content_hash,
    declare_expected_run,
    finalize_manifest,
    formula_capturable_recipe_ids,
    reconcile_run,
    verify_expected_run_payload,
    verify_manifest_payload,
    verify_observation_payload,
    verify_work_item_payload,
    write_manifest,
    write_observation,
    write_work_item,
)
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES


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


# ── task A4, increment 3: the capture population ───────────────────────────────────────────
#: A2's measurement, re-pinned from the capture side: 90 of the 317 registry recipes have a
#: bindable blueprint. A registry edit that loses derivability fails HERE too, where it changes
#: what the platform actually captures.
EXPECTED_CAPTURE_POPULATION = 90


def test_the_capture_population_is_every_recipe_with_a_bindable_blueprint():
    population = formula_capturable_recipe_ids()
    assert len(population) == EXPECTED_CAPTURE_POPULATION
    # Strictly wider than the two reviewed v1 entries it used to be — and it now contains the one
    # v2 recipe the registry calls FORMULA_AUTHORABLE, which was never capturable before A4.
    assert population > frozenset(RECIPE_FORMULA_EXPECTATIONS)
    assert "posted_debit_amount" in population
    # A recipe the registry never minted has no blueprint at all: an LLM intent or a user
    # definition can never be captured by accident.
    assert capture_blueprint_for("not_a_registry_recipe") is None
    assert "not_a_registry_recipe" not in population


def test_the_two_readings_of_formula_v1_still_agree_ON_WHAT_REMAINS():
    """A4-c, superseded by the v1 retirement — and the two readings have now DELIBERATELY diverged.

    `obligor_facility_count` was converted to ``formula-v2`` during the v1 routing retirement — its
    derived v2 blueprint carries the same grain as its reviewed entry, so the lane moved and nothing
    else did. `merchant_mcc_diversity` was NOT: its reviewed entry declares merchant grain while the
    definition computes per customer, so converting it would substitute a different grain for a
    reviewed decision. See `test_the_merchant_v1_entry_is_untouched`.

    So the two readings have deliberately diverged by one: the declaration side holds only
    `merchant_mcc_diversity`, while the v1 expectation registry still holds both entries. The
    obligor entry is now dead weight that nothing selects — pinned rather than removed, because
    `recipe_audit`, `recipe_formula_eval` and `recipe_formula_gate` still reference the registry and
    removing an entry would change those three for a reason unrelated to lane selection.
    """
    declared_v1 = {definition.recipe_id for definition in V2_RECIPES
                   if definition.formula is not None
                   and definition.formula.formula_schema_version == FORMULA_SCHEMA_V1}
    assert declared_v1 == {"merchant_mcc_diversity"}, (
        f"{sorted(declared_v1)} declare formula-v1: while any does, the v1 worker arm cannot be "
        f"removed and a missing declaration cannot become terminal")
    assert set(RECIPE_FORMULA_EXPECTATIONS) == {
        "merchant_mcc_diversity", "obligor_facility_count"}


@pytest.mark.parametrize(("recipe_id", "declared"), [
    ("merchant_mcc_diversity", FORMULA_SCHEMA_V1),
    ("obligor_facility_count", FORMULA_SCHEMA_V2),
    ("posted_debit_amount", FORMULA_SCHEMA_V2),
])
def test_a_recipe_resolves_the_blueprint_its_own_declaration_names(recipe_id, declared):
    resolved = capture_blueprint_for(recipe_id)
    assert resolved is not None
    assert resolved.declared_schema_version == declared
    is_v2 = isinstance(resolved.blueprint, RecipeFormulaExpectationBlueprintV2)
    assert is_v2 == (declared == FORMULA_SCHEMA_V2)
    if not is_v2:
        # The v1 arm serves the REVIEWED entry verbatim — A4 derives no substitute for it, so
        # D-7's merchant-grain disagreement stays exactly where the governance decision left it.
        assert resolved.blueprint is RECIPE_FORMULA_EXPECTATIONS[recipe_id]


def _entry_with_reason(entry: RankedCaptureEntryV1, reason: str) -> RankedCaptureEntryV1:
    return RankedCaptureEntryV1(**{**asdict(entry), "capture_reason": reason})


def test_the_renamed_capture_reasons_change_new_manifests_only(db):
    """A4 drops ``_V1`` from both capture-reason literals. They ride ``capture_entries`` into the
    SEALED ``manifest_hash``, so this asserts the consequence rather than discovering it: new runs
    hash differently, and an already-stored manifest is NEVER rewritten."""
    intent_id, run_id, revision_id, considered_hash, manifest_id = _declare(db, "reasons")
    ranked = _ranked()
    entries = build_capture_entries(
        generation_run_id=run_id, ranking_version="rank-v1", ranked=ranked,
        candidate_keys_by_recipe_id={"merchant_mcc_diversity": ("candidate-1",),
                                     "obligor_facility_count": ("candidate-2",)},
        capture_recipe_ids=formula_capturable_recipe_ids())
    assert [entry.capture_reason for entry in entries] == [
        "SELECTED_FORMULA_AUTHORABLE", "NOT_SELECTED"]

    write_kwargs = dict(
        generation_run_id=run_id, intent_id=intent_id, considered_revision_id=revision_id,
        considered_content_hash=considered_hash, ranking_version="rank-v1", ranked=ranked,
        ranking_enabled=True)
    write_manifest(db, manifest_id=manifest_id, entries=entries, **write_kwargs)
    stored = db.execute(
        "SELECT manifest_hash,capture_entries FROM recipe_formula_shadow_run_manifest "
        "WHERE manifest_id=%s", (manifest_id,)).fetchone()

    # The same run, with the PRE-A4 spellings, is a different manifest hash…
    legacy = tuple(_entry_with_reason(entries[0], "SELECTED_FORMULA_V1_AUTHORABLE")
                   if entry is entries[0] else entry for entry in entries)
    with pytest.raises(ShadowIntegrityError):
        write_manifest(db, manifest_id=manifest_id, entries=legacy, **write_kwargs)
    # …and the stored row is byte-identical to what it was. Append-only means append-only.
    assert db.execute(
        "SELECT manifest_hash,capture_entries FROM recipe_formula_shadow_run_manifest "
        "WHERE manifest_id=%s", (manifest_id,)).fetchone() == stored
    assert stored[1][0]["capture_reason"] == "SELECTED_FORMULA_AUTHORABLE"


def test_a_wider_population_truncates_at_the_budget_and_says_so(db):
    """A wider population makes ``BUDGET_TRUNCATED`` reachable — the honest outcome, and already
    an observation axis. Truncation is RECORDED, never a silent drop: every declared entry still
    gets exactly one observation, so the run reconciles COMPLETE."""
    over_budget = 3
    population = sorted(formula_capturable_recipe_ids())[
        :MAX_RECIPE_FORMULA_CAPTURES_PER_RUN + over_budget]
    assert len(population) == MAX_RECIPE_FORMULA_CAPTURES_PER_RUN + over_budget
    intent_id, run_id, scope_id, revision_id, considered_hash = _seed_lineage(db, "budget")
    ranked = tuple(
        SimpleNamespace(
            recipe_id=recipe_id, canonical_rank=index, selected_for_initial_view=True,
            rank_reasons=("primary",), initial_view_reasons=("selected",))
        for index, recipe_id in enumerate(population, start=1))
    result = capture_ranked_shadow(
        db,
        generation_run_id=run_id,
        intent_id=intent_id,
        confirmed_scope_id=scope_id,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
        metadata_snapshot_id="snapshot-budget",
        metadata_snapshot_content_hash="snapshot-hash-budget",
        ranked=ranked,
        ranking_version="rank-v1",
        ranking_enabled=True,
        candidate_keys_by_recipe_id={rid: (f"candidate-{rid}",) for rid in population},
        # No private context is handed over, so every in-budget entry stops at the same honest
        # place — the point of this test is the BUDGET, and it must not depend on binding.
        grounding_context_by_candidate_key={},
        identity=IdentityEnvelope(
            subject="user:test", actor_kind="human", authenticated=True,
            auth_method="password", role_claims=("analyst",)),
        request_read_scope_hash="scope-hash-budget")

    assert result.status == "COMPLETE"
    assert result.expected_observations == len(population)
    axes = dict(db.execute(
        "SELECT capture_axis,count(*) FROM recipe_formula_shadow_observation "
        "WHERE generation_run_id=%s GROUP BY capture_axis", (run_id,)).fetchall())
    assert axes == {"CAPTURE_INPUT_INCOMPLETE": MAX_RECIPE_FORMULA_CAPTURES_PER_RUN,
                    "BUDGET_TRUNCATED": over_budget}
    assert db.execute(
        "SELECT DISTINCT technical_axis FROM recipe_formula_shadow_observation "
        "WHERE generation_run_id=%s AND capture_axis='BUDGET_TRUNCATED'",
        (run_id,)).fetchall() == [("CAPTURE_INCOMPLETE",)]
    # A truncated entry is not work: nothing was enqueued for it.
    assert db.execute(
        "SELECT count(*) FROM recipe_formula_shadow_work_item WHERE generation_run_id=%s",
        (run_id,)).fetchone()[0] == 0


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
        binding_plan_by_candidate_key={},
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
    # Patched at the DISPATCH POINT rather than on one binder. This used to replace
    # `bind_formula_expectation` (v1) and stopped intercepting anything the moment the recipe
    # started routing v2 — `CaptureBlueprintV1.bind` chooses the binder by blueprint type, so it is
    # the one place that is true for both lanes. Binding is scaffolding here: what is under test is
    # that a redactor failure persists no raw prose.
    monkeypatch.setattr(
        shadow_module.CaptureBlueprintV1, "bind", lambda _self, _context: object())
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
        binding_plan_by_candidate_key={},
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


# ── B2: the frozen plan envelope rides the work item, never the provider payload ─────────────────

#: A real envelope shape — the nine keys ``fold_frozen_binding_plan`` returns, nothing invented.
_B2_PLAN = {
    "plan_kind": "single_source",
    "catalog_source": "posting_bank",
    "source_table": "txns",
    "population_ref": "txns",
    "read_set": ["public.txns.acct_id", "public.txns.event_ts", "public.txns.txn_amt"],
    "role_bindings": {"amount": "public.txns.txn_amt"},
    "pit": "trailing 90d observation window over event_time events",
    "output_grain": "account",
    "window": 90,
}


def _b2_values(db, suffix: str) -> dict:
    intent_id, run_id, revision_id, considered_hash, _manifest = _declare(db, suffix)
    return {
        "work_item_id": f"work-b2-{suffix}",
        "idempotency_key": f"work-b2-key-{suffix}",
        "capture_entry_id": f"entry-b2-{suffix}",
        "generation_run_id": run_id,
        "intent_id": intent_id,
        "considered_revision_id": revision_id,
        "considered_content_hash": considered_hash,
        "metadata_snapshot_id": None,
        "metadata_snapshot_content_hash": None,
        "recipe_id": "posted_debit_amount",
        "recipe_candidate_key": f"candidate-b2-{suffix}",
        "recipe_expectation": {"recipe": "posted_debit_amount"},
        "recipe_expectation_hash": content_hash({"recipe": "posted_debit_amount"}),
        "binding_envelope": {"bindings": []},
        "binding_envelope_hash": content_hash({"bindings": []}),
        "provider_input": {"hypothesis": "h"},
        "provider_input_hash": content_hash({"hypothesis": "h"}),
        "frozen_configuration": {"configuration_hash": "config-hash"},
        "frozen_configuration_hash": "config-hash",
        "request_identity": {"subject": "user:test"},
        "request_read_scope_hash": "scope-hash",
    }


def _stored_work_item(db, work_item_id: str) -> dict:
    cursor = db.execute(
        "SELECT * FROM recipe_formula_shadow_work_item WHERE work_item_id=%s", (work_item_id,))
    columns = [description.name for description in cursor.description]
    return dict(zip(columns, cursor.fetchone(), strict=True))


def test_the_work_item_carries_the_frozen_plan_and_its_hash(db):
    """B2: the envelope is a durable, SEALED field of the work item — its own hash, and folded
    into the payload material so ``verify_work_item_payload`` covers it."""
    values = _b2_values(db, "carry")
    write_work_item(db, **values, binding_plan=_B2_PLAN)
    row = _stored_work_item(db, "work-b2-carry")

    assert row["binding_plan_json"] == _B2_PLAN
    assert row["binding_plan_hash"] == content_hash(_B2_PLAN)
    assert verify_work_item_payload(row) is None
    # And the envelope is NOT in the provider payload: it is server-private plan detail.
    assert "binding_plan" not in row["provider_input_json"]


def test_a_pre_B2_work_item_still_verifies(db):
    """THE compatibility property. A work item written before B2 has NULL columns, so it must
    hash against the PRE-B2 material shape — 1023 forbids rewriting it, and a material that
    folded the two keys in unconditionally would terminalize every queued item with
    ``WORK_ITEM_PAYLOAD_HASH_MISMATCH``."""
    values = _b2_values(db, "legacy")
    write_work_item(db, **values)                      # no binding_plan — the pre-B2 call
    row = _stored_work_item(db, "work-b2-legacy")

    assert (row["binding_plan_json"], row["binding_plan_hash"]) == (None, None)
    assert verify_work_item_payload(row) is None


#: The material a work item hashed BEFORE B2, written out rather than derived — so the
#: compatibility claim is pinned against a literal and not against the implementation it is
#: supposed to constrain. A row sealed under these 21 keys must go on verifying forever: 1023
#: forbids rewriting it.
_PRE_B2_MATERIAL_KEYS = frozenset({
    "work_item_id", "idempotency_key", "capture_entry_id", "generation_run_id", "intent_id",
    "considered_revision_id", "considered_content_hash", "metadata_snapshot_id",
    "metadata_snapshot_content_hash", "recipe_id", "recipe_candidate_key", "recipe_expectation",
    "recipe_expectation_hash", "binding_envelope", "binding_envelope_hash", "provider_input",
    "provider_input_hash", "frozen_configuration", "frozen_configuration_hash",
    "request_identity", "request_read_scope_hash",
})


def test_the_two_material_shapes_are_pinned(db):
    """Both shapes, side by side, so neither can drift into the other. Without a plan the
    material is EXACTLY the pre-B2 dict (no new keys at all); with one it gains exactly two."""
    common = {
        "work_item_id": "w", "idempotency_key": "k", "capture_entry_id": "e",
        "generation_run_id": "r", "intent_id": "i", "considered_revision_id": "rev",
        "considered_content_hash": "ch", "metadata_snapshot_id": None,
        "metadata_snapshot_content_hash": None, "recipe_id": "x", "recipe_candidate_key": "c",
        "recipe_expectation": {}, "recipe_expectation_hash": "eh", "binding_envelope": {},
        "binding_envelope_hash": "bh", "provider_input": {}, "provider_input_hash": "ph",
        "frozen_configuration": {}, "frozen_configuration_hash": "fh",
        "request_identity": {}, "request_read_scope_hash": None,
    }
    without = shadow_module._work_item_material(**common)
    with_plan = shadow_module._work_item_material(
        **common, binding_plan=_B2_PLAN, binding_plan_hash=content_hash(_B2_PLAN))

    assert set(without) == _PRE_B2_MATERIAL_KEYS
    assert set(with_plan) - set(without) == {"binding_plan", "binding_plan_hash"}
    assert content_hash(with_plan) != content_hash(without)


def test_a_tampered_frozen_plan_fails_its_own_hash(db):
    """The envelope is sealed twice — by its own hash and by the payload material — so an
    out-of-band edit is named precisely rather than surfacing as a generic payload mismatch."""
    values = _b2_values(db, "tamper")
    write_work_item(db, **values, binding_plan=_B2_PLAN)
    row = _stored_work_item(db, "work-b2-tamper")

    row["binding_plan_json"] = {**_B2_PLAN, "source_table": "somewhere_else"}
    assert verify_work_item_payload(row) == "BINDING_PLAN_HASH_MISMATCH"


def test_a_repeated_work_item_with_a_DIFFERENT_plan_is_refused(db):
    """The envelope is part of the work item's identity: re-writing the same idempotency key with
    another plan conflicts with the stored material rather than being silently ignored."""
    values = _b2_values(db, "conflict")
    write_work_item(db, **values, binding_plan=_B2_PLAN)
    write_work_item(db, **values, binding_plan=_B2_PLAN)          # idempotent, same bytes
    with pytest.raises(ShadowIntegrityError):
        write_work_item(db, **values, binding_plan={**_B2_PLAN, "window": 30})


def test_the_provider_payload_is_byte_identical_to_before(db):
    """B2's governed-security half. The egress whitelist is FAIL-CLOSE, so the envelope must not
    be able to reach a provider at all — and "must not" is proved three ways rather than asserted:

    1. ``build_recipe_authoring_egress`` takes no plan argument, so no call site could pass one;
    2. ``recipe_egress`` is untouched by B2 — the v1 provider payload's pinned digest in
       ``test_recipe_egress.py`` (``09ce6764…``, which is also its ``content_hash``) is the
       byte-level guard and stays green;
    3. the bytes actually stored in ``provider_input_json`` are the caller's, unchanged, and carry
       no key of the envelope at any depth.
    """
    import inspect

    signature = inspect.signature(build_recipe_authoring_egress)
    assert set(signature.parameters) == {"hypothesis", "prediction_goal", "expectation"}

    values = _b2_values(db, "egress")
    payload = dict(values["provider_input"])
    write_work_item(db, **values, binding_plan=_B2_PLAN)
    row = _stored_work_item(db, "work-b2-egress")

    assert row["provider_input_json"] == payload
    assert content_hash(row["provider_input_json"]) == values["provider_input_hash"]
    envelope_keys = set(_B2_PLAN) - {"window"}      # "window" is a legitimate formula word
    assert envelope_keys.isdisjoint(_all_keys(row["provider_input_json"]))


def _all_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _all_keys(v)}
    if isinstance(value, list):
        return {k for item in value for k in _all_keys(item)}
    return set()
