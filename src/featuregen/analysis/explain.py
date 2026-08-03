"""WHICH COPY answered, WHICH of its rows, and WHAT ELSE was considered — rendered for a person.

Task 9's explain half: "render the selected source and row semantics in plan preview and result
provenance", and "explain alternatives considered and why they lost WITHOUT EXPOSING HIDDEN
CATALOGS". The second clause is the hard one, and it is why this lives here rather than inline in
the route.

**A DECISION IS RECORDED WHOLE AND RENDERED NARROW.** ``DatasetSourceSelectionV1`` seals every
candidate the selector considered, because functional rule 9 says a decision records what it looked
at — narrowing the RECORD to each reader would make the audit trail a function of who is looking at
it. The RENDERING is the opposite: a candidate this caller may not see becomes a COUNT and never a
name, because "there is a better copy of this data that you may not see" is an existence oracle
over a catalog they were never granted. Same rule the policy routes already apply, same words for
hidden and missing.

The count itself is deliberate and not a leak. "1 other candidate considered" says the decision was
contested without saying by what — which is exactly what a person needs to know to ask their
governance owner a question, and nothing more. Suppressing the count entirely would misrepresent a
contested decision as an uncontested one, which is a worse failure than the one being avoided.

**Row semantics, not a row label.** "valid at the report cutoff" is a label; the two column refs and
two operators under it are the claim. So the predicates are rendered — and, because they ARE column
refs, they follow the column-level scope rule the temporal policy route settled: if the caller may
not see every column the rule names, the whole predicate list is withheld rather than filtered.
Withholding one field at a time still answers "a column exists here that you may not see", one
field at a time.

**A WITHHELD DATASET IS WITHHELD EVERYWHERE.** The three places a dataset ref can reach the payload
— the selected source, the alternatives considered, and a refusal's subjects — are each scoped, and
so is the ROW rule, which is about a dataset too. Withholding it in one section while naming it in
another is not a narrower rendering; the test therefore asserts on the WHOLE serialized body rather
than on the section under repair, because a leak in a field nobody thought to check is the leak
that ships.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: The refusal FAMILIES, for a UI that must never frame an undecided thing as a failure. Every
#: closed selection/temporal code maps to exactly one: nobody has decided yet, someone has to look
#: at the data, or this source structurally cannot answer this question. The UI renders the family;
#: the code stays on the payload for the audit.
UNDECIDED = "undecided"
NEEDS_DATA_CHECK = "needs_data_check"
STRUCTURALLY_UNSUITABLE = "structurally_unsuitable"
NEEDS_SETUP = "needs_setup"

REFUSAL_FAMILIES: dict[str, str] = {
    "SELECTION_POPULATION_UNDECLARED": UNDECIDED,
    "SELECTION_SOURCE_AMBIGUOUS": UNDECIDED,
    "SELECTION_AUTHORITY_INSUFFICIENT": UNDECIDED,
    "TEMPORAL_MODEL_UNKNOWN": UNDECIDED,
    # An address nobody has registered is not a decision anybody is weighing — it is an operator
    # task, and calling it "undecided" sends the reader to the wrong person.
    "SELECTION_BINDING_MISSING": NEEDS_SETUP,
    # The DATA disagrees with the declaration. Only a look at the rows settles these.
    "TEMPORAL_SCD_OVERLAP": NEEDS_DATA_CHECK,
    "TEMPORAL_SNAPSHOT_TIE": NEEDS_DATA_CHECK,
    # The source keeps no history, so it cannot answer a historical question at all. Not a gap
    # waiting on anyone: a different source or an explicit limitation is the only way through.
    "TEMPORAL_HISTORICAL_CURRENT_ONLY": STRUCTURALLY_UNSUITABLE,
}


class _Scope:
    """Readability of dataset refs for one caller, resolved once per render.

    A cache rather than a query per candidate: a decision with six candidates would otherwise run
    six scope queries to render one line, and the answer cannot change inside one response.
    """

    def __init__(self, conn, roles: Sequence[str]) -> None:
        self._conn = conn
        self._roles = tuple(roles)
        self._seen: dict[str, bool] = {}

    def dataset(self, ref: str) -> bool:
        if ref not in self._seen:
            from featuregen.overlay.upload.source_selection import (
                SelectionError,
                assert_dataset_refs_readable,
            )

            try:
                assert_dataset_refs_readable(self._conn, (ref,), roles=self._roles)
                self._seen[ref] = True
            # ValueError: the ref does not ADDRESS a dataset at all (no schema half). That is a
            # catalog gap, not a hidden object — `SELECTION_BINDING_MISSING` already names it —
            # and treating it as hidden would withhold the one ref the reader most needs to see.
            # Reported by `addressable` and shown as-is; here it is simply not readable.
            except (SelectionError, ValueError):
                self._seen[ref] = False
        return self._seen[ref]

    @staticmethod
    def addressable(ref: str) -> bool:
        """Can this string name a dataset at all? Distinguishes "hidden" from "unaddressable"."""
        from featuregen.overlay.upload.object_ref import parse_ref

        if "::" not in ref:
            return False
        try:
            _source, _schema, _table, column = parse_ref(ref)
        except ValueError:
            return False
        return column is None

    def columns(self, refs: Sequence[str]) -> bool:
        """ALL of them, or none — the whole-policy rule (`dataset_policies.py`)."""
        from featuregen.overlay.upload.source_selection import (
            SelectionError,
            assert_column_refs_readable,
        )

        if not refs:
            return True
        try:
            assert_column_refs_readable(self._conn, refs, roles=self._roles)
        except SelectionError:
            return False
        return True


def _considered(selection, scope: _Scope) -> dict[str, Any]:
    """The alternatives, with the unreadable ones collapsed into a count."""
    visible, withheld = [], 0
    for candidate in selection.considered_candidates:
        if not scope.dataset(candidate.dataset_ref):
            withheld += 1
            continue
        visible.append({"dataset_ref": candidate.dataset_ref,
                        "disposition": candidate.disposition.value,
                        "reason_codes": list(candidate.reason_codes)})
    return {"considered": visible,
            "considered_withheld": withheld,
            "considered_total": len(selection.considered_candidates)}


def _row(row, scope: _Scope) -> dict[str, Any]:
    # A WITHHELD DATASET IS WITHHELD EVERYWHERE. The sources loop drops the whole entry for a
    # dataset this caller may not see; this one used to emit its `dataset_ref` regardless, so one
    # payload carried `{"withheld": true}` for the source AND named the same hidden table two keys
    # later. Withholding a decision in one section and naming its subject in another is not a
    # narrower rendering, it is the existence oracle with an extra step.
    if not scope.dataset(row.dataset_logical_ref):
        return {"withheld": True}
    column_refs = tuple(p["column_ref"] for p in row.predicate_payloads if p.get("column_ref"))
    readable = scope.columns(column_refs)
    return {
        "dataset_ref": row.dataset_logical_ref,
        "selection_kind": row.selection_kind.value,
        # The cutoff's NAME, never a date (§6.5) — a value here would be a different rule every day.
        "cutoff_value_ref": row.cutoff_value_ref,
        "predicates": ([{"column_ref": p["column_ref"], "operator": p["operator"]}
                        for p in row.predicate_payloads] if readable else []),
        "predicates_withheld": not readable,
        "withheld": False,
    }


def _refusal(refusal, scope: _Scope) -> dict[str, Any]:
    """A refusal names what it is ABOUT, and some of those subjects can be datasets this caller may
    not see — the ambiguity path enumerates a policy's whole eligible set. Same treatment."""
    subjects, withheld = [], 0
    for ref in refusal.subject_refs:
        # A subject is not always a dataset ref: a policy key, a column, or — for
        # `SELECTION_BINDING_MISSING` — a ref that cannot address a dataset at all, which is
        # precisely what that refusal is about. Anything unaddressable is shown as-is; only a real,
        # HIDDEN dataset is withheld.
        if scope.addressable(ref) and not scope.dataset(ref):
            withheld += 1
            continue
        subjects.append(ref)
    return {"code": refusal.code, "subjects": subjects, "subjects_withheld": withheld,
            "detail": refusal.detail,
            "family": REFUSAL_FAMILIES.get(refusal.code, UNDECIDED)}


def render_selection(conn, selections, *, roles: Sequence[str] = ()) -> dict | None:
    """The Release-B source/row decisions as this caller may see them. ``None`` with the flag off.

    Shown even when it refuses — especially then. "Which copy served this, and which of its rows"
    is half of what makes an answer explainable (§5.7), and a preview that showed only the resolved
    half would report the absence of a row rule as silence.
    """
    if selections is None:
        return None
    scope = _Scope(conn, roles)
    sources = []
    for selection in selections.source_selections:
        # A caller who cannot see the SELECTED dataset is not shown a redacted decision about it;
        # the whole entry is withheld, in the same words a decision that was never made gets.
        if not scope.dataset(selection.selected_dataset_ref):
            sources.append({"need_role": selection.need.need_role.value, "withheld": True})
            continue
        sources.append({
            "need_role": selection.need.need_role.value,
            "dataset_ref": selection.selected_dataset_ref,
            "selection_basis": selection.selection_basis.value,
            "authority_basis": selection.authority_basis.value,
            "authority_role": selection.authority_role,
            "withheld": False,
            **_considered(selection, scope),
        })
    return {
        "resolved": selections.resolved,
        "sources": sources,
        "rows": [_row(row, scope) for row in selections.row_selections],
        "refusals": [_refusal(r, scope) for r in selections.refusals],
        # Warnings ride a RESOLVED decision — `PROPOSED_AUTHORITY_USED` most of all. Visible, never
        # a log line: the point of letting an unconfirmed classification be useful is that its
        # authority travels with it.
        "warnings": list(selections.warnings),
    }


__all__ = ["REFUSAL_FAMILIES", "render_selection"]
