"""Release C Task 11, scope 2 — the measurement pass, against the pilot Postgres harness.

REAL TABLES, REAL SQL, no cluster and no LLM: three tables in the suite's own rolled-back
transaction, measured through the same `DirectSqlExecutor` transport discipline the shipped V2 probe
uses. Every number below was worked out by hand from the fixture rows and is asserted exactly — a
composed measurement whose arithmetic is only checked against itself proves nothing.

THE FIXTURE, and why it is shaped this way:

    cib_accounts        acct_xref (mapping, keeps history)      ftr_tran
    ------------        -----------------------------------     --------
    A                   A -> X1   (is_current = false)          X2
    A                   A -> X2   (is_current = true)           X2
    B                   B -> Y1   (is_current = true)           X2
    D                   C -> Z1   (is_current = true)           Y1
                                                                W9
                                                                X1

* the mapping's SOURCE tuple is duplicated ONLY in history, so a measurement taken before the
  temporal filter says "not unique" and one taken after says "unique" — the two answers this pass
  must never confuse;
* `D` is an unmatched source, `W9` an unmatched target, `C -> Z1` a mapping row whose target is
  absent — so coverage and orphan counts are non-trivial in both directions;
* `X2` occurs three times in the target and `A` twice in the source, so the composed fan-out is
  genuinely different from either leg's, and different in each direction.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload._crosswalk_fixtures import build_catalog

from featuregen.data_agent.crosswalk_measurement import (
    measure_crosswalk,
    resolve_mapping_row_rule,
)
from featuregen.data_agent.crosswalk_probe import (
    CrosswalkProbePlanV1,
    ProbePredicateKind,
    ProbePredicateV1,
    ProbeSide,
)
from featuregen.data_agent.observation import PartitionSelector
from featuregen.data_agent.physical import (
    PhysicalDatasetBindingV1,
    PhysicalObjectIdentityV1,
)
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.sql_postgres import PostgresDialect
from featuregen.overlay.upload.crosswalk_observation import (
    SOURCE_TO_TARGET,
    TARGET_TO_SOURCE,
    CrosswalkObservationCaveat,
)

NOW = datetime(2026, 8, 4, 11, tzinfo=UTC)
SCHEMA = "crosswalk_probe"
PRINCIPAL = "crosswalk-profiler"


@pytest.fixture
def tables(db):
    db.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    for name in ("cib_accounts", "acct_xref", "ftr_tran"):
        db.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{name}")
    db.execute(f"CREATE TABLE {SCHEMA}.cib_accounts (acct_no text, region text)")
    db.execute(
        f"CREATE TABLE {SCHEMA}.acct_xref (acct_no text, ext_acct_ref text, "
        "is_current boolean, valid_from text)")
    db.execute(f"CREATE TABLE {SCHEMA}.ftr_tran (counter_party_acct_no text, region text)")
    for row in (("A", "eu"), ("A", "eu"), ("B", "eu"), ("D", "us")):
        db.execute(f"INSERT INTO {SCHEMA}.cib_accounts VALUES (%s,%s)", row)
    for row in (("A", "X1", False, "2025-01-01"), ("A", "X2", True, "2026-01-01"),
                ("B", "Y1", True, "2026-01-01"), ("C", "Z1", True, "2026-01-01")):
        db.execute(f"INSERT INTO {SCHEMA}.acct_xref VALUES (%s,%s,%s,%s)", row)
    for row in (("X2", "eu"), ("X2", "eu"), ("X2", "eu"), ("Y1", "eu"), ("W9", "us"),
                ("X1", "eu")):
        db.execute(f"INSERT INTO {SCHEMA}.ftr_tran VALUES (%s,%s)", row)
    return db


def _binding(source: str, table: str, *, partition_columns: tuple[str, ...] = ()):
    return PhysicalDatasetBindingV1(
        binding_id=f"derived-{source}-{table}",
        catalog_logical_ref=f"{source}::{SCHEMA}.{table}",
        connection_id="local-postgres",
        identity=PhysicalObjectIdentityV1(
            catalog_source=source, database="featuregen_test", schema=SCHEMA, table=table,
            object_kind="table"),
        partition_columns=partition_columns,
        business_time_column=partition_columns[0] if partition_columns else None)


SOURCE_BINDING = _binding("cib", "cib_accounts")
MAPPING_BINDING = _binding("cib", "acct_xref")
TARGET_BINDING = _binding("ftr", "ftr_tran")


def _plan(**over) -> CrosswalkProbePlanV1:
    kwargs = dict(
        source_binding=SOURCE_BINDING, mapping_binding=MAPPING_BINDING,
        target_binding=TARGET_BINDING,
        source_columns=("acct_no",), mapping_source_columns=("acct_no",),
        mapping_target_columns=("ext_acct_ref",), target_columns=("counter_party_acct_no",),
        policy=ProfilePolicyV1(exact_distinct=True),
        scope_id="crosswalk-sandbox-scope", execution_principal=PRINCIPAL,
        snapshot_or_as_of="2026-08-04")
    kwargs.update(over)
    return CrosswalkProbePlanV1(**kwargs)


CURRENT_ONLY = ProbePredicateV1(
    predicate_id="mapping_current_rows", side=ProbeSide.MAPPING,
    kind=ProbePredicateKind.CURRENT_FLAG, column="is_current", operator="is_true")


def _measure(conn, plan, *, row_rule, **over):
    kwargs = dict(
        dialect=PostgresDialect(), definition_revision_id="cwd_" + "a" * 64,
        plan=plan, row_rule=row_rule,
        source_leg_kind="same_catalog", target_leg_kind="cross_catalog",
        source_snapshot_id="src-snap-1", mapping_snapshot_id="map-snap-1",
        target_snapshot_id="tgt-snap-1", observed_at=NOW)
    kwargs.update(over)
    return measure_crosswalk(conn, **kwargs)


def _unfiltered_rule():
    from featuregen.data_agent.crosswalk_measurement import MappingRowRuleV1

    return MappingRowRuleV1(
        (), (), None, None, CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value)


def _filtered_rule():
    from featuregen.data_agent.crosswalk_measurement import MappingRowRuleV1
    from featuregen.overlay.upload.crosswalk_observation import ObservedPredicatePinV1

    return MappingRowRuleV1(
        predicates=(CURRENT_ONLY,),
        pins=(ObservedPredicatePinV1(
            predicate_id="mapping_current_rows", dataset_ref="cib::public.acct_xref",
            column="is_current", operator="is_true"),),
        temporal_policy_revision_id="dtp_" + "b" * 64,
        row_selection_hash="c" * 64, caveat=None)


# ── the filtered measurement (the governed answer) ──────────────────────────────────────────────

def test_the_mapping_is_measured_after_its_row_filter_not_before(tables):
    """The whole ordering rule, as data. Unfiltered the mapping's source tuple `A` appears twice
    (history); filtered it appears once. Reporting the first as a statement about the current
    mapping is the mutation Task 13 must kill."""
    filtered = _measure(tables, _plan(predicates=(CURRENT_ONLY,)),
                        row_rule=_filtered_rule()).observation
    assert filtered.mapping.row_count == 3
    assert filtered.mapping.duplicate_source_tuple_count == 0
    assert filtered.mapping.max_rows_per_source_tuple == 1
    assert filtered.mapping.source_tuple_unique is True

    unfiltered = _measure(tables, _plan(), row_rule=_unfiltered_rule()).observation
    assert unfiltered.mapping.row_count == 4
    assert unfiltered.mapping.duplicate_source_tuple_count == 1
    assert unfiltered.mapping.max_rows_per_source_tuple == 2
    assert unfiltered.mapping.source_tuple_unique is False


def test_the_composed_fanout_differs_by_direction_and_is_not_the_leg_product(tables):
    """Hand-computed. Filtered: A(2 source rows) reaches X2(3 target rows) once => 3; B reaches Y1
    once => 1; so source->target max is 3. Backwards X2 reaches A's 2 rows => 2, Y1 reaches 1 => 1;
    so target->source max is 2. Neither is the product of the two legs' maxima (1 x 1 = 1)."""
    observation = _measure(tables, _plan(predicates=(CURRENT_ONLY,)),
                           row_rule=_filtered_rule()).observation
    assert observation.source_leg.max_mapping_matches_per_endpoint_row == 1
    assert observation.target_leg.max_mapping_matches_per_endpoint_row == 1
    assert observation.source_to_target_max_matches == 3
    assert observation.target_to_source_max_matches == 2
    assert observation.composed_row_count == 7        # 2*3 + 1*1


def test_the_unfiltered_composition_reaches_a_row_the_filtered_one_does_not(tables):
    """`A -> X1` is retired history. Unfiltered it still connects A to the X1 transaction, so the
    forward fan-out rises from 3 to 4 — a governed feature would silently have counted it."""
    observation = _measure(tables, _plan(), row_rule=_unfiltered_rule()).observation
    assert observation.composed_row_count == 9        # 2*1 (X1) + 2*3 (X2) + 1*1 (Y1)
    assert observation.source_to_target_max_matches == 4
    assert observation.target_to_source_max_matches == 2


def test_tuple_completeness_duplicates_overlap_and_unmatched_rows_are_measured(tables):
    observation = _measure(tables, _plan(predicates=(CURRENT_ONLY,)),
                           row_rule=_filtered_rule()).observation
    source = observation.source_leg
    assert (source.endpoint_row_count, source.endpoint_distinct_tuple_count) == (4, 3)
    assert source.endpoint_duplicate_tuple_count == 1        # `A` twice
    assert source.endpoint_max_rows_per_tuple == 2
    assert source.unmatched_endpoint_distinct == 1           # `D` maps to nothing
    assert source.endpoint_orphan_rows == 1
    assert source.joined_row_count == 3                      # A(2) + B(1) against the mapping

    target = observation.target_leg
    assert (target.endpoint_row_count, target.endpoint_distinct_tuple_count) == (6, 4)
    assert target.endpoint_duplicate_tuple_count == 1        # `X2` three times
    assert target.unmatched_endpoint_distinct == 2           # `W9` and (filtered out) `X1`
    assert target.joined_row_count == 4                      # X2(3) + Y1(1)

    # Composed coverage: two of three source tuples and two of four target tuples take part.
    assert observation.matched_source_distinct == 2
    assert observation.unmatched_source_distinct == 1
    assert observation.matched_target_distinct == 2
    assert observation.unmatched_target_distinct == 2


def test_a_null_in_the_mapping_disproves_uniqueness_in_both_directions(tables):
    tables.execute(
        f"INSERT INTO {SCHEMA}.acct_xref VALUES (NULL,'Q1',true,'2026-01-01')")
    observation = _measure(tables, _plan(predicates=(CURRENT_ONLY,)),
                           row_rule=_filtered_rule()).observation
    assert observation.mapping.null_row_count == 1
    assert observation.uniqueness_verdict(SOURCE_TO_TARGET) == "not_unique"
    assert observation.uniqueness_verdict(TARGET_TO_SOURCE) == "not_unique"


# ── the temporal machinery, both ways ───────────────────────────────────────────────────────────

@pytest.fixture
def catalog(db):
    build_catalog(db)
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "  schema_name) VALUES ('cib','public.acct_xref.is_current','column','acct_xref',"
        "  'is_current','dpl_eib') ON CONFLICT (catalog_source, object_ref) DO NOTHING")
    return db


def test_no_governed_policy_measures_unfiltered_and_says_so(catalog):
    """Never silently pretending: the measurement runs, the numbers are real, and the record names
    exactly which question they do not answer."""
    rule = resolve_mapping_row_rule(
        catalog, mapping_dataset_ref="cib::public.acct_xref",
        dataset_profile_hash="d" * 64)
    assert rule.filtered is False
    assert rule.caveat == CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value
    assert rule.temporal_policy_revision_id is None


def test_a_governed_current_record_policy_produces_a_filtered_measurement(catalog):
    from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
    from featuregen.overlay.upload.temporal_policy import (
        DatasetTemporalPolicyRevisionV1,
        TemporalSelectionKind,
    )
    from featuregen.overlay.upload.temporal_policy_store import publish_temporal_policy

    revision = DatasetTemporalPolicyRevisionV1(
        dataset_logical_ref="cib::public.acct_xref",
        temporal_storage_model=TemporalStorageModel.SCD2,
        current_selection=TemporalSelectionKind.CURRENT_RECORD,
        historical_selection=TemporalSelectionKind.EXPLICIT_ONLY,
        current_flag_ref="cib::public.acct_xref.is_current")
    publish_temporal_policy(catalog, revision, expected_pointer_version=0, actor="task11",
                            roles=("platform-admin",))

    rule = resolve_mapping_row_rule(
        catalog, mapping_dataset_ref="cib::public.acct_xref", dataset_profile_hash="d" * 64)
    assert rule.filtered is True
    assert rule.caveat is None
    assert rule.temporal_policy_revision_id == revision.revision_id
    assert rule.predicates[0].kind is ProbePredicateKind.CURRENT_FLAG
    assert rule.predicates[0].column == "is_current"
    assert rule.pins[0].operator == "is_true"


def test_a_policy_needing_a_cutoff_the_request_did_not_bind_is_UNRESOLVED_not_ABSENT(catalog):
    """Two different situations with two different fixes; conflating them sends the reader to the
    wrong one."""
    from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
    from featuregen.overlay.upload.temporal_policy import (
        DatasetTemporalPolicyRevisionV1,
        TemporalSelectionKind,
    )
    from featuregen.overlay.upload.temporal_policy_store import publish_temporal_policy

    revision = DatasetTemporalPolicyRevisionV1(
        dataset_logical_ref="cib::public.acct_xref",
        temporal_storage_model=TemporalStorageModel.SCD2,
        current_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
        historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
        effective_from_ref="cib::public.acct_xref.valid_from",
        effective_to_ref="cib::public.acct_xref.valid_from")
    publish_temporal_policy(catalog, revision, expected_pointer_version=0, actor="task11",
                            roles=("platform-admin",))
    rule = resolve_mapping_row_rule(
        catalog, mapping_dataset_ref="cib::public.acct_xref", dataset_profile_hash="d" * 64)
    assert rule.filtered is False
    assert rule.caveat == CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value


def test_the_caveat_reaches_the_stored_measurement(tables):
    observation = _measure(tables, _plan(), row_rule=_unfiltered_rule()).observation
    assert (CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value
            in observation.caveats)
    assert observation.mapping_temporal_policy_revision_id is None


def test_a_filtered_measurement_pins_its_policy_and_row_selection(tables):
    observation = _measure(tables, _plan(predicates=(CURRENT_ONLY,)),
                           row_rule=_filtered_rule()).observation
    assert observation.mapping_temporal_policy_revision_id == "dtp_" + "b" * 64
    assert observation.mapping_row_selection_hash == "c" * 64
    assert CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value not in (
        observation.caveats)


# ── the cross-reference rule ────────────────────────────────────────────────────────────────────

def test_a_two_endpoint_row_cannot_be_claimed_for_a_temporally_filtered_leg(tables):
    """The pair probe has no temporal predicate vocabulary, so it measured the UNFILTERED mapping —
    a different question wearing the same leg's name. Enforced, not documented."""
    with pytest.raises(ValueError, match="temporally filtered"):
        _measure(tables, _plan(predicates=(CURRENT_ONLY,)), row_rule=_filtered_rule(),
                 target_leg_v2=("rob_" + "1" * 64, "realization-1"))


def test_an_unfiltered_leg_may_carry_its_two_endpoint_row(tables):
    observation = _measure(tables, _plan(), row_rule=_unfiltered_rule(),
                           target_leg_v2=("rob_" + "1" * 64, "realization-1")).observation
    assert observation.target_leg.v2_observation_revision_id == "rob_" + "1" * 64
    assert observation.leg_observation_revision_ids == ("rob_" + "1" * 64,)


# ── pins and failures ───────────────────────────────────────────────────────────────────────────

def test_partitions_are_pinned_with_their_column(tables):
    plan = _plan(source_binding=_binding("cib", "cib_accounts", partition_columns=("region",)),
                 source_partitions=PartitionSelector(column="region", values=("eu",)))
    observation = _measure(tables, plan, row_rule=_unfiltered_rule()).observation
    pins = {(pin.column, pin.values) for pin in observation.partitions}
    assert ("region", ("eu",)) in pins
    # And the partition really restricted the read: `D` is a `us` row.
    assert observation.source_leg.endpoint_row_count == 3


def test_a_partition_pin_on_ANY_side_scopes_the_whole_measurement(tables):
    """The probe SQL knows exactly which partitions it pinned, so the record it produces must say
    so on the side it pinned them — for all three tables, not only the mapping. Downstream this is
    what stops partition-scoped evidence validating an unrestricted crosswalk."""
    source_scoped = _measure(
        tables,
        _plan(source_binding=_binding("cib", "cib_accounts", partition_columns=("region",)),
              source_partitions=PartitionSelector(column="region", values=("eu",))),
        row_rule=_unfiltered_rule()).observation
    assert (CrosswalkObservationCaveat.SOURCE_PARTITION_SCOPED.value
            in source_scoped.caveats)
    assert source_scoped.partition_scoped is True

    target_scoped = _measure(
        tables,
        _plan(target_binding=_binding("ftr", "ftr_tran", partition_columns=("region",)),
              target_partitions=PartitionSelector(column="region", values=("eu",))),
        row_rule=_unfiltered_rule()).observation
    assert (CrosswalkObservationCaveat.TARGET_PARTITION_SCOPED.value
            in target_scoped.caveats)

    unrestricted = _measure(tables, _plan(), row_rule=_unfiltered_rule()).observation
    assert unrestricted.partition_scoped is False


def test_an_engine_failure_becomes_typed_coverage_that_is_still_storable(tables):
    plan = _plan(source_columns=("no_such_column",))
    result = _measure(tables, plan, row_rule=_unfiltered_rule())
    assert result.observation.complete is False
    assert result.observation.failures and "no_such_column" in result.observation.failures[0]
    assert result.observation.uniqueness_verdict(SOURCE_TO_TARGET) == "unknown"
    # The caller's transaction survived the failed probe — the savepoint discipline.
    assert tables.execute("SELECT 1").fetchone()[0] == 1


def test_the_probe_plan_refuses_a_mapping_tuple_that_shares_a_column(tables):
    from featuregen.data_agent.observation import ObservationPlanError

    with pytest.raises(ObservationPlanError):
        _plan(mapping_target_columns=("acct_no",))


def test_the_probe_plan_refuses_unequal_leg_arity(tables):
    from featuregen.data_agent.observation import ObservationPlanError

    with pytest.raises(ObservationPlanError):
        _plan(source_columns=("acct_no", "region"))
