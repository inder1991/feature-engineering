"""B2: R3's PHYSICAL-PLAN ADOPTION chain — append-only, user-confirmed, environment-scoped
(migration 1136).

An adoption is the moment a person says *"generate with THIS realization"*. Everything downstream
pins the exact revision it produces, which is what makes the plan's claim — the join the user
confirmed is the join that generates — true rather than conventional.

**Environment scope is the chain key (R3).** The chain is keyed on
``(selection_revision_id, execution_context_revision_id)``, and 1130's context revision IS
``(environment_id, execution_tier, purpose)``. So one selection may hold a sandbox adoption and a
production adoption at the same time: two roots, two heads, both current, neither aware of the
other. Nothing here is global.

**The CAS is two partial-unique indexes, and nothing else.** The head is the row nothing supersedes
— found by ABSENCE of a successor, never by newest timestamp, because two adoptions recorded in one
transaction share a clock and "newest by time" would be a coin flip (1072's ``current_target_reading``
made the same choice for the same reason). Two confirmations racing on one head both try to insert a
successor of the same predecessor: ``UNIQUE (supersedes) WHERE supersedes IS NOT NULL`` lets exactly
one land. Two racing FIRST confirmations both try to insert a root:
``UNIQUE (selection, context) WHERE supersedes IS NULL`` lets exactly one land. The loser is not
guessed at — the insert is ``ON CONFLICT DO NOTHING`` and the read-back finds nothing, which is
:class:`AdoptionConflict`. A read-then-write check in Python would have had a window; this has none.

**Idempotent re-confirmation.** Confirming the physical plan the head ALREADY names is not a second
adoption: it returns the head, one row, unchanged — including its original ``confirmed_by``. And
because the content hash is SEMANTIC ONLY (selection, context, plan, predecessor — never the actor
and never the clock), even two callers who somehow reached the insert would converge on one row
rather than two revisions of one confirmation.

**What an adoption does NOT copy.** Not the logical digest, not the guard policy: both are already
inside the physical plan it names, and a second writable copy could disagree with the plan it claims
to describe. This module reads them THROUGH the pinned plan and verifies the agreement.

▲ **A carry-forward from step 3, undischarged and worth stating where it bites.** ``physical_digest``
is computed over a payload that INCLUDES the logical digest the plan realizes, so one physical
realization can never be reused across two logical plans — the identical column pairs, predicates,
normalization and temporal binding hash to a DIFFERENT physical plan under a different meaning. That
is safe (nothing can be re-aimed) and it is a real cost (no shared realization identity, and the
adoption chain is per-selection rather than per-realization). It is a step-3 contract decision, not
something this store may change; C3's card work should know it before it looks for physical reuse.

**Foreign keys** follow B1's doctrine. ``selection_revision_id`` is a real FK (1072 has no truncate
raiser). Every other leg — the execution context (1130), the physical plan (1134), the predecessor
(this table) — points at an append-only table whose BEFORE TRUNCATE raiser an FK would disarm, so
each is a VERIFYING LOAD instead: "an FK proves a row exists; a verifying load proves it can still
reproduce its identity."

Store discipline: ``conn`` positional, everything else keyword-only; typed refusals BEFORE any SQL;
``ON CONFLICT DO NOTHING`` with a verified read-back.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.execution_context import load_execution_context_revision
from featuregen.overlay.upload.planner.identity_store import (
    PHYSICAL_PLAN_ID_PREFIX,
    load_physical_execution_plan,
)
from featuregen.overlay.upload.planner.join_policy_store import require_join_validation_policy

if TYPE_CHECKING:
    from featuregen.contracts import DbConn

__all__ = [
    "ADOPTION_ID_PREFIX",
    "AdoptionConflict",
    "AdoptionDefect",
    "PhysicalPlanAdoptionV1",
    "adoption_chain",
    "confirm_physical_plan_adoption",
    "current_physical_plan_adoption",
    "load_physical_plan_adoption",
]

#: Deterministic id prefix for an adoption revision (the ``ecx_``/``jvp_``/``lfp_`` family).
ADOPTION_ID_PREFIX = "spa_"

_ADOPTION_CONTRACT = "selection_physical_plan_adoption_v1"

_COLUMNS = ("adoption_revision_id, selection_revision_id, execution_context_revision_id, "
            "physical_plan_revision_id, supersedes_adoption_revision_id, confirmed_by, "
            "confirmed_at, content_hash, recorded_at")


class AdoptionDefect(ValueError):
    """A refused adoption: a blank input, an identity nobody persisted, a plan that does not belong
    to this selection's meaning or to this context — raised BEFORE any write."""


class AdoptionConflict(RuntimeError):
    """The chain and this caller disagree. Raised for TWO different situations, and a caller that
    treats them alike will not survive the second:

    * from :func:`confirm_physical_plan_adoption` — someone else moved the chain first. The row this
      confirmation would have written lost its partial-unique index to a concurrent confirmation.
      RETRYABLE: re-read the head and decide again.
    * from :func:`load_physical_plan_adoption` / :func:`current_physical_plan_adoption` — a stored
      row does not reproduce its own identity. That is CORRUPTION, and it is not retryable: a retry
      loop that re-reads the head to try again will read the same bad row and spin forever. Its
      message says "does not reproduce its own identity"; a retry loop must stop on it and escalate.
    """


@dataclass(frozen=True, slots=True)
class PhysicalPlanAdoptionV1:
    """One adoption revision, mirroring the 1136 row.

    ``confirmed_by``/``confirmed_at``/``recorded_at`` are PROVENANCE: they are outside
    ``content_hash``, so re-confirming the same plan on the same head is the same revision."""

    adoption_revision_id: str
    selection_revision_id: str
    execution_context_revision_id: str
    physical_plan_revision_id: str
    supersedes_adoption_revision_id: str | None
    confirmed_by: str
    confirmed_at: str
    content_hash: str
    recorded_at: datetime | None = None


def _content_hash(*, selection_revision_id: str, execution_context_revision_id: str,
                  physical_plan_revision_id: str, supersedes: str | None) -> str:
    """The SEMANTIC identity of one adoption: whose choice, in which environment, of which plan, at
    which point in the chain. The actor and the clock are deliberately absent."""
    return materialize_hash({
        "contract": _ADOPTION_CONTRACT,
        "selection_revision_id": selection_revision_id,
        "execution_context_revision_id": execution_context_revision_id,
        "physical_plan_revision_id": physical_plan_revision_id,
        "supersedes_adoption_revision_id": supersedes,
    })


def _text(value: object, *, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdoptionDefect(f"{what} must be a non-empty string, got {value!r}")
    return value.strip()


def _row(row) -> PhysicalPlanAdoptionV1:
    adoption = PhysicalPlanAdoptionV1(*row)
    recomputed = _content_hash(
        selection_revision_id=adoption.selection_revision_id,
        execution_context_revision_id=adoption.execution_context_revision_id,
        physical_plan_revision_id=adoption.physical_plan_revision_id,
        supersedes=adoption.supersedes_adoption_revision_id)
    if recomputed != adoption.content_hash or \
            adoption.adoption_revision_id != f"{ADOPTION_ID_PREFIX}{recomputed}":
        raise AdoptionConflict(
            f"adoption {adoption.adoption_revision_id} does not reproduce its own identity — it "
            "would pin a build to a realization it no longer names")
    return adoption


def load_physical_plan_adoption(conn: DbConn,
                                adoption_revision_id: str) -> PhysicalPlanAdoptionV1 | None:
    """Load and CONTENT-VERIFY one adoption; ``None`` when absent, corruption raises."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM selection_physical_plan_adoption_revision "
        "WHERE adoption_revision_id = %s", (adoption_revision_id,)).fetchone()
    return None if row is None else _row(row)


def current_physical_plan_adoption(
    conn: DbConn, *, selection_revision_id: str, execution_context_revision_id: str,
) -> PhysicalPlanAdoptionV1 | None:
    """The head of one scope's chain — the adoption nothing supersedes.

    Found by ABSENCE of a successor, never by newest timestamp: the chain says which adoption is
    current, and a predecessor may be superseded at most once."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM selection_physical_plan_adoption_revision a "
        "WHERE a.selection_revision_id = %s AND a.execution_context_revision_id = %s "
        "  AND NOT EXISTS (SELECT 1 FROM selection_physical_plan_adoption_revision s "
        "                  WHERE s.supersedes_adoption_revision_id = a.adoption_revision_id)",
        (selection_revision_id, execution_context_revision_id)).fetchone()
    return None if row is None else _row(row)


def adoption_chain(conn: DbConn, *, selection_revision_id: str,
                   execution_context_revision_id: str) -> tuple[PhysicalPlanAdoptionV1, ...]:
    """One scope's chain from root to head, walked by supersession rather than by clock."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM selection_physical_plan_adoption_revision "
        "WHERE selection_revision_id = %s AND execution_context_revision_id = %s",
        (selection_revision_id, execution_context_revision_id)).fetchall()
    by_predecessor = {row[4]: _row(row) for row in rows}
    chain: list[PhysicalPlanAdoptionV1] = []
    cursor: str | None = None
    while cursor in by_predecessor:
        adoption = by_predecessor[cursor]
        chain.append(adoption)
        cursor = adoption.adoption_revision_id
    return tuple(chain)


def _verified_scope(conn: DbConn, *, selection_revision_id: str,
                    execution_context_revision_id: str, physical_plan_revision_id: str) -> None:
    """Everything an adoption must be able to prove before it may exist.

    The selection is checked by a foreign key at write; the rest are verifying loads, because their
    tables are append-only and an FK onto them would replace their TRUNCATE raiser with a
    foreign-key refusal (A4's discovery)."""
    selection = conn.execute(
        "SELECT considered_revision_id, option_id FROM feature_selection_revision "
        "WHERE revision_id = %s", (selection_revision_id,)).fetchone()
    if selection is None:
        raise AdoptionDefect(
            f"selection {selection_revision_id!r} does not exist, so there is no choice to adopt a "
            "physical plan for")

    if load_execution_context_revision(conn, execution_context_revision_id) is None:
        raise AdoptionDefect(
            f"the execution context {execution_context_revision_id!r} was never persisted "
            "(migration 1130 / task A3 owns it) — R3 scopes an adoption to the exact environment "
            "it was confirmed in")

    plan = load_physical_execution_plan(conn, physical_plan_revision_id)
    if plan is None:
        raise AdoptionDefect(
            f"the physical execution plan {physical_plan_revision_id!r} was never persisted — an "
            "adoption confirms a realization that exists, never one it would have to invent")
    if plan.execution_context_revision_id != execution_context_revision_id:
        raise AdoptionDefect(
            f"physical plan {physical_plan_revision_id} was built for execution context "
            f"{plan.execution_context_revision_id!r} and cannot be adopted into "
            f"{execution_context_revision_id!r}: a plan's environment is part of its identity, so "
            "adopting it elsewhere would generate something nobody planned")

    # The guard policy the plan pins. B1 could only store this id; with 1136's store it is checked.
    require_join_validation_policy(conn, plan.join_validation_policy_revision_id)

    # ▲ THE MEANING MUST AGREE. The plan realizes a logical digest; the option this selection was
    # made from is bound to one. A pre-plan (legacy) option has no binding at all, and is refused
    # here rather than adopted into a chain that could never say what it generates.
    bound = conn.execute(
        "SELECT logical_digest FROM considered_option_plan_binding "
        "WHERE considered_revision_id = %s AND option_id = %s", selection).fetchone()
    if bound is None:
        raise AdoptionDefect(
            f"selection {selection_revision_id} was made from option {selection[1]!r} of "
            f"{selection[0]!r}, which carries no logical plan binding: it is a PRE-PLAN option and "
            "is refused for cross-catalog generation — bind the option's logical plan first")
    if bound[0] != plan.logical_digest_ref:
        raise AdoptionDefect(
            f"physical plan {physical_plan_revision_id} realizes logical plan "
            f"{plan.logical_digest_ref} but selection {selection_revision_id} chose an option bound "
            f"to {bound[0]}: an adoption may not re-aim what a person chose at a different feature")


def confirm_physical_plan_adoption(
    conn: DbConn, *, selection_revision_id: str, execution_context_revision_id: str,
    physical_plan_revision_id: str, confirmed_by: str, confirmed_at: str,
) -> tuple[PhysicalPlanAdoptionV1, bool]:
    """Confirm one physical plan for one selection in one environment. Returns ``(adoption, created)``.

    R3's POST names ``physical_plan_revision_id`` — never a chain position. Where in the chain the
    confirmation lands is derived here from the CURRENT head, so a caller can neither skip a
    predecessor nor resurrect a superseded one.

    Idempotent: confirming what the head already names returns the head and creates nothing.

    Raises:
        AdoptionDefect: a blank input, an identity nobody persisted, a plan built for another
            context, a guard policy nobody declared, a pre-plan option, or a plan whose meaning is
            not the meaning this selection chose. All BEFORE any write.
        AdoptionConflict: another confirmation moved this chain first.
    """
    selection_revision_id = _text(selection_revision_id, what="selection_revision_id")
    execution_context_revision_id = _text(
        execution_context_revision_id, what="execution_context_revision_id")
    physical_plan_revision_id = _text(
        physical_plan_revision_id, what="physical_plan_revision_id")
    if not physical_plan_revision_id.startswith(PHYSICAL_PLAN_ID_PREFIX):
        raise AdoptionDefect(
            f"physical_plan_revision_id must be a {PHYSICAL_PLAN_ID_PREFIX}<digest> id, got "
            f"{physical_plan_revision_id!r} — R3's POST names the physical plan revision itself")
    confirmed_by = _text(confirmed_by, what="confirmed_by")
    confirmed_at = _text(confirmed_at, what="confirmed_at")

    _verified_scope(conn, selection_revision_id=selection_revision_id,
                    execution_context_revision_id=execution_context_revision_id,
                    physical_plan_revision_id=physical_plan_revision_id)

    head = current_physical_plan_adoption(
        conn, selection_revision_id=selection_revision_id,
        execution_context_revision_id=execution_context_revision_id)
    if head is not None and head.physical_plan_revision_id == physical_plan_revision_id:
        # Re-confirming what is already adopted is ONE decision recorded once, not a second one.
        return head, False

    supersedes = None if head is None else head.adoption_revision_id
    content_hash = _content_hash(
        selection_revision_id=selection_revision_id,
        execution_context_revision_id=execution_context_revision_id,
        physical_plan_revision_id=physical_plan_revision_id, supersedes=supersedes)
    adoption_revision_id = f"{ADOPTION_ID_PREFIX}{content_hash}"

    # ▲ NO CONFLICT TARGET, DELIBERATELY. Three unique constraints can refuse this row — the primary
    # key, the partial-unique ROOT and the partial-unique SUCCESSOR — and only a bare DO NOTHING
    # covers all three. The read-back then tells the two outcomes apart: the row is there (we wrote
    # it, or an identical confirmation already had) or it is not (someone else moved the chain).
    inserted = conn.execute(
        "INSERT INTO selection_physical_plan_adoption_revision "
        "  (adoption_revision_id, selection_revision_id, execution_context_revision_id, "
        "   physical_plan_revision_id, supersedes_adoption_revision_id, confirmed_by, "
        "   confirmed_at, content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING RETURNING adoption_revision_id",
        (adoption_revision_id, selection_revision_id, execution_context_revision_id,
         physical_plan_revision_id, supersedes, confirmed_by, confirmed_at, content_hash)
    ).fetchone()
    stored = load_physical_plan_adoption(conn, adoption_revision_id)
    if stored is None:
        raise AdoptionConflict(
            f"another confirmation moved the adoption chain for selection "
            f"{selection_revision_id} in context {execution_context_revision_id} first: this "
            f"confirmation would have superseded {supersedes!r}, which already has a successor. "
            "Re-read the head and confirm again")
    return stored, inserted is not None
