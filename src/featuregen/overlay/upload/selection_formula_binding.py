"""The immutable link between *the feature somebody chose* and *the formula that will be built*.

**What this closes.** A build set used to name only its selections, and the formula was resolved at
build time with ``ORDER BY updated_at DESC`` — so a newer draft landing between the decision and the
build changed what the build MEANT while the set's identity stayed put.

▲ **And pinning a draft id and a content hash directly on the member would not have closed it.** Two
independent columns on one row can disagree with the selection beside them: a valid READY formula
belonging to a DIFFERENT feature satisfies "the id exists and the hash matches", and the build
proceeds. Migration 1101 makes that unrepresentable with composite foreign keys into both source
identities; this module is its writer, and it raises a NAMED refusal before the database has to,
because a bare foreign-key violation tells an operator nothing about which fact disagreed.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.canonical import jcs_sha256

__all__ = [
    "BindingDisagreement",
    "SelectionFormulaBindingV1",
    "read_binding",
    "record_selection_formula_binding",
]


class BindingDisagreement(RuntimeError):
    """The draft and the selection do not describe the same work, so no binding exists."""


@dataclass(frozen=True, slots=True)
class SelectionFormulaBindingV1:
    """One pinned (selection, formula) pair. ``recorded_at`` is provenance and stays out of the id."""

    selection_revision_id: str
    formula_draft_id: str
    formula_content_hash: str
    considered_revision_id: str
    option_id: str
    planning_request_hash: str
    binding_plan_hash: str

    @property
    def binding_id(self) -> str:
        return jcs_sha256({
            "selection_revision_id": self.selection_revision_id,
            "formula_draft_id": self.formula_draft_id,
            "formula_content_hash": self.formula_content_hash,
            "considered_revision_id": self.considered_revision_id,
            "option_id": self.option_id,
            "planning_request_hash": self.planning_request_hash,
            "binding_plan_hash": self.binding_plan_hash,
        })


def record_selection_formula_binding(
    conn, *, selection_revision_id: str, formula_draft_id: str,
) -> tuple[SelectionFormulaBindingV1, bool]:
    """Pin this selection to this formula. Returns ``(binding, created)``. Idempotent on content.

    Raises:
        BindingDisagreement: the selection or the draft does not exist, the draft has produced no
            formula yet, or the two describe different work. ▲ The composite foreign keys would
            refuse the same row — this check exists so the refusal SAYS WHICH FACT disagreed rather
            than naming a constraint.
    """
    selection = conn.execute(
        "SELECT considered_revision_id, option_id, planning_request_hash, binding_plan_hash "
        "FROM feature_selection_revision WHERE revision_id = %s",
        (selection_revision_id,)).fetchone()
    if selection is None:
        raise BindingDisagreement(
            f"selection {selection_revision_id} does not exist, so there is no choice to pin a "
            f"formula to")

    draft = conn.execute(
        "SELECT considered_revision_id, option_id, planning_request_hash, formula_content_hash, "
        "state FROM formula_draft WHERE formula_draft_id = %s", (formula_draft_id,)).fetchone()
    if draft is None:
        raise BindingDisagreement(
            f"formula draft {formula_draft_id} does not exist, so there is no formula to pin")
    if draft[3] is None:
        raise BindingDisagreement(
            f"formula draft {formula_draft_id} is {draft[4]} and has produced no formula: a "
            f"binding pins a FORMULA, and there is not one yet. A build set declared against it "
            f"would name a pin with nothing behind it")

    # ▲ NAMED, FIELD BY FIELD. "The draft belongs to another candidate" is actionable; a foreign-key
    # violation citing five columns is a puzzle. The database still refuses it either way — this is
    # the message, not the guarantee.
    for field, chosen, authored in (
        ("considered revision", selection[0], draft[0]),
        ("option", selection[1], draft[1]),
        ("planning request", selection[2], draft[2]),
    ):
        if chosen != authored:
            raise BindingDisagreement(
                f"selection {selection_revision_id} and formula draft {formula_draft_id} disagree "
                f"about the {field}: the selection says {chosen!r} and the draft was authored for "
                f"{authored!r}. A formula authored for one candidate cannot be pinned to another")

    binding = SelectionFormulaBindingV1(
        selection_revision_id=selection_revision_id, formula_draft_id=formula_draft_id,
        formula_content_hash=draft[3], considered_revision_id=selection[0],
        option_id=selection[1], planning_request_hash=selection[2], binding_plan_hash=selection[3])

    inserted = conn.execute(
        "INSERT INTO selection_formula_binding (binding_id, selection_revision_id, "
        "formula_draft_id, formula_content_hash, considered_revision_id, option_id, "
        "planning_request_hash, binding_plan_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (binding_id) DO NOTHING RETURNING binding_id",
        (binding.binding_id, binding.selection_revision_id, binding.formula_draft_id,
         binding.formula_content_hash, binding.considered_revision_id, binding.option_id,
         binding.planning_request_hash, binding.binding_plan_hash)).fetchone()
    return binding, inserted is not None


def read_binding(conn, binding_id: str) -> SelectionFormulaBindingV1 | None:
    """One binding, reconstructed and identity-verified — or ``None``."""
    row = conn.execute(
        "SELECT selection_revision_id, formula_draft_id, formula_content_hash, "
        "considered_revision_id, option_id, planning_request_hash, binding_plan_hash "
        "FROM selection_formula_binding WHERE binding_id = %s", (binding_id,)).fetchone()
    if row is None:
        return None
    binding = SelectionFormulaBindingV1(*row)
    if binding.binding_id != binding_id:
        raise ValueError(
            f"selection-formula binding {binding_id} does not reproduce its own id: it would pin a "
            f"build to a formula it no longer names")
    return binding
