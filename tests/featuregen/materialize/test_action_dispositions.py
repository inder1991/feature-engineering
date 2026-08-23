"""§5 — the exhaustive disposition table, proved exhaustive MECHANICALLY.

The pattern extends `test_evaluator_contracts.py:50`'s discipline to the six-action table: the
domain is enumerated by reflection over the reasons module's own pinned vocabulary
(`REASON_FAMILIES` — the same closed set the family pin enforces), the actions over `ActionV1`,
and the full product must be covered with every cell a `Disposition` MEMBER. That last clause is
§5.1's rule made mechanical: it is what catches a cell containing "n/a", a UI behaviour, or a
narrative — exhaustive in shape and unusable in fact.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.action_authorization import ActionV1
from featuregen.materialize.action_dispositions import (
    ACTION_DISPOSITIONS,
    Disposition,
    UnknownReasonCode,
    disposition_for,
    fold_member_codes,
)
from featuregen.overlay.upload import semantic_eligibility_reasons as R

_A = ActionV1
_PRODUCTION = (_A.MATERIALIZE_PRODUCTION, _A.PUBLISH_PRODUCTION)


# ══ THE GATE — the full product, no cell missing, no cell extra, every cell a member ═════════════
def test_THE_TABLE_COVERS_EVERY_CODE_TIMES_EVERY_ACTION_EXACTLY():
    """Equality against the product, not a superset — §5: CI FAILS if any (code, action) pair is
    missing, and an extra row would be a code the vocabulary does not own."""
    vocabulary = set(R.REASON_FAMILIES)
    expected = {(code, action) for code in vocabulary for action in ActionV1}
    assert set(ACTION_DISPOSITIONS) == expected, sorted(
        set(map(str, set(ACTION_DISPOSITIONS) ^ expected)))[:20]


def test_EVERY_CELL_IS_A_DISPOSITION_MEMBER():
    """§5.1: a disposition cell contains a DISPOSITION — the check that would have caught all
    four narrative cells in §4's illustrative matrix."""
    for key, cell in ACTION_DISPOSITIONS.items():
        assert isinstance(cell, Disposition), key


def test_the_vocabulary_and_the_family_table_are_the_SAME_closed_set():
    """One enumeration, two exhaustive tables — §5's three-part-commit rule depends on a new code
    being unable to enter one table without the other's test failing."""
    covered = {code for code, _ in ACTION_DISPOSITIONS}
    assert covered == set(R.REASON_FAMILIES)


# ══ The rulings, spot-pinned so a future re-cell is a deliberate act ═════════════════════════════
def test_EXECUTE_SANDBOX_never_requires_verification():
    """§4's evasion-killer: execution is what PRODUCES verification. The merged 'Sandbox: Depends'
    column existed to avoid stating this row."""
    assert ACTION_DISPOSITIONS[(R.VERIFICATION_NOT_CURRENT, _A.EXECUTE_SANDBOX)] is Disposition.DROP
    assert ACTION_DISPOSITIONS[(R.VERIFICATION_NOT_CURRENT, _A.PUBLISH_SANDBOX)] is Disposition.BLOCK


def test_readiness_authorizes_NOTHING():
    """§6: recipe readiness is a discovery/maturity projection on every one of the six actions."""
    for action in ActionV1:
        assert ACTION_DISPOSITIONS[(R.READINESS_NOT_MATERIALIZATION_READY, action)] is Disposition.DROP


def test_the_certificate_set_is_the_only_categorical_production_gate_for_method_facts():
    """§4.1: origin and maturity facts never BLOCK production; the METHOD_CERTIFICATE_* rows do."""
    for code in (R.METHOD_CERTIFICATE_MISSING, R.METHOD_CERTIFICATE_STALE,
                 R.METHOD_CERTIFICATE_MISMATCHED, R.METHOD_IDENTITY_UNRECORDED):
        for action in _PRODUCTION:
            assert ACTION_DISPOSITIONS[(code, action)] is Disposition.BLOCK, (code, action)
    for code in (R.FORMULA_NOT_REVIEWED, R.FORMULA_REVIEW_UNMEASURED,
                 R.REVIEWED_EXPECTATION_LEGACY_VERSION, R.BLUEPRINT_DERIVED_NOT_REVIEWED):
        for action in _PRODUCTION:
            assert ACTION_DISPOSITIONS[(code, action)] is not Disposition.BLOCK, (code, action)


def test_purchase_conditions_never_gate_downstream_actions():
    """§4.1: a provider outage must not take production down — the downstream gate for 'there is
    no formula' is FORMULA_NOT_AUTHORED, which conversely never gates authoring (its cure)."""
    for code in (R.PROVIDER_UNAVAILABLE, R.COST_AUTHORIZATION_MISSING,
                 R.COST_AUTHORIZATION_EXHAUSTED):
        assert ACTION_DISPOSITIONS[(code, _A.AUTHOR_FORMULA)] is Disposition.BLOCK, code
        for action in ActionV1:
            if action is not _A.AUTHOR_FORMULA:
                assert ACTION_DISPOSITIONS[(code, action)] is Disposition.DROP, (code, action)
    assert ACTION_DISPOSITIONS[(R.FORMULA_NOT_AUTHORED, _A.AUTHOR_FORMULA)] is Disposition.DROP
    assert ACTION_DISPOSITIONS[(R.FORMULA_NOT_AUTHORED, _A.GENERATE_PREVIEW)] is Disposition.BLOCK


def test_the_spine_codes_block_every_action():
    """§0.1/§7.1: an act without its authorization or decision is a bypass, whichever act."""
    for code in (R.ACTION_AUTHORIZATION_MISSING, R.ACTION_AUTHORIZATION_REVOKED,
                 R.ACTION_DECISION_MISSING, R.DECISION_DRIFT, R.FORMULA_DRAFT_RETIRED):
        for action in ActionV1:
            assert ACTION_DISPOSITIONS[(code, action)] is Disposition.BLOCK, (code, action)


def test_the_three_policy_rows_WARN_at_authoring_and_block_from_preview():
    """§5.1: authoring genuinely proceeds and the formula is genuinely visible; the refusal
    arrives at preview. WARN is neither 'allowed' nor 'blocked' — the caller MUST be told."""
    for code in (R.TARGET_LEAKAGE_BLOCKED, R.RENDERER_CANNOT_DISPATCH,
                 R.CURRENCY_POLICY_MISSING):
        assert ACTION_DISPOSITIONS[(code, _A.AUTHOR_FORMULA)] is Disposition.WARN, code
        assert ACTION_DISPOSITIONS[(code, _A.GENERATE_PREVIEW)] is Disposition.BLOCK, code


def test_a_refused_deterministic_instantiation_blocks_by_name_with_NO_fallback():
    """The child's hard rule: a deterministic refusal is a deterministic refusal."""
    for action in ActionV1:
        assert ACTION_DISPOSITIONS[
            (R.REVIEWED_BLUEPRINT_NOT_EXECUTABLE, action)] is Disposition.BLOCK, action


# ══ The accessor and the fold ════════════════════════════════════════════════════════════════════
def test_an_unknown_code_raises_the_named_error_with_the_remedy():
    with pytest.raises(UnknownReasonCode, match="three-part commit"):
        disposition_for("NOT_A_CODE", _A.GENERATE_PREVIEW)


def test_fold_routes_each_code_by_its_cell():
    blockers, warnings, dropped = fold_member_codes(
        _A.GENERATE_PREVIEW,
        (R.BINDING_NOT_BOUND, R.PROVIDER_UNAVAILABLE, R.METHOD_CERTIFICATE_MISSING),
        ())
    assert blockers == (R.BINDING_NOT_BOUND,)
    assert warnings == (R.METHOD_CERTIFICATE_MISSING,)
    assert dropped == (R.PROVIDER_UNAVAILABLE,)


def test_the_table_ESCALATES_a_caller_supplied_warning_whose_cell_says_BLOCK():
    """Authoritative in both directions — a caller cannot launder a block into a warning."""
    blockers, warnings, dropped = fold_member_codes(
        _A.PUBLISH_SANDBOX, (), (R.VERIFICATION_NOT_CURRENT,))
    assert blockers == (R.VERIFICATION_NOT_CURRENT,)
    assert warnings == () and dropped == ()


def test_a_code_OUTSIDE_the_vocabulary_keeps_its_callers_channel():
    """Fail-closed for blockers: an unknown fact offered as a refusal stays one."""
    blockers, warnings, dropped = fold_member_codes(
        _A.EXECUTE_SANDBOX, ("SOME_LEGACY_CODE",), ("SOME_LEGACY_NOTE",))
    assert blockers == ("SOME_LEGACY_CODE",)
    assert warnings == ("SOME_LEGACY_NOTE",)
    assert dropped == ()
