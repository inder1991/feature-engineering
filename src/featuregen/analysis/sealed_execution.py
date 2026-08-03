"""Run a SEALED analysis plan — revalidate, rebuild, probe, execute (Release-B Task 9).

The route's ``_execution_inputs_or_none`` returned ``None`` unconditionally: "a preview that never
executes does not satisfy Release B". This is the real path, and it runs FROM THE SEAL rather than
by re-planning.

**Everything the executor needs is already in the sealed plan, except two things.**
``AnalysisExecutionIRV2``'s computation half pins the spine and event table ids and key columns,
the period column, the exact PARTITION VALUES of both windows, the measure, the comparison, the
dimension columns, the eligibility policy field by field, and the attribution or snapshot row rule.
So a replay reads its partitions from the plan rather than recomputing them from a clock — the same
plan run tomorrow reads the same months, which is what makes a replay a replay.

The two exceptions, and why each is not sealed:

* **the physical bindings.** Only the ``table_id`` is in the computation; the binding OBJECT is
  resolved at execution and then checked against the pinned ``pbr_`` revision by
  :func:`~featuregen.analysis.sealed_plan_store.revalidate_sealed_plan`. Sealing the object would
  freeze an address the operator is allowed to move — the pin exists so a move REFUSES, not so it
  is invisible.
* **the join evidence.** It cannot be configured at all (``assembly.py``): a verified join rests on
  measurement against live data, and a relationship someone merely declared is exactly what this
  analysis refuses to trust. So it is probed against the analysis engine immediately before the
  run, beside the spine-uniqueness and dimension-overlap gates that already run there.

**Why the cutoff VALUE is in the computation and the cutoff REF is in the decision.** Both are
right. ``DatasetRowSelectionV1`` refuses a literal date because a decision that re-keyed every day
it was replayed would be a different governed decision each morning. ``AnalysisExecutionIRV1``
carries the value because it is the ENGINE input, exactly as ``LatestSnapshotPolicyV1`` says. So a
sealed V2 is identified by both, and the same question asked at two cutoffs is two plans — which is
the truth about the answers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from featuregen.analysis.engine import AnalysisEngineV1
from featuregen.analysis.execution import ExecutionInputs
from featuregen.analysis.sealed_plan import SealedPlanError
from featuregen.analysis.sealed_plan_store import (
    SealedPlanRecordV1,
    SealedPlanRefusalV1,
    revalidate_sealed_plan,
)
from featuregen.data_agent.analysis import (
    AnalysisExecutionIRV1,
    AnalysisRow,
    Comparison,
    Dimension,
    Period,
    PopulationSpine,
    run_analysis,
)
from featuregen.data_agent.dimensions import (
    AttributionBasis,
    DimensionAttributionPolicyV1,
    MissingValueBehavior,
)
from featuregen.data_agent.eligibility import (
    NullBehavior,
    ReversalMode,
    TransactionEligibilityPolicyV1,
)
from featuregen.data_agent.physical import PhysicalDatasetBindingV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.relationship import RelationshipProbeV1, observe_relationship
from featuregen.data_agent.snapshots import LatestSnapshotPolicyV1, SnapshotScope

#: The need roles the assembler addresses, in the order it reports gaps for them.
_POPULATION = "population"
_EVENT_SOURCE = "event_source"
_DIMENSION_SOURCE = "dimension_source"


@dataclass(frozen=True, slots=True)
class SealedRunV1:
    """One executed sealed plan: its rows, and the provenance that says what produced them."""

    plan_hash: str
    rows: tuple[AnalysisRow, ...]
    record: SealedPlanRecordV1


# ── PLAN TIME: assemble from the live selections ─────────────────────────────────────────────────
#
# TWO ASSEMBLERS, and the asymmetry between them is the point rather than duplication. At PLAN time
# the windows are resolved from a reference instant, because "last completed month" is a question
# about now. At EXECUTION time they are read from the SEAL, because a replay that re-derived them
# would read different months tomorrow and still call itself the same plan.


def _column_of(logical_ref: str) -> str:
    return logical_ref.rsplit(".", 1)[-1].strip()


def _granularity(plan) -> Any:
    """The partition calendar, taken from the plan's own declaration and never guessed.

    ``Window.calendar_unit`` exists precisely so a plan can SAY whether its windows are whole
    months or whole days; inferring it from a value like ``2026-05`` fails silently on a table
    partitioned by something else. ``None`` when the plan does not say, which the caller reports as
    ``PARTITION_CALENDAR_UNKNOWN`` rather than assuming a month.
    """
    from featuregen.analysis.windows import PartitionGranularity

    units = {w.calendar_unit.strip().lower() for w in plan.windows if w.calendar_unit}
    if len(units) != 1:
        return None
    try:
        return PartitionGranularity(units.pop())
    except ValueError:
        return None


def execution_inputs_for_plan(conn, grounded, *, reference,
                              roles: Sequence[str] = ()) -> ExecutionInputs | None:
    """What the executor needs, assembled from the RESOLVED selections. ``None`` when a piece is
    genuinely missing — the route then names the first unmet requirement, as it always has.

    Returns ``None`` while the flag is off, without reading anything, so a flag-off preview is
    byte-identical to the one this release found.
    """
    from featuregen.analysis.windows import WindowResolutionError, resolve_window_partitions
    from featuregen.data_agent.eligibility_store import resolve_eligibility
    from featuregen.overlay.upload.object_ref import parse_ref
    from featuregen.overlay.upload.temporal_policy import TemporalSelectionKind
    from featuregen.overlay.upload.temporal_policy_store import current_temporal_policy
    from featuregen.overlay.upload.temporal_resolver import (
        attribution_policy_for,
        snapshot_policy_for,
    )

    selections = getattr(grounded, "selections", None)
    if selections is None or not selections.resolved:
        return None
    plan = grounded.plan
    by_role = {s.need.need_role.value: s for s in selections.source_selections}
    if _POPULATION not in by_role or _EVENT_SOURCE not in by_role:
        return None

    def _bind(dataset_ref: str) -> PhysicalDatasetBindingV1 | None:
        from featuregen.data_agent.binding_store import resolve_table
        from featuregen.data_agent.connection import ConnectionError_

        source, _schema, table, _column = parse_ref(dataset_ref)
        try:
            resolved = resolve_table(conn, catalog_source=source, table=table)
        except ConnectionError_:
            return None
        return None if resolved is None else resolved[0]

    spine_binding = _bind(by_role[_POPULATION].selected_dataset_ref)
    event_ref = by_role[_EVENT_SOURCE].selected_dataset_ref
    event_binding = _bind(event_ref)
    if spine_binding is None or event_binding is None:
        return None

    event_source, _schema, event_table, _column = parse_ref(event_ref)
    stored = resolve_eligibility(conn, catalog_source=event_source, table=event_table)
    if stored is None:
        return None

    granularity = _granularity(plan)
    if granularity is None or not plan.windows:
        return None
    lag = plan.windows[0].availability_lag_hours
    try:
        partitions = resolve_window_partitions(
            plan.windows, granularity=granularity, reference=reference, lag_hours=lag)
    except WindowResolutionError:
        return None

    dimension_binding = attribution = snapshot = None
    if plan.dimensions:
        row = selections.row_selections[0] if selections.row_selections else None
        dimension = by_role.get(_DIMENSION_SOURCE)
        if row is None or dimension is None:
            return None
        dimension_binding = _bind(dimension.selected_dataset_ref)
        found = current_temporal_policy(conn, row.dataset_logical_ref)
        if dimension_binding is None or found is None:
            return None
        # The cutoff VALUE, derived from the same effective instant the windows were measured back
        # from — `_ground`'s rule, made operational: "the instant its windows are measured back from
        # IS the report cutoff the dimension row rule reads". Never a separate clock read, or the
        # segment could be attributed at an instant the periods do not correspond to.
        from featuregen.analysis.windows import effective_cutoff

        cutoff_value = effective_cutoff(reference, lag_hours=lag).strftime("%Y-%m-%d")
        kind = row.selection_kind
        if kind is TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF:
            snapshot = snapshot_policy_for(found[0], cutoff_value=cutoff_value,
                                           key_column=_column_of(plan.population_key_ref))
        else:
            attribution = attribution_policy_for(found[0], kind=kind, cutoff_value=cutoff_value)

    inputs = ExecutionInputs(
        spine_binding=spine_binding,
        spine_key_column=_column_of(plan.population_key_ref),
        event_binding=event_binding,
        event_key_column=_column_of(plan.entity_ref),
        period_column=_column_of(plan.windows[0].anchor_ref),
        window_partitions=partitions,
        eligibility=stored.policy,
        dimension_binding=dimension_binding,
        attribution=attribution,
        snapshot_selection=snapshot)
    return inputs


# ── EXECUTION TIME: assemble from the seal ───────────────────────────────────────────────────────


def _binding_for(conn, dataset_ref: str, *, expected_table_id: str) -> PhysicalDatasetBindingV1:
    from featuregen.data_agent.binding_store import resolve_table
    from featuregen.overlay.upload.object_ref import parse_ref

    source, _schema, table, _column = parse_ref(dataset_ref)
    resolved = resolve_table(conn, catalog_source=source, table=table)
    if resolved is None:
        raise SealedPlanError(
            f"{dataset_ref!r} can no longer be addressed, so the sealed plan cannot be executed")
    binding = resolved[0]
    if binding.identity.table_id != expected_table_id:
        # Revalidation compares the `pbr_` revision and would normally have caught this first; the
        # check stays because the identity payload below is compared byte for byte, and a mismatch
        # here produces a far clearer message than "the rebuilt plan is not the sealed plan".
        raise SealedPlanError(
            f"{dataset_ref!r} now addresses {binding.identity.table_id!r}, not the "
            f"{expected_table_id!r} this plan was sealed against")
    return binding


def _sources_by_role(record: SealedPlanRecordV1) -> Mapping[str, Any]:
    return {source.need_role: source for source in record.sources}


def execution_inputs_from_sealed(conn, record: SealedPlanRecordV1) -> ExecutionInputs:
    """The executor's inputs, assembled from the SEAL and the current physical addresses.

    ``join_evidence`` is left empty here on purpose: it is measurement, not configuration, and it
    is attached by :func:`run_sealed_plan` from a probe against the analysis engine.
    """
    computation = record.computation
    by_role = _sources_by_role(record)
    for role in (_POPULATION, _EVENT_SOURCE):
        if role not in by_role:
            raise SealedPlanError(
                f"the sealed plan has no {role} decision; it cannot have been resolved")

    spine = computation["spine"]
    event = computation["event"]
    dimension_table_id = computation.get("dimension_table_id")
    dimension_binding = None
    if dimension_table_id:
        dimension = by_role.get(_DIMENSION_SOURCE)
        if dimension is None:
            raise SealedPlanError(
                "the sealed plan reads a dimension table but pinned no dimension source decision")
        dimension_binding = _binding_for(conn, dimension.dataset_ref,
                                         expected_table_id=dimension_table_id)

    # EVERY vocabulary is rebuilt as its ENUM, never left as the string JSON round-tripped it to.
    # Both engines branch with `is` (`snapshots.py`, `dimensions.py:120`, `eligibility.py:104`), and
    # a `str` that merely COMPARES equal fails every one of those tests silently — the plan would
    # compile to a different statement while hashing identically.
    e = computation["eligibility"]
    eligibility = TransactionEligibilityPolicyV1(
        status_column=e["status_column"],
        included_status_values=tuple(e["included_status_values"]),
        reversal_mode=ReversalMode(e["reversal_mode"]),
        reversal_column=e["reversal_column"],
        non_reversed_values=tuple(e["non_reversed_values"]),
        null_behavior=NullBehavior(e["null_behavior"]))

    attribution = None
    if computation.get("attribution") is not None:
        a = computation["attribution"]
        attribution = DimensionAttributionPolicyV1(
            attribution_basis=AttributionBasis(a["attribution_basis"]),
            effective_from_column=a["effective_from_column"],
            effective_to_column=a["effective_to_column"],
            report_cutoff=a["report_cutoff"],
            missing_value_behavior=MissingValueBehavior(a["missing_value_behavior"]),
            current_flag_column=a["current_flag_column"])

    return ExecutionInputs(
        spine_binding=_binding_for(conn, by_role[_POPULATION].dataset_ref,
                                   expected_table_id=spine["table_id"]),
        spine_key_column=spine["key_column"],
        event_binding=_binding_for(conn, by_role[_EVENT_SOURCE].dataset_ref,
                                   expected_table_id=event["table_id"]),
        event_key_column=event["key_column"],
        period_column=event["period_column"],
        # THE SEALED PARTITIONS, not a recomputation from the clock. A replay that re-derived its
        # windows from `now` would read different months tomorrow and still call itself the same
        # plan, which is the one thing a plan identity exists to prevent.
        window_partitions={label: tuple(values)
                           for label, values in computation["periods"].items()},
        eligibility=eligibility,
        dimension_binding=dimension_binding,
        attribution=attribution)


def _snapshot_selection(computation: Mapping[str, Any]) -> LatestSnapshotPolicyV1 | None:
    payload = computation.get("snapshot_selection")
    if payload is None:
        return None
    return LatestSnapshotPolicyV1(
        snapshot_column=payload["snapshot_column"], cutoff=payload["cutoff"],
        scope=SnapshotScope(payload["scope"]), key_column=payload["key_column"],
        tie_break_columns=tuple(payload["tie_break_columns"]),
        missing_value_behavior=MissingValueBehavior(payload["missing_value_behavior"]))


def rebuild_execution_ir(record: SealedPlanRecordV1,
                         inputs: ExecutionInputs) -> AnalysisExecutionIRV1:
    """The V1 the executor runs, rebuilt from the sealed computation and verified against it.

    The verification is the point: the rebuilt IR's own ``identity_payload()`` must equal the bytes
    that were sealed. If it does not, this build reconstructs a DIFFERENT computation from the same
    record — and the honest response is to refuse, not to run something that would be reported
    under the sealed plan's identity.
    """
    computation = record.computation
    ir = AnalysisExecutionIRV1(
        question="",
        spine=PopulationSpine(binding=inputs.spine_binding,
                              key_column=inputs.spine_key_column),
        event_binding=inputs.event_binding,
        event_key_column=inputs.event_key_column,
        period_column=inputs.period_column,
        current=Period(label="current", values=tuple(computation["periods"]["current"])),
        previous=Period(label="previous", values=tuple(computation["periods"]["previous"])),
        measure=computation["measure"],
        comparison=Comparison(computation["comparison"]),
        dimensions=tuple(Dimension(column=c) for c in computation["dimensions"]),
        eligibility=inputs.eligibility,
        dimension_binding=inputs.dimension_binding,
        attribution=inputs.attribution,
        snapshot_selection=_snapshot_selection(computation),
        bridge_realization_dependencies=tuple(
            (d["realization_revision_id"], d["dependency_snapshot_id"])
            for d in computation.get("bridge_realization_dependencies") or ()))
    if ir.identity_payload() != dict(computation):
        raise SealedPlanError(
            "the plan rebuilt from this seal does not reproduce the computation that was sealed; "
            "running it would report one plan's answer under another plan's identity")
    return ir


def _probe_join(engine: AnalysisEngineV1, ir: AnalysisExecutionIRV1) -> AnalysisExecutionIRV1:
    """Measure the join immediately before running it, and attach the evidence.

    EXACT, not sampled: a sample may DISPROVE uniqueness and can never establish it, and the spine
    side is the one that has to be a key — a duplicate there multiplies the whole population. The
    probe runs on the ANALYSIS ENGINE, next to the spine-uniqueness and overlap gates, because it
    is a question about the data rather than about the plan.
    """
    evidence = observe_relationship(
        engine.conn,
        RelationshipProbeV1(
            left_binding=ir.event_binding, left_column=ir.event_key_column,
            right_binding=ir.spine.binding, right_column=ir.spine.key_column,
            policy=ProfilePolicyV1(exact_distinct=True)),
        dialect=engine.dialect)
    return replace(ir, join_evidence=evidence)


def run_sealed_plan(conn, record: SealedPlanRecordV1, *, engine: AnalysisEngineV1,
                    roles: Sequence[str] = ()) -> SealedRunV1 | SealedPlanRefusalV1:
    """Revalidate, rebuild, probe, execute. A stale plan REFUSES and runs nothing.

    Revalidation happens first and against ``conn`` (the CATALOG), because every pin is catalog
    state; the engine connection is only ever used for the data itself.
    """
    refusal = revalidate_sealed_plan(conn, record, roles=roles)
    if refusal is not None:
        return refusal
    ir = rebuild_execution_ir(record, execution_inputs_from_sealed(conn, record))
    rows = run_analysis(engine.conn, _probe_join(engine, ir), dialect=engine.dialect)
    return SealedRunV1(plan_hash=record.plan_hash, rows=rows, record=record)


__all__ = [
    "SealedRunV1",
    "execution_inputs_from_sealed",
    "rebuild_execution_ir",
    "run_sealed_plan",
]
