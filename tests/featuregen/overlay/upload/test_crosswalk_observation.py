"""Release C Task 11, scope 1 — the COMPOSED crosswalk observation contract and its store.

The review proved three things the two-endpoint V2 store cannot represent (A5/A8), and each has a
test here: composed cross-leg fan-out in BOTH named directions, an ATOMIC multi-leg as-of, and
partition/predicate pinning that carries the column and the content rather than bare values and
bare ids.

Nothing here touches the shipped 1038 store's rows or semantics; the legs that CAN live there do,
unchanged, and this suite pins that the two never disagree.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen.overlay.upload._crosswalk_fixtures import (
    CIB_TABLE,
    FTR_TABLE,
    MAP_TABLE,
    binding,
    build_catalog,
    definition,
    leg,
    mapping_observation,
    record_bindings,
)

from featuregen.data_agent.relationship_observation import (
    EndpointTupleObservationV2,
    RelationshipObservationV2,
    RowCoverage,
)
from featuregen.overlay.upload.crosswalk_observation import (
    CROSSWALK_OBSERVATION_LEG_MISMATCH,
    CROSSWALK_OBSERVATION_MALFORMED,
    CROSSWALK_OBSERVATION_NOT_ATOMIC,
    MAPPING_TO_TARGET,
    SOURCE_TO_MAPPING,
    SOURCE_TO_TARGET,
    TARGET_TO_SOURCE,
    CrosswalkLegObservationV1,
    CrosswalkObservationCaveat,
    CrosswalkObservationError,
    ObservedPartitionPinV1,
    ObservedPredicatePinV1,
    compose_crosswalk_observation,
    crosswalk_observation_from_json,
    crosswalk_observation_to_json,
)
from featuregen.overlay.upload.crosswalk_observation_store import (
    CrosswalkObservationStoreConflict,
    crosswalk_observation_history,
    current_crosswalk_observation,
    current_crosswalk_observations_for_revision,
    load_crosswalk_observation,
    record_crosswalk_observation,
)
from featuregen.overlay.upload.crosswalk_store import publish_crosswalk_definition

NOW = datetime(2026, 8, 4, 10, tzinfo=UTC)
ROLES = ("platform-admin",)


@pytest.fixture
def bank(db):
    """One catalog, three bindings, one published crosswalk definition."""
    build_catalog(db)
    cib = binding("cib", CIB_TABLE)
    ftr = binding("ftr", FTR_TABLE, schema="dpl_ftr")
    mapping = binding("cib", MAP_TABLE)
    record_bindings(db, cib, ftr, mapping)
    revision = definition()
    publish_crosswalk_definition(
        db, revision, expected_pointer_version=0, actor="task11", roles=ROLES)
    return db, revision, cib, ftr, mapping


def _legs(cib, ftr, mapping, **over):
    source = leg(which=SOURCE_TO_MAPPING, endpoint_binding=cib, mapping_binding=mapping,
                 endpoint_column="acct_no", mapping_column="acct_no",
                 **over.pop("source", {}))
    target = leg(which=MAPPING_TO_TARGET, endpoint_binding=ftr, mapping_binding=mapping,
                 endpoint_column="counter_party_acct_no", mapping_column="ext_acct_ref",
                 cross_catalog=True, **over.pop("target", {}))
    return source, target


def _compose(revision, cib, ftr, map_binding, *, source=None, target=None, **over):
    source_leg, target_leg = _legs(cib, ftr, map_binding,
                                   source=source or {}, target=target or {})
    kwargs = dict(
        crosswalk_definition_revision_id=revision.revision_id,
        source_leg=source_leg, target_leg=target_leg,
        mapping=mapping_observation(map_binding),
        scope_id="crosswalk-sandbox-scope",
        matched_source_distinct=3, unmatched_source_distinct=1,
        matched_target_distinct=3, unmatched_target_distinct=0,
        composed_row_count=3,
        source_to_target_max_matches=1, target_to_source_max_matches=1,
        observed_at=NOW,
        mapping_temporal_policy_revision_id="dtp_policy_1",
        mapping_row_selection_hash="a" * 64)
    kwargs.update(over)
    return compose_crosswalk_observation(**kwargs)


# ── gap 1: composed cross-leg fan-out, BOTH named directions ────────────────────────────────────

def test_the_composed_fanout_is_recorded_in_both_named_directions(bank):
    """The two directions are SIDES of the canonically-ordered definition, never traversal order,
    and they are independent numbers — not one number and its inverse."""
    _db, revision, cib, ftr, mapping = bank
    observation = _compose(revision, cib, ftr, mapping,
                           source_to_target_max_matches=1, target_to_source_max_matches=4)
    assert observation.source_to_target_max_matches == 1
    assert observation.target_to_source_max_matches == 4
    assert observation.uniqueness_verdict(SOURCE_TO_TARGET) == "unique"
    assert observation.uniqueness_verdict(TARGET_TO_SOURCE) == "not_unique"


def test_a_1to1_forward_with_a_fanout_reverse_is_a_representable_shape(bank):
    """The whole point of two directions: "admit forward only" needs a record that can say it."""
    _db, revision, cib, ftr, mapping = bank
    observation = _compose(
        revision, cib, ftr, mapping,
        mapping=mapping_observation(mapping, duplicate_source_tuple_count=1,
                                    distinct_source_tuple_count=2, max_rows_per_source_tuple=2),
        source_to_target_max_matches=1, target_to_source_max_matches=2)
    assert observation.uniqueness_verdict(SOURCE_TO_TARGET) == "unique"
    assert observation.uniqueness_verdict(TARGET_TO_SOURCE) == "not_unique"


def test_a_composition_that_joined_nothing_cannot_have_observed_a_fanout(bank):
    _db, revision, cib, ftr, mapping = bank
    with pytest.raises(CrosswalkObservationError) as raised:
        _compose(revision, cib, ftr, mapping, composed_row_count=0,
                 source_to_target_max_matches=3)
    assert raised.value.code == CROSSWALK_OBSERVATION_MALFORMED


def test_a_sample_may_disprove_uniqueness_and_never_establish_it(bank):
    """The V2 asymmetry, read from this record rather than re-derived downstream."""
    _db, revision, cib, ftr, mapping = bank
    sampled = _compose(revision, cib, ftr, mapping,
                       source={"row_coverage": RowCoverage.SAMPLED})
    assert sampled.uniqueness_verdict(SOURCE_TO_TARGET) == "unknown"
    assert CrosswalkObservationCaveat.LEG_EVIDENCE_NOT_EXACT.value in sampled.caveats

    # …but the same sampled probe finding a duplicate DISPROVES it.
    disproved = _compose(
        revision, cib, ftr, mapping, source={"row_coverage": RowCoverage.SAMPLED},
        mapping=mapping_observation(mapping, duplicate_target_tuple_count=1))
    assert disproved.uniqueness_verdict(SOURCE_TO_TARGET) == "not_unique"


# ── gap 2: the atomic multi-leg as-of ───────────────────────────────────────────────────────────

def test_two_legs_read_at_two_instants_refuse_to_compose(bank):
    _db, revision, cib, ftr, mapping = bank
    with pytest.raises(CrosswalkObservationError) as raised:
        _compose(revision, cib, ftr, mapping, target={"as_of": "2026-08-03"})
    assert raised.value.code == CROSSWALK_OBSERVATION_NOT_ATOMIC
    assert "never existed" in str(raised.value)


def test_two_legs_read_by_two_principals_refuse_to_compose(bank):
    _db, revision, cib, ftr, mapping = bank
    with pytest.raises(CrosswalkObservationError) as raised:
        _compose(revision, cib, ftr, mapping, target={"principal": "someone-else"})
    assert raised.value.code == CROSSWALK_OBSERVATION_NOT_ATOMIC


def test_the_shared_instant_is_pinned_on_the_composition(bank):
    _db, revision, cib, ftr, mapping = bank
    observation = _compose(revision, cib, ftr, mapping)
    assert observation.as_of == "2026-08-04"
    assert observation.execution_principal == "crosswalk-profiler"


def test_the_composition_is_no_more_exact_than_the_weakest_leg(bank):
    _db, revision, cib, ftr, mapping = bank
    observation = _compose(revision, cib, ftr, mapping,
                           source={"method": "approximate"},
                           target={"row_coverage": RowCoverage.PARTIAL, "complete": False})
    assert observation.method == "approximate"
    assert observation.row_coverage is RowCoverage.PARTIAL
    assert observation.complete is False
    assert observation.exact_and_complete is False


# ── gap 3: pins that can actually be replayed ───────────────────────────────────────────────────

def test_a_partition_pin_carries_its_column_not_just_the_value(bank):
    """`EndpointTupleObservationV2.partitions_read` is a bare value tuple: given ('2026-07-01',) a
    replayer cannot know whether that was `business_dt` or `load_dt`."""
    _db, revision, cib, ftr, mapping = bank
    pin = ObservedPartitionPinV1(mapping.identity.table_id, "valid_from", ("2026-08-01",))
    observation = _compose(revision, cib, ftr, mapping, partitions=(pin,))
    assert observation.partitions[0].column == "valid_from"
    assert observation.partitions[0].values == ("2026-08-01",)


def test_a_predicate_pin_carries_its_content_not_just_its_id(bank):
    _db, revision, cib, ftr, mapping = bank
    pin = ObservedPredicatePinV1(
        predicate_id="mapping_active_only", dataset_ref=mapping.identity.table_id,
        column="status", operator="in", values=("ACTIVE",))
    observation = _compose(revision, cib, ftr, mapping, predicates=(pin,))
    assert observation.predicates[0].values == ("active",) or (
        observation.predicates[0].values == ("ACTIVE",))
    assert observation.predicates[0].operator == "in"


def test_a_predicate_pinning_neither_values_nor_a_parameter_records_nothing(bank):
    with pytest.raises(CrosswalkObservationError) as raised:
        ObservedPredicatePinV1(predicate_id="p", dataset_ref="d", column="c", operator="<=")
    assert raised.value.code == CROSSWALK_OBSERVATION_MALFORMED


def test_a_runtime_cutoff_pins_its_parameter_ref_never_a_literal(bank):
    """The Release-B rule: a governed decision carries the cutoff's REF so two replays hash alike."""
    _db, revision, cib, ftr, mapping = bank
    pin = ObservedPredicatePinV1(
        predicate_id="valid_at_cutoff", dataset_ref=mapping.identity.table_id,
        column="valid_from", operator="<=", parameter_ref="report_cutoff")
    observation = _compose(revision, cib, ftr, mapping, predicates=(pin,))
    assert observation.predicates[0].parameter_ref == "report_cutoff"
    assert observation.predicates[0].values == ()


def test_an_empty_partition_selector_is_refused_rather_than_read_as_no_restriction(bank):
    with pytest.raises(CrosswalkObservationError) as raised:
        ObservedPartitionPinV1("cib::x::y::z", "dt", ())
    assert raised.value.code == CROSSWALK_OBSERVATION_MALFORMED


# ── the temporal caveat: never silently pretending the mapping is current ───────────────────────

def test_a_missing_mapping_policy_records_a_typed_caveat_rather_than_a_silent_claim(bank):
    _db, revision, cib, ftr, mapping = bank
    observation = _compose(revision, cib, ftr, mapping,
                           mapping_temporal_policy_revision_id=None,
                           mapping_row_selection_hash=None)
    assert (CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value
            in observation.caveats)


def test_a_governed_policy_leaves_no_temporal_caveat(bank):
    _db, revision, cib, ftr, mapping = bank
    observation = _compose(revision, cib, ftr, mapping)
    assert CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value not in (
        observation.caveats)


def test_the_caveat_vocabulary_is_closed(bank):
    _db, revision, cib, ftr, mapping = bank
    with pytest.raises(ValueError):
        _compose(revision, cib, ftr, mapping, caveats=("looked_a_bit_odd",))


def test_a_partition_scoped_mapping_says_so(bank):
    _db, revision, cib, ftr, mapping = bank
    scoped = mapping_observation(mapping)
    scoped = replace(scoped, partitions_read=(
        ObservedPartitionPinV1(mapping.identity.table_id, "valid_from", ("2026-08-01",)),))
    observation = _compose(revision, cib, ftr, mapping, mapping=scoped)
    assert CrosswalkObservationCaveat.MAPPING_PARTITION_SCOPED.value in observation.caveats
    assert observation.partition_scoped is True


def test_a_partition_scoped_ENDPOINT_says_so_on_its_own_side(bank):
    """Three tables, three places a subset can enter. The mapping was the only side that raised a
    caveat, so an endpoint read over one region proved uniqueness over that region and reported it
    as though it had read the bank."""
    _db, revision, cib, ftr, mapping = bank
    source_pinned = _compose(revision, cib, ftr, mapping, source={
        "partitions_read": (ObservedPartitionPinV1(
            cib.identity.table_id, "region", ("eu",)),)})
    assert CrosswalkObservationCaveat.SOURCE_PARTITION_SCOPED.value in source_pinned.caveats
    assert CrosswalkObservationCaveat.MAPPING_PARTITION_SCOPED.value not in source_pinned.caveats
    assert source_pinned.partition_scoped is True

    target_pinned = _compose(revision, cib, ftr, mapping, target={
        "partitions_read": (ObservedPartitionPinV1(
            ftr.identity.table_id, "region", ("eu",)),)})
    assert CrosswalkObservationCaveat.TARGET_PARTITION_SCOPED.value in target_pinned.caveats
    assert target_pinned.partition_scoped is True


def test_an_unrestricted_measurement_is_not_partition_scoped(bank):
    _db, revision, cib, ftr, mapping = bank
    observation = _compose(revision, cib, ftr, mapping)
    assert observation.partition_scoped is False
    assert not {caveat for caveat in observation.caveats
                if caveat.endswith("partition_scoped")}


# ── leg shape ───────────────────────────────────────────────────────────────────────────────────

def test_the_legs_must_be_the_two_named_legs_in_their_named_places(bank):
    _db, revision, cib, ftr, mapping = bank
    source_leg, target_leg = _legs(cib, ftr, mapping)
    with pytest.raises(CrosswalkObservationError) as raised:
        compose_crosswalk_observation(
            crosswalk_definition_revision_id=revision.revision_id,
            source_leg=target_leg, target_leg=source_leg,     # SWAPPED
            mapping=mapping_observation(mapping), scope_id="s",
            matched_source_distinct=1, unmatched_source_distinct=0,
            matched_target_distinct=1, unmatched_target_distinct=0,
            composed_row_count=1, source_to_target_max_matches=1,
            target_to_source_max_matches=1, observed_at=NOW)
    assert raised.value.code == CROSSWALK_OBSERVATION_LEG_MISMATCH
    assert "inverts every direction" in str(raised.value)


def test_a_leg_recorded_in_the_two_endpoint_store_names_both_ids_or_neither(bank):
    _db, _revision, cib, _ftr, mapping = bank
    with pytest.raises(CrosswalkObservationError) as raised:
        leg(which=SOURCE_TO_MAPPING, endpoint_binding=cib, mapping_binding=mapping,
            endpoint_column="acct_no", mapping_column="acct_no",
            v2_observation_revision_id="rob_x")
    assert raised.value.code == CROSSWALK_OBSERVATION_LEG_MISMATCH


def test_the_v2_projection_keeps_the_endpoints_side_assignment(bank):
    """D4 forbids collapsing the side assignment; a leg projection must not swap the endpoint's
    numbers for the mapping's."""
    _db, _revision, cib, _ftr, mapping = bank
    observation = RelationshipObservationV2(
        realization_revision_id="realization-1", plan_hash="plan-1", scope_id="scope-1",
        left=EndpointTupleObservationV2(
            cib.identity.table_id, cib.binding_revision_id, cib.content_hash,
            ("acct_no",), 9, 8, 3, 1, 5, 6),
        right=EndpointTupleObservationV2(
            mapping.identity.table_id, mapping.binding_revision_id, mapping.content_hash,
            ("acct_no",), 3, 3, 3, 0, 0, 1),
        matched_left_distinct=2, unmatched_left_distinct=1,
        matched_right_distinct=2, unmatched_right_distinct=1,
        left_orphan_rows=1, right_orphan_rows=1, joined_row_count=7,
        max_right_matches_per_left_row=1, max_left_matches_per_right_row=6,
        normalization_ids=("identity_v1",), predicate_ids=(),
        left_source_snapshot_id="l1", right_source_snapshot_id="m1",
        snapshot_or_as_of="2026-08-04", execution_principal="crosswalk-profiler",
        method="exact", row_coverage=RowCoverage.FULL, complete=True, observed_at=NOW)
    projected = CrosswalkLegObservationV1.from_v2(
        observation, leg=SOURCE_TO_MAPPING, leg_kind="same_catalog",
        mapping_side="right", persisted=False)
    assert projected.endpoint_row_count == 9                 # the endpoint's, not the mapping's
    assert projected.endpoint_max_rows_per_tuple == 6
    assert projected.max_mapping_matches_per_endpoint_row == 1
    assert projected.max_endpoint_matches_per_mapping_row == 6
    assert projected.unmatched_endpoint_distinct == 1
    assert projected.v2_observation_revision_id is None       # not persisted, so not claimed


def test_the_v2_projection_flips_correctly_when_the_mapping_is_the_left_side(bank):
    _db, _revision, _cib, ftr, mapping = bank
    observation = RelationshipObservationV2(
        realization_revision_id="realization-1", plan_hash="plan-1", scope_id="scope-1",
        left=EndpointTupleObservationV2(
            mapping.identity.table_id, mapping.binding_revision_id, mapping.content_hash,
            ("ext_acct_ref",), 3, 3, 3, 0, 0, 1),
        right=EndpointTupleObservationV2(
            ftr.identity.table_id, ftr.binding_revision_id, ftr.content_hash,
            ("counter_party_acct_no",), 5, 5, 4, 1, 1, 2),
        matched_left_distinct=3, unmatched_left_distinct=0,
        matched_right_distinct=3, unmatched_right_distinct=1,
        left_orphan_rows=0, right_orphan_rows=1, joined_row_count=4,
        max_right_matches_per_left_row=2, max_left_matches_per_right_row=1,
        normalization_ids=("identity_v1",), predicate_ids=(),
        left_source_snapshot_id="m1", right_source_snapshot_id="r1",
        snapshot_or_as_of="2026-08-04", execution_principal="crosswalk-profiler",
        method="exact", row_coverage=RowCoverage.FULL, complete=True, observed_at=NOW)
    projected = CrosswalkLegObservationV1.from_v2(
        observation, leg=MAPPING_TO_TARGET, leg_kind="cross_catalog",
        mapping_side="left", persisted=False)
    assert projected.endpoint_row_count == 5
    assert projected.unmatched_endpoint_distinct == 1
    assert projected.max_mapping_matches_per_endpoint_row == 1
    assert projected.max_endpoint_matches_per_mapping_row == 2


# ── identity ────────────────────────────────────────────────────────────────────────────────────

def test_identity_is_the_content_and_the_json_round_trip_preserves_it(bank):
    _db, revision, cib, ftr, mapping = bank
    observation = _compose(revision, cib, ftr, mapping)
    restored = crosswalk_observation_from_json(crosswalk_observation_to_json(observation))
    assert restored == observation
    assert restored.observation_revision_id == observation.observation_revision_id
    assert observation.observation_revision_id.startswith("cwo_")


def test_a_different_measurement_is_a_different_revision_and_the_same_question(bank):
    """The scope key excludes the counts, deliberately: re-measuring the same question REPLACES."""
    _db, revision, cib, ftr, mapping = bank
    first = _compose(revision, cib, ftr, mapping)
    second = _compose(revision, cib, ftr, mapping, composed_row_count=9,
                      target_to_source_max_matches=3)
    assert first.observation_revision_id != second.observation_revision_id
    assert first.current_scope_key == second.current_scope_key


def test_a_different_pin_is_a_different_question(bank):
    _db, revision, cib, ftr, mapping = bank
    first = _compose(revision, cib, ftr, mapping)
    pinned = _compose(revision, cib, ftr, mapping, partitions=(
        ObservedPartitionPinV1(mapping.identity.table_id, "valid_from", ("2026-08-01",)),))
    assert first.current_scope_key != pinned.current_scope_key


# ── the store ───────────────────────────────────────────────────────────────────────────────────

def test_a_composed_measurement_persists_and_becomes_current(bank):
    db, revision, cib, ftr, mapping = bank
    observation = _compose(revision, cib, ftr, mapping)
    outcome = record_crosswalk_observation(db, observation)
    assert outcome.became_current is True and outcome.pointer_version == 1
    assert current_crosswalk_observation(db, observation.current_scope_key) == observation
    assert load_crosswalk_observation(db, observation.observation_revision_id) == observation


def test_recording_the_same_measurement_twice_is_idempotent(bank):
    db, revision, cib, ftr, mapping = bank
    observation = _compose(revision, cib, ftr, mapping)
    record_crosswalk_observation(db, observation)
    again = record_crosswalk_observation(db, observation)
    assert again.became_current is False and again.reason == "already_current"
    assert len(crosswalk_observation_history(db, revision.revision_id)) == 1


def test_a_weaker_measurement_never_displaces_a_stronger_one_but_stays_in_history(bank):
    """A crosswalk once proved exact does not lose the proof because somebody later ran a sample."""
    db, revision, cib, ftr, mapping = bank
    exact = _compose(revision, cib, ftr, mapping)
    record_crosswalk_observation(db, exact)
    sampled = _compose(revision, cib, ftr, mapping,
                       source={"row_coverage": RowCoverage.SAMPLED},
                       observed_at=NOW + timedelta(hours=1))
    outcome = record_crosswalk_observation(db, sampled)
    assert outcome.became_current is False
    assert outcome.reason == "weaker_or_older_observation"
    assert current_crosswalk_observation(db, exact.current_scope_key) == exact
    assert len(crosswalk_observation_history(db, revision.revision_id)) == 2


def test_an_equal_quality_newer_measurement_advances_the_pointer(bank):
    db, revision, cib, ftr, mapping = bank
    first = _compose(revision, cib, ftr, mapping)
    record_crosswalk_observation(db, first)
    second = _compose(revision, cib, ftr, mapping, composed_row_count=4,
                      observed_at=NOW + timedelta(hours=1))
    outcome = record_crosswalk_observation(db, second)
    assert outcome.became_current is True and outcome.pointer_version == 2


def test_a_stale_expected_pointer_version_does_not_advance(bank):
    db, revision, cib, ftr, mapping = bank
    record_crosswalk_observation(db, _compose(revision, cib, ftr, mapping))
    outcome = record_crosswalk_observation(
        db, _compose(revision, cib, ftr, mapping, composed_row_count=7,
                     observed_at=NOW + timedelta(hours=2)),
        expected_pointer_version=0)
    assert outcome.became_current is False and outcome.reason == "stale_current_pointer"


def test_a_leg_naming_a_two_endpoint_row_that_does_not_exist_is_refused(bank):
    db, revision, cib, ftr, mapping = bank
    observation = _compose(
        revision, cib, ftr, mapping,
        target={"v2_observation_revision_id": "rob_" + "f" * 64,
                "realization_revision_id": "realization-absent"})
    with pytest.raises(CrosswalkObservationStoreConflict, match="not persisted"):
        record_crosswalk_observation(db, observation)


# ── the agreement check, against a REAL 1038 row (both branches) ────────────────────────────────
#
# A bridge-backed leg exists in two places: embedded here in full, and as an ordinary row in the
# shipped two-endpoint store. Two places holding one truth is two places that can disagree. The
# "row is missing" branch above needs no stored row; the two below do, and only they can pin the
# JSON KEY NAMES the check reads out of `observation_json` — rename one upstream and, without
# these, the comparison silently stops comparing.


def _seed_realization(db):
    """One governed bridge realization, so a two-endpoint observation has something to hang off.

    `relationship_observation_revision.realization_revision_id` is NOT NULL with an FK to
    `bridge_join_realization_revision` (1038:7-8) — the same constraint that makes the crosswalk
    leg's row id optional in the first place."""
    from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
        _executable_pair,
    )
    from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
        _realization as build_realization,
    )

    from featuregen.data_agent.physical import record_binding_revision
    from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
    from featuregen.overlay.upload.bridge_realization import (
        BridgeRealizationCurrentV1,
        RealizationLifecycle,
        SafetyStatus,
    )
    from featuregen.overlay.upload.bridge_store import (
        BridgeDependencyRefV1,
        record_realization_revision,
    )

    left, right = _executable_pair()
    revision = build_realization(left, right)
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    record_realization_revision(
        db, revision,
        BridgeRealizationCurrentV1(
            revision.realization_id, revision.realization_revision_id,
            SafetyStatus.UNASSESSED, LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE, 1),
        dependencies=(BridgeDependencyRefV1(
            "bridge_fact", revision.bridge_fact_key, "bridge-head-1"),))
    return revision


def _persisted_target_leg_row(db, ftr, mapping, revision):
    """A REAL `rob_` row for the cross-catalog target leg, carrying the leg fixture's own numbers.

    Endpoint side = FTR (4 rows, 4 distinct tuples), mapping side = the cross-reference table, and
    `joined_row_count` 3 — exactly what `_crosswalk_fixtures.leg` embeds by default, so the two
    copies agree and the store's check has something true to confirm."""
    from featuregen.data_agent.store import record_relationship_observation

    observation = RelationshipObservationV2(
        realization_revision_id=revision.realization_revision_id,
        plan_hash="crosswalk-leg-plan", scope_id=revision.applicability_scope.scope_id,
        left=EndpointTupleObservationV2(
            ftr.identity.table_id, ftr.binding_revision_id, ftr.content_hash,
            ("counter_party_acct_no",), 4, 4, 4, 0, 0, 1),
        right=EndpointTupleObservationV2(
            mapping.identity.table_id, mapping.binding_revision_id, mapping.content_hash,
            ("ext_acct_ref",), 3, 3, 3, 0, 0, 1),
        matched_left_distinct=3, unmatched_left_distinct=1,
        matched_right_distinct=3, unmatched_right_distinct=0,
        left_orphan_rows=1, right_orphan_rows=0, joined_row_count=3,
        max_right_matches_per_left_row=1, max_left_matches_per_right_row=1,
        normalization_ids=("identity_v1",), predicate_ids=(),
        left_source_snapshot_id="endpoint-snapshot-1",
        right_source_snapshot_id="mapping-snapshot-1",
        snapshot_or_as_of="2026-08-04", execution_principal="crosswalk-profiler",
        method="exact", row_coverage=RowCoverage.FULL, complete=True, observed_at=NOW)
    record_relationship_observation(db, observation)
    return observation


def test_a_leg_that_AGREES_with_its_stored_two_endpoint_row_is_recorded(bank):
    """The happy branch, against a real 1038 row rather than an absence. This is also what pins the
    JSON key names (`left`/`right`, `binding_revision_id`, `columns`, `row_count`,
    `distinct_tuple_count`, `joined_row_count`) that the agreement check reads."""
    db, revision, cib, ftr, mapping = bank
    realization = _seed_realization(db)
    stored = _persisted_target_leg_row(db, ftr, mapping, realization)

    observation = _compose(
        revision, cib, ftr, mapping,
        target={"v2_observation_revision_id": stored.observation_revision_id,
                "realization_revision_id": realization.realization_revision_id})
    outcome = record_crosswalk_observation(db, observation)
    assert outcome.became_current is True
    # The composition's `rob_` id is a real, resolvable row — the FK holds it there.
    assert observation.leg_observation_revision_ids == (stored.observation_revision_id,)
    assert db.execute(
        "SELECT target_leg_observation_revision_id, source_leg_observation_revision_id "
        "FROM crosswalk_observation_revision WHERE observation_revision_id = %s",
        (observation.observation_revision_id,)).fetchone() == (
            stored.observation_revision_id, None)


def test_a_leg_that_DIVERGES_from_its_stored_two_endpoint_row_is_corruption(bank):
    """One truth in two places is two places that can disagree, and a disagreement is not a caller
    error to be tolerated: one of the two is not what was measured, and nothing downstream can say
    which. Nothing is written — the check runs before the insert."""
    from featuregen.data_agent.store import RelationshipObservationStoreCorruption

    db, revision, cib, ftr, mapping = bank
    realization = _seed_realization(db)
    stored = _persisted_target_leg_row(db, ftr, mapping, realization)
    assert stored.left.row_count == 4

    tampered = _compose(
        revision, cib, ftr, mapping,
        target={"v2_observation_revision_id": stored.observation_revision_id,
                "realization_revision_id": realization.realization_revision_id,
                "endpoint_row_count": 99})
    with pytest.raises(RelationshipObservationStoreCorruption, match=MAPPING_TO_TARGET):
        record_crosswalk_observation(db, tampered)
    assert db.execute(
        "SELECT count(*) FROM crosswalk_observation_revision").fetchone()[0] == 0


def test_current_measurements_are_listed_per_definition_revision(bank):
    db, revision, cib, ftr, mapping = bank
    unscoped = _compose(revision, cib, ftr, mapping)
    scoped = _compose(revision, cib, ftr, mapping, scope_id="crosswalk-production-scope")
    record_crosswalk_observation(db, unscoped)
    record_crosswalk_observation(db, scoped)
    current = current_crosswalk_observations_for_revision(db, revision.revision_id)
    assert {item.scope_id for item in current} == {
        "crosswalk-sandbox-scope", "crosswalk-production-scope"}


def test_an_unmeasured_crosswalk_has_no_observation_and_that_is_not_a_failure(bank):
    """"Discoverable, unmeasured" is a state, not an error — the no-blocked rule."""
    db, revision, _cib, _ftr, _mapping = bank
    assert current_crosswalk_observations_for_revision(db, revision.revision_id) == ()
    assert crosswalk_observation_history(db, revision.revision_id) == ()
