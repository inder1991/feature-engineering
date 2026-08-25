"""A4: BridgeRealizationSnapshotV1 — one frozen, batched read of realization state (migration 1131).

What these tests pin, in the task's order: the migration applies (session fixture applies all
migrations); a CONSTANT query count regardless of considered-set size (the counting-cursor idiom,
N=1 vs N=20); content-addressed determinism (same seeded state twice → same snapshot_id, one row);
deterministic CAP truncation (the SAME truncated keys both runs, cause "cap"); DEADLINE truncation
(an already-expired budget → cause "deadline" over the same stable order); append-only physics;
the persist→load round trip (assessment-relevant fields equal); the complete-flag law (a truncated
snapshot loads with ``complete=False``); typed refusals BEFORE any SQL; and the CompileBudget
extension semantics (``remaining`` decremented by what was captured; ``stopped_by_time`` remembers
which bound fired first — the shadow-planner convention, reused not reinvented).
"""
from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)

from featuregen.data_agent.physical import record_binding_revision
from featuregen.overlay.upload.bridge_assessment import read_overlay_identifier_link_state
from featuregen.overlay.upload.bridge_realization import (
    BridgeRealizationCurrentV1,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    LinkReviewStatus,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_realization_proposal import FEATURE_GENERATION_PURPOSE
from featuregen.overlay.upload.bridge_realization_snapshot import (
    SNAPSHOT_ID_PREFIX,
    TRUNCATION_CAP,
    TRUNCATION_CAP_AND_DEADLINE,
    TRUNCATION_DEADLINE,
    TRUNCATION_NONE,
    BridgeRealizationSnapshotV1,
    ConsideredBridgeV1,
    SnapshotContractDefect,
    build_bridge_realization_snapshot,
    load_bridge_realization_snapshot,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    append_realization_revision,
    bridge_dependency_snapshot_id,
    record_realization_revision,
)
from featuregen.overlay.upload.planner.declarations import CompileBudget
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality


class _NeverTouched:
    """A probe standing in for a connection: ANY attribute access fails the test, proving the
    refusal happened BEFORE SQL."""

    def __getattr__(self, name):  # pragma: no cover - reaching here IS the failure
        raise AssertionError(f"the builder touched the connection ({name!r}) before validating")


def _budget(*, remaining: int = 100, expired: bool = False) -> CompileBudget:
    """A deterministic budget: the injected clock never moves, so ONLY the seeded expiry state
    decides whether the deadline bites — never the test host's wall clock."""
    return CompileBudget(
        remaining=remaining,
        deadline_monotonic=0.0 if expired else 1_000_000.0,
        clock=lambda: 1.0,
    )


def _considered(*keys: str, pins: dict[str, str] | None = None) -> tuple[ConsideredBridgeV1, ...]:
    pins = pins or {}
    return tuple(
        ConsideredBridgeV1(bridge_fact_key=key,
                           pinned_realization_revision_id=pins.get(key))
        for key in keys
    )


def _build(db, bridges, **overrides):
    values = dict(
        bridges=bridges,
        execution_tier=ExecutionTier.SANDBOX,
        purpose=FEATURE_GENERATION_PURPOSE,
        budget=_budget(),
    )
    values.update(overrides)
    return build_bridge_realization_snapshot(db, **values)


def _seed_fact(db, key: str) -> None:
    govern_bridge_fact(
        db, key, entity="customer",
        left_source="cib", left_ref="public.customers.customer_id",
        right_source="ftr", right_ref="public.transactions.customer_id",
        status="DRAFT")


def _seed_realization(db, key: str, *, publish: bool = False,
                      cardinality: Cardinality | None = Cardinality.MANY_TO_ONE):
    """One stored realization revision for ``key`` — appended (the provisional half) or
    CAS-published with a current pointer. Bindings and their revisions really exist."""
    left, right = _executable_pair()
    assert left.physical_binding is not None and right.physical_binding is not None
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    _seed_fact(db, key)
    head = read_overlay_identifier_link_state(db, key).overlay_head_event_id
    assert head is not None
    dependencies = (
        BridgeDependencyRefV1("bridge_fact", key, head),
        BridgeDependencyRefV1(
            "physical_binding",
            left.physical_binding.binding_id, left.physical_binding.binding_revision_id),
        BridgeDependencyRefV1(
            "physical_binding",
            right.physical_binding.binding_id, right.physical_binding.binding_revision_id),
    )
    base = _realization(
        left, right,
        cardinality=(DirectionalCardinalityVerdictV1(cardinality)
                     if cardinality is not None
                     else DirectionalCardinalityVerdictV1.unknown()))
    revision = replace(
        base,
        bridge_fact_key=key,
        dependency_snapshot_id=bridge_dependency_snapshot_id(dependencies),
    )
    if publish:
        record_realization_revision(
            db, revision,
            BridgeRealizationCurrentV1(
                realization_id=revision.realization_id,
                realization_revision_id=revision.realization_revision_id,
                safety_status=SafetyStatus.DETERMINISTICALLY_VALIDATED,
                review_status=LinkReviewStatus.UNREVIEWED,
                lifecycle=RealizationLifecycle.ACTIVE,
                pointer_version=1),
            dependencies=dependencies)
    else:
        append_realization_revision(db, revision, dependencies=dependencies)
    return revision


# ── the migration applies ──────────────────────────────────────────────────────────────────────


def test_the_1131_table_exists_and_starts_empty_shaped(db) -> None:
    row = db.execute(
        "SELECT snapshot_id, execution_context_revision_id, execution_tier, purpose, captured, "
        "truncation, content_hash, recorded_at FROM bridge_realization_snapshot LIMIT 0"
    ).fetchone()
    assert row is None


# ── constant query count (the reviewer-proven per-row pattern is what this replaces) ───────────


def test_constant_query_count_n1_vs_n20(db) -> None:
    keys = tuple(f"bfk-a4-{index:02d}" for index in range(1, 21))
    _seed_realization(db, keys[0], publish=True)
    for key in keys[1:]:
        _seed_fact(db, key)

    def count(bridges) -> int:
        calls: list[str] = []
        original = db.execute

        def counting(query, *args, **kwargs):
            calls.append(str(query))
            return original(query, *args, **kwargs)

        db.execute = counting
        try:
            _build(db, bridges)
        finally:
            db.execute = original
        return len(calls)

    one = count(_considered(keys[0]))
    twenty = count(_considered(*keys))
    assert one == twenty, (one, twenty)
    # The exact inventory, pinned (the test_column_capabilities discipline): pinned revisions,
    # current pointers, dependency rows, candidate currentness, the events fold, binding
    # revisions, the INSERT and the content-verified read-back.
    assert one == 8, one


# ── determinism + content addressing ───────────────────────────────────────────────────────────


def test_same_seeded_state_twice_same_snapshot_id_one_row(db) -> None:
    revision = _seed_realization(db, "bfk-a4-det", publish=True)
    considered = _considered(
        "bfk-a4-det", pins={"bfk-a4-det": revision.realization_revision_id})
    first = _build(db, considered)
    second = _build(db, considered)
    assert first.snapshot_id == second.snapshot_id
    assert first.snapshot_id.startswith(SNAPSHOT_ID_PREFIX)
    assert first.entries == second.entries
    assert db.execute(
        "SELECT count(*) FROM bridge_realization_snapshot WHERE snapshot_id = %s",
        (first.snapshot_id,)).fetchone()[0] == 1


def test_cap_truncation_is_deterministic_and_disclosed(db) -> None:
    keys = tuple(f"bfk-a4-cap-{index}" for index in range(1, 6))
    for key in keys:
        _seed_fact(db, key)
    runs = []
    for _ in range(2):
        budget = _budget(remaining=3)
        runs.append((_build(db, _considered(*keys), budget=budget), budget))
    (first, first_budget), (second, _) = runs
    assert first.snapshot_id == second.snapshot_id
    assert first.truncation.cause == TRUNCATION_CAP
    assert first.truncation.cap_value == 3
    # The pinned order is lexical (bridge_fact_key, pin) — first 3 captured, SAME 2 truncated.
    assert tuple(entry.bridge_fact_key for entry in first.entries) == keys[:3]
    assert first.truncation.truncated_bridge_keys == keys[3:]
    assert second.truncation.truncated_bridge_keys == keys[3:]
    assert not first.complete
    # The CompileBudget extension: the count bound fired (False, per the shadow convention),
    # and the allowance was consumed by what was actually captured.
    assert first_budget.stopped_by_time is False
    assert first_budget.remaining == 0


def test_deadline_truncation_is_deterministic_and_named(db) -> None:
    keys = tuple(f"bfk-a4-ddl-{index}" for index in range(1, 4))
    for key in keys:
        _seed_fact(db, key)
    budget = _budget(expired=True)
    snapshot = _build(db, _considered(*keys), budget=budget)
    assert snapshot.truncation.cause == TRUNCATION_DEADLINE
    # The truncated LIST is the stable-order admitted set — deterministic even though WHERE a
    # deadline falls is time-dependent.
    assert snapshot.truncation.truncated_bridge_keys == keys
    assert snapshot.entries == ()
    assert not snapshot.complete
    assert snapshot.truncation.elapsed_note
    assert budget.stopped_by_time is True

    # A second expired run reproduces the same snapshot identity: the wall-clock note is
    # disclosure, never identity material.
    again = _build(db, _considered(*keys), budget=_budget(expired=True))
    assert again.snapshot_id == snapshot.snapshot_id


def test_cap_and_deadline_together_disclose_both_lists(db) -> None:
    keys = tuple(f"bfk-a4-both-{index}" for index in range(1, 6))
    for key in keys:
        _seed_fact(db, key)
    snapshot = _build(db, _considered(*keys), budget=_budget(remaining=3, expired=True))
    assert snapshot.truncation.cause == TRUNCATION_CAP_AND_DEADLINE
    assert snapshot.truncation.cap_truncated_bridge_keys == keys[3:]
    assert snapshot.truncation.deadline_truncated_bridge_keys == keys[:3]
    assert snapshot.truncation.truncated_bridge_keys == keys
    assert not snapshot.complete


# ── append-only physics ────────────────────────────────────────────────────────────────────────


def test_update_delete_truncate_refuse(db) -> None:
    _seed_fact(db, "bfk-a4-ap")
    _build(db, _considered("bfk-a4-ap"))
    for statement in (
        "UPDATE bridge_realization_snapshot SET recorded_at = now()",
        "DELETE FROM bridge_realization_snapshot",
        "TRUNCATE bridge_realization_snapshot",
    ):
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            with db.transaction():
                db.execute(statement)
        assert "append-only" in str(excinfo.value)


def test_the_named_checks_refuse_off_vocabulary_rows(db) -> None:
    for column, value in (("execution_tier", "staging"), ("purpose", "serving")):
        row = {"snapshot_id": "brsnap_" + "e" * 64, "execution_tier": "sandbox",
               "purpose": "feature_generation", "content_hash": "e" * 64}
        row[column] = value
        with pytest.raises(psycopg.errors.CheckViolation):
            with db.transaction():
                db.execute(
                    "INSERT INTO bridge_realization_snapshot (snapshot_id, execution_tier, "
                    "purpose, captured, truncation, content_hash) "
                    "VALUES (%s, %s, %s, '[]'::jsonb, '{}'::jsonb, %s)",
                    (row["snapshot_id"], row["execution_tier"], row["purpose"],
                     row["content_hash"]))


# ── round trip + the complete-flag law ─────────────────────────────────────────────────────────


def test_round_trip_persist_load_assessment_fields_equal(db) -> None:
    pinned = _seed_realization(db, "bfk-a4-rt-pin", publish=False,
                               cardinality=None)          # provisional: unknown cardinality
    published = _seed_realization(db, "bfk-a4-rt-pub", publish=True)
    _seed_fact(db, "bfk-a4-rt-bare")                       # a link with NO realization at all
    considered = _considered(
        "bfk-a4-rt-pin", "bfk-a4-rt-pub", "bfk-a4-rt-bare",
        pins={"bfk-a4-rt-pin": pinned.realization_revision_id})
    built = _build(db, considered)
    loaded = load_bridge_realization_snapshot(db, built.snapshot_id)
    assert loaded is not None
    assert isinstance(loaded, BridgeRealizationSnapshotV1)
    assert loaded.snapshot_id == built.snapshot_id
    assert loaded.entries == built.entries
    assert loaded.truncation == built.truncation
    assert loaded.execution_tier is ExecutionTier.SANDBOX
    assert loaded.purpose == FEATURE_GENERATION_PURPOSE
    assert loaded.complete and built.complete
    assert loaded.truncation.cause == TRUNCATION_NONE
    assert loaded.recorded_at is not None

    by_key = {entry.bridge_fact_key: entry for entry in loaded.entries}
    pin_entry = by_key["bfk-a4-rt-pin"]
    assert pin_entry.pin_found is True
    assert pin_entry.realization_revision_id == pinned.realization_revision_id
    assert pin_entry.cardinality == "unknown"
    assert pin_entry.safety_status is None                 # honest absence: no pointer row
    assert pin_entry.from_binding_revision_stored and pin_entry.to_binding_revision_stored
    assert pin_entry.dependency_snapshot_agrees is True
    assert pin_entry.overlay_head_event_id is not None     # A2's currentness pin, captured
    assert pin_entry.scope_execution_tier == ExecutionTier.SANDBOX.value
    assert FEATURE_GENERATION_PURPOSE in pin_entry.scope_purposes

    pub_entry = by_key["bfk-a4-rt-pub"]
    assert pub_entry.realization_revision_id == published.realization_revision_id
    assert pub_entry.safety_status == SafetyStatus.DETERMINISTICALLY_VALIDATED.value
    assert pub_entry.lifecycle == RealizationLifecycle.ACTIVE.value
    assert pub_entry.cardinality == Cardinality.MANY_TO_ONE.value

    bare = by_key["bfk-a4-rt-bare"]
    assert bare.realization_revision_id is None            # absence recorded, never invented
    assert bare.pin_found is None
    assert bare.link_available is True
    assert bare.overlay_head_event_id is not None


def test_a_truncated_snapshot_loads_with_complete_false(db) -> None:
    keys = tuple(f"bfk-a4-inc-{index}" for index in range(1, 4))
    for key in keys:
        _seed_fact(db, key)
    built = _build(db, _considered(*keys), budget=_budget(remaining=1))
    loaded = load_bridge_realization_snapshot(db, built.snapshot_id)
    assert loaded is not None
    assert loaded.complete is False
    assert loaded.truncation.cause == TRUNCATION_CAP
    assert loaded.truncation.truncated_bridge_keys == keys[1:]


def test_load_of_missing_snapshot_returns_none(db) -> None:
    assert load_bridge_realization_snapshot(db, SNAPSHOT_ID_PREFIX + "0" * 64) is None


# ── pin resolution ─────────────────────────────────────────────────────────────────────────────


def test_a_missing_pin_is_recorded_as_not_found_never_invented(db) -> None:
    _seed_fact(db, "bfk-a4-ghost")
    snapshot = _build(db, _considered(
        "bfk-a4-ghost", pins={"bfk-a4-ghost": "brvr_" + "0" * 64}))
    (entry,) = snapshot.entries
    assert entry.pin_found is False
    assert entry.realization_revision_id is None
    assert snapshot.complete                               # absence is a captured FACT, not truncation


def test_an_unpinned_bridge_resolves_its_active_current_pointer(db) -> None:
    published = _seed_realization(db, "bfk-a4-cur", publish=True)
    snapshot = _build(db, _considered("bfk-a4-cur"))
    (entry,) = snapshot.entries
    assert entry.pin_found is None
    assert entry.realization_revision_id == published.realization_revision_id
    assert entry.current_pointer_revision_id == published.realization_revision_id
    assert entry.current_realization_ids == (published.realization_id,)


# ── typed refusals, BEFORE any SQL ─────────────────────────────────────────────────────────────


def test_empty_considered_set_refuses_before_sql() -> None:
    with pytest.raises(SnapshotContractDefect):
        build_bridge_realization_snapshot(
            _NeverTouched(), bridges=(),
            execution_tier=ExecutionTier.SANDBOX, purpose=FEATURE_GENERATION_PURPOSE,
            budget=_budget())


@pytest.mark.parametrize("tier", ["STAGING", "Sandbox", "", None])
def test_unknown_tier_refuses_before_sql(tier) -> None:
    with pytest.raises(SnapshotContractDefect):
        build_bridge_realization_snapshot(
            _NeverTouched(), bridges=_considered("bfk-a4-x"),
            execution_tier=tier, purpose=FEATURE_GENERATION_PURPOSE, budget=_budget())


@pytest.mark.parametrize("purpose", ["serving", "feature_generation ", "", None])
def test_unknown_purpose_refuses_before_sql(purpose) -> None:
    with pytest.raises(SnapshotContractDefect):
        build_bridge_realization_snapshot(
            _NeverTouched(), bridges=_considered("bfk-a4-x"),
            execution_tier=ExecutionTier.SANDBOX, purpose=purpose, budget=_budget())


def test_blank_fact_key_refuses_at_construction() -> None:
    with pytest.raises(SnapshotContractDefect):
        ConsideredBridgeV1(bridge_fact_key="   ")


def test_duplicate_exact_considered_items_collapse_to_one_entry(db) -> None:
    _seed_fact(db, "bfk-a4-dup")
    snapshot = _build(
        db, _considered("bfk-a4-dup") + _considered("bfk-a4-dup"))
    assert len(snapshot.entries) == 1
