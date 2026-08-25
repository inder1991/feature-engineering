"""Server-owned execution-context revisions (cross-catalog serving plan task A3; migration 1130).

One immutable, content-addressed revision per ``(environment_id, execution_tier, purpose)``
triple. This is the identity :class:`~featuregen.overlay.upload.planner.physical_plan_v1.\
PhysicalExecutionPlanV1` pins as ``execution_context_revision_id``, and the id half of R3's
adoption key ``(selection_revision_id, execution_context_revision_id)``.

**R2 — the context never touches logical identity.** Nothing here appears in
``LogicalFeaturePlanV2`` or :func:`~featuregen.overlay.upload.planner.logical_plan_v2.\
logical_digest`; the test suite pins a logical plan's digest IDENTICAL across two different
context revisions.

**Reused vocabularies, never redefined.** ``execution_tier`` is
:class:`~featuregen.overlay.upload.bridge_realization.ExecutionTier` (the closed StrEnum whose
PERSISTED spellings are its lowercase values, the form every jsonb applicability-scope payload
already stores); ``purpose`` is the step-3/4 vocabulary — today exactly
:data:`~featuregen.overlay.upload.bridge_realization_proposal.FEATURE_GENERATION_PURPOSE`.
``environment_id`` binds to the platform's EXISTING environment surface, which is a COLUMN
CONVENTION (``data_source_connection.environment_id`` from 1037/1041 and the
``btrim(environment_id) <> ''`` CHECK every later table carries) — there is no canonical
environment table to foreign-key, so the binding is this store's validation plus the migration's
named CHECK.

**Absence semantics.** ``EXECUTION_CONTEXT_MISSING`` (registered by A1 in
``semantic_eligibility_reasons`` with its disposition row) is emitted by the CONSUMING assessment
and route layers when no adopted context exists — this store never emits it; it only mints and
loads revisions. The interim ``environment`` string plumbing in
:mod:`~featuregen.overlay.upload.bridge_realization_proposal` is replaced by this type at B2b,
not here.

Store discipline: ``conn`` positional, everything else keyword-only; validation refuses unknown
tier/purpose and blank environments with :class:`ExecutionContextDefect` BEFORE any SQL; the
insert is ``ON CONFLICT (revision_id) DO NOTHING`` with a content-verified read-back (the
``temporal_policy_store``/``crosswalk_observation_store`` idiom), and the table's UNIQUE
``content_hash`` is the DB backstop that makes concurrent-ish writers converge on one row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from featuregen.contracts import DbConn
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.bridge_realization_proposal import FEATURE_GENERATION_PURPOSE

__all__ = [
    "EXECUTION_CONTEXT_ID_PREFIX",
    "EXECUTION_CONTEXT_PURPOSES",
    "ExecutionContextDefect",
    "ExecutionContextRevisionV1",
    "ExecutionContextStoreConflict",
    "ensure_execution_context_revision",
    "load_execution_context_revision",
]

_CONTEXT_CONTRACT = "execution_context_revision_v1"

#: Deterministic id prefix for an execution-context revision (the ``dtp_``/``jvp_`` family).
EXECUTION_CONTEXT_ID_PREFIX = "ecx_"

#: The CLOSED purpose vocabulary — the step-3/4 constant, never a second spelling. Widening it
#: means widening migration 1130's named CHECK, which is a new migration and a review gate.
EXECUTION_CONTEXT_PURPOSES: tuple[str, ...] = (FEATURE_GENERATION_PURPOSE,)


class ExecutionContextDefect(ValueError):
    """A refused execution-context declaration: unknown tier, unknown purpose, or a blank
    environment — raised BEFORE any SQL."""


class ExecutionContextStoreConflict(RuntimeError):
    """The store and the table disagree (a row failing content verification, or an ensure whose
    read-back found nothing) — corruption, never served."""


def _environment(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ExecutionContextDefect(
            f"environment_id must be a non-blank string, got {raw!r} — the platform's "
            "environment surface is the environment_id column convention (1037/1041), and a "
            "blank one addresses nothing")
    return raw.strip()


def _tier(raw: object) -> ExecutionTier:
    if isinstance(raw, ExecutionTier):
        return raw
    try:
        return ExecutionTier(raw)  # by VALUE — the one persisted spelling
    except ValueError:
        raise ExecutionContextDefect(
            f"execution_tier must be one of {sorted(m.value for m in ExecutionTier)} "
            f"(bridge_realization.ExecutionTier — ONE spelling), got {raw!r}") from None


def _purpose(raw: object) -> str:
    if raw not in EXECUTION_CONTEXT_PURPOSES:
        raise ExecutionContextDefect(
            f"purpose must be one of {list(EXECUTION_CONTEXT_PURPOSES)} (the closed step-3/4 "
            f"vocabulary), got {raw!r}")
    return str(raw)


@dataclass(frozen=True, slots=True)
class ExecutionContextRevisionV1:
    """One immutable execution-context revision, mirroring the 1130 row.

    ``content_hash``/``revision_id`` derive from the SEMANTIC triple only; ``recorded_at`` is DB
    provenance (assigned by the table default, filled on load, never hashed)."""

    environment_id: str
    execution_tier: ExecutionTier
    purpose: str
    recorded_at: datetime | None = None
    content_hash: str = field(init=False, default="")
    revision_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment_id", _environment(self.environment_id))
        object.__setattr__(self, "execution_tier", _tier(self.execution_tier))
        object.__setattr__(self, "purpose", _purpose(self.purpose))
        content_hash = materialize_hash(self.content_payload())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "revision_id", f"{EXECUTION_CONTEXT_ID_PREFIX}{content_hash}")

    def content_payload(self) -> dict[str, Any]:
        """Canonical serialization — the semantic triple only, never provenance."""
        return {
            "contract": _CONTEXT_CONTRACT,
            "environment_id": self.environment_id,
            "execution_tier": self.execution_tier.value,
            "purpose": self.purpose,
        }


def ensure_execution_context_revision(conn: DbConn, *, environment_id: str,
                                      execution_tier: ExecutionTier | str,
                                      purpose: str) -> str:
    """Mint-or-find the revision for one semantic triple and return its ``revision_id``.

    Content-addressed idempotency: the same triple always answers the SAME id and never a second
    row. Validation happens at construction — an unknown tier/purpose or blank environment
    refuses with :class:`ExecutionContextDefect` before the connection is touched."""
    revision = ExecutionContextRevisionV1(
        environment_id=environment_id, execution_tier=execution_tier, purpose=purpose)
    conn.execute(
        "INSERT INTO execution_context_revision "
        "  (revision_id, environment_id, execution_tier, purpose, content_hash) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (revision_id) DO NOTHING",
        (revision.revision_id, revision.environment_id, revision.execution_tier.value,
         revision.purpose, revision.content_hash))
    if load_execution_context_revision(conn, revision.revision_id) is None:
        raise ExecutionContextStoreConflict(
            f"execution context revision {revision.revision_id} did not persist")
    return revision.revision_id


def load_execution_context_revision(conn: DbConn,
                                    revision_id: str) -> ExecutionContextRevisionV1 | None:
    """Load and CONTENT-VERIFY one revision; ``None`` when absent, corruption raises."""
    row = conn.execute(
        "SELECT environment_id, execution_tier, purpose, content_hash, recorded_at "
        "FROM execution_context_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    revision = ExecutionContextRevisionV1(
        environment_id=row[0], execution_tier=ExecutionTier(row[1]), purpose=row[2],
        recorded_at=row[4])
    if revision.content_hash != row[3] or revision.revision_id != revision_id:
        raise ExecutionContextStoreConflict(
            f"execution context revision {revision_id} fails content verification")
    return revision
