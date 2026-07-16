"""Phase-3B.3c C6 — the reason-bearing column-safety evaluator (UNIVERSAL safety only:
leakage anchors + protected/special sensitivities — F13; PII/read-scope is authorization,
enforced elsewhere). Parity-tested against templates._safe_to_bind over the ENTIRE concept
registry so the two views of the one policy can never drift."""
from __future__ import annotations

from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY
from featuregen.overlay.upload.planner.contracts import BindingSafety, ReasonCode
from featuregen.overlay.upload.planner.safety import (
    evaluate_binding_safety,
    evaluate_column_safety,
)
from featuregen.overlay.upload.templates import _Col, _safe_to_bind


def _col(concept_name: str | None, *, sensitivity: str | None = None) -> _Col:
    return _Col("s", "public.t.x", "t", "x", "text", False, False,
                concept_name, None, None, sensitivity, None)


def test_leakage_anchor_concept_is_unsafe_with_leakage_reason():
    # default_flag is a target-defining outcome column (§3.10): reading it ANYWHERE is leakage
    safety, reason = evaluate_column_safety(_col("default_flag"))
    assert safety is BindingSafety.unsafe
    assert reason is ReasonCode.leakage_anchor_read


def test_both_blocked_sensitivity_classes_share_the_protected_reason():
    # ECOA/fair-lending (protected_attribute) + GDPR special category — one reason vocabulary
    for name in ("protected_attribute", "special_category"):
        safety, reason = evaluate_column_safety(_col(name))
        assert safety is BindingSafety.unsafe
        assert reason is ReasonCode.protected_attribute_read


def test_pii_is_safe_here_universal_safety_is_not_authorization():
    # PII is a READ-SCOPE (authorization) concern — _load_columns' sensitivity filter — and is
    # deliberately NOT re-gated by the universal evaluator (F13)
    safety, reason = evaluate_column_safety(_col("pii", sensitivity="pii"))
    assert safety is BindingSafety.safe and reason is None


def test_untagged_and_unknown_concepts_are_permissively_safe():
    # matches _safe_to_bind: nothing dangerous is asserted about an untagged/unknown column
    for name in (None, "not_a_real_concept"):
        safety, reason = evaluate_column_safety(_col(name))
        assert safety is BindingSafety.safe and reason is None


def test_parity_with_safe_to_bind_over_the_whole_registry():
    # the ONE policy, two views: safe iff _safe_to_bind, for EVERY registered concept plus the
    # untagged/unknown edges; a reason iff unsafe; never structural not_evaluated (that verdict
    # belongs to safety_of_ref, which fires only when no _Col exists at all); and the bool
    # boundary stays a thin wrapper over the reason-bearing evaluator.
    for name in (None, "not_a_real_concept", *sorted(CONCEPT_REGISTRY)):
        col = _col(name)
        safety, reason = evaluate_column_safety(col)
        assert (safety is BindingSafety.safe) == _safe_to_bind(col), name
        assert safety is not BindingSafety.not_evaluated, name
        assert (reason is None) == (safety is BindingSafety.safe), name
        assert evaluate_binding_safety(col) is safety, name
