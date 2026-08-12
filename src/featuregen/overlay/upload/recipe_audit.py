"""BR-1 — the banking-recipe production-readiness audit (plan 2026-08-10, re-baselined 398cc0ab).

Make every identified defect MACHINE-COUNTABLE before changing behavior: this module is a pure
report over the recipe registry — no DB, no model, no mutation — whose counters become the debt
baseline a CI ratchet holds. New debt cannot increase; intentional decreases are the program's
progress meter; strict mode (every ratcheted counter zero) stays off until BR-17.

Every check is DEFINED here, precisely, because the review's prose numbers were estimates until a
regex pins them ("11 recipes admit a missing concept" measured 15 under the documented pattern —
the audit's definition is the authoritative one from BR-1 onward). Checks that reproduce the
review's counts exactly at re-baseline: 157 recipes, 126 multi-measure, 1,122 parameter
combinations, 145 identity-collision recipes, 33 downstream derivations, 14 primary objectives,
2 Formula-v1-authorable.

The ambiguous-binding disposition is CATALOG-dependent, so it lives in
:func:`dynamic_binding_audit` (optional, needs a connection) and is deliberately outside the
static ratchet — CI must not depend on which catalogs happen to be loaded.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import product

# ── documented check definitions ─────────────────────────────────────────────────────────────────

# The one parameter key the registry uses for "which quantity does this recipe emit".
_MEASURE_PARAM = "measure"

# Additivity classes inferred from a measure VALUE's token — used only to detect a multi-measure
# recipe whose single authored additivity cannot possibly describe all its measures (a share and
# an amount are different additivity classes; BR-2's one-output rule dissolves the conflict).
_NON_ADDITIVE_TOKENS = ("share", "ratio", "rate", "pct", "index", "score", "intensity")
_ADDITIVE_TOKENS = ("amount", "count", "sum", "value", "flow", "total")

# PIT placeholder syntax as authored in the registry: "{window}" etc. A placeholder naming a
# parameter the recipe does not declare is the BR-4 defect class (both known defects —
# merchant_mcc_diversity's {window_min} and maturity_ladder_runoff's {window} — are exactly this).
_PIT_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")

# "The required business concept does not exist", as the registry actually phrases it across
# degrade / eligibility / notes. THIS regex is the check's definition.
_MISSING_CONCEPT = re.compile(
    r"missing|absent|lacks|not (?:yet )?(?:a |in the )?(?:concept|vocabular)"
    r"|no [a-z_ ]*concept|until [a-z_ ]+ concept", re.I)

# Monetary semantics: a flow without a direction authority cannot distinguish inflow from outflow;
# any monetary operand without a currency policy cannot be summed safely across currencies. No
# template declares a currency policy today, so the second counter starts at the full monetary
# population — that is the honest baseline BR-2's policies exist to reduce.
_MONETARY_FLOW_CONCEPTS = frozenset({"monetary_flow"})
_MONETARY_CONCEPTS = frozenset({"monetary_flow", "monetary_stock"})
_DIRECTION_CONCEPTS = frozenset({"debit_credit_indicator"})

# The counters the CI ratchet holds. Everything else in the report is informational.
RATCHETED_COUNTERS: tuple[str, ...] = (
    "multi_measure_recipes",
    "multi_measure_additivity_conflict_recipes",
    "identity_collision_recipes",
    "pit_unmatched_placeholder_recipes",
    "recipes_without_primary_objective",
    "formula_unassessed_recipes",
    "downstream_derivation_recipes",
    "missing_concept_admission_recipes",
    "ambiguous_source_entity_recipes",
    "monetary_flow_without_direction_recipes",
    "monetary_without_currency_policy_recipes",
    "needs_inferred_join_role",
    "needs_inferred_temporal_role",
    "needs_unconstrained_source_grains",
    "legacy_recipes_not_in_v2",
    # BR-3: every V2 recipe's full variant space must mint DISTINCT canonical identities — the
    # 145-recipe collision class, held at zero for the V2 era from the first migrated recipe.
    "v2_variant_identity_collision_recipes",
)


@dataclass(frozen=True, slots=True)
class RecipeAuditReportV1:
    """The audit's whole output: ratcheted debt ``counters``, per-counter ``examples`` (exact
    recipe ids — a count nobody can act on is a number, not a finding), and ``informational``
    context counts that are NOT debt (registry size, combination totals, coverage)."""

    counters: dict = field(default_factory=dict)
    examples: dict = field(default_factory=dict)
    informational: dict = field(default_factory=dict)


def _additivity_classes(values: Iterable) -> set[str]:
    classes: set[str] = set()
    for value in values:
        token = str(value).lower()
        if any(t in token for t in _NON_ADDITIVE_TOKENS):
            classes.add("non_additive")
        elif any(t in token for t in _ADDITIVE_TOKENS):
            classes.add("additive")
        else:
            classes.add("unclassified")
    return classes


def audit_registry(templates: Sequence | None = None,
                   v2_recipe_ids: Iterable[str] | None = None,
                   v2_definitions: Sequence | None = None) -> RecipeAuditReportV1:
    """The pure audit. ``templates`` defaults to the live registry at call time; ``v2_recipe_ids``
    is the RecipeDefinitionV2 population's replaced-legacy ids — a legacy recipe with a V2
    replacement stops counting toward ``legacy_recipes_not_in_v2``, and the default is the REAL
    production registry's replacements (found wired to ``()`` the day BR-11 landed the first
    pack: the counter would have sat at 157 forever while the migration actually proceeded).
    Pass ``()`` explicitly to audit the legacy registry as if no replacement existed.
    ``v2_definitions`` defaults to the production V2 registry and feeds the BR-3
    variant-identity collision check (audit-side enumeration; request paths never enumerate)."""
    from featuregen.overlay.upload.recipe_formula_expectations import (
        RECIPE_FORMULA_EXPECTATIONS,
    )
    from featuregen.overlay.upload.recipe_formula_gold import FORMULA_GOLD_CASES
    from featuregen.overlay.upload.templates import (
        ALL_TEMPLATES,
        SourceEntityRoleResolution,
        _feature_name,
        resolve_source_entity_need_role,
    )

    ts = list(templates if templates is not None else ALL_TEMPLATES)
    if v2_recipe_ids is None:
        from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
        v2_recipe_ids = v2_replaced_legacy_ids()
    v2_ids = set(v2_recipe_ids)
    ex: dict[str, list[str]] = {name: [] for name in RATCHETED_COUNTERS}

    combos_total = 0
    distinct_names_total = 0
    needs_total = 0
    needs_inferred_join = needs_inferred_temporal = needs_no_grains = 0

    for t in ts:
        needs_total += len(t.needs)
        for need in t.needs:
            if need.join_role is None:
                needs_inferred_join += 1
            if need.temporal_role is None:
                needs_inferred_temporal += 1
            if not need.allowed_source_grains:
                needs_no_grains += 1

        measure_values = t.params.get(_MEASURE_PARAM, ())
        if len(measure_values) > 1:
            ex["multi_measure_recipes"].append(t.id)
            if len(_additivity_classes(measure_values)) > 1:
                ex["multi_measure_additivity_conflict_recipes"].append(t.id)

        keys = list(t.params)
        combos = list(product(*[t.params[k] for k in keys])) if keys else [()]
        combos_total += len(combos)
        rendered = {_feature_name(t, dict(zip(keys, c, strict=True))) for c in combos}
        distinct_names_total += len(rendered)
        if len(rendered) < len(combos):
            ex["identity_collision_recipes"].append(t.id)

        placeholders = set(_PIT_PLACEHOLDER.findall(t.pit or ""))
        if placeholders - set(t.params):
            ex["pit_unmatched_placeholder_recipes"].append(t.id)

        if t.primary_objective is None:
            ex["recipes_without_primary_objective"].append(t.id)
        if t.id not in RECIPE_FORMULA_EXPECTATIONS:
            ex["formula_unassessed_recipes"].append(t.id)
        if t.derived:
            ex["downstream_derivation_recipes"].append(t.id)
        prose = " | ".join([t.degrade or "", t.eligibility or "", *t.notes])
        if _MISSING_CONCEPT.search(prose):
            ex["missing_concept_admission_recipes"].append(t.id)
        if resolve_source_entity_need_role(t).resolution is SourceEntityRoleResolution.AMBIGUOUS:
            ex["ambiguous_source_entity_recipes"].append(t.id)

        concepts = {n.concept for n in t.needs}
        if concepts & _MONETARY_FLOW_CONCEPTS and not concepts & _DIRECTION_CONCEPTS:
            ex["monetary_flow_without_direction_recipes"].append(t.id)
        if concepts & _MONETARY_CONCEPTS:
            # No Template field can declare a currency policy today — every monetary recipe is
            # honestly in this counter until BR-2 gives it somewhere to declare one.
            ex["monetary_without_currency_policy_recipes"].append(t.id)

        if t.id not in v2_ids:
            ex["legacy_recipes_not_in_v2"].append(t.id)

    counters = {name: len(ids) for name, ids in ex.items()}
    counters["needs_inferred_join_role"] = needs_inferred_join
    counters["needs_inferred_temporal_role"] = needs_inferred_temporal
    counters["needs_unconstrained_source_grains"] = needs_no_grains
    # need-level counters carry no per-recipe example list; drop their empty placeholders
    for need_counter in ("needs_inferred_join_role", "needs_inferred_temporal_role",
                        "needs_unconstrained_source_grains"):
        ex.pop(need_counter, None)

    if v2_definitions is None:
        from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
        v2_definitions = V2_RECIPES
    from featuregen.overlay.upload.recipe_variants import enumerate_variant_identities
    for definition in v2_definitions:
        identities = enumerate_variant_identities(definition)
        if len(set(identities)) != len(identities):
            ex["v2_variant_identity_collision_recipes"].append(definition.recipe_id)
    counters["v2_variant_identity_collision_recipes"] = len(
        ex["v2_variant_identity_collision_recipes"])

    gold_recipes = {case.recipe_id for case in FORMULA_GOLD_CASES}
    informational = {
        "recipe_count": len(ts),
        "family_count": len({t.family for t in ts}),
        "parameter_combinations": combos_total,
        "distinct_rendered_identities": distinct_names_total,
        "primary_objective_recipes": sum(1 for t in ts if t.primary_objective is not None),
        "formula_v1_authorable_recipes": sum(
            1 for t in ts if t.id in RECIPE_FORMULA_EXPECTATIONS),
        "gold_corpus_cases": len(FORMULA_GOLD_CASES),
        "gold_covered_recipes": sum(1 for t in ts if t.id in gold_recipes),
        "v2_recipe_count": len(v2_ids),
        "needs_total": needs_total,
    }
    return RecipeAuditReportV1(
        counters=counters,
        examples={name: tuple(ids) for name, ids in ex.items()},
        informational=informational)


def compare_to_baseline(report: RecipeAuditReportV1, baseline_counters: dict) -> list[str]:
    """The ratchet: every ratcheted counter must be ≤ its baseline. Returns human-readable
    regressions (empty = pass). A counter MISSING from the baseline is a regression too — silently
    unratcheted debt is how ratchets die. Decreases are permitted and expected."""
    regressions = []
    for name in RATCHETED_COUNTERS:
        current = report.counters.get(name, 0)
        if name not in baseline_counters:
            regressions.append(f"{name}: no baseline recorded (current {current})")
            continue
        if current > baseline_counters[name]:
            fresh = set(report.examples.get(name, ())) - set()
            regressions.append(
                f"{name}: {current} > baseline {baseline_counters[name]}"
                f" (examples: {sorted(fresh)[:5]})")
    return regressions


def strict_violations(report: RecipeAuditReportV1) -> list[str]:
    """Strict mode (BR-17's exit gate, OFF until then): every ratcheted counter must be ZERO."""
    return [f"{name}: {report.counters[name]} != 0"
            for name in RATCHETED_COUNTERS if report.counters.get(name, 0)]


def dynamic_binding_audit(conn, *, catalog_source: str, roles: Iterable[str] = (),
                          templates: Sequence | None = None) -> dict:
    """The catalog-DEPENDENT half: ambiguous-binding disposition against one live catalog.
    Deliberately outside the static ratchet — CI must not depend on loaded catalogs. Counts
    grounded / unbuildable outcomes and, per grounded recipe, whether any binding was a recorded
    tie (``tied_candidate_refs``), with the recipe ids as examples."""
    from featuregen.overlay.upload.templates import ALL_TEMPLATES, ground_all_outcomes

    ts = list(templates if templates is not None else ALL_TEMPLATES)
    outcomes = ground_all_outcomes(conn, ts, catalog_source=catalog_source, roles=roles)
    grounded = [o for o in outcomes if o.feature is not None]
    ambiguous = sorted({
        o.template_id for o in grounded
        if any(r.tied_candidate_refs for r in o.feature.binding_resolutions)})
    return {
        "catalog_source": catalog_source,
        "grounded": len(grounded),
        "unbuildable": len(outcomes) - len(grounded),
        "ambiguous_binding_recipes": tuple(ambiguous),
    }
