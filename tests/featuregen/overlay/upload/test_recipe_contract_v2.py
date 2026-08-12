"""BR-2 — Recipe Contract v2: the audited debt classes are UNCONSTRUCTIBLE, not just counted.

Constructor-time validation means an invalid definition cannot exist long enough to serialize:
one atomic output with the `measure`-parameter side door closed, no readiness by implication
(UNASSESSED does not exist in this vocabulary), typed policies where the unit kind makes them
load-bearing, and a legacy adapter that projects every un-migrated Template as conceptual-only —
promotion happens only through an explicit reviewed replacement, never inference from prose.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.overlay.upload.recipe_contract_v2 import (
    FormulaReferenceV2,
    LeakageSpecV2,
    OperandSpecV2,
    OutputSpecV2,
    ParameterSpecV2,
    RecipeContractError,
    RecipeReviewV1,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_legacy_adapter import (
    LegacyRecipeProjectionV1,
    project_recipe,
    project_template,
    v2_by_legacy_id,
)
from featuregen.overlay.upload.recipe_registry_v2 import (
    PROBE_RECIPE,
    V2_RECIPES,
    v2_replaced_legacy_ids,
    validate_v2_registry,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES


def test_the_probe_recipe_constructs_and_the_registry_holds_the_migrated_packs():
    """BR-2 shipped the contract with an EMPTY production registry; BR-11 landed the first pack.
    The pin moves with the migration: every member is a validated V2 definition and the registry
    law holds over the real population."""
    assert PROBE_RECIPE.computation_kind == "deterministic_formula"
    assert len(V2_RECIPES) >= 23                     # the retail pack, growing by pack
    # Migration-era packs (the BR-11..16 families) replace declared legacy ids; the BR-18+
    # foundation/expansion packs are NEW recipes and replace nothing — both facts pinned.
    migration_families = {"retail_churn", "cross_sell", "credit_risk", "collections",
                          "fraud", "aml", "payments", "deposits_alm", "markets", "custody",
                          "asset_management", "insurance", "islamic", "esg", "corporate_cib"}
    for r in V2_RECIPES:
        if r.family in migration_families:
            assert r.replaces_legacy_ids, r.recipe_id
        else:
            assert not r.replaces_legacy_ids, r.recipe_id
    validate_v2_registry()


# ── the one-output rule and its side door ────────────────────────────────────────────────────────

def test_a_measure_selecting_parameter_is_rejected():
    """The multi-output ambiguity's re-entry path: 126 legacy recipes emit different quantities
    through a `measure` param. Under V2 each quantity is its own recipe revision."""
    with pytest.raises(RecipeContractError, match="selects the emitted quantity"):
        ParameterSpecV2(name="measure", parameter_class="semantic",
                        allowed_values=("net_amount", "net_share"),
                        identity_projection="measure={value}",
                        display_projection="{value}")


def test_output_policy_holes_are_rejected():
    with pytest.raises(RecipeContractError, match="currency policy"):
        replace(PROBE_RECIPE.output, currency_policy="")
    with pytest.raises(RecipeContractError, match="zero-denominator"):
        OutputSpecV2(output_id="r", display_label="r", output_type="numeric",
                     additivity="non_additive", unit_kind="ratio",
                     null_input_policy="excluded", empty_population_policy="null result")


def test_additivity_must_be_compatible_with_the_formula_result_class():
    """The 72-recipe conflict class: a sum cannot be non-additive, a ratio cannot be additive."""
    with pytest.raises(RecipeContractError, match="incompatible with .* result class"):
        replace(PROBE_RECIPE,
                output=replace(PROBE_RECIPE.output, additivity="non_additive"))
    with pytest.raises(RecipeContractError, match="incompatible"):
        replace(PROBE_RECIPE,
                formula=replace(PROBE_RECIPE.formula, result_class="ratio"))


# ── no readiness by implication ──────────────────────────────────────────────────────────────────

def test_unassessed_cannot_exist_on_a_v2_definition():
    with pytest.raises(RecipeContractError, match="readiness"):
        replace(PROBE_RECIPE, readiness="UNASSESSED")


def test_computation_kind_cross_rules():
    # executable without a formula
    with pytest.raises(RecipeContractError, match="exact formula reference"):
        replace(PROBE_RECIPE, formula=None)
    # conceptual with a formula, and conceptual without a reason
    with pytest.raises(RecipeContractError, match="may not reference a formula"):
        replace(PROBE_RECIPE, computation_kind="conceptual_pattern",
                readiness="CONCEPTUAL_ONLY", conceptual_reason="an idea")
    with pytest.raises(RecipeContractError, match="WHY no exact computation"):
        replace(PROBE_RECIPE, computation_kind="conceptual_pattern",
                readiness="CONCEPTUAL_ONLY", formula=None)
    # a model output is not a formula (BR-7A owns its spec)
    with pytest.raises(RecipeContractError, match="ModelFeatureSpec"):
        replace(PROBE_RECIPE, computation_kind="governed_model_output", formula=None)


def test_executable_operands_need_grains_and_parameters_need_class_and_projection():
    bare_operand = replace(PROBE_RECIPE.operands[0], allowed_source_grains=())
    with pytest.raises(RecipeContractError, match="allowed-source-grain"):
        replace(PROBE_RECIPE, operands=(bare_operand, *PROBE_RECIPE.operands[1:]))
    with pytest.raises(RecipeContractError, match="parameter_class"):
        ParameterSpecV2(name="window", parameter_class="whatever", allowed_values=(30,),
                        identity_projection="window={value}d", display_projection="{value}d")
    with pytest.raises(RecipeContractError, match="identity projection"):
        ParameterSpecV2(name="window", parameter_class="operational", allowed_values=(30,),
                        identity_projection="", display_projection="{value}d")
    with pytest.raises(RecipeContractError, match="reviewed policy"):
        ParameterSpecV2(name="threshold", parameter_class="governed_policy",
                        identity_projection="thr={value}", display_projection="{value}")


def test_structural_rules_hold():
    # a temporal window parameter must be declared
    with pytest.raises(RecipeContractError, match="not a declared parameter"):
        replace(PROBE_RECIPE, parameters=())
    # duplicate operand roles
    with pytest.raises(RecipeContractError, match="duplicate operand roles"):
        replace(PROBE_RECIPE, operands=(*PROBE_RECIPE.operands, PROBE_RECIPE.operands[0]))
    # primary objective must be a selectable taxonomy leaf
    with pytest.raises(RecipeContractError, match="selectable taxonomy leaf"):
        replace(PROBE_RECIPE, primary_objective="not_a_leaf")
    # a contractual-future anchor needs its horizon policy
    with pytest.raises(RecipeContractError, match="future-horizon policy"):
        TemporalSpecV2(anchor_kind="contractual_future")
    # a recorded review decision needs a reviewer
    with pytest.raises(RecipeContractError, match="needs a reviewer"):
        RecipeReviewV1(decision="approved")
    # a stage cannot be both permitted and prohibited
    with pytest.raises(RecipeContractError, match="both permitted and prohibited"):
        LeakageSpecV2(classification="near_label",
                      permitted_stages=("monitoring",), prohibited_stages=("monitoring",))
    # an operand concept must be classifier-producible (through the alias seam)
    with pytest.raises(RecipeContractError, match="not producible"):
        OperandSpecV2(role="x", concept="monetary_amount", operand_class="measure",
                      allowed_source_grains=("transaction",))


# ── canonical v2 hashing ─────────────────────────────────────────────────────────────────────────

def test_v2_hash_is_deterministic_and_every_edit_changes_it():
    base = canonical_recipe_v2_hash(PROBE_RECIPE)
    assert base == canonical_recipe_v2_hash(PROBE_RECIPE)
    edits = [
        replace(PROBE_RECIPE, revision=2),
        replace(PROBE_RECIPE, business_definition=PROBE_RECIPE.business_definition + "."),
        replace(PROBE_RECIPE, parameters=(
            replace(PROBE_RECIPE.parameters[0], allowed_values=(30, 90)),)),
        replace(PROBE_RECIPE, operands=(
            *PROBE_RECIPE.operands[:-1],
            replace(PROBE_RECIPE.operands[-1], temporal_role="event_time"))),
        replace(PROBE_RECIPE, temporal=replace(PROBE_RECIPE.temporal,
                                               cutoff_inclusivity="exclusive")),
        replace(PROBE_RECIPE, output=replace(PROBE_RECIPE.output,
                                             currency_policy="a different governed policy")),
        replace(PROBE_RECIPE, leakage=LeakageSpecV2(classification="near_label")),
        replace(PROBE_RECIPE, formula=FormulaReferenceV2(
            formula_schema_version="formula-v2", expectation_ref="probe:other",
            result_class="sum")),
    ]
    hashes = [canonical_recipe_v2_hash(e) for e in edits]
    assert len({base, *hashes}) == len(edits) + 1, \
        "every output / parameter / operand / temporal / policy edit must change the v2 hash"


# ── the legacy adapter: conceptual-only, always ──────────────────────────────────────────────────

def test_every_legacy_template_projects_conceptual_only_and_unassessed():
    for template in ALL_TEMPLATES:
        projection = project_template(template)
        assert projection.computation_kind == "conceptual_pattern"
        assert projection.readiness == "UNASSESSED"
        assert not hasattr(projection, "formula"), \
            "the projection type has no formula field to fill — prose can never become executable"


def test_an_explicit_replacement_wins_and_heuristics_do_not_exist():
    v2 = replace(PROBE_RECIPE, recipe_id="balance_trend_v2",
                 replaces_legacy_ids=("balance_trend",))
    index = v2_by_legacy_id([v2])
    balance_trend = next(t for t in ALL_TEMPLATES if t.id == "balance_trend")
    dormancy = next(t for t in ALL_TEMPLATES if t.id == "dormancy_days")
    assert project_recipe(balance_trend, index) is v2
    assert isinstance(project_recipe(dormancy, index), LegacyRecipeProjectionV1)
    # one legacy id, one successor set — a second claimant is a registry error
    with pytest.raises(ValueError, match="replaced by both"):
        v2_by_legacy_id([v2, replace(v2, recipe_id="balance_trend_v2b")])


def test_registry_law_and_the_audit_pipe():
    from featuregen.overlay.upload.recipe_audit import audit_registry
    from featuregen.overlay.upload.recipe_contract_v2 import RecipeContractError as Err

    v2 = replace(PROBE_RECIPE, recipe_id="balance_trend_v2",
                 replaces_legacy_ids=("balance_trend",))
    validate_v2_registry((v2,))
    with pytest.raises(Err, match="duplicate V2 recipe id"):
        validate_v2_registry((v2, v2))
    with pytest.raises(Err, match="unknown legacy ids"):
        validate_v2_registry((replace(v2, replaces_legacy_ids=("no_such_recipe",)),))
    with pytest.raises(Err, match="reuses a legacy id"):
        validate_v2_registry((replace(v2, recipe_id="dormancy_days"),))
    # the migration debt counter falls exactly as replacements land
    assert v2_replaced_legacy_ids((v2,)) == frozenset({"balance_trend"})
    report = audit_registry(v2_recipe_ids=v2_replaced_legacy_ids((v2,)))
    assert report.counters["legacy_recipes_not_in_v2"] == 156
