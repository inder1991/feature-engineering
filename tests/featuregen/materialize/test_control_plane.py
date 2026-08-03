"""Spec §12 — ``RunManifestV1``, the run-event vocabulary, the status fold and their ingestion.

Three properties are load-bearing and each has a test that fails if it stops holding:

1. **The plane is append-only in the code as well as the schema.** Migration 1034 blocks UPDATE,
   DELETE and TRUNCATE; this module must not attempt any of them, or every write path would be one
   trigger away from an exception nobody expects.
2. **``status`` is FOLDED, not stored.** An append-only row cannot hold a field that moves.
3. **The control plane never reads feature data.** ``RunManifestV1``'s field list is pinned
   field-for-field, so a value-carrying field cannot be added quietly.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import re

import psycopg
import pytest
from tests.featuregen.materialize.test_group_plan import _contract as _real_contract
from tests.featuregen.materialize.test_group_plan import _plan as _real_plan

from featuregen.materialize import control_plane
from featuregen.materialize.binding import (
    GroupContractBinding,
    GroupPlanRevision,
    current_plan_revision,
)
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.contract import contract_hash
from featuregen.materialize.control_plane import (
    RUN_MANIFEST_FIELDS,
    TERMINAL_RUN_EVENT_KINDS,
    CompiledArtifactV1,
    MaterializationGeneration,
    MaterializationRunEvent,
    RunEventKind,
    RunManifestV1,
    RunStatus,
    append_run_event,
    fold_run_status,
    published_generation_ids,
    read_compiled_artifact,
    read_group_binding,
    read_plan_revisions,
    read_run_events,
    read_run_manifest,
    record_compiled_artifact,
    record_generation,
    record_group_binding,
    record_plan_revision,
    record_run_manifest,
    run_status,
)
from featuregen.materialize.group_plan import group_plan_hash

GEN = "gen-cp"
RUN = "run-cp"
ATT = "att-cp"
T0 = "2026-07-28T09:00:00+00:00"
T1 = "2026-07-28T10:00:00+00:00"
T2 = "2026-07-28T11:00:00+00:00"


def _generation(generation_id: str = GEN) -> MaterializationGeneration:
    return MaterializationGeneration(
        generation_id=generation_id, logical_group_name="cif_daily",
        materialization_contract_hash="ct-hash", group_plan_hash="gp-hash",
        generated_project_hash="proj-hash", created_at=T0)


def _event(seq: int, kind: RunEventKind, *, run_id: str = RUN, occurred_at: str = T1,
           generation_id: str = GEN, detail: str = "") -> MaterializationRunEvent:
    return MaterializationRunEvent(run_id=run_id, seq=seq, generation_id=generation_id,
                                   event_kind=kind, occurred_at=occurred_at, detail=detail)


def _manifest(**overrides) -> RunManifestV1:
    values = dict(
        run_id=RUN, generation_id=GEN, group_plan_hash="gp-hash",
        materialization_contract_hash="ct-hash", generated_project_hash="proj-hash",
        sandbox_execution_hash="exec-hash", business_dt="2026-07-27",
        publication_mechanism="VERSIONED_POINTER", capability_attestation_id=ATT,
        expected_feature_columns=("total_debit_amount_30d", "distinct_merchant_count_90d"),
        staged_row_count=10, published_row_count=10, schema_hash="schema-hash",
        key_uniqueness_result="unique", required_column_result="present",
        orphan_grain_key_count=0, publication_location="sandbox_feature.cif_daily/v3",
        started_at=T1, published_at=T2, status=RunStatus.PUBLISHED)
    values.update(overrides)
    return RunManifestV1(**values)


def _attestation(conn, attestation_id: str = ATT) -> str:
    """Seeded directly: `record_attestation` is Task 16's and "accepts nothing else" than a probe
    result, so inventing a writer here would invent the shape a task early."""
    conn.execute(
        "INSERT INTO publication_capability_attestation (attestation_id, environment_id, "
        "hive_version, spark_version, metastore_version, mechanism, passed, "
        "covers_schema_evolution, evidence_hash, attested_at) VALUES (%s, 'env-1', '3.1.3', "
        "'4.2.0', '3.1.3', 'VERSIONED_POINTER', true, true, 'ev-hash', %s)",
        (attestation_id, T0))
    return attestation_id


# ── 1. the shapes ────────────────────────────────────────────────────────────────────────────────


def test_RunManifestV1_carries_EXACTLY_spec_12s_fields_in_order() -> None:
    """`==`, never a superset. This list is the whole defence against a data value entering the
    control plane: every member is an identity, a count, a location or a verdict, and an extra
    field is how "counts, types, hashes and locations only" would quietly stop being true."""
    assert RUN_MANIFEST_FIELDS == (
        "run_id", "generation_id", "group_plan_hash", "materialization_contract_hash",
        "generated_project_hash", "sandbox_execution_hash", "business_dt",
        "publication_mechanism", "capability_attestation_id", "expected_feature_columns",
        "staged_row_count", "published_row_count", "schema_hash", "key_uniqueness_result",
        "required_column_result", "orphan_grain_key_count", "publication_location",
        "started_at", "published_at", "status")


@pytest.mark.parametrize("record",
                         [MaterializationGeneration, MaterializationRunEvent, RunManifestV1,
                          CompiledArtifactV1])
def test_every_record_is_a_frozen_slotted_dataclass(record) -> None:
    assert dataclasses.is_dataclass(record)
    params = record.__dataclass_params__
    assert params.frozen, f"{record.__name__} is not frozen"
    assert getattr(record, "__slots__", None) is not None, f"{record.__name__} is not slotted"


def test_a_recorded_manifest_cannot_be_mutated_in_place() -> None:
    manifest = _manifest()
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.status = RunStatus.FAILED       # type: ignore[misc]


def _executed_sql() -> list[str]:
    """Every SQL literal this module passes to `.execute`, read from the AST.

    The AST, not a regex over the source: the module's own prose says "rejects UPDATE, DELETE and
    TRUNCATE" a dozen times, and a text scan finds those words in the docstring and reports the
    documentation as the defect.
    """
    tree = ast.parse(inspect.getsource(control_plane))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "execute"]
    statements = [call.args[0].value for call in calls
                  if call.args and isinstance(call.args[0], ast.Constant)]
    assert len(statements) == len(calls) > 0, \
        "an .execute() call passes SQL this test cannot read — the scan is blind, not clean"
    return statements


def test_the_module_issues_no_UPDATE_DELETE_or_TRUNCATE() -> None:
    """The schema blocks all three; the code must not attempt them either. A writer that issued one
    would be a write path that works right up until the trigger fires."""
    for sql in _executed_sql():
        upper = " ".join(sql.upper().split())
        assert upper.startswith(("INSERT", "SELECT")), f"not an append or a read: {sql!r}"
        for banned in ("UPDATE", "DELETE", "TRUNCATE", "ON CONFLICT"):
            assert banned not in upper, f"destructive SQL in the control plane: {sql!r}"


def test_the_module_reads_no_table_outside_the_control_plane() -> None:
    """"The control plane never reads feature data" is only true if it never reads anything else.
    Every table it touches is one migration 1034 created."""
    plane = {"materialization_generation", "materialization_run_event",
             "materialization_run_manifest", "materialization_compiled_artifact",
             "group_binding", "group_plan_revision"}
    for sql in _executed_sql():
        for table in re.findall(r"(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_][a-z0-9_]*)", sql):
            assert table in plane, f"the control plane touches {table!r}: {sql!r}"


# ── 2. the closed vocabularies ───────────────────────────────────────────────────────────────────


def test_RunEventKind_is_closed() -> None:
    assert {kind.value for kind in RunEventKind} == {
        "RUN_PREPARED", "RUN_SUBMITTED", "COMPUTATION_COMPLETED", "GATES_PASSED",
        "GATES_FAILED", "PUBLISHED", "PUBLICATION_REFUSED", "RUN_FAILED"}


def test_RunStatus_is_closed() -> None:
    assert {status.value for status in RunStatus} == {
        "prepared", "submitted", "computed", "validated", "rejected", "published", "refused",
        "failed"}


def test_the_terminal_kinds_are_exactly_the_four_that_end_a_run() -> None:
    assert TERMINAL_RUN_EVENT_KINDS == frozenset({
        RunEventKind.GATES_FAILED, RunEventKind.PUBLISHED, RunEventKind.PUBLICATION_REFUSED,
        RunEventKind.RUN_FAILED})


def test_every_kind_maps_to_a_status_and_every_status_is_reachable() -> None:
    """A kind with no status could be appended and then never folded; a status no kind produces is
    a state the plane claims to have and cannot record."""
    produced = {_event(0, kind).status() for kind in RunEventKind}
    assert produced == set(RunStatus)


def test_is_terminal_agrees_between_the_kind_and_the_status_it_folds_to() -> None:
    for kind in RunEventKind:
        assert _event(0, kind).is_terminal() is _event(0, kind).status().is_terminal()


# ── 3. the fold ──────────────────────────────────────────────────────────────────────────────────


def test_the_fold_reports_the_LAST_event() -> None:
    events = (_event(0, RunEventKind.RUN_PREPARED), _event(1, RunEventKind.RUN_SUBMITTED),
              _event(2, RunEventKind.COMPUTATION_COMPLETED), _event(3, RunEventKind.GATES_PASSED),
              _event(4, RunEventKind.PUBLISHED))
    assert fold_run_status(events) == RunStatus.PUBLISHED
    assert fold_run_status(events[:2]) == RunStatus.SUBMITTED


def test_the_fold_orders_by_seq_NOT_by_occurred_at() -> None:
    """`occurred_at` is a wall-clock reading from whichever host observed the moment and can run
    backwards between hosts; `seq` is the order the database holds unique. Here they disagree —
    the later event carries the EARLIER clock — and seq must win."""
    events = (_event(0, RunEventKind.RUN_PREPARED, occurred_at=T2),
              _event(1, RunEventKind.PUBLISHED, occurred_at=T0))
    assert fold_run_status(events) == RunStatus.PUBLISHED
    assert fold_run_status(tuple(reversed(events))) == RunStatus.PUBLISHED


def test_the_fold_refuses_a_run_with_no_events() -> None:
    with pytest.raises(ValueError, match="no events"):
        fold_run_status(())


def test_the_fold_refuses_events_from_two_runs() -> None:
    with pytest.raises(ValueError, match="2 runs"):
        fold_run_status((_event(0, RunEventKind.RUN_PREPARED),
                         _event(1, RunEventKind.PUBLISHED, run_id="other")))


def test_the_fold_refuses_a_repeated_seq() -> None:
    with pytest.raises(ValueError, match="sharing a seq"):
        fold_run_status((_event(0, RunEventKind.RUN_PREPARED),
                         _event(0, RunEventKind.PUBLISHED)))


def test_the_fold_refuses_an_event_after_a_terminal_one() -> None:
    """A run that ended and then carried on is a corrupt record. Reporting the last event's status
    would state a run had resumed; reporting the terminal one would hide the corruption."""
    with pytest.raises(ValueError, match="then continues"):
        fold_run_status((_event(0, RunEventKind.PUBLISHED),
                         _event(1, RunEventKind.RUN_SUBMITTED)))


# ── 4. what the records refuse ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["generation_id", "logical_group_name",
                                   "materialization_contract_hash", "group_plan_hash",
                                   "generated_project_hash"])
def test_a_generation_refuses_a_blank_identity(field: str) -> None:
    values = dataclasses.asdict(_generation()) | {field: "  "}
    with pytest.raises(ValueError, match=field):
        MaterializationGeneration(**values)


def test_a_generation_refuses_a_naive_created_at() -> None:
    with pytest.raises(ValueError, match="no UTC offset"):
        MaterializationGeneration(**(dataclasses.asdict(_generation())
                                     | {"created_at": "2026-07-28T09:00:00"}))


def test_an_event_coerces_a_plain_string_kind_to_the_enum() -> None:
    """The database returns text. A manifest whose kind stayed a str would be a second spelling the
    fold's dispatch table does not contain."""
    event = MaterializationRunEvent(run_id=RUN, seq=0, generation_id=GEN,
                                    event_kind="PUBLISHED", occurred_at=T1)  # type: ignore[arg-type]
    assert event.event_kind is RunEventKind.PUBLISHED


def test_an_event_refuses_an_unknown_kind() -> None:
    with pytest.raises(ValueError):
        MaterializationRunEvent(run_id=RUN, seq=0, generation_id=GEN,
                                event_kind="MADE_UP", occurred_at=T1)  # type: ignore[arg-type]


@pytest.mark.parametrize("seq", [-1, True, 1.5, "0"])
def test_an_event_refuses_a_seq_that_is_not_a_whole_non_negative_number(seq) -> None:
    with pytest.raises(ValueError, match="seq"):
        _event(seq, RunEventKind.RUN_PREPARED)


def test_a_manifest_refuses_a_NON_terminal_status() -> None:
    with pytest.raises(ValueError, match="written ONCE"):
        _manifest(status=RunStatus.SUBMITTED)


@pytest.mark.parametrize("missing", ["published_at", "publication_location",
                                     "published_row_count"])
def test_a_manifest_claiming_published_must_say_when_where_and_how_many(missing: str) -> None:
    with pytest.raises(ValueError, match="published"):
        _manifest(**{missing: None})


def test_a_non_published_manifest_may_leave_the_publication_fields_empty() -> None:
    manifest = _manifest(status=RunStatus.REJECTED, published_at=None,
                         publication_location=None, published_row_count=None)
    assert manifest.status is RunStatus.REJECTED


def test_a_manifest_refuses_an_empty_expected_column_set() -> None:
    with pytest.raises(ValueError, match="expected_feature_columns"):
        _manifest(expected_feature_columns=())


@pytest.mark.parametrize("field", ["staged_row_count", "published_row_count",
                                   "orphan_grain_key_count"])
def test_a_manifest_refuses_a_negative_count(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _manifest(**{field: -1})


def test_a_manifest_normalizes_its_expected_columns_to_a_tuple() -> None:
    """The database returns an array as a list; an unhashable, mutable field on a frozen record is
    a record that is frozen in name only."""
    assert _manifest(expected_feature_columns=["a", "b"]).expected_feature_columns == ("a", "b")


# ── 5. ingestion, against the real tables ────────────────────────────────────────────────────────


def test_events_round_trip_and_the_status_folds_from_the_database(conn) -> None:
    record_generation(conn, _generation())
    for seq, kind in enumerate((RunEventKind.RUN_PREPARED, RunEventKind.RUN_SUBMITTED,
                                RunEventKind.COMPUTATION_COMPLETED, RunEventKind.GATES_PASSED,
                                RunEventKind.PUBLISHED)):
        append_run_event(conn, _event(seq, kind, detail=f"step {seq}"))

    events = read_run_events(conn, RUN)
    assert [event.event_kind for event in events] == [
        RunEventKind.RUN_PREPARED, RunEventKind.RUN_SUBMITTED,
        RunEventKind.COMPUTATION_COMPLETED, RunEventKind.GATES_PASSED, RunEventKind.PUBLISHED]
    assert events[0].detail == "step 0"
    assert run_status(conn, RUN) == RunStatus.PUBLISHED
    assert run_status(conn, RUN) == "published"      # the StrEnum value Task 17 asserts against


def test_run_status_refuses_a_run_nobody_recorded(conn) -> None:
    """Fail closed: a run with no events has no status, and answering with a member would report a
    state about a run the plane has never seen."""
    with pytest.raises(ValueError, match="no events"):
        run_status(conn, "never-recorded")


def test_a_second_event_at_the_same_seq_is_REFUSED_by_the_database(conn) -> None:
    """`append_run_event` does not compute seq — that read-modify-write is a race two appenders can
    both win. Since 1044 the ordering trigger refuses the non-extending seq BEFORE INSERT, so this
    is the refusal a caller sees; the unique key remains the arbiter of the true concurrent race
    the trigger's reads cannot see (proven in test_migration_1034)."""
    record_generation(conn, _generation())
    append_run_event(conn, _event(0, RunEventKind.RUN_PREPARED))
    with pytest.raises(psycopg.errors.RaiseException, match="does not extend"), conn.transaction():
        append_run_event(conn, _event(0, RunEventKind.RUN_SUBMITTED))


def test_a_second_terminal_event_is_REFUSED_by_the_database(conn) -> None:
    """Since 1044 the ordering trigger refuses ANY event after a terminal one BEFORE INSERT; the
    one-terminal partial index remains the concurrent-race backstop (test_migration_1034)."""
    record_generation(conn, _generation())
    append_run_event(conn, _event(0, RunEventKind.PUBLISHED))
    with pytest.raises(psycopg.errors.RaiseException, match="terminal"), conn.transaction():
        append_run_event(conn, _event(1, RunEventKind.RUN_FAILED))


def test_the_manifest_round_trips_field_for_field(conn) -> None:
    record_generation(conn, _generation())
    _attestation(conn)
    manifest = _manifest()
    record_run_manifest(conn, manifest)
    assert read_run_manifest(conn, RUN) == manifest


def test_a_manifest_with_no_counts_round_trips_its_Nones(conn) -> None:
    """`staged_row_count`, `schema_hash` and the gate results are `| None` in §12 — a run that never
    staged has no count, and a zero would be a claim about an output nobody produced."""
    record_generation(conn, _generation())
    _attestation(conn)
    manifest = _manifest(status=RunStatus.FAILED, staged_row_count=None, published_row_count=None,
                         schema_hash=None, key_uniqueness_result=None,
                         required_column_result=None, orphan_grain_key_count=None,
                         publication_location=None, started_at=None, published_at=None)
    record_run_manifest(conn, manifest)
    assert read_run_manifest(conn, RUN) == manifest


def test_reading_a_manifest_a_run_never_wrote_yields_None(conn) -> None:
    assert read_run_manifest(conn, "no-such-run") is None


def test_a_second_manifest_for_a_run_is_REFUSED(conn) -> None:
    record_generation(conn, _generation())
    _attestation(conn)
    record_run_manifest(conn, _manifest())
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        record_run_manifest(conn, _manifest(status=RunStatus.FAILED, published_at=None,
                                            publication_location=None, published_row_count=None))


def test_the_binding_and_its_revisions_round_trip(conn) -> None:
    record_generation(conn, _generation())
    record_generation(conn, _generation("gen-2"))
    binding = GroupContractBinding(binding_id="bnd-1", logical_group_name="cif_daily",
                                   materialization_contract_hash="ct-hash",
                                   physical_target="sandbox_feature.cif_daily")
    record_group_binding(conn, binding)
    assert read_group_binding(conn, "cif_daily") == binding
    assert read_group_binding(conn, "nothing_bound") is None

    first = GroupPlanRevision(binding_id="bnd-1", group_plan_hash="gp-1", generation_id=GEN,
                              created_at=T1)
    second = GroupPlanRevision(binding_id="bnd-1", group_plan_hash="gp-2",
                               generation_id="gen-2", created_at=T2)
    record_plan_revision(conn, first)
    record_plan_revision(conn, second)
    assert read_plan_revisions(conn, "bnd-1") == (first, second)


def test_a_second_binding_for_a_logical_name_is_REFUSED(conn) -> None:
    """§10.1: the binding is written ONCE per logical name."""
    record_group_binding(conn, GroupContractBinding(
        binding_id="bnd-1", logical_group_name="cif_daily",
        materialization_contract_hash="ct-hash", physical_target="sandbox_feature.cif_daily"))
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        record_group_binding(conn, GroupContractBinding(
            binding_id="bnd-2", logical_group_name="cif_daily",
            materialization_contract_hash="other-hash",
            physical_target="sandbox_feature.cif_daily"))


def test_published_generation_ids_names_only_generations_that_PUBLISHED(conn) -> None:
    record_generation(conn, _generation())
    record_generation(conn, _generation("gen-failed"))
    record_generation(conn, _generation("gen-running"))
    append_run_event(conn, _event(0, RunEventKind.PUBLISHED, run_id="r-ok"))
    append_run_event(conn, _event(0, RunEventKind.GATES_FAILED, run_id="r-bad",
                                  generation_id="gen-failed"))
    append_run_event(conn, _event(0, RunEventKind.RUN_SUBMITTED, run_id="r-mid",
                                  generation_id="gen-running"))
    assert published_generation_ids(conn) == frozenset({GEN})


def test_the_current_plan_is_DERIVED_from_what_the_events_say_published(conn) -> None:
    """The seam §10.1 leaves open: `current_plan_revision` cannot read publication success off a
    revision row, so the answer comes from the append-only run events. A revision nobody published
    is not the current plan however recent it is."""
    record_generation(conn, _generation())
    record_generation(conn, _generation("gen-2"))
    binding = GroupContractBinding(binding_id="bnd-1", logical_group_name="cif_daily",
                                   materialization_contract_hash="ct-hash",
                                   physical_target="sandbox_feature.cif_daily")
    record_group_binding(conn, binding)
    published = GroupPlanRevision(binding_id="bnd-1", group_plan_hash="gp-1",
                                  generation_id=GEN, created_at=T1)
    newer_but_unpublished = GroupPlanRevision(binding_id="bnd-1", group_plan_hash="gp-2",
                                              generation_id="gen-2", created_at=T2)
    record_plan_revision(conn, published)
    record_plan_revision(conn, newer_but_unpublished)
    append_run_event(conn, _event(0, RunEventKind.PUBLISHED, run_id="r-ok"))
    append_run_event(conn, _event(0, RunEventKind.GATES_FAILED, run_id="r-bad",
                                  generation_id="gen-2"))

    current = current_plan_revision(
        read_plan_revisions(conn, "bnd-1"),
        published_generation_ids=published_generation_ids(conn))
    assert current == published


# ── 6. the compiled artifact — the BODIES the hashes name (§3.6, migration 1054) ─────────────────


def _compiled(conn):
    """A REAL plan, the REAL contract it was planned under, and the generation the plane records.

    Built through ``build_group_plan`` from a real ``ContractGroup`` (``test_group_plan``'s own
    fixtures) rather than hand-rolled. A fake plan would round-trip a shape nothing compiles, and
    the equality this section exists to prove — *the stored body re-derives to the hash the plane
    already holds* — would be an equality between two inventions.
    """
    plan = _real_plan()
    contract = _real_contract()
    record_generation(conn, MaterializationGeneration(
        generation_id=GEN, logical_group_name=plan.logical_group_name,
        materialization_contract_hash=plan.materialization_contract_hash,
        group_plan_hash=group_plan_hash(plan), generated_project_hash="proj-hash", created_at=T0))
    return plan, contract


def test_the_fixture_plan_really_was_planned_under_the_fixture_contract(conn) -> None:
    """The premise of every test below, asserted rather than assumed: the plan's contract hash IS
    this contract's. If the two fixtures ever drift apart, the round-trip below would compare the
    stored contract against a hash for some other contract and the mismatch would be reported as a
    store defect."""
    plan, contract = _compiled(conn)
    assert plan.materialization_contract_hash == contract_hash(contract)


def test_the_artifact_round_trips_and_its_BODIES_re_derive_to_the_planes_own_hashes(conn) -> None:
    """THE property. Only hashes used to survive a compile, so a human holding a ``group_plan_hash``
    could not answer "which features, which columns?" without re-deriving the whole compilation.

    The equality is the point three times over: the stored hashes are the plane's, the stored bodies
    hash BACK to them through the package's one hasher, and the second is what makes the first
    evidence rather than a pair of strings written side by side.
    """
    plan, contract = _compiled(conn)
    record_compiled_artifact(conn, generation_id=GEN, group_plan=plan, contract=contract)

    stored = read_compiled_artifact(conn, generation_id=GEN)
    assert stored is not None
    plane = conn.execute(
        "SELECT group_plan_hash, materialization_contract_hash FROM materialization_generation "
        "WHERE generation_id = %s", (GEN,)).fetchone()
    assert (stored.group_plan_hash, stored.contract_hash) == plane
    assert materialize_hash(stored.group_plan) == stored.group_plan_hash
    assert materialize_hash(stored.materialization_contract) == stored.contract_hash


def test_the_stored_body_IS_the_identity_payload_not_a_second_serialization(conn) -> None:
    """One hasher, ONE serialization (§14). The bodies stored are the very payloads
    ``materialize_hash`` consumes — not a ``dataclasses.asdict``, not a JSON rendering of the object
    — because a second serialization is a second identity, and a body that hashed to something the
    plane never recorded would be evidence about nothing."""
    plan, contract = _compiled(conn)
    record_compiled_artifact(conn, generation_id=GEN, group_plan=plan, contract=contract)

    stored = read_compiled_artifact(conn, generation_id=GEN)
    assert stored.group_plan == plan.identity_payload()
    assert stored.materialization_contract == contract.identity_payload()
    # ...and it is the PACKING LIST, readable without re-deriving the compile.
    assert [feature["column_name"] for feature in stored.group_plan["features"]] == \
        [feature.column_name for feature in plan.features]


def test_the_writer_accepts_no_caller_supplied_hash(conn) -> None:
    """The hashes are RE-DERIVED on write, never accepted. A convenience parameter would let a
    caller store a digest that does not describe the body beside it — and on an append-only table
    nothing could correct it afterwards."""
    parameters = set(inspect.signature(record_compiled_artifact).parameters)
    assert parameters == {"conn", "generation_id", "group_plan", "contract"}


def test_a_second_artifact_for_one_generation_is_REFUSED(conn) -> None:
    """One generation compiles one plan under one contract. A second row would be a second answer
    to a question that has one, and the append-only guard means nothing could ever say which."""
    plan, contract = _compiled(conn)
    record_compiled_artifact(conn, generation_id=GEN, group_plan=plan, contract=contract)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        record_compiled_artifact(conn, generation_id=GEN, group_plan=plan, contract=contract)


def test_reading_an_artifact_no_compile_wrote_yields_None(conn) -> None:
    assert read_compiled_artifact(conn, generation_id="no-such-generation") is None


@pytest.mark.parametrize("body,digest", [("group_plan", "group_plan_hash"),
                                          ("materialization_contract", "contract_hash")])
def test_a_body_that_does_not_re_derive_to_its_hash_is_REFUSED(body: str, digest: str) -> None:
    """The record checks itself, so the guarantee holds on the way OUT as well as in. The table's
    triggers stop a rewrite through ordinary DML; this is what stops a row that reached the table by
    any other means from being read back as evidence about a compile it does not describe."""
    fields = {"generation_id": GEN,
              "group_plan": {"logical_group_name": "cif_daily"},
              "group_plan_hash": materialize_hash({"logical_group_name": "cif_daily"}),
              "materialization_contract": {"entity": "customer"},
              "contract_hash": materialize_hash({"entity": "customer"})}
    assert CompiledArtifactV1(**fields).generation_id == GEN      # the must-survive control
    with pytest.raises(ValueError, match=digest):
        CompiledArtifactV1(**{**fields, body: {"tampered": True}})
