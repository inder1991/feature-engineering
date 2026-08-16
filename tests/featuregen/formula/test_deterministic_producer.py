"""The deterministic authoring producer — a reviewed blueprint becomes a v3 proposal, no provider.

Driven end to end through the REAL path — ``derive_blueprint_v2`` on the shipped
``posted_debit_amount`` recipe, then A1's binder against a grounded context — rather than a
hand-built expectation. A producer tested against a fixture of its own making proves only that it
agrees with itself.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.formula.deterministic_producer import (
    DeterministicAuthoringRefused,
    bypass_for,
    proposal_from_bound_expectation,
)
from featuregen.formula.result import AuthoringAxes
from featuregen.formula.result_v2 import (
    AuthoringAxesV2,
    CriticExecutedV2,
    ReviewedBlueprintBypassV2,
    derive_disposition_v2,
)
from featuregen.formula.schema_v3 import SelectionKind, TypedFormulaProposalV3
from featuregen.overlay.upload.recipe_formula_blueprint_derivation import derive_blueprint_v2
from featuregen.overlay.upload.recipe_formula_contracts_v2 import bind_formula_expectation_v2
from featuregen.overlay.upload.recipe_grounding_context import (
    RecipeGroundingContextV1,
    semantic_parameter_hash,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    content_hash as grounding_content_hash,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.templates import (
    BindingResolution,
    GroundedNeedBinding,
    SourceEntityRoleResolution,
)


def _binding(role: str, ref: str) -> GroundedNeedBinding:
    return GroundedNeedBinding(
        role=role, catalog_source="bank", logical_ref=ref,
        graph_object_ref=ref.replace("bank::", "public."), expected_concept=role,
        optional=False, join_role=None, temporal_role=None, distinct_binding_group=None,
        binding_resolution=BindingResolution.UNIQUE, tied_candidate_logical_refs=(ref,),
        tied_candidate_set_hash="set-hash")


def _bound(recipe_id: str = "posted_debit_amount"):
    """The pilot's blueprint, derived from the SHIPPED recipe and bound to real refs."""
    blueprint = derive_blueprint_v2(v2_recipe_by_id(recipe_id))
    parameters = (("window", 90),)
    definition_json = {"version": "deterministic-producer-probe"}
    context = RecipeGroundingContextV1(
        recipe_candidate_key="candidate", recipe_id=recipe_id,
        source_entity_need_role="account",
        source_entity_role_resolution=SourceEntityRoleResolution.INFERRED_UNAMBIGUOUS,
        need_bindings=(_binding("account", "bank::public.txns.acct_id"),
                       _binding("amount", "bank::public.txns.txn_amt"),
                       _binding("event_ts", "bank::public.txns.booking_ts")),
        semantic_parameters=parameters,
        semantic_parameter_binding_hash=semantic_parameter_hash(recipe_id, parameters),
        template_definition=definition_json,
        template_content_hash=grounding_content_hash(definition_json))
    return bind_formula_expectation_v2(context, blueprint)


# ── the pilot authors itself, with no provider call ─────────────────────────────────────────────
def test_the_pilot_blueprint_produces_a_valid_v3_proposal():
    proposal = proposal_from_bound_expectation(_bound())
    assert isinstance(proposal, TypedFormulaProposalV3)
    assert proposal.formula_schema_version == 3
    assert proposal.grain.entity == "account"
    assert proposal.grain.keys == ("bank::public.txns.acct_id",)
    expr = proposal.body.expr
    assert expr.operand == "bank::public.txns.txn_amt"
    assert expr.source_relation.table_ref == "bank::public.txns"
    assert expr.window.event_time_ref == "bank::public.txns.booking_ts"


def test_the_declared_direction_reaches_the_proposal():
    """C-A3b's whole point, end to end: the recipe DECLARED debit, and the formula says debit —
    with nothing anywhere reading the recipe's name."""
    expr = proposal_from_bound_expectation(_bound()).body.expr
    assert [(s.kind, s.role, s.semantic_value) for s in expr.row_selections] == [
        (SelectionKind.TRANSACTION_DIRECTION, "direction", "debit")]
    assert expr.filter is None, "a reviewed blueprint declares structurally, never by filter"


def test_the_credit_twin_produces_the_OPPOSITE_selection():
    """The two recipes were once distinguishable only by name. Now the formulas differ."""
    debit = proposal_from_bound_expectation(_bound("posted_debit_amount"))
    credit = proposal_from_bound_expectation(_bound("posted_credit_amount"))
    assert debit.body.expr.row_selections[0].semantic_value == "debit"
    assert credit.body.expr.row_selections[0].semantic_value == "credit"


def test_the_resolved_window_length_is_used_not_the_parameter_NAME():
    """`length_parameter` is a reference (`"window_days"`); the BOUND expectation carries the
    resolved value. Reading the reference where the value belongs would put a parameter name into
    a formula's identity."""
    bound = _bound()
    assert bound.expressions[0].window_length == 90
    assert proposal_from_bound_expectation(bound).body.expr.window.length == 90


def test_a_deterministic_proposal_declares_no_parameters():
    """Every parameter is already RESOLVED into the bound expectation — that is what bound means.
    Re-declaring one would put an unbound name into the formula's identity."""
    assert proposal_from_bound_expectation(_bound()).parameters == ()


def test_the_proposal_goes_through_the_SAME_gate_as_an_authored_one():
    """Not a second admission path: a blueprint that cannot make a legal formula fails here exactly
    as a model's proposal would."""
    bound = _bound()
    broken = replace(bound, grain_key_refs=())
    with pytest.raises(DeterministicAuthoringRefused, match="does not determine a legal"):
        proposal_from_bound_expectation(broken)


# ── review, stated honestly ─────────────────────────────────────────────────────────────────────
def test_the_bypass_names_the_exact_blueprint_it_stood_on():
    bound = _bound()
    bypass = bypass_for(bound)
    assert isinstance(bypass, ReviewedBlueprintBypassV2)
    assert bypass.expectation_hash == bound.blueprint_content_hash
    assert bypass.blueprint_revision == bound.recipe_candidate_key


def test_a_deterministic_run_folds_with_no_critic_status():
    """The end of the chain: a reviewed blueprint authors a formula and the result says the critic
    did not run — never `"clean"`, which would claim it ran and found nothing."""
    bound = _bound()
    proposal = proposal_from_bound_expectation(bound)
    result = derive_disposition_v2(
        AuthoringAxesV2(
            structural_status="ok", capability_status="ok", output_status="needs_authority",
            expectation_status="match", review=bypass_for(bound), technical_status="ok"),
        authoring_run_id="run_deterministic", candidate_proposal=proposal,
        reviewed_expectation_hash=bound.blueprint_content_hash)

    assert result.critic_status is None
    assert result.review == bypass_for(bound)
    assert result.critic_findings_hash is None
    assert result.candidate_proposal is proposal


def test_a_bypass_for_ANOTHER_recipe_is_refused():
    """Neutrality is checked, not asserted: the bypass must cover the expectation the run opened
    for. Before this check one bypass value folded any formula neutral."""
    debit, credit = _bound("posted_debit_amount"), _bound("posted_credit_amount")
    assert debit.blueprint_content_hash != credit.blueprint_content_hash
    from featuregen.formula.result import IncoherentResultError
    with pytest.raises(IncoherentResultError, match="covered a different formula"):
        derive_disposition_v2(
            AuthoringAxesV2(
                structural_status="ok", capability_status="ok", output_status="needs_authority",
                expectation_status="match", review=bypass_for(credit), technical_status="ok"),
            authoring_run_id="r", candidate_proposal=proposal_from_bound_expectation(debit),
            reviewed_expectation_hash=debit.blueprint_content_hash)


def test_no_provider_client_is_reachable_from_the_producer():
    """The claim "no provider call" made structural: the module imports no LLM seam at all."""
    import inspect

    from featuregen.formula import deterministic_producer

    source = inspect.getsource(deterministic_producer)
    for forbidden in ("LLMClient", "author_formula", "critique", "audited", "intake.llm"):
        assert forbidden not in source, forbidden


def test_the_v1_axes_path_is_untouched():
    """A v1-shaped run still folds with a real critic status and no review."""
    result = derive_disposition_v2(
        AuthoringAxes(structural_status="ok", capability_status="ok",
                      output_status="needs_authority", expectation_status="match",
                      critic_status="clean", technical_status="ok"),
        authoring_run_id="run_v1", candidate_proposal=proposal_from_bound_expectation(_bound()))
    assert result.critic_status == "clean"
    assert result.review is None
    assert not isinstance(result.review, CriticExecutedV2)
