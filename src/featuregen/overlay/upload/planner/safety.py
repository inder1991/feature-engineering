"""The stable planner-facing safety boundary. Mirrors templates._safe_to_bind's EXACT predicates
(leakage anchor, then the blocked protected/special sensitivities) while exposing WHY — the
parity tests pin the two views of the one policy together so they can never drift.
Invariant: the planner may add STRICTER eligibility, but NEVER accepts a binding _safe_to_bind
rejects.

UNIVERSAL safety only (F13): the concerns that hold for EVERY caller. PII/read-scope is
AUTHORIZATION — enforced by _load_columns' sensitivity filter — and is deliberately not re-gated
here. This evaluator never returns not_evaluated: structural incompleteness (no resolvable _Col
at all) is safety_of_ref's verdict (declarations.py), not a column-safety outcome."""
from __future__ import annotations

from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.planner.contracts import BindingSafety, ReasonCode
from featuregen.overlay.upload.templates import _BLOCKED_SENSITIVITIES, _Col


def evaluate_column_safety(col: _Col) -> tuple[BindingSafety, ReasonCode | None]:
    """Reason-bearing column safety — the SAME predicates _safe_to_bind applies, in the SAME
    order, mapped to the C6 reason vocabulary. Untagged (concept None) and unknown-concept
    columns are permissively safe, matching _safe_to_bind: nothing dangerous is asserted."""
    if col.concept is None:
        return BindingSafety.safe, None
    con = concept(col.concept)
    if con is None:
        return BindingSafety.safe, None
    if con.leakage_anchor:
        return BindingSafety.unsafe, ReasonCode.leakage_anchor_read      # §3.10/§3.7 — the target
    if con.sensitivity in _BLOCKED_SENSITIVITIES:
        return BindingSafety.unsafe, ReasonCode.protected_attribute_read  # ECOA + GDPR special
    return BindingSafety.safe, None


def evaluate_binding_safety(col: _Col) -> BindingSafety:
    return evaluate_column_safety(col)[0]
