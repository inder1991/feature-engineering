"""C-C3/C-C4/C-C5 — the full-read-set leakage gate, the policy-read union, and per-read time.

Driven off a REAL compiled V1 IR, as ``test_boundary_v2`` is, so the read set the gate walks is one
the compiler actually produced. The S6 gate the plan names — *"a mutation replacing a policy
status/direction column with the target ref refuses"* — is the load-bearing test here: it is the
case a formula-only leakage check cannot see, because the target never appears in the formula.
"""
from __future__ import annotations

import pytest
from tests.featuregen.materialize.test_ir import (
    _ROLES,
    DECLARATION,
    INVENTORY,
    _admitted,
    compile_ir,
    seed_catalog,
)

from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.schema_leaves import AdditivityClass
from featuregen.formula.schema_v2 import AuthorityRefsV2, FinalOperationV2
from featuregen.materialize.boundary_v2 import (
    AuthorizedCompilationV2,
    DeclaredPoliciesV2,
    FormulaExecutionIRV2,
    KnowledgeTimeBasisV2,
    PlannedFormulaExecutionIRV2,
    PolicyReadV2,
    TemporalReadV2,
)
from featuregen.materialize.contract import AvailabilityPromiseV1
from featuregen.materialize.ir import FormulaExecutionIRV1
from featuregen.materialize.leakage_v2 import (
    DIRECT_TARGET_READ,
    LEAKAGE_CLAIM_V2,
    POST_CUTOFF_POLICY_READ,
    LeakageVerdictV2,
    full_read_set_leakage_gate_v2,
)

STATUS_POLICY = "eligible_status:foundation-posted-events"
#: A policy column the FORMULA does not read, so union tests isolate the policy path.
STATUS_COLUMN = "hdfc::public.status_policy.eligible_cd"
#: A column the seeded fixture genuinely reads BOTH ways.
SHARED_COLUMN = "hdfc::public.transactions.status_cd"


@pytest.fixture
def catalog(db):
    return seed_catalog(db)


@pytest.fixture
def v1_ir(catalog) -> FormulaExecutionIRV1:
    ir = compile_ir(catalog, _admitted("total_debit_amount_30d"), roles=_ROLES,
                    spine_decl=DECLARATION, inventory=INVENTORY)
    assert isinstance(ir, FormulaExecutionIRV1), ir
    return ir


def _as_of(promise_days: int | None = 1) -> TemporalReadV2:
    return TemporalReadV2(
        basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        declared_promise=None if promise_days is None else AvailabilityPromiseV1(
            calendar_days=promise_days))


def _status_read(logical_ref: str = STATUS_COLUMN, *,
                 temporal: TemporalReadV2 | None = None) -> PolicyReadV2:
    return PolicyReadV2(policy_ref=STATUS_POLICY, role="status", logical_ref=logical_ref,
                        temporal=temporal or _as_of())


def _v2_ir(v1: FormulaExecutionIRV1, *, declare_status: bool = True) -> FormulaExecutionIRV2:
    path = v1.expressions[0].expr_path
    policies = (DeclaredPoliciesV2(expr_path=path,
                                   refs=AuthorityRefsV2(status_policy_ref=STATUS_POLICY)),)
    return FormulaExecutionIRV2(
        feature_name="posted_debit_amount_30d", formula_content_hash="sha256:pilot",
        final_operation=FinalOperationV2.IDENTITY, zero_denominator=None,
        grain_entity=v1.grain_entity, grain_keys=v1.grain_keys, expressions=v1.expressions,
        row_selections=(), policies=policies if declare_status else (), spine=v1.spine,
        output_policy=FormulaOutputPolicyV2(
            output_type="decimal", unit="monetary", currency="fixed:AED",
            output_additivity=AdditivityClass.ADDITIVE, external_type_required=False),
        authoring_run_id="run-v2-0001")


def _planned(v1: FormulaExecutionIRV1, *, reads=None) -> PlannedFormulaExecutionIRV2:
    return PlannedFormulaExecutionIRV2.plan(
        _v2_ir(v1), policy_reads=(_status_read(),) if reads is None else reads)


# ══ C-C4 — the union INCLUDES policy reads ═══════════════════════════════════════════════════════
def test_a_policy_read_enters_the_planned_read_set(v1_ir):
    """§5.2 classifies sensitivity over "what this feature reads". A policy column kept BESIDE the
    read set would be read at run time and absent from the classification."""
    planned = _planned(v1_ir)
    assert STATUS_COLUMN in planned.read_set


def test_a_DECLARED_policy_with_no_read_is_refused(v1_ir):
    """A governed policy is applied by reading its columns, so a declaration with no read would be
    applied on data Gate 2 never authorized."""
    with pytest.raises(ValueError, match="plans no read for them"):
        PlannedFormulaExecutionIRV2.plan(_v2_ir(v1_ir), policy_reads=())


def test_a_read_for_an_UNDECLARED_policy_is_refused(v1_ir):
    """The other direction: a read attributed to a policy the formula never declared has no reason
    to be in the union, and widening a read set is what the union exists to prevent."""
    with pytest.raises(ValueError, match="which it never declared"):
        PlannedFormulaExecutionIRV2.plan(
            _v2_ir(v1_ir, declare_status=False), policy_reads=(_status_read(),))


def test_policy_reads_have_NO_default(v1_ir):
    """A default of `()` would mean "this feature reads no policy columns" — a claim, and the wrong
    one for every feature that declares a policy."""
    with pytest.raises(TypeError):
        PlannedFormulaExecutionIRV2.plan(_v2_ir(v1_ir))  # type: ignore[call-arg]


def test_the_TOKEN_authorizes_the_policy_column_too(v1_ir):
    """C-C4 restated: Gate 2's read set is DERIVED, so there is no separate list to add the policy
    table to — and a token that misses it refuses."""
    planned = _planned(v1_ir)
    without_policy = tuple(ref for ref in planned.read_set if ref != STATUS_COLUMN)
    with pytest.raises(ValueError, match="does not authorize"):
        AuthorizedCompilationV2(planned=(planned,), spine=v1_ir.spine,
                                authorized_refs=without_policy, roles_used=_ROLES)
    AuthorizedCompilationV2(planned=(planned,), spine=v1_ir.spine,
                            authorized_refs=planned.read_set, roles_used=_ROLES)


# ══ C-C3 — the gate, over the FULL read set ══════════════════════════════════════════════════════
def test_a_clean_pilot_is_admitted(v1_ir):
    verdict = full_read_set_leakage_gate_v2(
        [_planned(v1_ir)], target_ref="hdfc::public.transactions.churned_flag")
    assert verdict.admitted
    assert verdict.findings == ()


def test_the_TARGET_ARRIVING_AS_A_POLICY_COLUMN_refuses(v1_ir):
    """THE S6 gate. The target appears nowhere in the formula — it arrives as the status policy's
    own column, which is exactly what a leakage check over operands and filters cannot see."""
    planned = _planned(v1_ir, reads=(_status_read(logical_ref="hdfc::public.transactions.target"),))
    verdict = full_read_set_leakage_gate_v2(
        [planned], target_ref="hdfc::public.transactions.target")

    assert not verdict.admitted
    (finding,) = [f for f in verdict.findings if f.code == DIRECT_TARGET_READ]
    assert finding.source == "policy_read:status"
    assert STATUS_POLICY in finding.detail
    assert "through the policy rather than through the formula" in finding.detail


def test_a_target_ALIAS_refuses_too(v1_ir):
    """"canonical target plus RESOLVED aliases" — alias resolution is the catalog's job and is
    passed in, because a gate that guessed would be inventing the fact it checks against."""
    planned = _planned(v1_ir, reads=(_status_read(logical_ref="hdfc::public.transactions.tgt_v2"),))
    verdict = full_read_set_leakage_gate_v2(
        [planned], target_ref="hdfc::public.transactions.target",
        target_aliases=("hdfc::public.transactions.tgt_v2",))
    assert not verdict.admitted
    assert verdict.findings[0].code == DIRECT_TARGET_READ


def test_a_STRUCTURAL_read_of_the_target_refuses_and_is_attributed_differently(v1_ir):
    """Same code, different source — "the target is read" and "the target is read as a policy's
    status column" send an author to different places."""
    planned = _planned(v1_ir)
    target = next(ref for ref in planned.read_set if ref != STATUS_COLUMN)
    verdict = full_read_set_leakage_gate_v2([planned], target_ref=target)
    assert not verdict.admitted
    assert [f.source for f in verdict.findings] == ["structural_read"]


def test_a_ref_read_BOTH_WAYS_produces_BOTH_findings(v1_ir):
    """`status_cd` in this fixture is genuinely read as an operand-side filter AND as the status
    policy's column. Reporting one path would send an author to close one door while the other
    still admits the target."""
    planned = _planned(v1_ir, reads=(_status_read(logical_ref=SHARED_COLUMN),))
    assert SHARED_COLUMN in planned.structural_read_set, "the fixture reads it structurally"
    verdict = full_read_set_leakage_gate_v2([planned], target_ref=SHARED_COLUMN)
    assert not verdict.admitted
    assert sorted(f.source for f in verdict.findings) == ["policy_read:status", "structural_read"]


def test_an_empty_walk_is_REFUSED_not_admitted(v1_ir):
    """An empty walk finds nothing; returning `admitted` would be a pass nobody earned."""
    with pytest.raises(ValueError, match="no planned features"):
        full_read_set_leakage_gate_v2([], target_ref="hdfc::public.transactions.target")


def test_a_blank_target_is_refused(v1_ir):
    with pytest.raises(ValueError, match="needs a canonical target ref"):
        full_read_set_leakage_gate_v2([_planned(v1_ir)], target_ref="  ")


# ══ C-C5 — per-read time, SEPARATE from the promise ══════════════════════════════════════════════
def test_a_post_cutoff_policy_read_refuses(v1_ir):
    """"post-cutoff FX and post-cutoff reversal refuse" — a reversal flag or rate read at current
    state tells you something that became true AFTER the cutoff."""
    latest = TemporalReadV2(basis=KnowledgeTimeBasisV2.LATEST_AVAILABLE, declared_promise=None)
    verdict = full_read_set_leakage_gate_v2(
        [_planned(v1_ir, reads=(_status_read(temporal=latest),))],
        target_ref="hdfc::public.transactions.churned_flag")
    assert not verdict.admitted
    assert verdict.findings[0].code == POST_CUTOFF_POLICY_READ


def test_a_GENEROUS_PROMISE_does_not_rescue_a_post_cutoff_read(v1_ir):
    """The two fields answer different questions. A promise says when data should ARRIVE, not which
    instant a read observes — conflating them is how a post-cutoff read looks fine."""
    latest = TemporalReadV2(basis=KnowledgeTimeBasisV2.LATEST_AVAILABLE,
                            declared_promise=AvailabilityPromiseV1(calendar_days=30))
    verdict = full_read_set_leakage_gate_v2(
        [_planned(v1_ir, reads=(_status_read(temporal=latest),))],
        target_ref="hdfc::public.transactions.churned_flag")
    assert not verdict.admitted
    assert "does not change that" in verdict.findings[0].detail


def test_the_basis_and_the_promise_are_SEPARATE_fields():
    import dataclasses

    names = {f.name for f in dataclasses.fields(TemporalReadV2)}
    assert names == {"basis", "declared_promise"}


def test_an_as_of_cutoff_read_is_fine(v1_ir):
    verdict = full_read_set_leakage_gate_v2(
        [_planned(v1_ir, reads=(_status_read(temporal=_as_of(promise_days=None)),))],
        target_ref="hdfc::public.transactions.churned_flag")
    assert verdict.admitted


# ══ invariant 18 — the claim stays NARROW ════════════════════════════════════════════════════════
def test_the_verdict_carries_the_narrow_claim(v1_ir):
    """So a caller reporting "leakage gate passed" has the disclaimer in hand rather than in a
    docstring nobody opened."""
    verdict = full_read_set_leakage_gate_v2(
        [_planned(v1_ir)], target_ref="hdfc::public.transactions.churned_flag")
    assert verdict.claim == LEAKAGE_CLAIM_V2


def test_the_claim_names_what_it_does_NOT_prove():
    lowered = LEAKAGE_CLAIM_V2.lower()
    assert "does not prove" in lowered
    assert "semantic prox" in lowered
    assert "descendant" in lowered
    for overclaim in ("no leakage", "leak-free", "guarantees", "is safe"):
        assert overclaim not in lowered, overclaim


def test_a_verdict_cannot_be_admitted_WITH_findings():
    """The two fields would disagree, and a caller reading only `admitted` would ship a compilation
    the gate refused."""
    from featuregen.materialize.leakage_v2 import LeakageFindingV2

    finding = LeakageFindingV2(code=DIRECT_TARGET_READ, logical_ref="r", source="s",
                               feature_name="f", detail="d")
    with pytest.raises(ValueError, match="cannot be admitted while carrying"):
        LeakageVerdictV2(admitted=True, findings=(finding,))
    with pytest.raises(ValueError, match="refusal with no findings"):
        LeakageVerdictV2(admitted=False, findings=())
