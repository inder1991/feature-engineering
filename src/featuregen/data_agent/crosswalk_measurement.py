"""The crosswalk MEASUREMENT pass — governed row policy first, then the three-table probe.

The order is the whole point. A mapping table that keeps history has many rows per identifier, and
its uniqueness measured over that history is a statement about the history. So:

1. the mapping dataset's rows go through the RELEASE-B temporal machinery
   (``temporal_resolver.resolve_row_selection``) BEFORE anything is counted;
2. the resolved row rule becomes the ``mapping_rows`` CTE of one bounded statement
   (``crosswalk_probe``), so every count downstream — mapping uniqueness, both legs' coverage, the
   composed fan-out — is measured over the filtered rows by construction, not by convention;
3. the result is shaped into one :class:`CrosswalkExecutionObservationV1` with the full pins.

**No policy is not a silent pass.** Where no governed policy exists the measurement still runs, over
UNFILTERED rows, and records :attr:`CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT`.
Where a policy exists but the resolver refuses it for this request (typically: it reads rows AS OF an
instant and the caller named no cutoff), the caveat is ``MAPPING_TEMPORAL_POLICY_UNRESOLVED`` — two
different situations with two different fixes, and neither is ever reported as "current".

**A sampled run may DISPROVE uniqueness and never establish it.** The rule lives in the observation
contract and is read from there; this module never re-derives it.

**Failures are typed coverage, not exceptions** — the ``DirectSqlExecutor`` discipline: a probe that
could not run produces a ``complete=False`` measurement with a redacted reason, which is storable
evidence that the question was asked and could not be answered.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from featuregen.data_agent.crosswalk_probe import (
    CROSSWALK_PROBE_METRIC_COUNT,
    CrosswalkProbePlanV1,
    ProbePredicateKind,
    ProbePredicateV1,
    ProbeSide,
    render_crosswalk_probe_sql,
    render_crosswalk_probe_tail_sql,
)
from featuregen.data_agent.relationship_observation import (
    RelationshipMethod,
    RowCoverage,
    utc_now,
)
from featuregen.overlay.upload.crosswalk_observation import (
    MAPPING_TO_TARGET,
    SOURCE_TO_MAPPING,
    CrosswalkExecutionObservationV1,
    CrosswalkLegObservationV1,
    CrosswalkObservationCaveat,
    MappingTupleObservationV1,
    ObservedPartitionPinV1,
    ObservedPredicatePinV1,
    compose_crosswalk_observation,
)

logger = logging.getLogger(__name__)

#: The resolver's refusal codes that mean "nobody has declared how this dataset stores history",
#: as distinct from "a declaration exists and could not be applied to THIS request".
_ABSENT_MISSING_KINDS = frozenset({"temporal_policy"})


@dataclass(frozen=True, slots=True)
class MappingRowRuleV1:
    """What the governed temporal machinery decided for the mapping dataset, as measurable pins."""

    predicates: tuple[ProbePredicateV1, ...]
    pins: tuple[ObservedPredicatePinV1, ...]
    temporal_policy_revision_id: str | None
    row_selection_hash: str | None
    caveat: str | None

    @property
    def filtered(self) -> bool:
        return bool(self.predicates)


def resolve_mapping_row_rule(
    conn,
    *,
    mapping_dataset_ref: str,
    dataset_profile_hash: str,
    cutoff_value_ref: str | None = None,
    cutoff_value: str | None = None,
    roles: tuple[str, ...] = (),
) -> MappingRowRuleV1:
    """Ask the Release-B resolver which mapping rows are current, and translate its answer to pins.

    The resolved decision keeps the cutoff's REF (so two replays of one decision hash alike); only
    this adapter — the engine boundary — takes the VALUE. A kind that needs a cutoff and has no
    bound value is treated as UNRESOLVED rather than quietly dropped, because a temporal predicate
    silently omitted is the difference between "the current mapping" and "every row that ever was".
    """
    from featuregen.overlay.upload.temporal_resolver import (
        RequestTemporality,
        resolve_row_selection,
    )

    outcome = resolve_row_selection(
        conn, dataset_logical_ref=mapping_dataset_ref,
        dataset_profile_hash=dataset_profile_hash,
        temporality=RequestTemporality.CURRENT,
        cutoff_value_ref=cutoff_value_ref, roles=roles)
    if outcome.refusal is not None:
        missing = getattr(outcome.refusal, "missing", None)
        caveat = (CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value
                  if missing in _ABSENT_MISSING_KINDS or outcome.policy is None
                  else CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value)
        return MappingRowRuleV1((), (), None, None, caveat)

    selection = outcome.row_selection
    assert selection is not None
    predicates: list[ProbePredicateV1] = []
    pins: list[ObservedPredicatePinV1] = []
    for index, payload in enumerate(selection.predicate_payloads):
        kind = str(payload["kind"])
        column_ref = str(payload["column_ref"])
        column = column_ref.rpartition(".")[2]
        operator = str(payload["operator"])
        parameter_ref = payload.get("parameter_ref")
        if kind == ProbePredicateKind.CURRENT_FLAG.value:
            probe = ProbePredicateV1(
                predicate_id=f"mapping_row_rule_{index}", side=ProbeSide.MAPPING,
                kind=ProbePredicateKind.CURRENT_FLAG, column=column, operator="is_true")
        else:
            if parameter_ref is not None and not cutoff_value:
                # The rule needs an instant the caller did not bind. Refusing to render it is the
                # only honest option: rendering it without a value is not possible, and dropping it
                # would measure "whatever is there now" while reporting a governed row rule.
                return MappingRowRuleV1(
                    (), (), selection.temporal_policy_revision_id, None,
                    CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value)
            probe = ProbePredicateV1(
                predicate_id=f"mapping_row_rule_{index}", side=ProbeSide.MAPPING,
                kind=ProbePredicateKind(kind), column=column, operator=operator,
                values=(str(cutoff_value),), parameter_ref=parameter_ref)
        predicates.append(probe)
        pins.append(ObservedPredicatePinV1(
            predicate_id=probe.predicate_id, dataset_ref=mapping_dataset_ref, column=column,
            operator=operator, values=probe.values, parameter_ref=parameter_ref))
    return MappingRowRuleV1(
        tuple(predicates), tuple(pins), selection.temporal_policy_revision_id,
        selection.content_hash, None)


@dataclass(frozen=True, slots=True)
class CrosswalkMeasurementV1:
    """One measurement pass: the composed observation, plus what governed the mapping rows."""

    observation: CrosswalkExecutionObservationV1
    row_rule: MappingRowRuleV1
    plan_hash: str


def measure_crosswalk(
    conn,
    *,
    dialect,
    definition_revision_id: str,
    plan: CrosswalkProbePlanV1,
    row_rule: MappingRowRuleV1,
    source_leg_kind: str,
    target_leg_kind: str,
    source_snapshot_id: str,
    mapping_snapshot_id: str,
    target_snapshot_id: str,
    observed_at: datetime | None = None,
    source_leg_v2: tuple[str, str] | None = None,
    target_leg_v2: tuple[str, str] | None = None,
) -> CrosswalkMeasurementV1:
    """Run the three-table probe and shape one composed observation.

    ``source_leg_v2`` / ``target_leg_v2`` are ``(observation_revision_id, realization_revision_id)``
    for a leg ALSO written to the shipped two-endpoint store. A caller may only pass them for a leg
    whose mapping-side filter is empty: with a filter applied, the pair probe measured a different
    question, and claiming its row here would be the "measure uniqueness before time filtering"
    mutation arriving by cross-reference. Enforced, not documented.
    """
    if row_rule.filtered and (source_leg_v2 or target_leg_v2):
        raise ValueError(
            "a two-endpoint observation cannot be claimed for a leg whose mapping rows were "
            "temporally filtered: the pair probe has no temporal predicate vocabulary "
            "(RelationshipFixedPredicateV1 is equality-to-literal only), so it measured the "
            "unfiltered mapping — a different question wearing the same leg's name")

    timestamp = observed_at or utc_now()
    method = _effective_method(dialect, plan)
    statement = render_crosswalk_probe_sql(plan, dialect=dialect)
    tail = render_crosswalk_probe_tail_sql(plan, dialect=dialect)
    try:
        head_row = _run(conn, plan, statement, dialect)
        tail_row = _run(conn, plan, tail, dialect)
    except Exception as exc:                # noqa: BLE001 — engine failures are typed coverage
        reason = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
        logger.info("crosswalk probe failed for %s: %s", definition_revision_id, reason)
        return CrosswalkMeasurementV1(
            observation=_failed(plan, definition_revision_id, row_rule, method, timestamp,
                                reason, source_leg_kind, target_leg_kind, source_snapshot_id,
                                mapping_snapshot_id, target_snapshot_id),
            row_rule=row_rule, plan_hash=plan.plan_hash)

    if len(head_row) != CROSSWALK_PROBE_METRIC_COUNT:
        raise AssertionError(
            f"crosswalk probe returned {len(head_row)} metrics, "
            f"expected {CROSSWALK_PROBE_METRIC_COUNT}")
    (map_rows, map_non_null, map_src_distinct, map_src_dupes, map_src_max,
     map_tgt_distinct, map_tgt_dupes, map_tgt_max, map_pairs,
     src_rows, src_non_null, src_distinct, src_dupes, src_max,
     tgt_rows, tgt_non_null, tgt_distinct, tgt_dupes, tgt_max,
     leg1_joined, leg1_max_mapping, leg1_orphan_rows,
     leg2_joined, leg2_max_mapping, leg2_orphan_rows,
     composed_rows, source_max_reach) = (int(v) for v in head_row)
    (matched_source, matched_target, target_max_reach,
     leg1_orphan_distinct, leg2_orphan_distinct) = (int(v) for v in tail_row)

    mapping_pins = _partition_pins(plan.mapping_binding, plan.mapping_partitions)
    mapping = MappingTupleObservationV1(
        physical_id=plan.mapping_binding.identity.table_id,
        binding_revision_id=plan.mapping_binding.binding_revision_id,
        binding_content_hash=plan.mapping_binding.content_hash,
        source_columns=plan.mapping_source_columns,
        target_columns=plan.mapping_target_columns,
        row_count=map_rows, non_null_row_count=map_non_null,
        distinct_source_tuple_count=map_src_distinct,
        distinct_target_tuple_count=map_tgt_distinct,
        distinct_pair_count=map_pairs,
        duplicate_source_tuple_count=map_src_dupes,
        duplicate_target_tuple_count=map_tgt_dupes,
        max_rows_per_source_tuple=map_src_max,
        max_rows_per_target_tuple=map_tgt_max,
        partitions_read=mapping_pins)

    source_leg = CrosswalkLegObservationV1(
        leg=SOURCE_TO_MAPPING, leg_kind=source_leg_kind,
        endpoint_dataset_ref=plan.source_binding.identity.table_id,
        endpoint_binding_revision_id=plan.source_binding.binding_revision_id,
        endpoint_columns=plan.source_columns, mapping_columns=plan.mapping_source_columns,
        endpoint_row_count=src_rows, endpoint_non_null_row_count=src_non_null,
        endpoint_distinct_tuple_count=src_distinct, endpoint_duplicate_tuple_count=src_dupes,
        endpoint_max_rows_per_tuple=src_max,
        matched_endpoint_distinct=max(0, src_distinct - leg1_orphan_distinct),
        unmatched_endpoint_distinct=leg1_orphan_distinct,
        endpoint_orphan_rows=leg1_orphan_rows, joined_row_count=leg1_joined,
        max_mapping_matches_per_endpoint_row=leg1_max_mapping,
        max_endpoint_matches_per_mapping_row=src_max,
        normalization_ids=("identity_v1",) * len(plan.source_columns),
        endpoint_source_snapshot_id=source_snapshot_id,
        mapping_source_snapshot_id=mapping_snapshot_id,
        as_of=plan.snapshot_or_as_of, execution_principal=plan.execution_principal,
        method=method, row_coverage=RowCoverage.FULL, complete=True,
        partitions_read=_partition_pins(plan.source_binding, plan.source_partitions),
        predicates=_side_pins(plan, ProbeSide.SOURCE),
        v2_observation_revision_id=source_leg_v2[0] if source_leg_v2 else None,
        realization_revision_id=source_leg_v2[1] if source_leg_v2 else None)

    target_leg = CrosswalkLegObservationV1(
        leg=MAPPING_TO_TARGET, leg_kind=target_leg_kind,
        endpoint_dataset_ref=plan.target_binding.identity.table_id,
        endpoint_binding_revision_id=plan.target_binding.binding_revision_id,
        endpoint_columns=plan.target_columns, mapping_columns=plan.mapping_target_columns,
        endpoint_row_count=tgt_rows, endpoint_non_null_row_count=tgt_non_null,
        endpoint_distinct_tuple_count=tgt_distinct, endpoint_duplicate_tuple_count=tgt_dupes,
        endpoint_max_rows_per_tuple=tgt_max,
        matched_endpoint_distinct=max(0, tgt_distinct - leg2_orphan_distinct),
        unmatched_endpoint_distinct=leg2_orphan_distinct,
        endpoint_orphan_rows=leg2_orphan_rows, joined_row_count=leg2_joined,
        max_mapping_matches_per_endpoint_row=leg2_max_mapping,
        max_endpoint_matches_per_mapping_row=tgt_max,
        normalization_ids=("identity_v1",) * len(plan.target_columns),
        endpoint_source_snapshot_id=target_snapshot_id,
        mapping_source_snapshot_id=mapping_snapshot_id,
        as_of=plan.snapshot_or_as_of, execution_principal=plan.execution_principal,
        method=method, row_coverage=RowCoverage.FULL, complete=True,
        partitions_read=_partition_pins(plan.target_binding, plan.target_partitions),
        predicates=_side_pins(plan, ProbeSide.TARGET),
        v2_observation_revision_id=target_leg_v2[0] if target_leg_v2 else None,
        realization_revision_id=target_leg_v2[1] if target_leg_v2 else None)

    observation = compose_crosswalk_observation(
        crosswalk_definition_revision_id=definition_revision_id,
        source_leg=source_leg, target_leg=target_leg, mapping=mapping,
        scope_id=plan.scope_id,
        matched_source_distinct=matched_source,
        unmatched_source_distinct=max(0, src_distinct - matched_source),
        matched_target_distinct=matched_target,
        unmatched_target_distinct=max(0, tgt_distinct - matched_target),
        composed_row_count=composed_rows,
        source_to_target_max_matches=source_max_reach,
        target_to_source_max_matches=target_max_reach,
        observed_at=timestamp,
        partitions=tuple(dict.fromkeys(
            (*mapping_pins, *_partition_pins(plan.source_binding, plan.source_partitions),
             *_partition_pins(plan.target_binding, plan.target_partitions)))),
        predicates=tuple(dict.fromkeys((*row_rule.pins, *_side_pins(plan, ProbeSide.SOURCE),
                                        *_side_pins(plan, ProbeSide.TARGET)))),
        mapping_temporal_policy_revision_id=(
            row_rule.temporal_policy_revision_id if row_rule.filtered else None),
        mapping_row_selection_hash=row_rule.row_selection_hash if row_rule.filtered else None,
        caveats=(row_rule.caveat,) if row_rule.caveat else ())
    return CrosswalkMeasurementV1(
        observation=observation, row_rule=row_rule, plan_hash=plan.plan_hash)


# ── internals ───────────────────────────────────────────────────────────────────────────────────

def _effective_method(dialect, plan: CrosswalkProbePlanV1) -> str:
    """What the ENGINE actually does, not what the plan asked for — the executor's rule.

    The composed probe is a census of the filtered rows in every dialect this repo speaks, so the
    honest answer is ``exact`` unless a dialect says otherwise for this plan.
    """
    effective = getattr(dialect, "effective_method", None)
    if callable(effective):
        try:
            return str(effective(plan))
        except Exception:                   # noqa: BLE001 — a dialect that cannot answer for this
            pass                            # plan shape is not a measurement failure
    return (RelationshipMethod.EXACT.value if plan.policy.exact_distinct
            else RelationshipMethod.APPROXIMATE.value)


def _run(conn, plan: CrosswalkProbePlanV1, statement: str, dialect):
    """Execute one bounded statement inside a savepoint where the driver offers one.

    The savepoint is the ``DirectSqlExecutor`` discipline: a failed probe must not abort the
    caller's transaction, because the typed-coverage measurement it returns has to remain storable
    through the same connection.
    """
    transaction = getattr(conn, "transaction", None)
    if callable(transaction):
        with conn.transaction():
            return _execute(conn, plan, statement, dialect)
    return _execute(conn, plan, statement, dialect)


def _execute(conn, plan: CrosswalkProbePlanV1, statement: str, dialect):
    cursor = conn.cursor()
    try:
        for bound in dialect.timeout_statements(plan):
            cursor.execute(bound)
        cursor.execute(statement)
        row = cursor.fetchone()
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()
    if row is None:
        raise AssertionError("crosswalk probe returned no row")
    return row


def _partition_pins(binding, selector) -> tuple[ObservedPartitionPinV1, ...]:
    if selector is None:
        return ()
    return (ObservedPartitionPinV1(
        dataset_ref=binding.identity.table_id, column=selector.column,
        values=tuple(selector.values)),)


def _side_pins(plan: CrosswalkProbePlanV1, side: ProbeSide) -> tuple[ObservedPredicatePinV1, ...]:
    binding = {ProbeSide.SOURCE: plan.source_binding, ProbeSide.MAPPING: plan.mapping_binding,
               ProbeSide.TARGET: plan.target_binding}[side]
    return tuple(
        ObservedPredicatePinV1(
            predicate_id=predicate.predicate_id, dataset_ref=binding.identity.table_id,
            column=predicate.column, operator=predicate.operator, values=predicate.values,
            parameter_ref=predicate.parameter_ref)
        for predicate in plan.predicates if predicate.side is side)


def _failed(plan, definition_revision_id, row_rule, method, timestamp, reason,
            source_leg_kind, target_leg_kind, source_snapshot_id, mapping_snapshot_id,
            target_snapshot_id) -> CrosswalkExecutionObservationV1:
    """A probe that could not run is EVIDENCE that the question was asked and not answered."""
    def leg(name, kind, binding, endpoint_columns, mapping_columns, snapshot, side):
        return CrosswalkLegObservationV1(
            leg=name, leg_kind=kind,
            endpoint_dataset_ref=binding.identity.table_id,
            endpoint_binding_revision_id=binding.binding_revision_id,
            endpoint_columns=endpoint_columns, mapping_columns=mapping_columns,
            endpoint_row_count=0, endpoint_non_null_row_count=0,
            endpoint_distinct_tuple_count=0, endpoint_duplicate_tuple_count=0,
            endpoint_max_rows_per_tuple=0, matched_endpoint_distinct=0,
            unmatched_endpoint_distinct=0, endpoint_orphan_rows=0, joined_row_count=0,
            max_mapping_matches_per_endpoint_row=0, max_endpoint_matches_per_mapping_row=0,
            normalization_ids=("identity_v1",) * len(endpoint_columns),
            endpoint_source_snapshot_id=snapshot,
            mapping_source_snapshot_id=mapping_snapshot_id,
            as_of=plan.snapshot_or_as_of, execution_principal=plan.execution_principal,
            method=method, row_coverage=RowCoverage.PARTIAL, complete=False,
            partitions_read=_partition_pins(binding, getattr(plan, f"{side}_partitions")),
            predicates=_side_pins(plan, ProbeSide(side)))

    return compose_crosswalk_observation(
        crosswalk_definition_revision_id=definition_revision_id,
        source_leg=leg(SOURCE_TO_MAPPING, source_leg_kind, plan.source_binding,
                       plan.source_columns, plan.mapping_source_columns, source_snapshot_id,
                       "source"),
        target_leg=leg(MAPPING_TO_TARGET, target_leg_kind, plan.target_binding,
                       plan.target_columns, plan.mapping_target_columns, target_snapshot_id,
                       "target"),
        mapping=MappingTupleObservationV1(
            physical_id=plan.mapping_binding.identity.table_id,
            binding_revision_id=plan.mapping_binding.binding_revision_id,
            binding_content_hash=plan.mapping_binding.content_hash,
            source_columns=plan.mapping_source_columns,
            target_columns=plan.mapping_target_columns,
            row_count=0, non_null_row_count=0, distinct_source_tuple_count=0,
            distinct_target_tuple_count=0, distinct_pair_count=0,
            duplicate_source_tuple_count=0, duplicate_target_tuple_count=0,
            max_rows_per_source_tuple=0, max_rows_per_target_tuple=0,
            partitions_read=_partition_pins(plan.mapping_binding, plan.mapping_partitions)),
        scope_id=plan.scope_id,
        matched_source_distinct=0, unmatched_source_distinct=0,
        matched_target_distinct=0, unmatched_target_distinct=0,
        composed_row_count=0, source_to_target_max_matches=0, target_to_source_max_matches=0,
        observed_at=timestamp,
        partitions=_partition_pins(plan.mapping_binding, plan.mapping_partitions),
        predicates=row_rule.pins,
        mapping_temporal_policy_revision_id=(
            row_rule.temporal_policy_revision_id if row_rule.filtered else None),
        mapping_row_selection_hash=row_rule.row_selection_hash if row_rule.filtered else None,
        caveats=(row_rule.caveat,) if row_rule.caveat else (),
        failures=(reason,), complete=False)
