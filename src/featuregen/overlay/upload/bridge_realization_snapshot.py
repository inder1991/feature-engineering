"""A4: BridgeRealizationSnapshotV1 — one frozen, batched read of realization state (migration 1131).

**Why this exists (the reviewer-proven multi-query pattern).** For a considered set of N
cross-catalog links, the step-4 preview assessment and the production readers each re-read
realization state per bridge: the pinned revision row, its dependency rows, the overlay lifecycle
fold, candidate currentness, the shared current pointer and BOTH endpoints' binding revisions —
per bridge, per call (`bridge_store.executable_bridge_realizations` /
`revalidate_bridge_realization`; `bridge_realization_proposal.assess_realization_for_preview`).
:func:`build_bridge_realization_snapshot` replaces that pattern FOR THE PLANNING PATH with a
CONSTANT number of batched ``= ANY(%s)`` reads regardless of set size, all inside the caller's
transaction — one snapshot is one read moment; no per-bridge re-reads happen after the batch.
``revalidate_bridge_realization`` (production revalidation) is deliberately untouched.

**What R11's ``pinned_dependency_snapshot_id`` names.** The snapshot persists immutably
(``bridge_realization_snapshot``, append-only, content-addressed ``brsnap_`` id) so
``assess_realization_for_preview`` can later re-read EXACTLY what planning saw. THE B2b SEAM:
today the assessment's ``pinned_dependency_snapshot_id`` parameter still means the per-revision
dependency-set hash (``brds_``); routing it through this persisted snapshot — including the
mandatory ``complete`` check below — is B2b's wiring change, noted there rather than smuggled in
here (changing the parameter's meaning re-contracts the assessment and its suite).

**Cap vs deadline (rounds 9/14; the round-8 M5 correction: EXTEND ``CompileBudget``).** The
builder consumes the planner's existing :class:`~featuregen.overlay.upload.planner.declarations.\
CompileBudget` — its ``remaining`` count cap and MONOTONIC-clock ``deadline_monotonic`` — and
keeps that type's split intact:

* THE PINNED ORDER: considered items are processed in lexical ``(bridge_fact_key, pin)`` order —
  documented here, asserted in tests, the one order truncation is disclosed in.
* The CAP truncates DETERMINISTICALLY: the first ``remaining`` items in the pinned order are
  captured; the remainder is truncated with cause ``"cap"`` — the same list every run.
* The DEADLINE is a real elapsed-time bound over the injected ``clock``. It is checked at entry
  and between batch phases; when it bites, the ENTIRE admitted set is truncated with cause
  ``"deadline"`` (batched phases are all-or-nothing: a half-captured bridge is not a snapshot).
  The truncated LIST stays deterministic — it is the stable-order admitted list — even though
  WHETHER the deadline fires is time-dependent; the wall-clock ``elapsed_note`` is disclosure and
  NEVER identity material (the ``CompileBudget`` law: the clock never enters a hash).
* ``budget.remaining`` is decremented by the number of entries actually captured and
  ``budget.stopped_by_time`` records which bound fired first (``False``=count, ``True``=deadline
  — the shadow-planner convention, reused).

**Nothing downstream may treat a truncated snapshot as complete**: consumers must check
:attr:`BridgeRealizationSnapshotV1.complete` (``truncation.cause == "none"``).

**Honest absence.** A considered bridge with no realization is captured as absence
(``realization_revision_id=None``); a pin that names no stored revision is captured as
``pin_found=False`` — never invented, and neither is truncation. A resolved revision with no
current-pointer row (the provisional normal case) captures ``safety_status=None``: no published
safety verdict exists, and this store does not fabricate ``unassessed`` on the pointer's behalf.

Store discipline: ``conn`` positional, everything else keyword-only; typed refusals
(:class:`SnapshotContractDefect`) BEFORE any SQL; content-addressed idempotency (same captured
state → same ``snapshot_id``; ``ON CONFLICT DO NOTHING`` + content-verified read-back, the
``execution_context``/``temporal_policy_store`` sibling idiom).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.bridge_assessment import (
    LinkAvailability,
    link_state_from_stream,
)
from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    BridgeStoreCorruption,
    bridge_dependency_snapshot_id,
    realization_from_json,
)
from featuregen.overlay.upload.planner.declarations import CompileBudget

if TYPE_CHECKING:
    from featuregen.contracts import DbConn
    from featuregen.overlay.upload.bridge_realization import BridgeJoinRealizationRevisionV1

__all__ = [
    "SNAPSHOT_ID_PREFIX",
    "TRUNCATION_CAP",
    "TRUNCATION_CAP_AND_DEADLINE",
    "TRUNCATION_DEADLINE",
    "TRUNCATION_NONE",
    "BridgeRealizationSnapshotEntryV1",
    "BridgeRealizationSnapshotV1",
    "ConsideredBridgeV1",
    "SnapshotContractDefect",
    "SnapshotStoreConflict",
    "SnapshotTruncationV1",
    "build_bridge_realization_snapshot",
    "load_bridge_realization_snapshot",
]

_SNAPSHOT_CONTRACT = "bridge_realization_snapshot_v1"

#: Deterministic id prefix (the ``ecx_``/``brds_``/``dtp_``/``jvp_`` family).
SNAPSHOT_ID_PREFIX = "brsnap_"

#: Truncation causes — the closed vocabulary the migration's rationale documents.
TRUNCATION_NONE = "none"
TRUNCATION_CAP = "cap"
TRUNCATION_DEADLINE = "deadline"
TRUNCATION_CAP_AND_DEADLINE = "cap_and_deadline"

#: The step-3/4 purpose vocabulary (the 1130/1131 named CHECK) — the platform constant, imported
#: lazily in validation to avoid a cycle with :mod:`bridge_realization_proposal`.
_PURPOSES = ("feature_generation",)


class SnapshotContractDefect(ValueError):
    """A refused snapshot request: blank keys, unknown tier/purpose, an empty considered set, a
    foreign budget type, or a pin naming another bridge — raised BEFORE any SQL runs (or, for the
    pin-ownership defect, before anything is persisted)."""


class SnapshotStoreConflict(RuntimeError):
    """The store and the table disagree (a row failing content verification, or a build whose
    read-back found nothing) — corruption, never served."""


@dataclass(frozen=True, slots=True)
class ConsideredBridgeV1:
    """One member of the considered set: the link's governed fact key, optionally pinned to an
    EXACT realization revision (R11: adoption pins the revision; never latest)."""

    bridge_fact_key: str
    pinned_realization_revision_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.bridge_fact_key, str) or not self.bridge_fact_key.strip():
            raise SnapshotContractDefect("bridge_fact_key must be a non-blank string")
        object.__setattr__(self, "bridge_fact_key", self.bridge_fact_key.strip())
        pin = self.pinned_realization_revision_id
        if pin is not None:
            if not isinstance(pin, str) or not pin.strip():
                raise SnapshotContractDefect(
                    "pinned_realization_revision_id must be a non-blank string when given")
            object.__setattr__(self, "pinned_realization_revision_id", pin.strip())

    def sort_key(self) -> tuple[str, str]:
        """THE PINNED ORDER's key: lexical (fact key, pin) — '' sorts an unpinned item first."""
        return (self.bridge_fact_key, self.pinned_realization_revision_id or "")


@dataclass(frozen=True, slots=True)
class BridgeRealizationSnapshotEntryV1:
    """Everything the step-4 assessment consults about ONE considered bridge, captured at the
    snapshot's single read moment. Immutable facts (cardinality, scope) ride the pinned revision
    id — the revision row itself is append-only, so the id alone re-reads them exactly; they are
    duplicated here so a consumer needs no further query. Mutable facts (pointer state, link
    availability, candidate currentness, binding-revision presence) are what the snapshot exists
    to freeze."""

    bridge_fact_key: str
    pinned_realization_revision_id: str | None
    #: ``None`` = no pin was given; ``False`` = the pin names no stored revision (honest absence).
    pin_found: bool | None
    #: The RESOLVED revision: the pin when given and found, else the lexically-first ACTIVE
    #: current pointer's revision, else ``None`` (no realization exists for this link).
    realization_revision_id: str | None
    realization_id: str | None
    #: ``DirectionalCardinalityVerdictV1.identity_payload()`` — "unknown" or the Cardinality
    #: value; ``None`` only on absence.
    cardinality: str | None
    cardinality_basis: str | None
    #: The shared current pointer's facts for the resolved realization; all ``None`` when no
    #: pointer row exists (the provisional normal case — no published verdict is fabricated).
    safety_status: str | None
    lifecycle: str | None
    pointer_version: int | None
    current_pointer_revision_id: str | None
    #: EVERY current-pointer realization id seen for this fact key (disclosure of directional
    #: multiplicity), sorted.
    current_realization_ids: tuple[str, ...]
    #: A2's two currentness pins (BridgeCurrentnessV1), captured consistently at the read moment.
    candidate_revision_id: str | None
    overlay_head_event_id: str | None
    link_available: bool
    #: ``bridge_candidate_currentness``'s tri-state, batched: True/False, ``None`` = no candidate
    #: record (a directly governed legacy bridge keeps its generic lifecycle behavior).
    candidate_currentness: bool | None
    scope_execution_tier: str | None
    scope_purposes: tuple[str, ...]
    scope_environment: str | None
    from_binding_revision_id: str | None
    to_binding_revision_id: str | None
    from_binding_revision_stored: bool
    to_binding_revision_stored: bool
    dependency_snapshot_id: str | None
    #: The stored dependency rows, sorted ``(kind, key, revision)``.
    dependencies: tuple[tuple[str, str, str], ...]
    #: Whether the stored rows re-derive the revision's ``dependency_snapshot_id`` (the brds_
    #: agreement the assessment checks); ``None`` on absence.
    dependency_snapshot_agrees: bool | None
    has_unresolved_requirements: bool | None

    def payload(self) -> dict[str, Any]:
        return {
            "bridge_fact_key": self.bridge_fact_key,
            "pinned_realization_revision_id": self.pinned_realization_revision_id,
            "pin_found": self.pin_found,
            "realization_revision_id": self.realization_revision_id,
            "realization_id": self.realization_id,
            "cardinality": self.cardinality,
            "cardinality_basis": self.cardinality_basis,
            "safety_status": self.safety_status,
            "lifecycle": self.lifecycle,
            "pointer_version": self.pointer_version,
            "current_pointer_revision_id": self.current_pointer_revision_id,
            "current_realization_ids": list(self.current_realization_ids),
            "candidate_revision_id": self.candidate_revision_id,
            "overlay_head_event_id": self.overlay_head_event_id,
            "link_available": self.link_available,
            "candidate_currentness": self.candidate_currentness,
            "scope_execution_tier": self.scope_execution_tier,
            "scope_purposes": list(self.scope_purposes),
            "scope_environment": self.scope_environment,
            "from_binding_revision_id": self.from_binding_revision_id,
            "to_binding_revision_id": self.to_binding_revision_id,
            "from_binding_revision_stored": self.from_binding_revision_stored,
            "to_binding_revision_stored": self.to_binding_revision_stored,
            "dependency_snapshot_id": self.dependency_snapshot_id,
            "dependencies": [list(dependency) for dependency in self.dependencies],
            "dependency_snapshot_agrees": self.dependency_snapshot_agrees,
            "has_unresolved_requirements": self.has_unresolved_requirements,
        }


def _entry_from_payload(payload: dict[str, Any]) -> BridgeRealizationSnapshotEntryV1:
    return BridgeRealizationSnapshotEntryV1(
        bridge_fact_key=payload["bridge_fact_key"],
        pinned_realization_revision_id=payload["pinned_realization_revision_id"],
        pin_found=payload["pin_found"],
        realization_revision_id=payload["realization_revision_id"],
        realization_id=payload["realization_id"],
        cardinality=payload["cardinality"],
        cardinality_basis=payload["cardinality_basis"],
        safety_status=payload["safety_status"],
        lifecycle=payload["lifecycle"],
        pointer_version=payload["pointer_version"],
        current_pointer_revision_id=payload["current_pointer_revision_id"],
        current_realization_ids=tuple(payload["current_realization_ids"]),
        candidate_revision_id=payload["candidate_revision_id"],
        overlay_head_event_id=payload["overlay_head_event_id"],
        link_available=payload["link_available"],
        candidate_currentness=payload["candidate_currentness"],
        scope_execution_tier=payload["scope_execution_tier"],
        scope_purposes=tuple(payload["scope_purposes"]),
        scope_environment=payload["scope_environment"],
        from_binding_revision_id=payload["from_binding_revision_id"],
        to_binding_revision_id=payload["to_binding_revision_id"],
        from_binding_revision_stored=payload["from_binding_revision_stored"],
        to_binding_revision_stored=payload["to_binding_revision_stored"],
        dependency_snapshot_id=payload["dependency_snapshot_id"],
        dependencies=tuple(tuple(dependency) for dependency in payload["dependencies"]),
        dependency_snapshot_agrees=payload["dependency_snapshot_agrees"],
        has_unresolved_requirements=payload["has_unresolved_requirements"],
    )


@dataclass(frozen=True, slots=True)
class SnapshotTruncationV1:
    """Which considered items the snapshot does NOT cover, and why — persisted and disclosed
    (rounds 9/14). Keys are listed in THE PINNED ORDER. ``cap_value`` is recorded only when the
    cap actually bit (a cap that never fired is not part of the verdict); ``elapsed_note`` is
    wall-clock DISCLOSURE and never enters the snapshot's identity."""

    cause: str
    truncated_bridge_keys: tuple[str, ...]
    cap_truncated_bridge_keys: tuple[str, ...] = ()
    deadline_truncated_bridge_keys: tuple[str, ...] = ()
    cap_value: int | None = None
    elapsed_note: str | None = None

    def __post_init__(self) -> None:
        causes = (TRUNCATION_NONE, TRUNCATION_CAP, TRUNCATION_DEADLINE,
                  TRUNCATION_CAP_AND_DEADLINE)
        if self.cause not in causes:
            raise SnapshotContractDefect(
                f"truncation cause must be one of {causes}, got {self.cause!r}")

    def identity_payload(self) -> dict[str, Any]:
        """The identity-bearing half: everything EXCEPT the wall-clock ``elapsed_note``."""
        return {
            "cause": self.cause,
            "truncated_bridge_keys": list(self.truncated_bridge_keys),
            "cap_truncated_bridge_keys": list(self.cap_truncated_bridge_keys),
            "deadline_truncated_bridge_keys": list(self.deadline_truncated_bridge_keys),
            "cap_value": self.cap_value,
        }

    def payload(self) -> dict[str, Any]:
        return {**self.identity_payload(), "elapsed_note": self.elapsed_note}


def _truncation_from_payload(payload: dict[str, Any]) -> SnapshotTruncationV1:
    return SnapshotTruncationV1(
        cause=payload["cause"],
        truncated_bridge_keys=tuple(payload["truncated_bridge_keys"]),
        cap_truncated_bridge_keys=tuple(payload["cap_truncated_bridge_keys"]),
        deadline_truncated_bridge_keys=tuple(payload["deadline_truncated_bridge_keys"]),
        cap_value=payload["cap_value"],
        elapsed_note=payload.get("elapsed_note"),
    )


@dataclass(frozen=True, slots=True)
class BridgeRealizationSnapshotV1:
    """One immutable snapshot of realization state for a complete considered set, mirroring the
    1131 row. ``snapshot_id``/``content_hash`` derive from the captured state, scope scalars and
    the truncation VERDICT — never ``recorded_at``, never the ``elapsed_note``."""

    execution_tier: ExecutionTier
    purpose: str
    entries: tuple[BridgeRealizationSnapshotEntryV1, ...]
    truncation: SnapshotTruncationV1
    execution_context_revision_id: str | None = None
    recorded_at: datetime | None = None
    content_hash: str = field(init=False, default="")
    snapshot_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_tier", _tier(self.execution_tier))
        object.__setattr__(self, "purpose", _purpose(self.purpose))
        object.__setattr__(self, "entries", tuple(self.entries))
        content_hash = materialize_hash(self.content_payload())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "snapshot_id", f"{SNAPSHOT_ID_PREFIX}{content_hash}")

    @property
    def complete(self) -> bool:
        """The law consumers must check: NOTHING downstream may treat a truncated snapshot as a
        complete considered set."""
        return self.truncation.cause == TRUNCATION_NONE

    def content_payload(self) -> dict[str, Any]:
        """Canonical serialization — the captured semantics only, never provenance/wall-clock."""
        return {
            "contract": _SNAPSHOT_CONTRACT,
            "execution_tier": self.execution_tier.value,
            "purpose": self.purpose,
            "execution_context_revision_id": self.execution_context_revision_id,
            "entries": [entry.payload() for entry in self.entries],
            "truncation": self.truncation.identity_payload(),
        }


# ── validation (typed refusals, BEFORE any SQL) ────────────────────────────────────────────────
def _tier(raw: object) -> ExecutionTier:
    if isinstance(raw, ExecutionTier):
        return raw
    try:
        return ExecutionTier(raw)  # by VALUE — the one persisted spelling
    except (TypeError, ValueError):
        raise SnapshotContractDefect(
            f"execution_tier must be one of {sorted(m.value for m in ExecutionTier)} "
            f"(bridge_realization.ExecutionTier — ONE spelling), got {raw!r}") from None


def _purpose(raw: object) -> str:
    if raw not in _PURPOSES:
        raise SnapshotContractDefect(
            f"purpose must be one of {list(_PURPOSES)} (the closed step-3/4 vocabulary), "
            f"got {raw!r}")
    return str(raw)


def _validated_set(bridges: object) -> tuple[ConsideredBridgeV1, ...]:
    items = tuple(bridges) if isinstance(bridges, (tuple, list)) else None
    if not items:
        raise SnapshotContractDefect(
            "the considered set must be a non-empty tuple of ConsideredBridgeV1 — a snapshot of "
            "nothing freezes nothing")
    for item in items:
        if not isinstance(item, ConsideredBridgeV1):
            raise SnapshotContractDefect(
                f"considered items must be ConsideredBridgeV1, got {type(item).__name__}")
    # Dedupe exact duplicates, then pin THE order: lexical (bridge_fact_key, pin).
    return tuple(sorted(set(items), key=ConsideredBridgeV1.sort_key))


def _validated_budget(budget: object) -> CompileBudget:
    if not isinstance(budget, CompileBudget):
        raise SnapshotContractDefect(
            "budget must be the planner's CompileBudget (the existing cap/deadline split is "
            f"EXTENDED here, never reinvented), got {type(budget).__name__}")
    if not isinstance(budget.remaining, int):
        raise SnapshotContractDefect("budget.remaining must be an int")
    return budget


# ── the batched read phases ────────────────────────────────────────────────────────────────────
#: The columns ``events.serde.row_to_event`` consumes, enumerated so the batched read stays on
#: ``conn.execute`` (the counting-cursor idiom counts every query this module issues).
_EVENT_COLUMNS = (
    "event_id", "global_seq", "aggregate", "aggregate_id", "stream_version", "type",
    "schema_version", "table_version", "actor", "payload", "provenance", "occurred_at",
    "recorded_at", "request_id", "feature_id", "run_id", "overlay_fact_id",
    "feature_contract_id", "caused_by",
)


def _read_pinned_revisions(conn: DbConn, pins: list[str]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT realization_revision_id, realization_json "
        "FROM bridge_join_realization_revision WHERE realization_revision_id = ANY(%s)",
        (pins,),
    ).fetchall()
    return {str(revision_id): payload for revision_id, payload in rows}


def _read_current_pointers(conn: DbConn, keys: list[str]) -> list[tuple]:
    return conn.execute(
        "SELECT r.bridge_fact_key, c.realization_id, c.realization_revision_id, "
        "c.safety_status, c.lifecycle, c.pointer_version, r.realization_json "
        "FROM bridge_join_realization_current c "
        "JOIN bridge_join_realization_revision r "
        "  ON r.realization_revision_id = c.realization_revision_id "
        "WHERE r.bridge_fact_key = ANY(%s) ORDER BY c.realization_id",
        (keys,),
    ).fetchall()


def _read_dependencies(
    conn: DbConn, revision_ids: list[str]
) -> dict[str, tuple[tuple[str, str, str], ...]]:
    rows = conn.execute(
        "SELECT realization_revision_id, dependency_kind, dependency_key, dependency_revision "
        "FROM bridge_realization_dependency WHERE realization_revision_id = ANY(%s) "
        "ORDER BY realization_revision_id, dependency_kind, dependency_key, dependency_revision",
        (revision_ids,),
    ).fetchall()
    out: dict[str, list[tuple[str, str, str]]] = {}
    for revision_id, kind, key, revision in rows:
        out.setdefault(str(revision_id), []).append((str(kind), str(key), str(revision)))
    return {revision_id: tuple(deps) for revision_id, deps in out.items()}


def _read_candidate_currentness(
    conn: DbConn, keys: list[str]
) -> dict[str, list[tuple[str, str, str]]]:
    """The batched twin of ``bridge_store.bridge_candidate_currentness`` — SAME fact-key
    resolution SQL, ``= ANY`` over the whole set, plus the pointer's candidate_revision_id (A2's
    currentness pin)."""
    rows = conn.execute(
        "SELECT coalesce(r.assessment_json->>'bridge_fact_key', "
        "               r.assessment_json->>'fact_key') AS fact_key, "
        "c.candidate_id, c.candidate_revision_id, c.lifecycle "
        "FROM governed_candidate_current c "
        "JOIN governed_candidate_revision r "
        "  ON r.candidate_revision_id = c.candidate_revision_id "
        "WHERE coalesce(r.assessment_json->>'bridge_fact_key', "
        "               r.assessment_json->>'fact_key') = ANY(%s) "
        "ORDER BY c.candidate_id",
        (keys,),
    ).fetchall()
    out: dict[str, list[tuple[str, str, str]]] = {}
    for fact_key, candidate_id, candidate_revision_id, lifecycle in rows:
        out.setdefault(str(fact_key), []).append(
            (str(candidate_id), str(candidate_revision_id), str(lifecycle)))
    return out


def _read_link_states(conn: DbConn, keys: list[str]) -> dict[str, Any]:
    """One events read for EVERY considered fact key, folded per key through the SAME
    availability authority the per-key reader uses (``link_state_from_stream``)."""
    from featuregen.events.serde import row_to_event

    columns = ", ".join(_EVENT_COLUMNS)
    rows = conn.execute(
        f"SELECT {columns} FROM events "
        "WHERE aggregate = 'overlay_fact' AND aggregate_id = ANY(%s) "
        "ORDER BY aggregate_id, stream_version",
        (keys,),
    ).fetchall()
    streams: dict[str, list] = {}
    for row in rows:
        mapping = dict(zip(_EVENT_COLUMNS, row, strict=True))
        streams.setdefault(str(mapping["aggregate_id"]), []).append(row_to_event(mapping))
    return {key: link_state_from_stream(streams.get(key, ())) for key in keys}


def _read_binding_revisions(conn: DbConn, revision_ids: list[str]) -> dict[str, tuple]:
    rows = conn.execute(
        "SELECT binding_revision_id, binding_id, content_hash, catalog_logical_ref, "
        "connection_id, physical_id "
        "FROM physical_dataset_binding_revision WHERE binding_revision_id = ANY(%s)",
        (revision_ids,),
    ).fetchall()
    return {str(row[0]): tuple(row[1:]) for row in rows}


def _binding_stored(endpoint, stored: dict[str, tuple]) -> bool:
    """The batched twin of ``bridge_store._binding_revision_is_stored`` — same field-by-field
    comparison against the persisted revision row."""
    binding = endpoint.physical_binding
    revision_id = endpoint.binding_revision_id
    if binding is None or revision_id is None:
        return False
    return stored.get(revision_id) == (
        binding.binding_id,
        binding.content_hash,
        binding.catalog_logical_ref,
        binding.connection_id,
        binding.identity.table_id,
    )


# ── the builder ────────────────────────────────────────────────────────────────────────────────
def build_bridge_realization_snapshot(
    conn: DbConn,
    *,
    bridges: tuple[ConsideredBridgeV1, ...],
    execution_tier: ExecutionTier | str,
    purpose: str,
    budget: CompileBudget,
    execution_context_revision_id: str | None = None,
) -> BridgeRealizationSnapshotV1:
    """Capture, persist and return ONE immutable realization snapshot for the considered set.

    A CONSTANT number of batched queries regardless of set size, all on the caller's connection
    inside the caller's transaction (one snapshot = one read moment; no per-bridge re-reads after
    the batch). See the module docstring for the cap/deadline law and THE PINNED ORDER.
    """
    tier = _tier(execution_tier)
    purpose = _purpose(purpose)
    ordered = _validated_set(bridges)
    budget = _validated_budget(budget)
    if execution_context_revision_id is not None and (
        not isinstance(execution_context_revision_id, str)
        or not execution_context_revision_id.strip()
    ):
        raise SnapshotContractDefect(
            "execution_context_revision_id must be a non-blank string when given")

    def expired() -> bool:
        return budget.clock() >= budget.deadline_monotonic

    # THE CAP, first and deterministic: the first `remaining` items in the pinned order.
    cap = max(budget.remaining, 0)
    admitted = ordered[:cap]
    cap_truncated = tuple(item.bridge_fact_key for item in ordered[cap:])
    if cap_truncated and budget.stopped_by_time is None:
        budget.stopped_by_time = False  # the count bound fired (the shadow convention)

    if execution_context_revision_id is not None:
        from featuregen.overlay.upload.execution_context import (
            load_execution_context_revision,
        )

        context = load_execution_context_revision(conn, execution_context_revision_id)
        if context is None:
            raise SnapshotContractDefect(
                f"execution context revision {execution_context_revision_id!r} does not exist — "
                "a snapshot pins revisions that exist, never ones it would have to invent")
        if context.execution_tier is not tier or context.purpose != purpose:
            raise SnapshotContractDefect(
                "the pinned execution-context revision disagrees with the requested "
                f"tier/purpose ({context.execution_tier.value}/{context.purpose} vs "
                f"{tier.value}/{purpose})")

    entries: tuple[BridgeRealizationSnapshotEntryV1, ...] = ()
    deadline_truncated: tuple[str, ...] = ()
    elapsed_note: str | None = None

    def deadline_bites(phase: str) -> bool:
        nonlocal deadline_truncated, elapsed_note
        if not expired():
            return False
        deadline_truncated = tuple(item.bridge_fact_key for item in admitted)
        elapsed_note = (
            f"deadline_monotonic={budget.deadline_monotonic!r} reached at clock="
            f"{budget.clock()!r} before phase {phase}; the admitted set was abandoned whole "
            "(batched phases are all-or-nothing)")
        if budget.stopped_by_time is None:
            budget.stopped_by_time = True
        return True

    if admitted and not deadline_bites("pinned-revisions"):
        keys = sorted({item.bridge_fact_key for item in admitted})
        pins = sorted({
            item.pinned_realization_revision_id
            for item in admitted
            if item.pinned_realization_revision_id is not None
        })
        pinned_rows = _read_pinned_revisions(conn, pins)
        pointer_rows = _read_current_pointers(conn, keys)

        pointers_by_realization: dict[str, tuple] = {}
        pointers_by_key: dict[str, list[tuple]] = {}
        for row in pointer_rows:
            fact_key, realization_id = str(row[0]), str(row[1])
            pointers_by_realization[realization_id] = row
            pointers_by_key.setdefault(fact_key, []).append(row)

        # Resolve every admitted item to its exact revision IN MEMORY (parse once per revision).
        revisions: dict[str, BridgeJoinRealizationRevisionV1] = {}

        def _revision_of(revision_id: str, payload: dict[str, Any]):
            if revision_id not in revisions:
                revision = realization_from_json(payload)
                if revision.realization_revision_id != revision_id:
                    raise BridgeStoreCorruption(
                        f"realization identity mismatch for {revision_id}")
                revisions[revision_id] = revision
            return revisions[revision_id]

        resolved: list[tuple[ConsideredBridgeV1, Any, bool | None]] = []
        for item in admitted:
            pin = item.pinned_realization_revision_id
            if pin is not None:
                payload = pinned_rows.get(pin)
                if payload is None:
                    resolved.append((item, None, False))
                    continue
                revision = _revision_of(pin, payload)
                if revision.bridge_fact_key != item.bridge_fact_key:
                    raise SnapshotContractDefect(
                        f"pin {pin!r} belongs to bridge {revision.bridge_fact_key!r}, not "
                        f"{item.bridge_fact_key!r} — a considered item may only pin its own "
                        "link's realization")
                resolved.append((item, revision, True))
                continue
            active = [
                row for row in pointers_by_key.get(item.bridge_fact_key, ())
                if str(row[4]) == "active"
            ]
            if not active:
                resolved.append((item, None, None))
                continue
            first = min(active, key=lambda row: str(row[1]))  # lexically-first: deterministic
            resolved.append((item, _revision_of(str(first[2]), first[6]), None))

        if not deadline_bites("dependencies"):
            revision_ids = sorted(revisions)
            dependency_rows = _read_dependencies(conn, revision_ids)
            candidate_rows = _read_candidate_currentness(conn, keys)

            if not deadline_bites("link-states"):
                link_states = _read_link_states(conn, keys)
                binding_ids = sorted({
                    endpoint.binding_revision_id
                    for revision in revisions.values()
                    for endpoint in (revision.from_endpoint, revision.to_endpoint)
                    if endpoint.binding_revision_id is not None
                })
                binding_rows = _read_binding_revisions(conn, binding_ids)

                built: list[BridgeRealizationSnapshotEntryV1] = []
                for item, revision, pin_found in resolved:
                    key = item.bridge_fact_key
                    state = link_states[key]
                    candidates = candidate_rows.get(key, [])
                    active_candidates = [
                        row for row in candidates if row[2] == "active"]
                    currentness = (
                        None if not candidates else bool(active_candidates))
                    candidate_revision_id = (
                        active_candidates[0][1] if active_candidates else None)
                    pointer = (
                        pointers_by_realization.get(revision.realization_id)
                        if revision is not None else None)
                    dependencies = (
                        dependency_rows.get(revision.realization_revision_id, ())
                        if revision is not None else ())
                    built.append(BridgeRealizationSnapshotEntryV1(
                        bridge_fact_key=key,
                        pinned_realization_revision_id=item.pinned_realization_revision_id,
                        pin_found=pin_found,
                        realization_revision_id=(
                            None if revision is None
                            else revision.realization_revision_id),
                        realization_id=(
                            None if revision is None else revision.realization_id),
                        cardinality=(
                            None if revision is None
                            else revision.cardinality.identity_payload()),
                        cardinality_basis=(
                            None if revision is None
                            else revision.cardinality_basis.value),
                        safety_status=None if pointer is None else str(pointer[3]),
                        lifecycle=None if pointer is None else str(pointer[4]),
                        pointer_version=None if pointer is None else int(pointer[5]),
                        current_pointer_revision_id=(
                            None if pointer is None else str(pointer[2])),
                        current_realization_ids=tuple(sorted(
                            str(row[1]) for row in pointers_by_key.get(key, ()))),
                        candidate_revision_id=candidate_revision_id,
                        overlay_head_event_id=state.overlay_head_event_id,
                        link_available=state.availability is LinkAvailability.AVAILABLE,
                        candidate_currentness=currentness,
                        scope_execution_tier=(
                            None if revision is None
                            else revision.applicability_scope.execution_tier.value),
                        scope_purposes=(
                            () if revision is None
                            else tuple(revision.applicability_scope.purposes)),
                        scope_environment=(
                            None if revision is None
                            else revision.applicability_scope.environment),
                        from_binding_revision_id=(
                            None if revision is None
                            else revision.from_endpoint.binding_revision_id),
                        to_binding_revision_id=(
                            None if revision is None
                            else revision.to_endpoint.binding_revision_id),
                        from_binding_revision_stored=(
                            revision is not None
                            and _binding_stored(revision.from_endpoint, binding_rows)),
                        to_binding_revision_stored=(
                            revision is not None
                            and _binding_stored(revision.to_endpoint, binding_rows)),
                        dependency_snapshot_id=(
                            None if revision is None else revision.dependency_snapshot_id),
                        dependencies=dependencies,
                        dependency_snapshot_agrees=(
                            None if revision is None
                            else revision.dependency_snapshot_id
                            == bridge_dependency_snapshot_id(tuple(
                                BridgeDependencyRefV1(*dependency)
                                for dependency in dependencies))),
                        has_unresolved_requirements=(
                            None if revision is None
                            else revision.has_unresolved_requirements),
                    ))
                entries = tuple(built)

    budget.remaining -= len(entries)

    if cap_truncated and deadline_truncated:
        cause = TRUNCATION_CAP_AND_DEADLINE
    elif deadline_truncated:
        cause = TRUNCATION_DEADLINE
    elif cap_truncated:
        cause = TRUNCATION_CAP
    else:
        cause = TRUNCATION_NONE
    # The union list is disclosed in THE PINNED ORDER: the deadline abandons the admitted HEAD
    # of the order while the cap truncates its TAIL, so deadline keys precede cap keys.
    truncation = SnapshotTruncationV1(
        cause=cause,
        truncated_bridge_keys=deadline_truncated + cap_truncated,
        cap_truncated_bridge_keys=cap_truncated,
        deadline_truncated_bridge_keys=deadline_truncated,
        cap_value=cap if cap_truncated else None,
        elapsed_note=elapsed_note,
    )

    snapshot = BridgeRealizationSnapshotV1(
        execution_tier=tier,
        purpose=purpose,
        entries=entries,
        truncation=truncation,
        execution_context_revision_id=execution_context_revision_id,
    )
    conn.execute(
        "INSERT INTO bridge_realization_snapshot "
        "  (snapshot_id, execution_context_revision_id, execution_tier, purpose, captured, "
        "   truncation, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (snapshot_id) DO NOTHING",
        (
            snapshot.snapshot_id,
            snapshot.execution_context_revision_id,
            snapshot.execution_tier.value,
            snapshot.purpose,
            Jsonb([entry.payload() for entry in snapshot.entries]),
            Jsonb(snapshot.truncation.payload()),
            snapshot.content_hash,
        ),
    )
    stored = load_bridge_realization_snapshot(conn, snapshot.snapshot_id)
    if stored is None:
        raise SnapshotStoreConflict(
            f"bridge realization snapshot {snapshot.snapshot_id} did not persist")
    return stored


def load_bridge_realization_snapshot(
    conn: DbConn, snapshot_id: str
) -> BridgeRealizationSnapshotV1 | None:
    """Load and CONTENT-VERIFY one snapshot; ``None`` when absent, corruption raises.

    The loaded value re-derives its ``snapshot_id`` from the captured state — a row that cannot
    reproduce its own primary key is a store-integrity failure, never served. Consumers MUST
    check :attr:`BridgeRealizationSnapshotV1.complete` before treating the entries as the whole
    considered set."""
    row = conn.execute(
        "SELECT execution_context_revision_id, execution_tier, purpose, captured, truncation, "
        "content_hash, recorded_at "
        "FROM bridge_realization_snapshot WHERE snapshot_id = %s",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    snapshot = BridgeRealizationSnapshotV1(
        execution_tier=ExecutionTier(row[1]),
        purpose=row[2],
        entries=tuple(_entry_from_payload(payload) for payload in row[3]),
        truncation=_truncation_from_payload(row[4]),
        execution_context_revision_id=row[0],
        recorded_at=row[6],
    )
    if snapshot.content_hash != row[5] or snapshot.snapshot_id != snapshot_id:
        raise SnapshotStoreConflict(
            f"bridge realization snapshot {snapshot_id} fails content verification")
    return snapshot
