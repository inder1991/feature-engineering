"""B2: THE TOTAL BINDING CHAIN — one logical identity carried from the option a person was shown to
the build member that generates (migration 1135).

Four links, each one making a specific disagreement UNREPRESENTABLE rather than merely checked:

    considered_option_plan_binding    the served option IS a logical plan
    formula_draft_plan_binding        the draft was authored against THAT plan
    selection_formula_plan_binding    the selection pins THAT draft's plan
    build_member_combined_binding     the build member generates from THAT plan, the physical
                                      realization its user adopted, and one render profile

**The chain is closed by composite foreign keys, not by validation.** A draft binding carries
``(considered_revision_id, option_id, logical_digest)`` into the option binding's unique key, so a
draft cannot name a plan that is not its option's. A selection binding carries
``(formula_draft_id, logical_digest)`` into the draft binding's, so a selection cannot be bound
across plans. A combined binding carries ``(selection_revision_id, formula_draft_id,
logical_digest)`` into the selection binding's. Worker-side validation is the kind of check that is
correct on the day it is written and bypassed by the next caller (1101's own words); a shared column
inside two composite keys cannot be satisfied with two different values by anyone, ever.

**Totality is DDL, and it is scoped.** 1135's header states the mechanism per table — a
parent-carried binding id with a composite FK where insert order allows it (``build_set_member``), a
DEFERRED CONSTRAINT TRIGGER checked at COMMIT where circularity forbids it (option, draft, member).
It fires only for rows in the PLANNED lane: an option declares itself with
``requires_logical_plan_binding``, a draft inherits the requirement from its option, a build member
inherits it from its selection's plan binding. Everything else is honestly PRE-PLAN, and this module
refuses those rows for cross-catalog generation (:func:`require_planned_selection`) rather than
back-filling a plan nobody chose.

**Which legs are foreign keys** is decided by A4's discovery, not by habit: a leg is a real FK when
the referenced table has no BEFORE TRUNCATE raiser to disarm (``formula_draft``,
``selection_formula_binding``, the binding tables themselves); it is a store check when the
referenced table is append-only and guarded (``semantic_option_decision``, all of 1134, 1136's
adoption). B1's doctrine: "an FK proves a row exists; a verifying load proves it can still reproduce
its identity" — and the store checks here are NOT all the same strength, so they are named
separately rather than blurred into one flattering word:

* the 1134 and 1136 legs are VERIFYING LOADS. Each goes through the owning store's loader on a
  derivable primary key, which rebuilds the typed contract and recomputes the digest, so a row that
  has drifted from the identity it publishes stops the binding.
* ▲ the ``semantic_option_decision`` leg is an EXISTENCE CHECK, and cannot be more than one. That
  table has no reproducible identity to verify against — no content hash over a canonical payload,
  no derivable id — so there is nothing for a load to re-derive and disagree with. It is read with a
  plain ``SELECT`` for the two facts the binding needs (the provenance hash, and the arming marker),
  and the honest description of that is "the row is there", not "the row still proves itself".
  Calling it a verifying load would claim a guarantee this leg does not carry.

**The legacy hashes ride as PROVENANCE** (round 10: relational agreement, never hash equality).
``planning_request_hash`` and ``binding_plan_hash`` are copied from the option and the selection and
stored beside each binding so an operator can trace the request a plan came from. They are NEVER
compared for equality against a logical digest — different payloads, different meanings, and
comparing them would be a check that always fails or always passes without ever saying anything.

Store discipline: ``conn`` positional, everything else keyword-only; typed refusals BEFORE any SQL;
content-addressed or key-addressed idempotency with a verified read-back.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.execution_context import load_execution_context_revision
from featuregen.overlay.upload.planner.adoption_store import (
    current_physical_plan_adoption,
    load_physical_plan_adoption,
)
from featuregen.overlay.upload.planner.identity_store import (
    LOGICAL_PLAN_ID_PREFIX,
    RENDER_PROFILE_ID_PREFIX,
    load_logical_feature_plan,
    load_physical_execution_plan,
    load_render_profile,
)

if TYPE_CHECKING:
    from featuregen.contracts import DbConn

__all__ = [
    "COMBINED_BINDING_ID_PREFIX",
    "BindingChainConflict",
    "BindingChainDefect",
    "CombinedBindingV1",
    "ConsideredOptionPlanBindingV1",
    "FormulaDraftPlanBindingV1",
    "SelectionFormulaPlanBindingV1",
    "assert_build_environment_match",
    "bind_build_member_combined",
    "bind_considered_option_plan",
    "bind_formula_draft_plan",
    "bind_selection_formula_plan",
    "load_combined_binding",
    "load_considered_option_plan_binding",
    "load_formula_draft_plan_binding",
    "load_selection_formula_plan_binding",
    "pin_build_set_member",
    "require_planned_selection",
]

#: Deterministic id prefix for a combined binding (the ``lfp_``/``pxp_``/``rpf_``/``spa_`` family).
COMBINED_BINDING_ID_PREFIX = "cmb_"

_COMBINED_CONTRACT = "build_member_combined_binding_v1"


class BindingChainDefect(ValueError):
    """A refused binding: a blank input, a parent nobody recorded, a PRE-PLAN row asked to serve
    cross-catalog generation, or two facts that describe different work — raised BEFORE any write."""


class BindingChainConflict(RuntimeError):
    """The store and the table disagree — a binding that did not persist, or an existing binding
    that names a different plan than the one just asked for. Never served."""


# ──────────────────────────────────────────────────────────────────────────────────────────────
# the records
# ──────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ConsideredOptionPlanBindingV1:
    """The served option's logical plan. ``planning_request_hash`` is provenance."""

    considered_revision_id: str
    option_id: str
    logical_plan_revision_id: str
    logical_digest: str
    planning_request_hash: str
    recorded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FormulaDraftPlanBindingV1:
    """The draft's logical plan — the same plan as its option's, by composite foreign key."""

    formula_draft_id: str
    considered_revision_id: str
    option_id: str
    logical_plan_revision_id: str
    logical_digest: str
    planning_request_hash: str
    recorded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SelectionFormulaPlanBindingV1:
    """The selection's logical plan. Both legacy hashes ride here as PROVENANCE PINS."""

    selection_revision_id: str
    formula_draft_id: str
    logical_plan_revision_id: str
    logical_digest: str
    planning_request_hash: str
    binding_plan_hash: str
    recorded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CombinedBindingV1:
    """Everything one build member generates from: logical, physical (as adopted, in one execution
    context) and render. Content-addressed over exactly those."""

    combined_binding_id: str
    selection_revision_id: str
    formula_draft_id: str
    logical_digest: str
    physical_plan_revision_id: str
    physical_digest: str
    physical_adoption_revision_id: str
    execution_context_revision_id: str
    render_profile_revision_id: str
    render_profile_digest: str
    content_hash: str
    recorded_at: datetime | None = None


_OPTION_COLUMNS = ("considered_revision_id, option_id, logical_plan_revision_id, logical_digest, "
                   "planning_request_hash, recorded_at")
_DRAFT_COLUMNS = ("formula_draft_id, considered_revision_id, option_id, logical_plan_revision_id, "
                  "logical_digest, planning_request_hash, recorded_at")
_SELECTION_COLUMNS = ("selection_revision_id, formula_draft_id, logical_plan_revision_id, "
                      "logical_digest, planning_request_hash, binding_plan_hash, recorded_at")
_COMBINED_COLUMNS = ("combined_binding_id, selection_revision_id, formula_draft_id, "
                     "logical_digest, physical_plan_revision_id, physical_digest, "
                     "physical_adoption_revision_id, execution_context_revision_id, "
                     "render_profile_revision_id, render_profile_digest, content_hash, "
                     "recorded_at")


def _text(value: object, *, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BindingChainDefect(f"{what} must be a non-empty string, got {value!r}")
    return value.strip()


def _verified_logical_digest(conn: DbConn, logical_plan_revision_id: str) -> str:
    """The VERIFYING LOAD that stands in for the foreign key 1134 cannot carry.

    Returns the plan's digest — recomputed from the stored payload by the 1134 loader, so a plan row
    that has drifted from the identity it publishes stops the binding rather than being bound."""
    logical_plan_revision_id = _text(
        logical_plan_revision_id, what="logical_plan_revision_id")
    if not logical_plan_revision_id.startswith(LOGICAL_PLAN_ID_PREFIX):
        raise BindingChainDefect(
            f"logical_plan_revision_id must be a {LOGICAL_PLAN_ID_PREFIX}<digest> id, got "
            f"{logical_plan_revision_id!r}")
    if load_logical_feature_plan(conn, logical_plan_revision_id) is None:
        raise BindingChainDefect(
            f"the logical plan {logical_plan_revision_id!r} was never persisted (migration 1134 / "
            "task B1 owns it) — a binding pins a meaning that exists, never one it would have to "
            "invent")
    return logical_plan_revision_id[len(LOGICAL_PLAN_ID_PREFIX):]


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 1. the option's logical plan
# ──────────────────────────────────────────────────────────────────────────────────────────────
def bind_considered_option_plan(
    conn: DbConn, *, considered_revision_id: str, option_id: str, logical_plan_revision_id: str,
) -> ConsideredOptionPlanBindingV1:
    """Bind one served option to the logical plan it IS. Idempotent on the pair.

    ``planning_request_hash`` is COPIED from the option's own row rather than accepted from the
    caller: a provenance pin that a caller could type is a provenance pin that can be wrong.

    ▲ THIS IS ALSO THE ARMING CHECK. 1135's option and draft laws are both gated on
    ``semantic_option_decision.requires_logical_plan_binding``, which is set ONCE when the option row
    is written and can never be set later (1063 refuses UPDATE; the production writer ends in
    ``ON CONFLICT DO NOTHING``, so a second, marked write is silently dropped). Binding an UNMARKED
    option would therefore build a chain whose totality triggers are permanently dormant — green
    everywhere, enforcing nothing. Refusing here is what makes that visible at the moment it can
    still be fixed, rather than at the incident that discovers it.

    Raises:
        BindingChainDefect: the option was never recorded, the option was not served with the
            planned marker set, or the logical plan was not persisted.
        BindingChainConflict: the option is already bound to a DIFFERENT plan. One option, one
            meaning — the primary key already forbids two rows; this says which one is there.
    """
    considered_revision_id = _text(considered_revision_id, what="considered_revision_id")
    option_id = _text(option_id, what="option_id")

    option = conn.execute(
        "SELECT planning_request_hash, requires_logical_plan_binding "
        "FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (considered_revision_id, option_id)).fetchone()
    if option is None:
        raise BindingChainDefect(
            f"option {option_id!r} of {considered_revision_id!r} was never recorded, so there is "
            "nothing to bind a plan to")
    if not option[1]:
        raise BindingChainDefect(
            f"option {option_id!r} of {considered_revision_id!r} was not served as a planned "
            "cross-catalog option: `requires_logical_plan_binding` is false, so the totality law "
            "for this option AND for every draft beneath it is DORMANT, and binding a plan here "
            "would produce a chain that looks governed and enforces nothing. The marker is set "
            "ONCE, by the serving path, at the moment the option row is written — 1063 refuses "
            "UPDATE and the production writer ends in ON CONFLICT DO NOTHING, so it can never be "
            "set afterwards and this option can never be planned. Serve the option again with the "
            "marker set")
    digest = _verified_logical_digest(conn, logical_plan_revision_id)

    conn.execute(
        "INSERT INTO considered_option_plan_binding (considered_revision_id, option_id, "
        "  logical_plan_revision_id, logical_digest, planning_request_hash) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (considered_revision_id, option_id) DO NOTHING",
        (considered_revision_id, option_id, logical_plan_revision_id, digest, option[0]))
    stored = load_considered_option_plan_binding(
        conn, considered_revision_id=considered_revision_id, option_id=option_id)
    if stored is None:
        raise BindingChainConflict(
            f"the plan binding for option {option_id!r} of {considered_revision_id!r} did not "
            "persist")
    if stored.logical_digest != digest:
        raise BindingChainConflict(
            f"option {option_id!r} of {considered_revision_id!r} is already bound to logical plan "
            f"{stored.logical_digest}, and was asked to bind {digest}: an option is ONE feature "
            "meaning, and re-aiming it would change what every draft and selection beneath it means")
    return stored


def load_considered_option_plan_binding(
    conn: DbConn, *, considered_revision_id: str, option_id: str,
) -> ConsideredOptionPlanBindingV1 | None:
    """One option's plan binding, or ``None``."""
    row = conn.execute(
        f"SELECT {_OPTION_COLUMNS} FROM considered_option_plan_binding "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (considered_revision_id, option_id)).fetchone()
    return None if row is None else ConsideredOptionPlanBindingV1(*row)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 2. the draft's logical plan
# ──────────────────────────────────────────────────────────────────────────────────────────────
def bind_formula_draft_plan(conn: DbConn, *,
                            formula_draft_id: str) -> FormulaDraftPlanBindingV1:
    """Bind one formula draft to the plan its OPTION carries. Idempotent on the draft.

    The plan is never a parameter: it is read from the draft's option, so a draft cannot be bound to
    a plan its candidate does not have. The composite foreign key would refuse it anyway; deriving
    it means the refusal never has to happen.

    Raises:
        BindingChainDefect: the draft does not exist, or its option is PRE-PLAN (no binding).
    """
    formula_draft_id = _text(formula_draft_id, what="formula_draft_id")
    draft = conn.execute(
        "SELECT considered_revision_id, option_id, planning_request_hash FROM formula_draft "
        "WHERE formula_draft_id = %s", (formula_draft_id,)).fetchone()
    if draft is None:
        raise BindingChainDefect(
            f"formula draft {formula_draft_id!r} does not exist, so there is nothing to bind")
    considered_revision_id, option_id, planning_request_hash = draft

    option_binding = load_considered_option_plan_binding(
        conn, considered_revision_id=considered_revision_id, option_id=option_id)
    if option_binding is None:
        raise BindingChainDefect(
            f"option {option_id!r} of {considered_revision_id!r} carries no logical plan binding: "
            "it is a PRE-PLAN option and is refused for cross-catalog generation — bind the "
            "option's plan first, and never invent one for a draft")

    conn.execute(
        "INSERT INTO formula_draft_plan_binding (formula_draft_id, considered_revision_id, "
        "  option_id, logical_plan_revision_id, logical_digest, planning_request_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (formula_draft_id) DO NOTHING",
        (formula_draft_id, considered_revision_id, option_id,
         option_binding.logical_plan_revision_id, option_binding.logical_digest,
         planning_request_hash))
    stored = load_formula_draft_plan_binding(conn, formula_draft_id)
    if stored is None:
        raise BindingChainConflict(
            f"the plan binding for formula draft {formula_draft_id!r} did not persist")
    if stored.logical_digest != option_binding.logical_digest:
        raise BindingChainConflict(
            f"formula draft {formula_draft_id!r} is already bound to logical plan "
            f"{stored.logical_digest} but its option now carries {option_binding.logical_digest}")
    return stored


def load_formula_draft_plan_binding(conn: DbConn,
                                    formula_draft_id: str) -> FormulaDraftPlanBindingV1 | None:
    """One draft's plan binding, or ``None``."""
    row = conn.execute(
        f"SELECT {_DRAFT_COLUMNS} FROM formula_draft_plan_binding WHERE formula_draft_id = %s",
        (formula_draft_id,)).fetchone()
    return None if row is None else FormulaDraftPlanBindingV1(*row)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 3. the selection's logical plan
# ──────────────────────────────────────────────────────────────────────────────────────────────
def bind_selection_formula_plan(
    conn: DbConn, *, selection_revision_id: str, formula_draft_id: str,
) -> SelectionFormulaPlanBindingV1:
    """Bind one (selection, draft) pin to the draft's logical plan. Idempotent on the pair.

    ▲ This EXTENDS 1101's pin rather than repeating it. 1101's composite foreign keys already proved
    the selection and the draft describe the same work (candidate, option, planning request, formula
    content); this link requires that pin to exist and adds the one fact 1101 could not carry — the
    logical plan both of them are for.

    Raises:
        BindingChainDefect: the 1101 binding is missing (the selection and the draft were never
            pinned to each other), or the draft carries no plan binding.
    """
    selection_revision_id = _text(selection_revision_id, what="selection_revision_id")
    formula_draft_id = _text(formula_draft_id, what="formula_draft_id")

    if conn.execute(
            "SELECT 1 FROM selection_formula_binding WHERE selection_revision_id = %s "
            "AND formula_draft_id = %s",
            (selection_revision_id, formula_draft_id)).fetchone() is None:
        raise BindingChainDefect(
            f"selection {selection_revision_id!r} and formula draft {formula_draft_id!r} are not "
            "pinned to each other (migration 1101's selection_formula_binding): a plan binding "
            "EXTENDS that pin and cannot stand without it")

    draft_binding = load_formula_draft_plan_binding(conn, formula_draft_id)
    if draft_binding is None:
        raise BindingChainDefect(
            f"formula draft {formula_draft_id!r} carries no logical plan binding: it is a PRE-PLAN "
            "draft and is refused for cross-catalog generation")

    selection = conn.execute(
        "SELECT planning_request_hash, binding_plan_hash FROM feature_selection_revision "
        "WHERE revision_id = %s", (selection_revision_id,)).fetchone()
    if selection is None:
        raise BindingChainDefect(
            f"selection {selection_revision_id!r} does not exist")

    conn.execute(
        "INSERT INTO selection_formula_plan_binding (selection_revision_id, formula_draft_id, "
        "  logical_plan_revision_id, logical_digest, planning_request_hash, binding_plan_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (selection_revision_id, formula_draft_id) DO NOTHING",
        (selection_revision_id, formula_draft_id, draft_binding.logical_plan_revision_id,
         draft_binding.logical_digest, selection[0], selection[1]))
    stored = load_selection_formula_plan_binding(
        conn, selection_revision_id=selection_revision_id, formula_draft_id=formula_draft_id)
    if stored is None:
        raise BindingChainConflict(
            f"the plan binding for selection {selection_revision_id!r} and draft "
            f"{formula_draft_id!r} did not persist")
    return stored


def load_selection_formula_plan_binding(
    conn: DbConn, *, selection_revision_id: str, formula_draft_id: str,
) -> SelectionFormulaPlanBindingV1 | None:
    """One selection-formula plan binding, or ``None``."""
    row = conn.execute(
        f"SELECT {_SELECTION_COLUMNS} FROM selection_formula_plan_binding "
        "WHERE selection_revision_id = %s AND formula_draft_id = %s",
        (selection_revision_id, formula_draft_id)).fetchone()
    return None if row is None else SelectionFormulaPlanBindingV1(*row)


def require_planned_selection(conn: DbConn,
                              selection_revision_id: str) -> SelectionFormulaPlanBindingV1:
    """The PRE-PLAN refusal, stated once so every caller says the same thing.

    A selection with no plan binding is not broken — it is a legacy, single-catalog choice made
    before logical plans existed. It is simply not eligible for cross-catalog generation, and saying
    so is different from back-filling it with a plan nobody chose."""
    row = conn.execute(
        f"SELECT {_SELECTION_COLUMNS} FROM selection_formula_plan_binding "
        "WHERE selection_revision_id = %s ORDER BY formula_draft_id LIMIT 1",
        (selection_revision_id,)).fetchone()
    if row is None:
        raise BindingChainDefect(
            f"selection {selection_revision_id!r} carries no logical plan binding: it is a "
            "PRE-PLAN selection and is refused for cross-catalog generation. Its legacy hashes "
            "record which request it came from; they are not a plan, and are never read as one")
    return SelectionFormulaPlanBindingV1(*row)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 4. the build member's COMBINED binding
# ──────────────────────────────────────────────────────────────────────────────────────────────
def bind_build_member_combined(
    conn: DbConn, *, selection_revision_id: str, formula_draft_id: str,
    physical_adoption_revision_id: str, render_profile_revision_id: str,
) -> CombinedBindingV1:
    """Combine one selection's logical plan, the physical realization its user ADOPTED, and a render
    profile into the single identity a build member points at. Idempotent on content.

    ▲ THE ADOPTION MUST BE THE CURRENT HEAD of its scope. A superseded adoption is a realization the
    user replaced; starting a NEW build from it would generate what they walked away from. A build
    already bound keeps its pin — supersession never rewrites history, it only refuses the next
    binding.

    Raises:
        BindingChainDefect: the selection is PRE-PLAN, the adoption does not exist or belongs to
            another selection, the adoption has been superseded, the physical plan does not realize
            this selection's meaning, or the render profile was never persisted.
    """
    selection_revision_id = _text(selection_revision_id, what="selection_revision_id")
    formula_draft_id = _text(formula_draft_id, what="formula_draft_id")
    physical_adoption_revision_id = _text(
        physical_adoption_revision_id, what="physical_adoption_revision_id")
    render_profile_revision_id = _text(
        render_profile_revision_id, what="render_profile_revision_id")
    if not render_profile_revision_id.startswith(RENDER_PROFILE_ID_PREFIX):
        raise BindingChainDefect(
            f"render_profile_revision_id must be a {RENDER_PROFILE_ID_PREFIX}<digest> id, got "
            f"{render_profile_revision_id!r}")

    plan_binding = load_selection_formula_plan_binding(
        conn, selection_revision_id=selection_revision_id, formula_draft_id=formula_draft_id)
    if plan_binding is None:
        raise BindingChainDefect(
            f"selection {selection_revision_id!r} and draft {formula_draft_id!r} carry no logical "
            "plan binding: a build member generates from a pinned plan, and a PRE-PLAN pair is "
            "refused for cross-catalog generation")

    adoption = load_physical_plan_adoption(conn, physical_adoption_revision_id)
    if adoption is None:
        raise BindingChainDefect(
            f"the physical-plan adoption {physical_adoption_revision_id!r} does not exist: a "
            "selection is previewable because a person CONFIRMED a realization for it")
    if adoption.selection_revision_id != selection_revision_id:
        raise BindingChainDefect(
            f"adoption {physical_adoption_revision_id!r} was confirmed for selection "
            f"{adoption.selection_revision_id!r}, not {selection_revision_id!r}: an adoption "
            "belongs to the choice it was confirmed against")
    head = current_physical_plan_adoption(
        conn, selection_revision_id=selection_revision_id,
        execution_context_revision_id=adoption.execution_context_revision_id)
    if head is None or head.adoption_revision_id != adoption.adoption_revision_id:
        raise BindingChainDefect(
            f"adoption {physical_adoption_revision_id!r} has been superseded by "
            f"{None if head is None else head.adoption_revision_id!r}: binding a build to a "
            "realization the user replaced would generate what they walked away from — adopt "
            "again, or bind the head")

    plan = load_physical_execution_plan(conn, adoption.physical_plan_revision_id)
    if plan is None:
        raise BindingChainDefect(
            f"the physical execution plan {adoption.physical_plan_revision_id!r} this adoption "
            "names was never persisted")
    if plan.logical_digest_ref != plan_binding.logical_digest:
        raise BindingChainDefect(
            f"the adopted physical plan realizes logical plan {plan.logical_digest_ref} but the "
            f"selection is bound to {plan_binding.logical_digest}: a build member's physical and "
            "logical halves must be halves of ONE feature")

    if load_render_profile(conn, render_profile_revision_id) is None:
        raise BindingChainDefect(
            f"the render profile {render_profile_revision_id!r} was never persisted (migration "
            "1134 / task B1 owns it)")

    physical_digest = adoption.physical_plan_revision_id.split("_", 1)[1]
    render_digest_value = render_profile_revision_id[len(RENDER_PROFILE_ID_PREFIX):]
    content_hash = materialize_hash({
        "contract": _COMBINED_CONTRACT,
        "selection_revision_id": selection_revision_id,
        "formula_draft_id": formula_draft_id,
        "logical_digest": plan_binding.logical_digest,
        "physical_plan_revision_id": adoption.physical_plan_revision_id,
        "physical_adoption_revision_id": adoption.adoption_revision_id,
        "execution_context_revision_id": adoption.execution_context_revision_id,
        "render_profile_revision_id": render_profile_revision_id,
    })
    combined_binding_id = f"{COMBINED_BINDING_ID_PREFIX}{content_hash}"

    conn.execute(
        "INSERT INTO build_member_combined_binding (combined_binding_id, selection_revision_id, "
        "  formula_draft_id, logical_digest, physical_plan_revision_id, physical_digest, "
        "  physical_adoption_revision_id, execution_context_revision_id, "
        "  render_profile_revision_id, render_profile_digest, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (combined_binding_id) DO NOTHING",
        (combined_binding_id, selection_revision_id, formula_draft_id,
         plan_binding.logical_digest, adoption.physical_plan_revision_id, physical_digest,
         adoption.adoption_revision_id, adoption.execution_context_revision_id,
         render_profile_revision_id, render_digest_value, content_hash))
    stored = load_combined_binding(conn, combined_binding_id)
    if stored is None:
        raise BindingChainConflict(
            f"the combined binding {combined_binding_id} did not persist")
    return stored


def load_combined_binding(conn: DbConn, combined_binding_id: str) -> CombinedBindingV1 | None:
    """One combined binding, identity-verified, or ``None``."""
    row = conn.execute(
        f"SELECT {_COMBINED_COLUMNS} FROM build_member_combined_binding "
        "WHERE combined_binding_id = %s", (combined_binding_id,)).fetchone()
    if row is None:
        return None
    binding = CombinedBindingV1(*row)
    recomputed = materialize_hash({
        "contract": _COMBINED_CONTRACT,
        "selection_revision_id": binding.selection_revision_id,
        "formula_draft_id": binding.formula_draft_id,
        "logical_digest": binding.logical_digest,
        "physical_plan_revision_id": binding.physical_plan_revision_id,
        "physical_adoption_revision_id": binding.physical_adoption_revision_id,
        "execution_context_revision_id": binding.execution_context_revision_id,
        "render_profile_revision_id": binding.render_profile_revision_id,
    })
    if recomputed != binding.content_hash or \
            binding.combined_binding_id != f"{COMBINED_BINDING_ID_PREFIX}{recomputed}":
        raise BindingChainConflict(
            f"combined binding {combined_binding_id} does not reproduce its own identity — it "
            "would pin a build to a combination it no longer names")
    return binding


def pin_build_set_member(
    conn: DbConn, *, build_set_revision_id: str, position: int, selection_revision_id: str,
    combined_binding_id: str,
) -> None:
    """Write one build-set member that CARRIES its combined binding (1135's parent-carried leg).

    The composite foreign key ``(combined_binding_id, selection_revision_id)`` makes it impossible
    for the member and its binding to name different selections; the deferred trigger makes it
    impossible for a member of a planned selection to have no binding at all. This writer exists so
    the planned lane has one door — B3 routes the coordinator through it; nothing else is rewired
    here."""
    build_set_revision_id = _text(build_set_revision_id, what="build_set_revision_id")
    selection_revision_id = _text(selection_revision_id, what="selection_revision_id")
    combined_binding_id = _text(combined_binding_id, what="combined_binding_id")
    if not isinstance(position, int) or isinstance(position, bool) or position < 0:
        raise BindingChainDefect(f"position must be a non-negative integer, got {position!r}")
    binding = load_combined_binding(conn, combined_binding_id)
    if binding is None:
        raise BindingChainDefect(
            f"the combined binding {combined_binding_id!r} does not exist")
    if binding.selection_revision_id != selection_revision_id:
        raise BindingChainDefect(
            f"combined binding {combined_binding_id!r} is for selection "
            f"{binding.selection_revision_id!r}, not {selection_revision_id!r}")
    conn.execute(
        "INSERT INTO build_set_member (revision_id, position, selection_revision_id, "
        "  selection_formula_binding_id, combined_binding_id) "
        "SELECT %s, %s, %s, b.binding_id, %s FROM selection_formula_binding b "
        "WHERE b.selection_revision_id = %s AND b.formula_draft_id = %s",
        (build_set_revision_id, position, selection_revision_id, combined_binding_id,
         selection_revision_id, binding.formula_draft_id))


def assert_build_environment_match(conn: DbConn, *, build_set_revision_id: str,
                                   environment_id: str) -> None:
    """R3's build-environment match, checked against the environment a generation REQUEST names.

    The structural half is DDL — 1135's deferred trigger already forbids one build set from mixing
    execution contexts. This is the half DDL cannot state: `generation_request.environment_id` is
    where a build's environment actually lives (a build set declaration has no environment field),
    so the comparison happens when a request is opened. Nothing is wired to it yet: B3 threads the
    pinned plan through generation and calls this at the request boundary."""
    environment_id = _text(environment_id, what="environment_id")
    rows = conn.execute(
        "SELECT DISTINCT c.execution_context_revision_id FROM build_set_member m "
        "JOIN build_member_combined_binding c ON c.combined_binding_id = m.combined_binding_id "
        "WHERE m.revision_id = %s", (build_set_revision_id,)).fetchall()
    for (context_id,) in rows:
        context = load_execution_context_revision(conn, context_id)
        if context is None:
            raise BindingChainDefect(
                f"build set {build_set_revision_id!r} names execution context {context_id!r}, "
                "which was never persisted")
        if context.environment_id != environment_id:
            raise BindingChainDefect(
                f"build set {build_set_revision_id!r} was adopted in environment "
                f"{context.environment_id!r} but is being generated for {environment_id!r}: R3 "
                "scopes an adoption to ONE environment, and a build may not borrow another one's "
                "confirmation")
