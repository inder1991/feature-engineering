"""C-C1/C-C2 — the V2 execution boundary.

Driven off a REAL compiled V1 IR (``compile_ir`` against the seeded catalog), not a hand-built
expression fixture. The module's central claim is that V2 reuses V1's language-neutral
``ExpressionExecutionIR`` and ``SpineSpec`` rather than re-declaring them; a fixture built by hand
would prove only that the V2 types accept objects shaped the way this file happens to shape them.
Compiling a real V1 feature and carrying its expressions into a V2 IR is the claim itself.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.materialize.test_ir import (  # the V1 machinery, reused not re-created
    _ROLES,
    DECLARATION,
    INVENTORY,
    _admitted,
    compile_ir,
    seed_catalog,
)

from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.schema import AdditivityClass
from featuregen.formula.schema_v2 import FinalOperationV2
from featuregen.formula.schema_v3 import SelectionKind, SemanticRowSelectionV1
from featuregen.materialize.boundary_v2 import (
    AuthorizedCompilationV2,
    CompilationIdentityV2,
    FeatureGroupPlanV2,
    FormulaExecutionIRV2,
    MaterializationContractV2,
    PlannedFormulaExecutionIRV2,
    SelectedRowsV2,
    build_compilation_identity_v2,
    group_plan_hash_v2,
    ir_hash_v2,
)
from featuregen.materialize.contract import MaterializationContractV1, derive_contract
from featuregen.materialize.ir import (
    FormulaExecutionIRV1,
    physical_read_set,
    physical_read_set_of,
)

#: C-C6 will DEFINE this policy. This file only needs an id of the right shape — the content is not
#: this task's, and asserting anything about what it means here would be inventing it.
POLICY_ID = "formula-v2/physical-types@1"

DEBIT = SemanticRowSelectionV1(
    kind=SelectionKind.TRANSACTION_DIRECTION, role="direction", semantic_value="debit")
ELIGIBLE = SemanticRowSelectionV1(
    kind=SelectionKind.ELIGIBILITY, role="eligibility", semantic_value="eligible")


@pytest.fixture
def catalog(db):
    return seed_catalog(db)


@pytest.fixture
def v1_ir(catalog) -> FormulaExecutionIRV1:
    """One really-compiled V1 IR — the source of the expressions and spine V2 reuses."""
    ir = compile_ir(catalog, _admitted("total_debit_amount_30d"), roles=_ROLES,
                    spine_decl=DECLARATION, inventory=INVENTORY)
    assert isinstance(ir, FormulaExecutionIRV1), ir
    return ir


def _output_v2(currency: str = "fixed:AED") -> FormulaOutputPolicyV2:
    return FormulaOutputPolicyV2(
        output_type="decimal", unit="monetary", currency=currency,
        output_additivity=AdditivityClass.ADDITIVE, external_type_required=False)


def _v2_ir(v1: FormulaExecutionIRV1, *, name: str = "posted_debit_amount_30d",
           selections: tuple[SelectedRowsV2, ...] | None = None,
           currency: str = "fixed:AED") -> FormulaExecutionIRV2:
    path = v1.expressions[0].expr_path
    return FormulaExecutionIRV2(
        feature_name=name, formula_content_hash=f"sha256:{name}",
        final_operation=FinalOperationV2.IDENTITY, zero_denominator=None,
        grain_entity=v1.grain_entity, grain_keys=v1.grain_keys,
        expressions=v1.expressions,
        row_selections=((SelectedRowsV2(expr_path=path, selections=(DEBIT,)),)
                        if selections is None else selections),
        spine=v1.spine, output_policy=_output_v2(currency), authoring_run_id="run-v2-0001")


# ══ V1 is unchanged by the extraction ════════════════════════════════════════════════════════════
def test_the_shared_walk_returns_EXACTLY_what_v1s_own_read_set_returns(v1_ir):
    """The extraction's whole safety claim. If these ever diverge, a V2 group is authorized for a
    different read set than the identical V1 group — the failure the shared walk exists to prevent.
    """
    assert physical_read_set_of(
        [(v1_ir.feature_name, v1_ir.expressions)], v1_ir.spine) == physical_read_set(
            [v1_ir], v1_ir.spine)


def test_v1s_union_DELEGATES_to_the_shared_core_rather_than_duplicating_it():
    """Why V1's existing coverage is also the shared core's coverage.

    The join-endpoint-by-kind walk, the join-predicate reads and the empty-read-set refusal are all
    exercised by ``test_ir.py`` against V1. That only carries over to V2 if V1 genuinely routes
    through the extracted function — if ``_union_of`` kept its own copy of the walk, both would pass
    today and drift apart on the first change to either.
    """
    import inspect

    from featuregen.materialize import ir as ir_module

    assert "_union_elements(" in inspect.getsource(ir_module._union_of)
    assert "_union_elements(" in inspect.getsource(ir_module.physical_read_set_of)
    # the walk itself lives in ONE place
    assert inspect.getsource(ir_module._union_of).count("physical_read_set") <= 1


# ══ the V2 IR ════════════════════════════════════════════════════════════════════════════════════
def test_the_v2_ir_carries_v1s_expressions_by_IDENTITY_not_a_rebuild(v1_ir):
    """`ExpressionExecutionIR` is language-neutral and is REUSED. A rebuild here would be a second
    answer to "what does this expression read"."""
    ir = _v2_ir(v1_ir)
    assert ir.expressions is v1_ir.expressions
    assert ir.spine is v1_ir.spine


def test_the_declared_selection_reaches_the_ir_identity(v1_ir):
    """C-A3b end to end into execution: the recipe declared debit, and the compiled identity says
    debit — semantically, never as the pilot ledger's `"D"`."""
    payload = _v2_ir(v1_ir).identity_payload()
    assert payload["row_selections"] == [{
        "expr_path": v1_ir.expressions[0].expr_path,
        "selections": [{"kind": "transaction_direction", "role": "direction",
                        "semantic_value": "debit"}]}]


def test_dropping_the_selection_CHANGES_the_ir_hash(v1_ir):
    """The selection is identity-bearing. Were it not, a debit feature and an unfiltered one would
    be one computation and could share a materialization group."""
    path = v1_ir.expressions[0].expr_path
    with_selection = ir_hash_v2(_v2_ir(v1_ir))
    without = ir_hash_v2(_v2_ir(v1_ir, selections=()))
    debit_and_eligible = ir_hash_v2(_v2_ir(v1_ir, selections=(
        SelectedRowsV2(expr_path=path, selections=(DEBIT, ELIGIBLE)),)))
    assert len({with_selection, without, debit_and_eligible}) == 3


def test_selection_ORDER_is_preserved_not_sorted(v1_ir):
    """V3 canonicalizes `row_selections` as an ORDERED tuple, so two orderings are two formulas.
    Sorting here would give those two formulas one IR hash and silently merge them."""
    path = v1_ir.expressions[0].expr_path
    forward = _v2_ir(v1_ir, selections=(SelectedRowsV2(path, (DEBIT, ELIGIBLE)),))
    reverse = _v2_ir(v1_ir, selections=(SelectedRowsV2(path, (ELIGIBLE, DEBIT)),))
    assert ir_hash_v2(forward) != ir_hash_v2(reverse)


def test_a_selection_naming_no_expression_is_REFUSED(v1_ir):
    """A selection pointing nowhere selects nothing, so the feature would LOOK filtered and execute
    unfiltered — the exact shape of a silently-wrong number."""
    with pytest.raises(ValueError, match="not one of its expressions"):
        _v2_ir(v1_ir, selections=(SelectedRowsV2("body.expr.nowhere", (DEBIT,)),))


def test_two_selection_entries_for_ONE_expression_are_refused(v1_ir):
    path = v1_ir.expressions[0].expr_path
    with pytest.raises(ValueError, match="two row-selection entries"):
        _v2_ir(v1_ir, selections=(SelectedRowsV2(path, (DEBIT,)),
                                  SelectedRowsV2(path, (ELIGIBLE,))))


def test_an_EMPTY_selection_tuple_is_refused():
    with pytest.raises(ValueError, match="EMPTY selection tuple"):
        SelectedRowsV2(expr_path="body.expr", selections=())


def test_the_conversion_a_feature_depends_on_is_part_of_what_it_IS(v1_ir):
    """V2's `currency` carries `"converted:<ref>"`, so a converted feature and a fixed-currency one
    are different computations. V1's field could only ever say WHICH currency."""
    fixed = ir_hash_v2(_v2_ir(v1_ir, currency="fixed:AED"))
    converted = ir_hash_v2(_v2_ir(v1_ir, currency="converted:currency_conversion:foundation-base"))
    assert fixed != converted


def test_the_authoring_run_id_is_NOT_in_the_identity(v1_ir):
    """Provenance, not identity — and it matters more now, because C-A5's deterministic producer
    authors the same formula from the same reviewed blueprint on every run."""
    once = _v2_ir(v1_ir)
    twice = dataclasses.replace(once, authoring_run_id="run-v2-9999")
    assert ir_hash_v2(once) == ir_hash_v2(twice)


# ══ C-C2: the ordering is expressed in TYPES ═════════════════════════════════════════════════════
def test_planning_derives_the_read_set_through_the_shared_walk(v1_ir):
    planned = PlannedFormulaExecutionIRV2.plan(_v2_ir(v1_ir))
    assert planned.read_set == physical_read_set([v1_ir], v1_ir.spine)
    assert planned.read_set, "a real feature reads something"


def test_a_FORGED_narrow_read_set_cannot_be_constructed(v1_ir):
    """The only thing standing between "Gate 2 authorized these refs" and "the run read those refs"
    — so a hand-built plan claiming a narrower read set than its IR derives is refused."""
    ir = _v2_ir(v1_ir)
    full = physical_read_set_of([(ir.feature_name, ir.expressions)], ir.spine)
    with pytest.raises(ValueError, match="MISSING"):
        PlannedFormulaExecutionIRV2(ir=ir, read_set=full[:1])


def test_authorization_is_TYPED_on_planned_irs_not_bare_ones():
    """C-C2's gate, checked structurally: an authorization over unplanned IRs is unconstructible,
    not merely incorrect."""
    field = {f.name: f for f in dataclasses.fields(AuthorizedCompilationV2)}["planned"]
    assert "PlannedFormulaExecutionIRV2" in str(field.type)
    assert "FormulaExecutionIRV2" not in str(field.type).replace(
        "PlannedFormulaExecutionIRV2", "")


def test_a_bare_ir_cannot_be_authorized(v1_ir):
    with pytest.raises((AttributeError, ValueError, TypeError)):
        AuthorizedCompilationV2(
            planned=(_v2_ir(v1_ir),), spine=v1_ir.spine,  # type: ignore[arg-type]
            authorized_refs=(), roles_used=_ROLES)


def test_authorization_refuses_refs_the_group_actually_reads(v1_ir):
    """A token naming fewer refs than the compilation reads is the exact shape of an authorization
    bypass."""
    planned = PlannedFormulaExecutionIRV2.plan(_v2_ir(v1_ir))
    with pytest.raises(ValueError, match="does not authorize"):
        AuthorizedCompilationV2(planned=(planned,), spine=v1_ir.spine,
                                authorized_refs=planned.read_set[:1], roles_used=_ROLES)


def test_an_empty_group_is_refused(v1_ir):
    with pytest.raises(ValueError, match="names no feature"):
        AuthorizedCompilationV2(planned=(), spine=v1_ir.spine, authorized_refs=(),
                                roles_used=_ROLES)


def test_the_token_covers_the_read_set_and_names_its_ir_hashes(v1_ir):
    planned = PlannedFormulaExecutionIRV2.plan(_v2_ir(v1_ir))
    token = AuthorizedCompilationV2(planned=(planned,), spine=v1_ir.spine,
                                    authorized_refs=planned.read_set, roles_used=_ROLES)
    assert token.ir_hashes == (ir_hash_v2(planned.ir),)


# ══ physical types are a POLICY ID, not an ordinal ═══════════════════════════════════════════════
def _contract(catalog, v1_ir, **overrides) -> MaterializationContractV2:
    """A V2 contract mapped from a REALLY DERIVED V1 one.

    Every field is copied across by name, so this is also the parity check: if V1's contract gains
    or renames a field, ``MaterializationContractV2(**kwargs)`` raises here instead of the two types
    quietly drifting into contracts that group differently.
    """
    from tests.featuregen.materialize.test_contract import CADENCE, NEXT_DAY

    v1 = derive_contract(catalog, v1_ir, cadence=CADENCE, availability_promise=NEXT_DAY,
                         overrides=None)
    assert isinstance(v1, MaterializationContractV1), v1
    kwargs = {field.name: getattr(v1, field.name) for field in dataclasses.fields(v1)}
    assert kwargs.pop("physical_type_policy_version") is not None, "V1 carries the ordinal"
    kwargs["physical_type_policy"] = POLICY_ID
    kwargs.update(overrides)
    return MaterializationContractV2(**kwargs)


def test_the_v2_contract_is_v1s_FIELD_FOR_FIELD_apart_from_the_policy_id(catalog, v1_ir):
    contract = _contract(catalog, v1_ir)
    assert contract.physical_type_policy == POLICY_ID
    assert not hasattr(contract, "physical_type_policy_version")
    assert contract.identity_payload()["physical_type_policy"] == POLICY_ID


@pytest.mark.parametrize("bad", ["", "  ", "1", "2", "v1", "formula-v2/physical-types",
                                 "formula-v2@1", "Formula-V2/Physical-Types@1"])
def test_v1s_ordinal_cannot_be_smuggled_into_the_policy_id(catalog, v1_ir, bad):
    """An ordinal says which counter value was current, not which rules decided a type — and the
    two are indistinguishable once persisted as a string."""
    with pytest.raises(ValueError, match="physical-type policy id"):
        _contract(catalog, v1_ir, physical_type_policy=bad)


def test_the_group_plan_refuses_the_ordinal_too():
    with pytest.raises(ValueError, match="physical-type policy id"):
        FeatureGroupPlanV2(
            logical_group_name="g", materialization_contract_hash="c",
            entity_key_columns=("account_id",), business_dt_column="business_dt",
            features=(), physical_type_policy="1")


# ══ identity ═════════════════════════════════════════════════════════════════════════════════════
def test_the_identity_pairs_hashes_FROM_THE_TOKEN(v1_ir):
    """The identity names what Gate 2 admitted, so a caller cannot build one over features the gate
    never saw — and the two hash lists are paired one per feature by construction."""
    planned = PlannedFormulaExecutionIRV2.plan(_v2_ir(v1_ir))
    token = AuthorizedCompilationV2(planned=(planned,), spine=v1_ir.spine,
                                    authorized_refs=planned.read_set, roles_used=_ROLES)
    plan = FeatureGroupPlanV2(
        logical_group_name="account_daily", materialization_contract_hash="sha256:contract",
        entity_key_columns=("account_id",), business_dt_column="business_dt",
        features=(), physical_type_policy=POLICY_ID)
    identity = build_compilation_identity_v2(token, "sha256:contract", plan)

    assert identity.ir_hashes == (ir_hash_v2(planned.ir),)
    assert identity.formula_content_hashes == (planned.ir.formula_content_hash,)
    assert identity.group_plan_hash == group_plan_hash_v2(plan)


def test_an_identity_over_no_feature_is_refused():
    with pytest.raises(ValueError, match="names no feature"):
        CompilationIdentityV2(formula_content_hashes=(), ir_hashes=(),
                              materialization_contract_hash="c", group_plan_hash="g")


def test_unequal_hash_lists_are_refused():
    with pytest.raises(ValueError, match="paired one per feature"):
        CompilationIdentityV2(formula_content_hashes=("a",), ir_hashes=("x", "y"),
                              materialization_contract_hash="c", group_plan_hash="g")
