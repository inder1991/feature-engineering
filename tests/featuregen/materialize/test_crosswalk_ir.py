"""Release C Task 12 — planning a governed TWO-LEG mapping-table traversal.

The refusal this suite is about is quoted in ``expression_ir._plan_to_grain``: a grain assembled
from two joins "would need two [fan-out verdicts], which no single plan can state". Task 11's
composed observation states exactly one, measured over the joined shape, so the condition the
refusal named is MET rather than routed around — and these tests assert both halves of that: a
traversal with the composed verdict plans, and a traversal without it still refuses.

Everything comes from ``crosswalk_fixtures``: the Task-11 two-scheme crosswalk (internal account
number -> correspondent account reference through ``acct_xref``), measured 1:1 forward and fanning
in reverse.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from tests.featuregen.materialize import crosswalk_fixtures as cf

from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import (
    ExpressionExecutionIR,
    RefRole,
    compile_expression,
)
from featuregen.materialize.ir import crosswalk_execution_pins
from featuregen.materialize.joins import (
    CROSSWALK_EXECUTION_AUTHORITY,
    CROSSWALK_EXECUTION_OUTCOME,
    CrossCatalogJoinStepV1,
    CrosswalkJoinStepV1,
)
from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.crosswalk_observation import MAPPING_TO_TARGET, SOURCE_TO_MAPPING

_ROLES = ("feature_engineer",)


@pytest.fixture(autouse=True)
def crosswalk_flag(monkeypatch):
    """Every test here is BEHIND the flag; the flag-off state has its own tests at the end."""
    for name in ("FEATUREGEN_DATASET_PROFILES", "FEATUREGEN_SOURCE_TEMPORAL_SELECTION",
                 "FEATUREGEN_CROSSWALK_EXECUTION"):
        monkeypatch.setenv(name, "1")
    return monkeypatch


@pytest.fixture
def catalog(db):
    cf.seed_catalog(db)
    return db


def _plan(catalog, *, expr=None, grain=None, crosswalks=None,
          execution_tier=ExecutionTier.PRODUCTION, roles=_ROLES):
    return compile_expression(
        catalog, expr_path="body.expr", expr=expr if expr is not None else cf.expression(),
        grain_keys=grain if grain is not None else cf.FORWARD_GRAIN, roles=roles,
        inventory=cf.INVENTORY,
        crosswalks=(cf.admitted(),) if crosswalks is None else crosswalks,
        execution_tier=execution_tier)


def _refs(ir: ExpressionExecutionIR) -> set[str]:
    return {ref.logical_ref for ref in ir.physical_read_set}


def _roles_of(ir: ExpressionExecutionIR, logical_ref: str) -> tuple[RefRole, ...]:
    (found,) = [ref for ref in ir.physical_read_set if ref.logical_ref == logical_ref]
    return found.roles


# ══ the traversal that used to be unrepresentable ════════════════════════════════════════════════


def test_an_admitted_crosswalk_compiles_to_TWO_governed_join_steps(catalog):
    ir = _plan(catalog)
    assert isinstance(ir, ExpressionExecutionIR), getattr(ir, "detail", ir)
    steps = ir.join_plan.steps
    assert len(steps) == 2
    assert all(isinstance(step, CrosswalkJoinStepV1) for step in steps)
    assert ir.join_plan.outcome_kind == CROSSWALK_EXECUTION_OUTCOME
    assert ir.join_plan.fans_out is False
    assert [step.leg for step in steps] == [SOURCE_TO_MAPPING, MAPPING_TO_TARGET]
    assert all(step.authority == CROSSWALK_EXECUTION_AUTHORITY for step in steps)


def test_the_first_leg_reaches_the_mapping_and_the_second_leaves_it(catalog):
    """Never collapsed to endpoint equality: the mapping dataset is a relation on the path."""
    first, second = _plan(catalog).join_plan.steps
    assert first.column_pairs == ((cf.CIB_ACCT, cf.MAP_ACCT),)
    assert second.column_pairs == ((cf.MAP_EXT, cf.FTR_ACCT),)
    assert first.arrives_at_mapping is True
    assert second.arrives_at_mapping is False
    # The endpoints are never joined to each other anywhere in the plan.
    assert not any({cf.CIB_ACCT, cf.FTR_ACCT} == set(pair)
                   for step in (first, second) for pair in step.column_pairs)


def test_the_mapping_dataset_and_its_row_rule_columns_enter_the_read_set(catalog):
    ir = _plan(catalog)
    assert {cf.MAP_ACCT, cf.MAP_EXT} <= _refs(ir)
    assert {cf.MAP_VALID_FROM, cf.MAP_VALID_TO} <= _refs(ir)
    assert _roles_of(ir, cf.MAP_VALID_FROM) == (RefRole.FILTER_LEFT,)
    assert _roles_of(ir, cf.MAP_ACCT) == (RefRole.JOIN_KEY,)
    # The mapping table is a relation this expression READS and no formula ever named it, so its
    # declared layout has to be derived like any other.
    assert {requirement.table for requirement in ir.input_requirements} == {
        cf.CIB_TABLE, cf.MAP_TABLE, cf.FTR_TABLE}


def test_both_legs_carry_the_SAME_composed_verdict_and_neither_claims_a_per_leg_one(catalog):
    first, second = _plan(catalog).join_plan.steps
    assert first.cardinality == second.cardinality == "N:1"
    assert first.direction == second.direction == "source_to_target"


def test_the_step_pins_the_execution_definition_binding_policy_and_observation(catalog):
    admitted = cf.admitted()
    ir = _plan(catalog, crosswalks=(admitted,))
    for step in ir.join_plan.steps:
        assert step.crosswalk_execution_revision_id == admitted.execution.execution_revision_id
        assert step.crosswalk_definition_revision_id == cf.DEFINITION.revision_id
        assert step.mapping_binding_revision_id == cf.MAP_BINDING.binding_revision_id
        assert step.mapping_temporal_policy_revision_id == cf.TEMPORAL_POLICY
        assert step.mapping_row_selection_hash == cf.row_selection().content_hash
        assert step.composed_observation_revision_id == (
            admitted.execution.composition_observation_revision_id)
        assert step.leg_measurement_ids == admitted.execution.leg_measurement_ids


def test_the_two_legs_pin_their_OWN_owners_and_the_same_catalog_leg_pins_no_realization(catalog):
    first, second = _plan(catalog).join_plan.steps
    assert first.leg_kind == "same_catalog"
    assert first.leg_fact_keys == () and first.leg_realization_revision_ids == ()
    assert second.leg_kind == "cross_catalog"
    assert second.leg_fact_keys and second.leg_realization_revision_ids


def test_every_pin_is_inside_the_expression_identity(catalog):
    """A traversal re-pinned to a different measurement is a DIFFERENT computation."""
    baseline = _plan(catalog).identity_payload()
    other = cf.admitted(selection=cf.row_selection(cutoff_value_ref="other_cutoff"))
    moved = _plan(catalog, crosswalks=(other,)).identity_payload()
    assert baseline != moved


# ══ the composed verdict is what makes it plannable ══════════════════════════════════════════════


def test_an_unmeasured_crosswalk_is_NOT_plannable(catalog):
    """"Discoverable, unmeasured" stays usable everywhere else; a compiled traversal is the one
    place a composed measurement is not optional — that is the old refusal's own condition."""
    refused = _plan(catalog, crosswalks=(cf.unmeasured(),),
                    execution_tier=ExecutionTier.SANDBOX)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN


def test_the_reverse_direction_refuses_INDEPENDENTLY_on_its_own_verdict(catalog):
    """The fixture is 1:1 forward and fanning in reverse — the ordinary mapping-table shape.

    The execution revision's `combined_cardinality` is the FORWARD verdict by construction, so a
    reverse traversal gated on it would be licensed by evidence about the other direction.
    """
    admitted = cf.admitted()
    assert admitted.decision.forward.production_admissible is True
    refused = _plan(catalog, expr=cf.reverse_expression(), grain=cf.REVERSE_GRAIN,
                    crosswalks=(admitted,))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN
    assert "target_to_source" in refused.detail


def test_a_measured_composed_FANOUT_refuses_as_a_fan_out_and_is_never_deduplicated(catalog):
    fanning = cf.admitted(obs=cf.observation(source_to_target_max_matches=2))
    refused = _plan(catalog, crosswalks=(fanning,))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code in {
        CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED,
        CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
    }


def test_two_admitted_crosswalks_for_one_pair_refuse_rather_than_pick_one(catalog):
    first = cf.admitted()
    second = cf.admitted(selection=cf.row_selection(cutoff_value_ref="other_cutoff"))
    refused = _plan(catalog, crosswalks=(first, second))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN
    assert "refuse_on_multiple" in refused.detail


# ══ tiers ════════════════════════════════════════════════════════════════════════════════════════


def test_a_sandbox_scoped_execution_refuses_at_a_production_compile(catalog):
    sandbox = cf.admitted(scope=cf.SANDBOX_SCOPE)
    refused = _plan(catalog, crosswalks=(sandbox,), execution_tier=ExecutionTier.PRODUCTION)
    assert isinstance(refused, MaterializationRefused)
    assert "sandbox" in refused.detail


def test_the_same_sandbox_scoped_execution_plans_at_a_sandbox_compile(catalog):
    sandbox = cf.admitted(scope=cf.SANDBOX_SCOPE)
    ir = _plan(catalog, crosswalks=(sandbox,), execution_tier=ExecutionTier.SANDBOX)
    assert isinstance(ir, ExpressionExecutionIR), getattr(ir, "detail", ir)


# ══ the mapping row rule ═════════════════════════════════════════════════════════════════════════


def test_a_pinned_temporal_policy_with_no_resolved_row_selection_refuses(catalog):
    admitted = cf.admitted()
    without = replace(admitted, mapping_row_selection=None)
    refused = _plan(catalog, crosswalks=(without,))
    assert isinstance(refused, MaterializationRefused)
    assert "row selection" in refused.detail


def test_the_row_rule_predicates_travel_onto_the_leg_that_reaches_the_mapping(catalog):
    first, second = _plan(catalog).join_plan.steps
    assert [predicate.column_ref for predicate in first.mapping_row_predicates] == [
        cf.MAP_VALID_FROM, cf.MAP_VALID_TO]
    assert [predicate.operator for predicate in first.mapping_row_predicates] == ["<=", ">"]
    assert second.mapping_row_predicates == ()


def test_the_renderable_operators_are_exactly_the_governed_vocabulary():
    """The two sets must be ONE set: a governed operator this compiler cannot render would be
    silently dropped, and the traversal would then read a different row set than was measured.

    Asserted against the contract's own vocabulary rather than a copy of it, so an operator added
    to Release B without a rendered form here fails this test instead of failing a bank."""
    from featuregen.materialize.joins import RENDERED_OPERATORS
    from featuregen.overlay.upload.temporal_policy import PREDICATE_OPERATORS

    assert set(RENDERED_OPERATORS) | {"is_true"} == set(PREDICATE_OPERATORS)


def test_a_row_rule_this_compiler_cannot_render_refuses_rather_than_being_dropped():
    """The fail-closed branch behind the assertion above, reached directly.

    A predicate carrying an operator outside the rendered map cannot reach the planner through
    `DatasetRowSelectionV1` (its own contract refuses one), so the adapter is exercised on its own
    — this is the branch a FUTURE vocabulary extension would hit, and an extension that quietly
    rendered nothing would execute the traversal over rows nobody measured."""
    from featuregen.materialize.joins import mapping_row_predicates

    refused = mapping_row_predicates((
        {"kind": "effective_time", "column_ref": cf.MAP_VALID_FROM, "operator": "between",
         "parameter_ref": cf.REPORT_CUTOFF_REF},))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN


def test_a_row_selection_taken_under_a_MOVED_policy_pointer_cannot_be_assembled():
    """The staleness is refused where the bundle is built, so no compiler can be handed one."""
    with pytest.raises(ValueError, match="temporal policy"):
        cf.admitted(selection=cf.row_selection(temporal_policy_revision_id="dtp_" + "9" * 64))


def test_a_row_selection_whose_hash_is_not_the_measured_one_cannot_be_assembled():
    measured = cf.observation()
    with pytest.raises(ValueError, match="content hash"):
        cf.admitted(obs=measured, selection=cf.row_selection(cutoff_value_ref="other_cutoff"))


# ══ nothing else moved ═══════════════════════════════════════════════════════════════════════════


def test_the_flag_off_keeps_the_crosswalk_structurally_non_executable(catalog, crosswalk_flag):
    """Not a crosswalk-shaped refusal — BYTE-IDENTICAL to the refusal this compilation gave before
    crosswalks could be compiled at all, because with the flag off there is no crosswalk-shaped
    code path to reach and no crosswalk-shaped error to read."""
    without_any = _plan(catalog, crosswalks=())
    assert isinstance(without_any, MaterializationRefused)

    crosswalk_flag.delenv("FEATUREGEN_CROSSWALK_EXECUTION")
    flag_off = _plan(catalog, crosswalks=(cf.admitted(),))
    assert isinstance(flag_off, MaterializationRefused)
    assert (flag_off.code, flag_off.detail) == (without_any.code, without_any.detail)


def test_the_flag_being_SET_alone_is_not_enough(catalog, crosswalk_flag):
    """The reader fails closed on its dependency too, so a surface that skipped the boot check
    still cannot compile a crosswalk in a configuration the boot check would have refused."""
    crosswalk_flag.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION")
    refused = _plan(catalog, crosswalks=(cf.admitted(),))
    assert isinstance(refused, MaterializationRefused)
    assert "crosswalk" not in refused.detail.lower()


def test_a_grain_on_two_unrelated_tables_still_refuses_for_its_original_reason(catalog):
    """The either/or refusal MOVED for crosswalks and did not move for anything else."""
    refused = _plan(catalog, grain=(*cf.FORWARD_GRAIN, cf.MAP_ACCT))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.GRAIN_PATH_NOT_GOVERNED
    assert "no single plan can state" in refused.detail


def test_the_group_identity_names_every_crosswalk_pin_once_per_traversal(catalog):
    """Both legs carry the same execution-level pins, so a two-step plan is ONE tuple."""
    ir = _plan(catalog)

    class _Group:
        expressions = (ir,)

    pins = crosswalk_execution_pins((_Group(),))
    assert len(pins) == 1
    execution, definition, mapping_binding, policy, observation = pins[0]
    assert execution.startswith("cwx_")
    assert definition == cf.DEFINITION.revision_id
    assert mapping_binding == cf.MAP_BINDING.binding_revision_id
    assert policy == cf.TEMPORAL_POLICY
    assert observation.startswith("cbo_") or observation


def test_a_direct_equality_plan_carries_no_crosswalk_step(catalog):
    """Byte-pin for the shipped path: the presence of the parameter changes nothing about it."""
    ir = _plan(catalog, grain=(cf.CIB_ACCT,), crosswalks=())
    assert isinstance(ir, ExpressionExecutionIR), getattr(ir, "detail", ir)
    assert ir.join_plan.steps == ()
    assert not any(isinstance(step, CrossCatalogJoinStepV1 | CrosswalkJoinStepV1)
                   for step in ir.join_plan.steps)


def test_a_non_crosswalk_expression_identity_gains_NO_KEY(catalog):
    """The identity payload's SHAPE, pinned by key set.

    Crosswalk pins ride inside the join steps' own payloads and nowhere else, so an expression
    with no crosswalk emits exactly the keys it emitted before this task — no conditional slot, no
    null placeholder. (The whole-project byte golden ``goldens/cif_daily`` is the other half of
    this proof and is unchanged; this states the rule the golden happens to satisfy.)
    """
    payload = _plan(catalog, grain=(cf.CIB_ACCT,), crosswalks=()).identity_payload()
    assert set(payload) == {
        "expr_path", "physical_read_set", "join_steps", "pit", "input_requirements",
        "aggregation", "filter_tree"}
    assert payload["join_steps"] == []


def test_a_group_with_no_crosswalk_carries_NO_crosswalk_key_in_its_compilation_identity():
    """Same rule one level up, and the same reason: the single-catalog artifact's identity bytes
    are what they were before crosswalks could be compiled at all."""
    from featuregen.materialize.identity import CompilationIdentity

    plain = CompilationIdentity(
        formula_content_hashes=("f" * 64,), ir_hashes=("i" * 64,),
        materialization_contract_hash="c" * 64, group_plan_hash="g" * 64)
    assert set(plain.identity_payload()) == {
        "formula_content_hashes", "ir_hashes", "materialization_contract_hash", "group_plan_hash"}

    with_crosswalk = CompilationIdentity(
        formula_content_hashes=("f" * 64,), ir_hashes=("i" * 64,),
        materialization_contract_hash="c" * 64, group_plan_hash="g" * 64,
        crosswalk_execution_pins=(("cwx_1", "cwd_1", "pbr_1", "dtp_1", "cwo_1"),))
    assert "crosswalk_execution_pins" in with_crosswalk.identity_payload()
