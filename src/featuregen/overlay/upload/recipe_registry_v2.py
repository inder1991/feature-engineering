"""The ACTIVE recipe registry — Recipe Contract v2, fully populated (BR-17 cutover).

Every one of the 157 legacy templates has an explicit V2 replacement here (the audit's
``legacy_recipes_not_in_v2`` counter is ZERO, pinned by test). ``PROBE_RECIPE`` remains the one
NON-production definition (BR-2's end-to-end construction proof). Registry validation runs at
import: unique ids, explicit legacy replacements only, no id squatting.

**The alias seam (BR-17):** :data:`LEGACY_ALIAS_MAP` is DERIVED from each definition's
``replaces_legacy_ids`` — source-controlled, never heuristic. A legacy id with ONE replacement
resolves directly; a multi-target id (a legacy multi-measure recipe split into atoms) REFUSES to
resolve without the output alias — :func:`resolve_legacy_alias` raises
:class:`AmbiguousLegacyAlias` naming the atoms, because silently picking one would smuggle the
one-output rule's whole defect class back in.

**Compatibility window:** v1/v2 suggestion generation still reads the legacy Template projection;
contract v3's execution truth grounds HERE. The legacy registry is READ-ONLY (its freeze test
fails CI on any new Template) and is removed only when the v1/v2 contracts retire (BR-24).
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
from featuregen.overlay.upload.recipes.aml import AML_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.asset_management import AM_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.collections import COLLECTIONS_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.corporate_cib import CORPORATE_CIB_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.credit import CREDIT_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.cross_sell import CROSS_SELL_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.custody import CUSTODY_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.deposits_alm import DEPOSITS_ALM_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.esg import ESG_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.fraud import FRAUD_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.insurance import INSURANCE_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.islamic import ISLAMIC_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.markets import MARKETS_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.payments import PAYMENTS_RECIPES  # noqa: E402
from featuregen.overlay.upload.recipes.retail import RETAIL_RECIPES  # noqa: E402

V2_RECIPES: tuple[RecipeDefinitionV2, ...] = (*RETAIL_RECIPES, *CROSS_SELL_RECIPES,
                                              *CREDIT_RECIPES, *COLLECTIONS_RECIPES,
                                              *FRAUD_RECIPES, *AML_RECIPES,
                                              *PAYMENTS_RECIPES, *DEPOSITS_ALM_RECIPES,
                                              *MARKETS_RECIPES, *CUSTODY_RECIPES,
                                              *AM_RECIPES, *INSURANCE_RECIPES,
                                              *ISLAMIC_RECIPES, *ESG_RECIPES,
                                              *CORPORATE_CIB_RECIPES)

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


class AmbiguousLegacyAlias(RecipeContractError):
    """A multi-target legacy id resolved without its output alias — refused, never guessed."""


def _build_alias_map() -> dict[str, tuple[str, ...]]:
    aliases: dict[str, list[str]] = {}
    for recipe in V2_RECIPES:
        for legacy_id in recipe.replaces_legacy_ids:
            aliases.setdefault(legacy_id, []).append(recipe.recipe_id)
    return {legacy_id: tuple(targets) for legacy_id, targets in aliases.items()}


#: legacy recipe id -> the atomic V2 recipe ids replacing it, in pack declaration order.
LEGACY_ALIAS_MAP: dict[str, tuple[str, ...]] = {}

_BY_ID: dict[str, RecipeDefinitionV2] = {}


def v2_recipe_by_id(recipe_id: str) -> RecipeDefinitionV2 | None:
    return _BY_ID.get(recipe_id)


def resolve_legacy_alias(legacy_id: str,
                         output_alias: str | None = None) -> RecipeDefinitionV2:
    """One legacy id -> ONE atomic V2 definition, or a refusal that names the choices.

    A single-replacement id resolves directly. A multi-measure id split into atoms REQUIRES
    ``output_alias`` (one of its target recipe ids): resolving `salary_signal` without saying
    WHICH of its four outputs you mean is exactly the ambiguity the one-output rule closed."""
    targets = LEGACY_ALIAS_MAP.get(legacy_id)
    if targets is None:
        raise RecipeContractError(f"unknown legacy recipe id {legacy_id!r} — the alias map is "
                                  "derived from replaces_legacy_ids, never guessed")
    if output_alias is not None:
        if output_alias not in targets:
            raise RecipeContractError(
                f"{output_alias!r} is not a replacement of {legacy_id!r}; its atoms are "
                f"{list(targets)}")
        return _BY_ID[output_alias]
    if len(targets) == 1:
        return _BY_ID[targets[0]]
    raise AmbiguousLegacyAlias(
        f"legacy id {legacy_id!r} split into {len(targets)} atomic outputs "
        f"{list(targets)} — name the output alias; resolving it silently would pick a "
        "quantity nobody chose")


validate_v2_registry()
LEGACY_ALIAS_MAP.update(_build_alias_map())
_BY_ID.update({recipe.recipe_id: recipe for recipe in V2_RECIPES})
