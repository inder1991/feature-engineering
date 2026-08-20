"""Step 9 — the narrow pilot: an admitted V3 feature, all the way to a sealed artifact.

**Every stage below already existed and nothing joined them.** §4.1 records the generation
orchestrator as a MISSING contract, and it was: the V1 chain is assembled by hand inside tests, which
proves the pieces fit a test's idea of them and proves nothing about a caller. This is the first
function that runs the whole thing, and it is deliberately the NARROW one — sum and count, one
relation, no policy — because the pilot is the first thing that can be wrong in an interesting way,
and a slice is where that is cheapest to find out.

The chain, and who decides what::

    resolve_output_v2        what the number MEANS: unit, currency, additivity
    compile_ir_v2            what the feature computes, planned against its own read set
    authorize_generation_v2  leakage, then read scope — the two gates, in that order
    contracts_for            what a run of it means: cadence, availability, point-in-time
    group_by_contract_v2     one contract per group, or a refusal
    resolve_physical_type_v3 the published column's type, from the DECLARED policy
    build_group_plan_v2      the packing list
    build_operator_graph_v2  the executable shape
                             ---- AND IT STOPS HERE ----

**▲ WHAT THIS DOES NOT DO, corrected 2026-08-20.** An earlier version of this header listed
`render_calculation_node`, `render_project` and `seal_v2` as part of the chain. They are not. This
function returns the authorization, the group plan, one operator graph per feature and the contract
hash, and returns. It also does NOT call `resolve_executable_output_v2`, `record_bound_formula`,
`record_group_plan` or `evaluate_generate`.

More consequentially: `AdmittedFeatureV2` drops the restored `candidate_output` and
`output_intent`, so what happens below is a re-resolution of a basic output policy from
caller-supplied operand facts — NOT the authored-intent-versus-governed-output reconciliation in
`output_resolution_v2.resolve_executable_output_v2`. A caller must not read a successful return here
as "the output was reconciled against what the author declared".

And rendering could not be reached even if it were called: `render/publish.py`,
`render/nodes_compute.py` and `render/nodes_gate.py` are typed on `FeatureGroupPlanV1` and
`MaterializationContractV1` at every entry point, so a `FeatureGroupPlanV2` raises `TypeError` at
`published_dataset_name`. Widening `render_project`'s outer signature did not change that.

**Refusals are RETURNED and the FIRST one wins.** Every stage answers about the same group, so the
first thing wrong with it is the thing to report: continuing would collect verdicts that are all
consequences of one cause, and the operator would have to work out which came first.

**Nothing here decides anything a stage already decides.** No default cadence, no assumed empty
value, no invented environment. Every argument this function cannot derive is required, because the
one thing an orchestrator must not do is quietly supply the inputs that make the chain succeed.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.formula.output_authority_v2 import (
    InvalidOutputV2,
    OperandFactsV2,
    resolve_output_v2,
)
from featuregen.materialize.admission_v2 import AdmittedFeatureV2
from featuregen.materialize.authorize_generation_v2 import (
    AuthorizedGenerationV2,
    authorize_generation_v2,
)
from featuregen.materialize.boundary_v2 import FeatureGroupPlanV2
from featuregen.materialize.build_operator_graph_v2 import build_operator_graph_v2
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.compile_ir_v2 import compile_ir_v2
from featuregen.materialize.contract import AvailabilityPromiseV1, CadenceDecl
from featuregen.materialize.contract_v2 import contracts_for, group_by_contract_v2
from featuregen.materialize.generation_authorization import GenerationAuthorizationV1
from featuregen.materialize.group_plan import PlannedFeature
from featuregen.materialize.group_plan_v2 import build_group_plan_v2
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.materialize.operator_graph_v2 import OperatorGraphV2
from featuregen.materialize.output_resolution_v2 import (
    IntentMismatchV1,
    resolve_executable_output_v2,
)
from featuregen.materialize.physical_types_v2 import DecimalTypeV2
from featuregen.materialize.resolve_physical_type_v3 import resolve_physical_type_v3
from featuregen.materialize.spine import SpineSpec

__all__ = ["CompiledGenerationV2", "compile_generation_v2"]


@dataclass(frozen=True, slots=True)
class CompiledGenerationV2:
    """One group compiled, authorized, planned and shaped — everything but the rendered bytes.

    Stops short of rendering deliberately. Rendering needs an environment's engine versions and its
    resolved spine requirement, which are facts about a CLUSTER rather than about a compilation, and
    a function that took them would refuse for cluster reasons while claiming to be about a formula.
    The caller renders from this, and :attr:`graphs` is what it seals.
    """

    authorized: AuthorizedGenerationV2
    plan: FeatureGroupPlanV2
    graphs: Mapping[str, OperatorGraphV2]
    contract_hash: str

    def __post_init__(self) -> None:
        planned = {feature.column_name for feature in self.plan.features}
        shaped = set(self.graphs)
        if planned != shaped:
            raise ValueError(
                f"the plan publishes {sorted(planned)} and the graphs describe {sorted(shaped)}: a "
                f"published column with no graph is a column nothing computes, and a graph with no "
                f"column computes something nobody publishes")


def compile_generation_v2(
    conn: DbConn,
    admitted: Sequence[AdmittedFeatureV2],
    *,
    spine: SpineSpec,
    inventory: ClusterInventoryV1,
    authorization: GenerationAuthorizationV1,
    cadence: CadenceDecl,
    availability_promise: AvailabilityPromiseV1,
    physical_type_policy: str,
    empty_values: Mapping[str, str | None],
    operand_facts: Mapping[str, OperandFactsV2],
    logical_group_name: str,
    roles: Sequence[str] = (),
    target_aliases: Sequence[str] = (),
    policy_realization_ids: Mapping[str, str] = (),
) -> CompiledGenerationV2 | MaterializationRefused:
    """Run the whole chain over one build set, or return the FIRST refusal.

    Args:
        admitted: the features this generation was requested for.
        empty_values: what a population key with NO in-window rows publishes, per feature name.
            Required per feature and possibly ``None`` (meaning NULL), because the compiled IR does
            not carry the declared empty-window result and "had no transactions" and "transacted
            zero" are different published answers.
        operand_facts: the governed unit and currency of each operand ref, for output authority.
            An operand absent from it is treated as carrying no governed facts, which is what
            ``resolve_output_v2`` already does — a monetary operand with per-row currency and no
            declared conversion refuses there rather than summing across currencies.
        policy_realization_ids: declared policy ref → the realization that decides it. The narrow
            pilot has none; a feature that declares one refuses here rather than being rendered
            without it.

    Returns:
        :class:`CompiledGenerationV2`, or the first stage's refusal.

    Raises:
        ValueError: the call is assembled wrongly — no features, or an ``empty_values`` mapping that
            does not describe exactly the features supplied. Both are caller errors rather than
            governed verdicts: an orchestrator that filled either in would be choosing what gets
            published.
    """
    features = tuple(admitted)
    if not features:
        raise ValueError(
            "compile_generation_v2 was called with no admitted features: an empty generation "
            "produces an artifact that publishes nothing, and every check below would pass over it")
    names = {feature.feature_name for feature in features}
    if set(empty_values) != names:
        raise ValueError(
            f"empty_values must describe exactly the features being compiled: missing "
            f"{sorted(names - set(empty_values))}, unexpected {sorted(set(empty_values) - names)}. "
            f"A feature whose empty-window answer was omitted would be published with whichever "
            f"one this function happened to pick")

    # ── 1. COMPILE ──────────────────────────────────────────────────────────────────────────────
    planned = []
    for feature in features:
        # Output authority FIRST, because it is about what the number means and needs no cluster:
        # a feature that cannot say what unit it is in is refused before anything is resolved
        # physically, and for the reason that actually blocks it.
        output = resolve_output_v2(feature.proposal, dict(operand_facts))
        if isinstance(output, InvalidOutputV2):
            return MaterializationRefused(
                CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED,
                f"{feature.feature_name}: {output.code} at {output.path} — {output.detail}")

        # AND THEN RECONCILED against what the author declared. Resolving alone answers "what do the
        # governed facts permit"; it does not answer "does that agree with what the author said this
        # number would be", and an earlier version of this function stopped at the first question
        # while reading like it had answered both.
        #
        # Skipped only when the run carried no intent — a run whose output_status is not `resolved`
        # has nothing to reconcile, and inventing an intent to compare against would manufacture
        # agreement.
        reconciled = _reconcile_output(feature, output)
        if isinstance(reconciled, MaterializationRefused):
            return reconciled
        compiled = compile_ir_v2(
            conn, feature, spine=spine, inventory=inventory, roles=roles,
            output_policy=output, policy_realization_ids=policy_realization_ids)
        if isinstance(compiled, MaterializationRefused):
            return compiled
        planned.append(compiled)

    # ── 2. THE TWO GATES ────────────────────────────────────────────────────────────────────────
    authorized = authorize_generation_v2(
        conn, planned, spine=spine, authorization=authorization,
        target_aliases=target_aliases, roles=roles)
    if isinstance(authorized, MaterializationRefused):
        return authorized

    # ── 3. WHAT A RUN OF IT MEANS, AND THAT THE GROUP AGREES ────────────────────────────────────
    contracts = contracts_for(
        conn, authorized.token.planned, cadence=cadence,
        availability_promise=availability_promise, physical_type_policy=physical_type_policy)
    if isinstance(contracts, MaterializationRefused):
        return contracts
    group = group_by_contract_v2(contracts)
    if isinstance(group, MaterializationRefused):
        return group

    # ── 4. THE PUBLISHED COLUMNS ────────────────────────────────────────────────────────────────
    columns = []
    for member in authorized.token.ordered_planned():
        resolved = _physical_type(member, features)
        if isinstance(resolved, MaterializationRefused):
            return resolved
        columns.append(PlannedFeature(
            column_name=member.ir.feature_name,
            ir_hash=_ir_hash(member),
            physical_type=resolved))
    plan = build_group_plan_v2(
        group, tuple(columns), logical_group_name=logical_group_name,
        physical_type_policy=physical_type_policy)

    # ── 5. THE EXECUTABLE SHAPE ─────────────────────────────────────────────────────────────────
    graphs = {}
    for member in authorized.token.ordered_planned():
        name = member.ir.feature_name
        graph = build_operator_graph_v2(member, empty_value=empty_values[name])
        if isinstance(graph, MaterializationRefused):
            return graph
        graphs[name] = graph

    return CompiledGenerationV2(
        authorized=authorized, plan=plan, graphs=graphs, contract_hash=group.contract_hash)


def _reconcile_output(feature: AdmittedFeatureV2, resolved) -> None | MaterializationRefused:
    """The authored intent against the governed policy — `resolve_executable_output_v2`'s job.

    This is the check `AdmittedFeatureV2` was dropping the inputs for. The author declares a unit
    and a currency; the governed operand facts imply one; the two can disagree, and a feature
    published under a unit its author did not mean is wrong in a way no type check catches.

    Returns ``None`` when there is nothing to reconcile — no carried intent — rather than a pass.
    The distinction matters: "the two agreed" and "there was only one of them" are different
    states, and reporting the second as the first is how an unreconciled output comes to look
    checked.
    """
    intent = feature.output_intent
    if intent is None:
        return None

    mismatch = resolve_executable_output_v2(
        intent,
        resolved,
        formula_content_hash=feature.proposal_content_hash,
        # The arithmetic's own type and nullability. Taken from the DECLARED decimal policy, which
        # is what `resolve_physical_type_v3` publishes — the two must agree or the reconciliation
        # would be against a width nothing emits.
        physical_type=DecimalTypeV2(precision=feature.proposal.decimal.precision,
                                    scale=feature.proposal.decimal.scale),
        currency_code=_currency_code(resolved),
        nullable=True,
    )
    if isinstance(mismatch, IntentMismatchV1):
        return MaterializationRefused(
            CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED,
            f"{feature.feature_name}: the author's output intent and the governed facts disagree "
            f"about {mismatch.field} — intended {mismatch.intended!r}, resolved "
            f"{mismatch.resolved!r} ({mismatch.code}). Publishing either one silently would put a "
            f"number under a meaning somebody did not choose")
    return None


def _currency_code(resolved) -> str:
    """The three-letter code out of a resolved policy's currency field.

    `"fixed:AED"` -> `"AED"`; `"converted:<ref>"` has no code recoverable here — S4 says it comes
    from the realization that performed the conversion — so it is passed through empty rather than
    guessed, and the reconciliation refuses on the field rather than on a code this function made up.
    """
    currency = getattr(resolved, "currency", "") or ""
    return currency.split(":", 1)[1] if currency.startswith("fixed:") else ""


def _physical_type(member, features: Sequence[AdmittedFeatureV2]):
    """The published type for one member, from the proposal it was compiled from.

    The evidence is read off the COMPILED IR rather than gathered again: it is a statement the
    compiler already made about each operand, and a second read could disagree with the one the
    expressions were resolved under.
    """
    proposal = next(
        (feature.proposal for feature in features
         if feature.feature_name == member.ir.feature_name), None)
    if proposal is None:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_HASH_MISMATCH,
            f"{member.ir.feature_name} was authorized and no admitted feature supplied it: the "
            f"column would be typed from a formula this generation was never asked to build")
    return resolve_physical_type_v3(
        proposal,
        operand_types={e.expr_path: e.operand_type for e in member.ir.expressions})


def _ir_hash(member) -> str:
    from featuregen.materialize.boundary_v2 import ir_hash_v2

    return ir_hash_v2(member.ir)
