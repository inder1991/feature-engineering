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

from featuregen.materialize import control_plane
from featuregen.materialize.binding import (
    GroupContractBinding,
    GroupPlanRevision,
    current_plan_revision,
)
from featuregen.materialize.control_plane import (
    RUN_MANIFEST_FIELDS,
    TERMINAL_RUN_EVENT_KINDS,
    MaterializationGeneration,
    MaterializationRunEvent,
    RunEventKind,
    RunManifestV1,
    RunStatus,
    append_run_event,
    fold_run_status,
    published_generation_ids,
    read_group_binding,
    read_plan_revisions,
    read_run_events,
    read_run_manifest,
    record_generation,
    record_group_binding,
    record_plan_revision,
    record_run_manifest,
    run_status,
)

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
                         [MaterializationGeneration, MaterializationRunEvent, RunManifestV1])
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
             "materialization_run_manifest", "group_binding", "group_plan_revision"}
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
