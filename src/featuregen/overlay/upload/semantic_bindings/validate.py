"""D2 — candidate validation. Fail closed, never silent-drop.

``validate(candidate, table_view)`` re-checks ONE candidate against the roster and returns an
``ok | reason_code`` outcome — the four gates from the brief:

* **referent** — the subject (and, for a currency binding, the target) MUST be a column of the
  server roster. A subject/target that is not in ``table_view.columns`` → rejected.
* **role** — the subject/target roles must fit the kind: a currency subject is a measure and its
  target is a currency-eligible column; an entity subject is identifier-eligible and carries NO
  target ref. A term_type=='measure' subject can never be an entity key.
* **ambiguity** — a candidate claiming ``strong`` while several equally plausible currency targets
  exist for its measure is downgraded (``ambiguous_target``).
* **bound** — a per-table candidate cap (:func:`validate_candidates`): candidates beyond the cap are
  rejected (``over_bound``), never dropped.

A failing candidate is rewritten to ``disposition='rejected'`` with a durable reason code — it is
NEVER dropped, so every decision is reviewable.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.overlay.upload.semantic_bindings.shortlist import (
    PassBColumn,
    PassCIdentifier,
    is_currency_column,
    is_measure_column,
)
from featuregen.overlay.upload.semantic_bindings.types import (
    CURRENCY_BINDING,
    ENTITY_ASSIGNMENT,
    RC_AMBIGUOUS_TARGET,
    RC_ENTITY_NOT_KNOWN,
    RC_MISSING_ENTITY_VALUE,
    RC_OVER_BOUND,
    RC_SUBJECT_NOT_IN_ROSTER,
    RC_SUBJECT_ROLE_MISMATCH,
    RC_TARGET_NOT_IN_ROSTER,
    RC_TARGET_ROLE_MISMATCH,
    RC_UNKNOWN_BINDING_KIND,
    REJECTED,
    STRONG,
    SemanticBindingCandidate,
)
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

# The per-table candidate cap (the bound check) — a fail-closed guard against an enumeration blowing
# up a review queue for one table. Deterministic overflow ordering is the candidate sort order.
DEFAULT_CANDIDATE_CAP = 200


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    ok: bool
    reason_code: str | None = None


def _roster(table_view: object) -> dict[str, object]:
    return {c.logical_ref: c for c in table_view.columns}


def validate(
    candidate: SemanticBindingCandidate,
    table_view: object,
    *,
    pass_b: Mapping[str, PassBColumn] | None = None,
    pass_c: Mapping[str, PassCIdentifier] | None = None,
) -> ValidationOutcome:
    """Referent + role + ambiguity check for ONE candidate. Returns ``ok`` or the FIRST durable
    reason code. Pure (no DB / no LLM); re-derives currency roles from the roster and identifier
    eligibility from ``pass_c`` (Pass C metadata), so it re-checks exactly what the shortlist saw."""
    roster = _roster(table_view)
    subj_col = roster.get(candidate.subject.logical_ref)
    if subj_col is None:
        return ValidationOutcome(False, RC_SUBJECT_NOT_IN_ROSTER)

    if candidate.binding_kind == CURRENCY_BINDING:
        if candidate.target is None or candidate.target.logical_ref not in roster:
            return ValidationOutcome(False, RC_TARGET_NOT_IN_ROSTER)
        tgt_col = roster[candidate.target.logical_ref]
        pb = pass_b or {}
        if not is_measure_column(subj_col, pb.get(subj_col.logical_ref)):
            return ValidationOutcome(False, RC_SUBJECT_ROLE_MISMATCH)
        if not is_currency_column(tgt_col, pb.get(tgt_col.logical_ref)):
            return ValidationOutcome(False, RC_TARGET_ROLE_MISMATCH)
        if candidate.disposition == STRONG:
            targets = [c for c in table_view.columns
                       if is_currency_column(c, pb.get(c.logical_ref))
                       and c.logical_ref != subj_col.logical_ref]
            if len(targets) > 1:
                return ValidationOutcome(False, RC_AMBIGUOUS_TARGET)
        return ValidationOutcome(True, None)

    if candidate.binding_kind == ENTITY_ASSIGNMENT:
        if candidate.target is not None:
            return ValidationOutcome(False, RC_TARGET_ROLE_MISMATCH)  # entity carries no target ref
        if not candidate.entity_id:
            return ValidationOutcome(False, RC_MISSING_ENTITY_VALUE)
        if (getattr(subj_col, "term_type", None) or "").strip().lower() == "measure":
            return ValidationOutcome(False, RC_SUBJECT_ROLE_MISMATCH)  # rule 6: measure ≠ entity key
        pc = (pass_c or {}).get(subj_col.logical_ref)
        if pc is not None and not pc.join_key_eligible:
            return ValidationOutcome(False, RC_SUBJECT_ROLE_MISMATCH)
        if candidate.entity_id not in known_entities():
            return ValidationOutcome(False, RC_ENTITY_NOT_KNOWN)
        return ValidationOutcome(True, None)

    return ValidationOutcome(False, RC_UNKNOWN_BINDING_KIND)


def validate_candidates(
    candidates: Sequence[SemanticBindingCandidate],
    table_view: object,
    *,
    pass_b: Mapping[str, PassBColumn] | None = None,
    pass_c: Mapping[str, PassCIdentifier] | None = None,
    cap: int = DEFAULT_CANDIDATE_CAP,
) -> tuple[SemanticBindingCandidate, ...]:
    """Validate a whole shortlist + enforce the per-table bound. Each candidate that fails a gate is
    rewritten to ``rejected`` with its reason code (NEVER dropped); an already-``rejected`` candidate
    passes through unchanged (its reason is durable). Beyond ``cap`` non-rejected candidates, the
    remainder are rejected ``over_bound`` — deterministic overflow (the candidates' own sort order)."""
    ordered = sorted(candidates, key=SemanticBindingCandidate.sort_key)
    out: list[SemanticBindingCandidate] = []
    kept = 0
    for cand in ordered:
        if cand.disposition == REJECTED:
            out.append(cand)
            continue
        result = validate(cand, table_view, pass_b=pass_b, pass_c=pass_c)
        if not result.ok:
            out.append(cand.rejected_with(result.reason_code or RC_UNKNOWN_BINDING_KIND))
            continue
        if kept >= cap:
            out.append(cand.rejected_with(RC_OVER_BOUND))
            continue
        kept += 1
        out.append(cand)
    return tuple(out)
