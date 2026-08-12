"""Spec §12 — ``RunManifestV1`` and the APPEND-ONLY control plane.

The control plane is the record of *what was published*. A record that can be rewritten proves
nothing, so every table it writes to (migration ``1034``) rejects UPDATE, DELETE **and TRUNCATE**,
and nothing here issues any of the three: the module's whole write surface is ``INSERT``.

**Why ``status`` is folded rather than stored.** ``RunManifestV1`` carries a ``status``, and a status
implies mutation — a run is prepared, then submitted, then published. An append-only plane cannot
hold a field that moves, so the moving part lives in :class:`MaterializationRunEvent` rows and the
current status is *derived* from them by :func:`fold_run_status`. A single terminal manifest may be
inserted at the end; nothing is ever updated in place. This is the same split §10.1 makes between
``GroupContractBinding`` (written once) and ``GroupPlanRevision`` (appended), and for the same
reason.

**The control plane never reads feature data.** Counts, types, hashes and locations only — never a
value. That is a property of the *record shapes* here, not a rule an ingestion path is asked to
remember: ``RunManifestV1``'s fields are exactly §12's, ``MaterializationRunEvent`` carries an
operator-facing ``detail`` string under the same rule that governs
:attr:`~featuregen.materialize.codes.MaterializationRefused.detail`, and no field anywhere accepts a
row, a column value or a sample.

**Nothing is generated here — every id, hash and timestamp is supplied.** There is no clock and no
id factory in this module, matching :mod:`featuregen.materialize.binding`: a control plane that
minted its own timestamps would record when it was *told*, not when the thing happened, and a test
could not pin either. ``seq`` is likewise supplied rather than computed as ``max(seq) + 1``, because
that read-modify-write is a race: two appenders would both read the same maximum and one would
silently overwrite the other's place in the order. Supplied ``seq`` turns that race into a
``UniqueViolation`` the caller must handle.

*One carve-out, added by Phase G §3.6:* :func:`record_compiled_artifact` DERIVES the two hashes it
stores, from the very payloads it stores, and accepts neither from a caller. That is the same rule
seen from the other side rather than an exception to it — everything supplied above is a fact about
the world (when a thing happened, where it sits in an order) that this module could only invent,
while a hash is a pure function of a body this module is already holding. A hash a caller could
supply is a hash that could describe some other object, and on an append-only table nothing could
ever correct it.

**The one record here that carries a BODY, and why it still holds no data.** Phase G §3.6 added
``materialization_compiled_artifact`` (migration ``1054``): the group plan and the materialization
contract, stored as the very canonical payloads their hashes were derived from. It is the same kind
of record as everything else here — append-only evidence about a compilation — and it carries no
feature data either: a packing list names columns, types and refs, and a contract names keys,
classes and policies. What it adds is *readability*: before it, a generation's three hashes named
bodies nothing had kept, so no human could audit what a run intended to read and §3.3's reconciler
had no compile-side evidence to reconcile against.

**What this module does NOT own.** ``pipeline_validation_report`` and
``publication_capability_attestation`` are created by the same migration — the plan puts all six
tables in one place — but their ingestion paths belong to the tasks that own the records:
``ValidationReportV1`` (§11) and ``record_attestation`` (§10.3), which "accepts nothing else" than a
probe result. Writing them from here would mean inventing both shapes a task later.
"""
from __future__ import annotations

import datetime as _datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

from featuregen.contracts.db import DbConn
from featuregen.materialize.binding import GroupContractBinding, GroupPlanRevision
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.contract import MaterializationContractV1
from featuregen.materialize.group_plan import FeatureGroupPlanV1

__all__ = [
    "TERMINAL_RUN_EVENT_KINDS",
    "CompiledArtifactV1",
    "MaterializationGeneration",
    "MaterializationRunEvent",
    "RunEventKind",
    "RunManifestV1",
    "RunStatus",
    "append_run_event",
    "fold_run_status",
    "published_generation_ids",
    "read_compiled_artifact",
    "read_group_binding",
    "read_plan_revisions",
    "read_run_events",
    "read_run_manifest",
    "record_compiled_artifact",
    "record_generation",
    "record_group_binding",
    "record_plan_revision",
    "record_run_manifest",
    "run_status",
]


class RunEventKind(StrEnum):
    """The CLOSED vocabulary of run events — one member per moment the spec names.

    Nothing else may be appended: migration ``1034`` carries the same set as a ``CHECK``, so a kind
    invented at a call site is refused by the database rather than stored as a run state the fold
    cannot classify.

    The four terminal kinds are collected in :data:`TERMINAL_RUN_EVENT_KINDS`, and a run may carry
    **at most one** of them (a partial unique index enforces it): two terminal events would make
    "current status" a choice rather than a reading.
    """

    #: Run preparation resolved snapshots and prepared parameters (§11.1).
    RUN_PREPARED = "RUN_PREPARED"
    #: The prepared project and parameters were submitted for execution (§11.1).
    RUN_SUBMITTED = "RUN_SUBMITTED"
    #: Staging and assembly finished; the §9 gates have not yet run.
    COMPUTATION_COMPLETED = "COMPUTATION_COMPLETED"
    #: Every §9 gate passed; the group may be published.
    GATES_PASSED = "GATES_PASSED"
    #: A §9 gate failed. TERMINAL — the group is rejected and the previous partition stays untouched.
    GATES_FAILED = "GATES_FAILED"
    #: The group was published atomically (§10). TERMINAL.
    PUBLISHED = "PUBLISHED"
    #: A ``PublicationRefusalCode`` decision stopped publication (§10.1/§10.3). TERMINAL.
    PUBLICATION_REFUSED = "PUBLICATION_REFUSED"
    #: The run failed outside the gates — execution, submission or the environment. TERMINAL.
    RUN_FAILED = "RUN_FAILED"


class RunStatus(StrEnum):
    """A run's CURRENT status — always derived from events, never stored on a mutable field."""

    PREPARED = "prepared"
    SUBMITTED = "submitted"
    COMPUTED = "computed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    PUBLISHED = "published"
    REFUSED = "refused"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        """Whether the run is over. A terminal manifest may carry only a terminal status."""
        return self in _TERMINAL_STATUSES


_STATUS_OF_KIND: dict[RunEventKind, RunStatus] = {
    RunEventKind.RUN_PREPARED: RunStatus.PREPARED,
    RunEventKind.RUN_SUBMITTED: RunStatus.SUBMITTED,
    RunEventKind.COMPUTATION_COMPLETED: RunStatus.COMPUTED,
    RunEventKind.GATES_PASSED: RunStatus.VALIDATED,
    RunEventKind.GATES_FAILED: RunStatus.REJECTED,
    RunEventKind.PUBLISHED: RunStatus.PUBLISHED,
    RunEventKind.PUBLICATION_REFUSED: RunStatus.REFUSED,
    RunEventKind.RUN_FAILED: RunStatus.FAILED,
}

#: The event kinds after which a run is over. Kept as the ONE definition — the migration's partial
#: unique index and :meth:`RunStatus.is_terminal` are both read against it by the test suite, so a
#: kind promoted to terminal in one place and not the other is caught rather than deployed.
TERMINAL_RUN_EVENT_KINDS: frozenset[RunEventKind] = frozenset({
    RunEventKind.GATES_FAILED,
    RunEventKind.PUBLISHED,
    RunEventKind.PUBLICATION_REFUSED,
    RunEventKind.RUN_FAILED,
})

_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    _STATUS_OF_KIND[kind] for kind in TERMINAL_RUN_EVENT_KINDS)


def _text(value: object, *, field: str, why: str) -> str:
    """A required identifier/hash: present, a string, and not blank."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"control-plane {field} is blank ({value!r}): {why}")
    return value


def _instant(value: str, *, field: str) -> _datetime.datetime:
    """Parse an offset-aware ISO 8601 timestamp, or say why it cannot be ordered.

    Mirrors :mod:`featuregen.materialize.binding`: two naive timestamps written in two zones are not
    orderable, and comparing mixed offsets as text picks the wrong record outright.
    """
    try:
        parsed = _datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"control-plane {field} {value!r} is not an ISO 8601 timestamp ({exc}): the control "
            f"plane is a record of when things happened, and a record that names no instant is not "
            f"one") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"control-plane {field} {value!r} carries no UTC offset: two naive timestamps written "
            f"in two zones are not orderable, so any 'latest' derived from them would be a guess")
    return parsed


def _count(value: object, *, field: str) -> None:
    """A count is a whole, non-negative number — or absent. Never a data value."""
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(
            f"control-plane {field} is {value!r}: a count is a whole, non-negative number, and "
            f"anything else is a writer assembled wrongly")


# ── the records ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MaterializationGeneration:
    """ONE compilation of ONE group — the thing every other control-plane record points at.

    Its fields are :class:`~featuregen.materialize.identity.RenderedArtifactIdentity`'s group-level
    values: the contract the group publishes under, the packing list it packs and the bytes that
    were rendered. The per-feature formula/IR hash tuples are deliberately not repeated here — they
    are already covered by ``group_plan_hash`` and re-stating them would create a second place for
    the same identity to disagree with itself.
    """

    generation_id: str
    logical_group_name: str
    materialization_contract_hash: str
    group_plan_hash: str
    generated_project_hash: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("generation_id", "logical_group_name", "materialization_contract_hash",
                     "group_plan_hash", "generated_project_hash"):
            _text(getattr(self, name), field=name,
                  why="every later record is attributed to this generation, and one that names no "
                      "group, contract, plan or project attributes nothing")
        _instant(self.created_at, field="generation created_at")


@dataclass(frozen=True, slots=True)
class MaterializationRunEvent:
    """One appended fact about a run. ``seq`` is the run's order; ``occurred_at`` is when.

    Both are present because they answer different questions and neither can stand in for the other:
    ``seq`` is a total order the database enforces as unique, while ``occurred_at`` is a wall-clock
    reading from whichever host observed the moment. The fold orders by ``seq``.

    ``detail`` is operator-facing text under §14's rule — counts, types, hashes and locations only,
    never a data value.
    """

    run_id: str
    seq: int
    generation_id: str
    event_kind: RunEventKind
    occurred_at: str
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.run_id, field="run event run_id",
              why="an event that names no run cannot be folded into any run's status")
        _text(self.generation_id, field="run event generation_id",
              why="a run event is evidence about one compilation, and one that names no generation "
                  "is evidence about nothing")
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 0:
            raise ValueError(
                f"run event seq is {self.seq!r}: seq is the run's order and must be a whole, "
                f"non-negative number")
        # Coerced, not merely annotated — a plain string read back from the database must become the
        # member the fold dispatches on, rather than a second spelling nothing compares equal to.
        object.__setattr__(self, "event_kind", RunEventKind(self.event_kind))
        _instant(self.occurred_at, field="run event occurred_at")
        if not isinstance(self.detail, str):
            raise ValueError(
                f"run event detail is {type(self.detail).__name__}: detail is operator-facing text "
                f"(counts, types, hashes and locations), never a structured payload")

    def is_terminal(self) -> bool:
        return self.event_kind in TERMINAL_RUN_EVENT_KINDS

    def status(self) -> RunStatus:
        return _STATUS_OF_KIND[self.event_kind]


@dataclass(frozen=True, slots=True)
class RunManifestV1:
    """§12's terminal record of ONE run: what was published, from what, and how much of it.

    Exactly §12's fields, in §12's order. Nothing here is a data value: every field is an identity,
    a count, a location or a verdict, and the shape is asserted field-for-field by the test suite so
    that a value-carrying field cannot be added quietly.

    ``status`` is terminal by construction — the manifest is inserted once, at the end, and the
    moving status lives in the events (:func:`fold_run_status`).
    """

    run_id: str
    generation_id: str
    group_plan_hash: str
    materialization_contract_hash: str
    generated_project_hash: str
    sandbox_execution_hash: str
    business_dt: str
    publication_mechanism: str
    capability_attestation_id: str
    expected_feature_columns: tuple[str, ...]
    staged_row_count: int | None
    published_row_count: int | None
    schema_hash: str | None
    key_uniqueness_result: str | None
    required_column_result: str | None
    orphan_grain_key_count: int | None
    publication_location: str | None
    started_at: str | None
    published_at: str | None
    status: RunStatus

    def __post_init__(self) -> None:
        for name in ("run_id", "generation_id", "group_plan_hash",
                     "materialization_contract_hash", "generated_project_hash",
                     "sandbox_execution_hash", "business_dt", "publication_mechanism",
                     "capability_attestation_id"):
            _text(getattr(self, name), field=f"run manifest {name}",
                  why="the manifest is the record of what was published, and a blank identity "
                      "makes the record unattributable")
        # §10.3: an execution is not identified without naming the attestation that let it publish;
        # the FK in migration 1034 is the other half of this.
        object.__setattr__(self, "expected_feature_columns",
                           tuple(self.expected_feature_columns))
        if not self.expected_feature_columns:
            raise ValueError(
                "run manifest expected_feature_columns is empty: a group publishes at least one "
                "feature, and a manifest expecting none could not be contradicted by any output")
        for name in ("staged_row_count", "published_row_count", "orphan_grain_key_count"):
            _count(getattr(self, name), field=f"run manifest {name}")
        for name in ("started_at", "published_at"):
            value = getattr(self, name)
            if value is not None:
                _instant(value, field=f"run manifest {name}")
        object.__setattr__(self, "status", RunStatus(self.status))
        if not self.status.is_terminal():
            raise ValueError(
                f"run manifest status is {self.status.value!r}: the manifest is written ONCE at the "
                f"end of a run, so a non-terminal status would be a terminal record of a run that "
                f"had not finished — the moving status is folded from the run events")
        if self.status is RunStatus.PUBLISHED and (
                self.published_at is None or self.publication_location is None
                or self.published_row_count is None):
            raise ValueError(
                "a manifest claiming 'published' must state when, where and how many rows: without "
                "all three it records that something was published but nothing a reader could "
                "check the published table against")


#: §12's field list, pinned. The manifest is the one record with enough fields for a data value to
#: be smuggled in unnoticed, so the shape is asserted rather than described.
RUN_MANIFEST_FIELDS: tuple[str, ...] = tuple(field.name for field in fields(RunManifestV1))


@dataclass(frozen=True, slots=True)
class CompiledArtifactV1:
    """Phase G §3.6 — the BODIES a generation's hashes name, so a compile is auditable afterwards.

    Until migration ``1054`` a compile left only hashes: a human holding a ``group_plan_hash`` could
    not answer *which features, which columns, which spine?* without re-deriving the whole
    compilation, and §3.3's reconciler — which resolves a mid-chain failure by reading evidence —
    had no compile-side evidence to read.

    **The bodies are the canonical payloads, and the record proves it.** ``group_plan`` and
    ``materialization_contract`` are exactly the mappings
    :func:`~featuregen.materialize.canonical.materialize_hash` consumed, never a second rendering of
    the same objects: the package has ONE canonicalization scheme by invariant (§14), and a second
    serialization is a second identity. ``__post_init__`` re-derives both digests, so a body and a
    hash that describe different objects cannot become a record at all — on the way in *or* on the
    way back out of the database. The table's triggers stop a rewrite through ordinary DML; this is
    what stops a row that reached it by any other means from being read back as evidence.

    ``recorded_at`` is not a field here, matching :class:`MaterializationGeneration`: when the row
    was *stored* is the database's bookkeeping, not part of what was compiled.
    """

    generation_id: str
    group_plan: Mapping[str, Any]
    group_plan_hash: str
    materialization_contract: Mapping[str, Any]
    contract_hash: str

    def __post_init__(self) -> None:
        _text(self.generation_id, field="compiled artifact generation_id",
              why="the artifact describes ONE compilation, and one that names no generation "
                  "describes nothing")
        for body, digest in (("group_plan", "group_plan_hash"),
                             ("materialization_contract", "contract_hash")):
            value = getattr(self, body)
            if not isinstance(value, Mapping):
                raise ValueError(
                    f"compiled artifact {body} is {type(value).__name__}: it is the identity "
                    f"payload the one hasher consumes, which is a mapping — a fragment or a scalar "
                    f"would re-derive to a digest nothing in the plane holds")
            # Copied, so the record is compared by value and cannot be mutated behind the digest
            # that was checked against it. Frozen guards rebinding, not the contents of a mapping.
            object.__setattr__(self, body, dict(value))
            _text(getattr(self, digest), field=f"compiled artifact {digest}",
                  why="the body is stored beside its hash precisely so the two can be compared, "
                      "and a blank digest compares nothing to nothing and passes")
            derived = materialize_hash(getattr(self, body))
            if derived != getattr(self, digest):
                raise ValueError(
                    f"compiled artifact {digest} is {getattr(self, digest)!r} and its {body} "
                    f"re-derives to {derived!r}: the body and the hash must describe the SAME "
                    f"object, or the record is evidence about a compilation nobody performed")


# ── the fold ─────────────────────────────────────────────────────────────────────────────────────


def fold_run_status(events: Sequence[MaterializationRunEvent]) -> RunStatus:
    """The run's current status, derived from its appended events (§12).

    Ordered by ``seq``, which the database holds unique per run — not by ``occurred_at``, which is a
    wall-clock reading from whichever host observed the moment and can run backwards between hosts.

    Raises:
        ValueError: ``events`` is empty (a run that recorded nothing has no status to read, and
            answering with any member would be an invention), the events belong to more than one
            run, two events share a ``seq``, or a terminal event is followed by another event — a
            run that ended and then carried on is a corrupt record, and folding it would report a
            status the record does not support.
    """
    supplied = tuple(events)
    if not supplied:
        raise ValueError(
            "fold_run_status was given no events: a run's status is DERIVED from what was "
            "appended, so a run with nothing appended has no status — reporting one would be an "
            "invention about a run nobody recorded")

    runs = {event.run_id for event in supplied}
    if len(runs) > 1:
        raise ValueError(
            f"fold_run_status was given events from {len(runs)} runs ({', '.join(sorted(runs))}): "
            f"each run has its own order and its own status, and folding two together derives a "
            f"status for neither")

    ordered = sorted(supplied, key=lambda event: event.seq)
    seqs = [event.seq for event in ordered]
    if len(set(seqs)) != len(seqs):
        raise ValueError(
            f"fold_run_status was given events sharing a seq ({sorted(seqs)}): seq is the run's "
            f"order, so a repeat means the latest event is ambiguous")

    for event in ordered[:-1]:
        if event.is_terminal():
            raise ValueError(
                f"run {event.run_id!r} records {event.event_kind.value} at seq {event.seq} and "
                f"then continues: a terminal event ends the run, so an event after one is a "
                f"corrupt record rather than a later status")
    return ordered[-1].status()


# ── persistence: INSERT and SELECT, and nothing else ─────────────────────────────────────────────


def record_generation(conn: DbConn, generation: MaterializationGeneration) -> None:
    """Append the generation every other control-plane record points at."""
    conn.execute(
        "INSERT INTO materialization_generation (generation_id, logical_group_name, "
        "materialization_contract_hash, group_plan_hash, generated_project_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (generation.generation_id, generation.logical_group_name,
         generation.materialization_contract_hash, generation.group_plan_hash,
         generation.generated_project_hash, generation.created_at))


def append_run_event(conn: DbConn, event: MaterializationRunEvent) -> None:
    """Append one run event.

    The event's place in the run (``seq``) is the caller's, not this function's: computing
    ``max(seq) + 1`` here would be a read-modify-write two appenders could both win. The database
    guarantees three things this function deliberately does not police:

    * a duplicate ``(run_id, seq)`` is refused by the primary key (``UniqueViolation``);
    * a second terminal event for the run is refused by the one-terminal partial unique index
      (``UniqueViolation``);
    * any event after a terminal one, and any ``seq`` that does not extend the run's max, is
      refused by migration 1044's ``materialization_run_event_ordered`` BEFORE INSERT trigger
      (``RaiseException``) — either write would brick ``run_status()`` forever on a table whose
      append-only triggers leave no repair path.

    Within one session the ordering trigger fires first, so those refusals surface as its
    ``RaiseException``; the two unique constraints remain the arbiters of the concurrent race the
    trigger's reads cannot see. Every one is a refusal the caller must handle rather than a silent
    reordering.
    """
    conn.execute(
        "INSERT INTO materialization_run_event (run_id, seq, generation_id, event_kind, "
        "occurred_at, detail) VALUES (%s, %s, %s, %s, %s, %s)",
        (event.run_id, event.seq, event.generation_id, event.event_kind.value,
         event.occurred_at, event.detail))


def read_run_events(conn: DbConn, run_id: str) -> tuple[MaterializationRunEvent, ...]:
    """Every event appended for ``run_id``, in ``seq`` order."""
    rows = conn.execute(
        "SELECT run_id, seq, generation_id, event_kind, occurred_at, detail "
        "FROM materialization_run_event WHERE run_id = %s ORDER BY seq", (run_id,)).fetchall()
    return tuple(MaterializationRunEvent(*row) for row in rows)


def run_status(conn: DbConn, run_id: str) -> RunStatus:
    """``run_id``'s current status, folded from its appended events (§12)."""
    return fold_run_status(read_run_events(conn, run_id))


def published_generation_ids(conn: DbConn) -> frozenset[str]:
    """The generations that published successfully — the evidence §10.1's derivation needs.

    ``current_plan_revision`` cannot read publication success off a revision row, because whether a
    plan published is known only afterwards. This is where that answer comes from: the append-only
    run events, folded exactly as :func:`fold_run_status` folds them.
    """
    rows = conn.execute(
        "SELECT DISTINCT generation_id FROM materialization_run_event WHERE event_kind = %s",
        (RunEventKind.PUBLISHED.value,)).fetchall()
    return frozenset(row[0] for row in rows)


def record_run_manifest(conn: DbConn, manifest: RunManifestV1) -> None:
    """Insert the run's ONE terminal manifest. A second insert for the run is a ``UniqueViolation``."""
    conn.execute(
        "INSERT INTO materialization_run_manifest (run_id, generation_id, group_plan_hash, "
        "materialization_contract_hash, generated_project_hash, sandbox_execution_hash, "
        "business_dt, publication_mechanism, capability_attestation_id, expected_feature_columns, "
        "staged_row_count, published_row_count, schema_hash, key_uniqueness_result, "
        "required_column_result, orphan_grain_key_count, publication_location, started_at, "
        "published_at, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (manifest.run_id, manifest.generation_id, manifest.group_plan_hash,
         manifest.materialization_contract_hash, manifest.generated_project_hash,
         manifest.sandbox_execution_hash, manifest.business_dt, manifest.publication_mechanism,
         manifest.capability_attestation_id, list(manifest.expected_feature_columns),
         manifest.staged_row_count, manifest.published_row_count, manifest.schema_hash,
         manifest.key_uniqueness_result, manifest.required_column_result,
         manifest.orphan_grain_key_count, manifest.publication_location, manifest.started_at,
         manifest.published_at, manifest.status.value))


def read_run_manifest(conn: DbConn, run_id: str) -> RunManifestV1 | None:
    """``run_id``'s terminal manifest, or ``None`` when the run has not written one."""
    row = conn.execute(
        "SELECT run_id, generation_id, group_plan_hash, materialization_contract_hash, "
        "generated_project_hash, sandbox_execution_hash, business_dt, publication_mechanism, "
        "capability_attestation_id, expected_feature_columns, staged_row_count, "
        "published_row_count, schema_hash, key_uniqueness_result, required_column_result, "
        "orphan_grain_key_count, publication_location, started_at, published_at, status "
        "FROM materialization_run_manifest WHERE run_id = %s", (run_id,)).fetchone()
    return None if row is None else RunManifestV1(*row)


def record_group_binding(conn: DbConn, binding: GroupContractBinding) -> None:
    """Write §10.1's binding — ONCE per logical name; a second is a ``UniqueViolation``."""
    conn.execute(
        "INSERT INTO group_binding (binding_id, logical_group_name, "
        "materialization_contract_hash, physical_target) VALUES (%s, %s, %s, %s)",
        (binding.binding_id, binding.logical_group_name,
         binding.materialization_contract_hash, binding.physical_target))


def read_group_binding(conn: DbConn, logical_group_name: str) -> GroupContractBinding | None:
    """The binding recorded for ``logical_group_name`` — the lookup ``bind_group`` compares against."""
    row = conn.execute(
        "SELECT binding_id, logical_group_name, materialization_contract_hash, physical_target "
        "FROM group_binding WHERE logical_group_name = %s", (logical_group_name,)).fetchone()
    return None if row is None else GroupContractBinding(*row)


def record_plan_revision(conn: DbConn, revision: GroupPlanRevision) -> None:
    """Append §10.1's plan revision. One generation compiles one plan, so a second revision for the
    same ``(binding, generation)`` is a ``UniqueViolation`` rather than a second answer."""
    conn.execute(
        "INSERT INTO group_plan_revision (binding_id, generation_id, group_plan_hash, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (revision.binding_id, revision.generation_id, revision.group_plan_hash,
         revision.created_at))


def record_compiled_artifact(
    conn: DbConn,
    *,
    generation_id: str,
    group_plan: FeatureGroupPlanV1,
    contract: MaterializationContractV1,
) -> None:
    """Store §3.6's compile-side bodies for ``generation_id`` — ONCE; a second is a
    ``UniqueViolation``.

    **The bodies are the objects' own canonical payloads, and the hashes are re-derived from the
    very mappings that are stored** — not from a second ``identity_payload()`` call, and never from
    a caller. ``materialize_hash`` is the package's one hasher (§14): a second serialization here
    would be a second identity, and a body that hashed to a value the plane does not hold would be
    evidence about nothing. There is deliberately no parameter through which a caller could supply
    either digest, because on an append-only table nothing could correct one afterwards.

    **The row is read BACK inside the caller's transaction and checked.** ``jsonb`` is a normalizing
    type — it is not the bytes handed to it — so "the body we meant to store" and "the body a reader
    will get" are two different objects, and only the second one is evidence. Re-deriving it here
    means a normalization that perturbed the payload aborts the compile that produced it, rather
    than surfacing months later in a reconciler holding a row nothing can repair or delete.

    **The two arguments must belong together, and that is checked.** ``build_group_plan`` guarantees
    the plan carries *its own group's* contract hash; nothing guarantees that the ``contract`` passed
    here is that group's contract. Without the check, a mismatched pair would store two bodies that
    each re-derive to the digest beside them — internally consistent, describing two different
    compilations — and the stored ``contract_hash`` would disagree with the
    ``materialization_contract_hash`` the plane holds for this generation. It is a ``ValueError``,
    not a §14 code: a call assembled wrongly is not a governed verdict about a feature.

    This function does NOT open a transaction: it is called from inside ``compile/chain.py``'s
    commit block, whose whole point is that the generation row, the §10.1 records, this artifact,
    the terminal event and the tree land together or not at all.
    """
    plan_payload = group_plan.identity_payload()
    contract_payload = contract.identity_payload()
    contract_digest = materialize_hash(contract_payload)
    if contract_digest != group_plan.materialization_contract_hash:
        raise ValueError(
            f"the compiled artifact for generation {generation_id!r} was given a contract hashing "
            f"to {contract_digest!r} and a plan whose materialization_contract_hash is "
            f"{group_plan.materialization_contract_hash!r}: the plan is the packing list for the "
            f"group THIS contract governs, and storing the pair would record two different "
            f"compilations as one — on a table where nothing can be corrected afterwards")
    row = conn.execute(
        "INSERT INTO materialization_compiled_artifact (generation_id, group_plan, "
        "group_plan_hash, materialization_contract, contract_hash) VALUES (%s, %s, %s, %s, %s) "
        "RETURNING generation_id, group_plan, group_plan_hash, materialization_contract, "
        "contract_hash",
        (generation_id, Jsonb(plan_payload), materialize_hash(plan_payload),
         Jsonb(contract_payload), contract_digest)).fetchone()
    if row is None:
        # Unreachable: an `INSERT … RETURNING` with no `ON CONFLICT` either returns the row it
        # inserted or raises. It is a narrow rather than an `assert` (which `python -O` strips,
        # turning the line below into a confusing TypeError) so the check can never be skipped
        # silently — an unverifiable write must not be treated as a recorded one.
        raise RuntimeError(
            f"the compiled artifact for generation {generation_id!r} returned no row from its own "
            f"INSERT, so what was stored cannot be checked against what was hashed")
    # Constructing the record IS the check: `CompiledArtifactV1` re-derives both digests from the
    # bodies the database returned, and raises if either pair describes two different objects.
    CompiledArtifactV1(*row)


def read_compiled_artifact(conn: DbConn, *, generation_id: str) -> CompiledArtifactV1 | None:
    """The bodies ``generation_id`` compiled, or ``None`` when no compile recorded them.

    ``None`` also covers every generation written before migration ``1054`` existed: they have
    hashes and no bodies, which is exactly the gap this table closes going forward and cannot close
    backwards.
    """
    row = conn.execute(
        "SELECT generation_id, group_plan, group_plan_hash, materialization_contract, "
        "contract_hash FROM materialization_compiled_artifact WHERE generation_id = %s",
        (generation_id,)).fetchone()
    return None if row is None else CompiledArtifactV1(*row)


def read_plan_revisions(conn: DbConn, binding_id: str) -> tuple[GroupPlanRevision, ...]:
    """Every revision appended under ``binding_id`` — the sequence the derivation folds.

    Ordered by ``recorded_at`` (the insertion instant), **never** by ``created_at``: that column is
    text carrying a UTC offset, and ordering mixed offsets as text is the exact defect §10.1 warns
    about — ``2026-07-27T23:00:00+05:30`` sorts after ``…T19:00:00+00:00`` and is 90 minutes before
    it in fact. Which revision is *current* is not this function's answer anyway;
    ``current_plan_revision`` derives it from instants and from what published.
    """
    rows = conn.execute(
        "SELECT binding_id, group_plan_hash, generation_id, created_at FROM group_plan_revision "
        "WHERE binding_id = %s ORDER BY recorded_at", (binding_id,)).fetchall()
    return tuple(GroupPlanRevision(*row) for row in rows)
