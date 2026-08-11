"""BR-2 — the V2 recipe registry: empty in production until the family migrations (BR-11..16).

``V2_RECIPES`` is the production population — the audit's ``legacy_recipes_not_in_v2`` counter
falls as replacements land here. ``PROBE_RECIPE`` is the one NON-production definition BR-2
registers to prove end-to-end construction, serialization and hashing; it never enters
``V2_RECIPES`` and never grounds. Registry validation runs at import: unique ids, explicit legacy
replacements only (every replaced id must exist in ``ALL_TEMPLATES`` — no heuristic aliasing),
and no id squatting on a legacy id it does not replace.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    LeakageSpecV2,
    OperandSpecV2,
    OutputSpecV2,
    ParameterSpecV2,
    RecipeContractError,
    RecipeDefinitionV2,
    TemporalSpecV2,
)

# The production V2 population, populated family by family as the BR-11..16 packs land. Until
# BR-17's cutover the packs change AUDIT accounting only — no grounding or suggestion path reads
# this tuple yet.
from featuregen.overlay.upload.recipes.cross_sell import CROSS_SELL_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.retail import RETAIL_RECIPES  # noqa: E402

V2_RECIPES: tuple[RecipeDefinitionV2, ...] = (*RETAIL_RECIPES, *CROSS_SELL_RECIPES)

# The end-to-end probe: a complete, valid, EXECUTABLE-shaped definition exercising every nested
# spec — deliberately outside V2_RECIPES (non-production) and deliberately FORMULA_BLOCKED with a
# named blocker, because its expectation is a placeholder no registry reviews.
PROBE_RECIPE = RecipeDefinitionV2(
    recipe_id="v2_probe_posted_debit_amount",
    revision=1,
    family="probe",
    primary_objective="customer.relationship_attrition.churn",
    business_definition=(
        "Sum of eligible posted debit economic amount per account over a trailing window — "
        "the BR-18 canonical exemplar's shape, used here only to prove the contract."),
    decision_context="non-production serialization probe",
    computation_kind="deterministic_formula",
    output=OutputSpecV2(
        output_id="posted_debit_amount",
        display_label="Posted debit amount",
        output_type="numeric",
        additivity="additive",
        unit_kind="monetary",
        unit_policy="account base currency units",
        currency_policy="convert through the governed rate policy at booking timestamp",
        null_input_policy="null amounts are excluded per the reviewed source policy",
        empty_population_policy="an empty window returns zero",
        aggregation_over_entity="sum over accounts within one currency",
        aggregation_over_time="sum over disjoint windows"),
    operands=(
        OperandSpecV2(role="account", concept="account_id", operand_class="entity_key",
                      allowed_source_grains=("transaction",)),
        OperandSpecV2(role="amount", concept="monetary_flow", operand_class="measure",
                      allowed_source_grains=("transaction",),
                      unit_expectation="monetary", currency_expectation="per-row currency",
                      sign_direction_expectation="unsigned amount plus direction authority"),
        OperandSpecV2(role="direction", concept="debit_credit_indicator",
                      operand_class="direction", allowed_source_grains=("transaction",),
                      status_policy_ref="policy:eligible-posted-status"),
        OperandSpecV2(role="event_ts", concept="event_timestamp",
                      operand_class="event_timestamp", allowed_source_grains=("transaction",)),
    ),
    source_grain="transaction",
    output_grain="account",
    temporal=TemporalSpecV2(
        anchor_kind="event",
        event_time_role="event_ts",
        window_basis="trailing",
        window_unit="days",
        window_parameter="window",
        cutoff_inclusivity="inclusive"),
    readiness="FORMULA_BLOCKED",
    parameters=(
        ParameterSpecV2(name="window", parameter_class="operational",
                        allowed_values=(30, 90, 180),
                        identity_projection="window={value}d",
                        display_projection="{value}-day window"),
    ),
    eligibility=EligibilitySpecV2(
        included="posted transactions in an eligible status",
        excluded="failed, reversed and technical events",
        policy_refs=("policy:eligible-posted-status",)),
    leakage=LeakageSpecV2(classification="standard"),
    formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                               expectation_ref="probe:posted_debit_amount",
                               result_class="sum"),
)


def validate_v2_registry(recipes: tuple[RecipeDefinitionV2, ...] = V2_RECIPES) -> None:
    """Import-time registry law. Individual definitions validated themselves at construction;
    this checks the POPULATION: id uniqueness, explicit legacy replacement, no squatting."""
    from featuregen.overlay.upload.templates import ALL_TEMPLATES

    legacy_ids = {t.id for t in ALL_TEMPLATES}
    seen: set[str] = set()
    for recipe in recipes:
        if recipe.recipe_id in seen:
            raise RecipeContractError(f"duplicate V2 recipe id {recipe.recipe_id!r}")
        seen.add(recipe.recipe_id)
        unknown = [rid for rid in recipe.replaces_legacy_ids if rid not in legacy_ids]
        if unknown:
            raise RecipeContractError(
                f"{recipe.recipe_id!r} replaces unknown legacy ids {unknown} — replacement is "
                "explicit, never heuristic")
        if recipe.recipe_id in legacy_ids and recipe.recipe_id not in recipe.replaces_legacy_ids:
            raise RecipeContractError(
                f"{recipe.recipe_id!r} reuses a legacy id it does not declare it replaces")


def v2_replaced_legacy_ids(recipes: tuple[RecipeDefinitionV2, ...] = V2_RECIPES) -> frozenset[str]:
    """The legacy ids with a V2 replacement — the audit's ``v2_recipe_ids`` input, so the
    migration debt counter falls exactly as replacements land."""
    return frozenset(rid for recipe in recipes for rid in recipe.replaces_legacy_ids)


validate_v2_registry()
