"""Translate a grounded :class:`AnalysisPlanV1` into an executable :class:`AnalysisExecutionIRV1`.

The two halves of the data agent were built and never joined. `featuregen.analysis` turns a question
into a grounded plan in the catalog's identities; `featuregen.data_agent` compiles an execution IR to
SQL and runs it. `ground_analysis_plan` had no caller in `src/`, and nothing outside
`data_agent/analysis.py` referenced the IR — so a grounded plan could not reach the executor.

**Most of this module refuses.** Three things the executor requires cannot be expressed in
`AnalysisPlanV1`, and inventing a default for any of them would destroy the property it protects:

* **the population spine.** The plan carries one ``base_table_ref``. The IR needs a spine table
  distinct from the event table, because the spine is the LEFT side and the period aggregates hang
  off it — that is what keeps a customer who fell to zero in the answer. Defaulting the spine to the
  event table would produce a well-typed IR that silently drops exactly the rows the question is
  about.
* **eligibility.** The plan cannot say which rows count. Guessing "all of them" counts pending,
  failed and reversed transactions as activity, which changes the answer rather than widening it.
* **partition values.** A :class:`Window` is a length in days; the executor reads named partitions.
  Deriving one from the other needs the partition column's calendar and, when the availability basis
  is ``event_time_plus_lag``, a cutoff shifted back by that lag — otherwise the window includes rows
  that had not landed yet. That is a time-semantics decision, not an arithmetic one.

So :class:`ExecutionInputs` carries them explicitly and each absence is a named refusal. Every
refusal maps into the learning vocabulary, so a question blocked here lands in the queue a human
works instead of surfacing as a stack trace — and three gap codes that had no producer at all
(``POPULATION_UNRESOLVED``, ``PHYSICAL_BINDING_MISSING``, ``POINT_IN_TIME_RULE_MISSING``) finally
have one.

**Findings never block.** ``plan.py``'s central rule is that a finding travels with the answer and a
refusal is only for a plan that cannot be expressed. A bridge that refused on findings would turn
every disclosure into a dead end on a young catalog, where findings are the expected state.
"""

from __future__ import annotations

from dataclasses import dataclass

from featuregen.analysis.plan import AnalysisPlanV1, GroundedPlan
from featuregen.data_agent.analysis import (
    AnalysisExecutionIRV1,
    Comparison,
    Period,
    PopulationSpine,
)
from featuregen.data_agent.analysis import Dimension as IRDimension
from featuregen.data_agent.dimensions import DimensionAttributionPolicyV1
from featuregen.data_agent.eligibility import TransactionEligibilityPolicyV1
from featuregen.data_agent.learning import RequiredAction
from featuregen.data_agent.physical import PhysicalDatasetBindingV1
from featuregen.data_agent.relationship import RelationshipEvidenceV1
from featuregen.overlay.upload.bridge_realization import eligible_for_production
from featuregen.overlay.upload.bridge_store import CurrentBridgeRealizationV1


class BridgeRefusal(ValueError):
    """The plan cannot be turned into something executable. Carries the subject so the refusal names
    what a human would fix, not merely that translation failed."""

    def __init__(self, code: str, message: str, *, subject: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.subject = subject


#: Each refusal, as the ontology gap it represents and the action that would clear it. Closed, and
#: checked against ``GAP_CODES`` by test, so a refusal can never be introduced without deciding what
#: a human would do about it.
BRIDGE_REFUSAL_TO_GAP: dict[str, tuple[str, RequiredAction]] = {
    "SPINE_SAME_AS_EVENTS": ("POPULATION_UNRESOLVED", RequiredAction.CONFIRM_POPULATION),
    "SPINE_BINDING_ABSENT": ("PHYSICAL_BINDING_MISSING", RequiredAction.BIND_PHYSICAL_SOURCE),
    "ELIGIBILITY_ABSENT": ("MEASURE_DEFINITION_UNRESOLVED",
                           RequiredAction.CONFIRM_BUSINESS_POLICY),
    "WINDOW_NOT_PARTITION_ALIGNED": ("POINT_IN_TIME_RULE_MISSING",
                                     RequiredAction.CONFIRM_TIME_SEMANTICS),
    "MEASURE_UNSUPPORTED": ("MEASURE_DEFINITION_UNRESOLVED",
                            RequiredAction.CONFIRM_BUSINESS_POLICY),
    "ATTRIBUTION_ABSENT": ("DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED",
                           RequiredAction.CONFIRM_BUSINESS_POLICY),
    "JOIN_REALIZATION_ABSENT": ("JOIN_CARDINALITY_UNKNOWN",
                                RequiredAction.PROFILE_DATA),
}

#: The plan's comparison words, in the executor's vocabulary. An unknown word is refused rather than
#: defaulted: silently treating "change" as "decrease" would answer a different question.
_COMPARISONS: dict[str, Comparison] = {
    "decrease": Comparison.DECREASED,
    "increase": Comparison.INCREASED,
    "change": Comparison.CHANGED,
}


@dataclass(frozen=True, slots=True)
class ExecutionInputs:
    """What the plan contract cannot carry, supplied by the caller rather than guessed here.

    This type exists to make the missing pieces enumerable. A caller that cannot fill one gets a
    refusal naming it, which is the honest failure — as opposed to a default that produces a number.
    """

    spine_binding: PhysicalDatasetBindingV1 | None
    spine_key_column: str
    event_binding: PhysicalDatasetBindingV1 | None
    event_key_column: str
    period_column: str
    #: Window label -> the exact partition values that window covers.
    window_partitions: dict[str, tuple[str, ...]]
    eligibility: TransactionEligibilityPolicyV1 | None = None
    dimension_binding: PhysicalDatasetBindingV1 | None = None
    attribution: DimensionAttributionPolicyV1 | None = None
    join_evidence: RelationshipEvidenceV1 | None = None
    bridge_realizations: tuple[CurrentBridgeRealizationV1, ...] = ()


def _column_of(logical_ref: str) -> str:
    """The bare column name from a catalog logical ref (``source::schema.table.column``).

    SQL needs the identifier; passing the ref through would be refused by ``require_identifier``,
    since ``::`` and dots are not identifier characters. Taking the last dotted segment keeps this a
    pure string operation with no catalog lookup — the ref's shape is the contract.
    """
    return logical_ref.rsplit(".", 1)[-1].strip()


def plan_to_execution_ir(grounded: GroundedPlan,
                         inputs: ExecutionInputs) -> AnalysisExecutionIRV1:
    """A grounded plan plus the executor's un-plannable inputs, as an executable IR.

    Refuses rather than defaults. See the module docstring for why each refusal cannot be a default.
    """
    plan: AnalysisPlanV1 = grounded.plan

    if not grounded.answerable:
        why = ", ".join(code for code, _ in grounded.refusals) or "no reason recorded"
        raise BridgeRefusal(
            "PLAN_NOT_ANSWERABLE",
            f"grounding refused this plan ({why}); it could not be expressed against the catalog, "
            "so executing it would answer a question the catalog disowned",
            subject=plan.base_table_ref)

    if plan.measure.op != "count":
        raise BridgeRefusal(
            "MEASURE_UNSUPPORTED",
            f"measure op {plan.measure.op!r} is not supported by this executor slice, which counts "
            "rows per entity",
            subject=plan.measure.logical_ref or plan.base_table_ref)

    comparison = _COMPARISONS.get((plan.comparison or "").strip().lower())
    if comparison is None:
        raise BridgeRefusal(
            "COMPARISON_UNSUPPORTED",
            f"comparison {plan.comparison!r} is not one of {sorted(_COMPARISONS)}",
            subject=plan.base_table_ref)

    if len(plan.windows) != 2:
        raise BridgeRefusal(
            "COMPARISON_NEEDS_TWO_WINDOWS",
            f"a period-over-period comparison needs exactly two windows, got {len(plan.windows)}",
            subject=plan.base_table_ref)

    if inputs.spine_binding is None:
        raise BridgeRefusal(
            "SPINE_BINDING_ABSENT",
            f"no physical binding for the population of {plan.entity!r}",
            subject=plan.entity_ref)

    if inputs.event_binding is None:
        raise BridgeRefusal(
            "SPINE_BINDING_ABSENT", "no physical binding for the event table",
            subject=plan.base_table_ref)

    spine_id = inputs.spine_binding.identity.table_id
    if plan.population_table_ref:
        # The caller resolved a binding; this checks it is the one the human DECLARED. Without the
        # check the declaration is decorative — a caller could pass any table and the audit trail
        # would still show a population that was chosen by a person.
        declared = plan.population_table_ref.rsplit(".", 1)[-1].rsplit("::", 1)[-1].lower()
        if declared and declared != inputs.spine_binding.identity.table.lower():
            raise BridgeRefusal(
                "SPINE_NOT_THE_DECLARED_POPULATION",
                f"the declared population is {plan.population_table_ref!r} but the supplied spine "
                f"binding is {spine_id!r}; a population that was declared and then substituted is "
                "worse than one never declared, because the audit trail says a person chose it",
                subject=plan.population_table_ref)
    if spine_id == inputs.event_binding.identity.table_id:
        raise BridgeRefusal(
            "SPINE_SAME_AS_EVENTS",
            "the population spine and the event table are the same table. The spine is the LEFT "
            "side of the query, so collapsing them removes every entity with no events in a period "
            f"— for a {plan.comparison!r} question that is exactly the population being asked about",
            subject=spine_id)

    if inputs.eligibility is None:
        raise BridgeRefusal(
            "ELIGIBILITY_ABSENT",
            "the plan does not say which rows count, and defaulting to all of them counts pending, "
            "failed and reversed activity as activity",
            subject=plan.base_table_ref)

    realization_dependencies: list[tuple[str, str]] = []
    for fact_key in plan.join_refs:
        matches = tuple(
            realization
            for realization in inputs.bridge_realizations
            if realization.revision.bridge_fact_key == fact_key
            and eligible_for_production(realization.revision, realization.current)
        )
        if len(matches) != 1:
            raise BridgeRefusal(
                "JOIN_REALIZATION_ABSENT",
                f"identifier link {fact_key!r} has {len(matches)} exact current deterministic "
                "directional realizations; production analysis requires exactly one and never "
                "infers direction or cardinality from the symmetric link",
                subject=fact_key,
            )
        realization_dependencies.append((
            matches[0].revision.realization_revision_id,
            matches[0].revision.dependency_snapshot_id,
        ))

    # The plan orders windows most-recent-last by convention only, so the CURRENT window is chosen by
    # the smallest offset rather than by position — a reordered plan must not swap the two periods and
    # invert the answer.
    ordered = sorted(plan.windows, key=lambda w: w.offset_days)
    current_window, previous_window = ordered[0], ordered[-1]

    periods: dict[str, Period] = {}
    for label, window in (("current", current_window), ("previous", previous_window)):
        values = inputs.window_partitions.get(window.label)
        if not values:
            raise BridgeRefusal(
                "WINDOW_NOT_PARTITION_ALIGNED",
                f"no partition values for window {window.label!r} ({window.length_days}d at offset "
                f"{window.offset_days}d on {window.anchor_ref}): a length in days cannot become a "
                "set of partitions without the column's calendar, and an "
                "`event_time_plus_lag` basis also needs the cutoff shifted back by the lag",
                subject=window.anchor_ref)
        periods[label] = Period(label=label, values=tuple(values))

    dimensions = tuple(IRDimension(column=_column_of(d.logical_ref)) for d in plan.dimensions)
    if dimensions and inputs.attribution is None:
        raise BridgeRefusal(
            "ATTRIBUTION_ABSENT",
            "dimensions were asked for with no attribution policy: nothing says whether a customer "
            "is classified as of the cutoff, per period, or as they are today",
            subject=", ".join(d.logical_ref for d in plan.dimensions))

    return AnalysisExecutionIRV1(
        question=plan.question,
        spine=PopulationSpine(binding=inputs.spine_binding, key_column=inputs.spine_key_column),
        event_binding=inputs.event_binding,
        event_key_column=inputs.event_key_column,
        period_column=inputs.period_column,
        current=periods["current"],
        previous=periods["previous"],
        measure="count",
        comparison=comparison,
        dimensions=dimensions,
        eligibility=inputs.eligibility,
        dimension_binding=inputs.dimension_binding,
        attribution=inputs.attribution,
        join_evidence=inputs.join_evidence,
        bridge_realization_dependencies=tuple(realization_dependencies),
    )
