"""C-D9 — the three evaluator interfaces and the sixteen-row blocker table.

The gate is *"a typed-contract test over stub records; the sixteen-row table is complete"*. The
completeness test is the one that matters, and it is written against the codes the SHIPPED
activation policy actually emits rather than a list copied into this file — a copied list is how
revision 17 lost `PERSONAL_DATA_POLICY_REQUIRED`.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.evaluator_contracts import (
    ACTIVATION_BLOCKER_DISPOSITIONS,
    EVALUATOR_ONLY_BLOCKERS,
    BlockerDisposition,
    EvaluatorAction,
    EvaluatorVerdictV1,
    GenerateEvaluator,
    PublishSandboxEvaluator,
    VerifyEvaluator,
    carried_blockers,
)


def _codes_the_activation_policy_emits() -> set[str]:
    """Every `R.CODE` the shipped activation policy references — read from ITS source.

    Not a list copied here: the point of the table is to be exhaustive over what the system can
    actually produce, and a copy drifts the moment somebody adds a blocker.
    """
    source = Path("src/featuregen/overlay/upload/activation_policy.py").read_text()
    return {getattr(R, name) for name in set(re.findall(r"\bR\.([A-Z_]+)\b", source))}


# ══ THE GATE — the table is complete over what the system emits ══════════════════════════════════
def test_THE_TABLE_IS_COMPLETE_OVER_THE_CODES_THE_POLICY_EMITS():
    """Eighteen from the activation policy, plus exactly the six the EVALUATORS add — and no more.

    Eighteen, not sixteen, since §6's tri-state: `FORMULA_REVIEW_UNMEASURED` and
    `ENGINE_CAPABILITY_UNMEASURED` are what the policy emits where a false
    `FORMULA_SCHEMA_UNSUPPORTED` used to stand for unmeasured support.

    Stated as an equality against a named set rather than loosened to "⊇ what the policy emits":
    a superset check would let a genuinely lost code back in, which is the failure this test
    exists to prevent. Adding an evaluator-only blocker therefore stays a deliberate act — the
    constant has to be extended, and a reason written beside the disposition.
    """
    emitted = _codes_the_activation_policy_emits()
    assert len(emitted) == 18, sorted(emitted)
    assert emitted | EVALUATOR_ONLY_BLOCKERS == set(ACTIVATION_BLOCKER_DISPOSITIONS), (
        sorted((emitted | EVALUATOR_ONLY_BLOCKERS) ^ set(ACTIVATION_BLOCKER_DISPOSITIONS)))
    assert not emitted & EVALUATOR_ONLY_BLOCKERS, (
        "an evaluator-only blocker is also emitted by the activation policy — it is not "
        "evaluator-only, and the constant is now a lie about which layer answers it")


def test_the_EVALUATOR_ONLY_blockers_are_EXECUTION_CHAIN_facts():
    """Both are discovered while compiling, not while listing candidates. Asking the activation
    policy for them would mean compiling every candidate to answer a list query."""
    assert EVALUATOR_ONLY_BLOCKERS == {
        # generate (S4/S8)
        R.POLICY_REFERENCE_UNRESOLVABLE, R.RENDERER_CANNOT_DISPATCH,
        # verify (S11)
        R.ARTIFACT_NOT_SERVABLE, R.ENVIRONMENT_INCOMPATIBLE,
        # publish sandbox (S11)
        R.VERIFICATION_NOT_CURRENT, R.PUBLICATION_CAPABILITY_MISSING,
    }
    for code in EVALUATOR_ONLY_BLOCKERS:
        disposition, reason = ACTIVATION_BLOCKER_DISPOSITIONS[code]
        assert disposition is BlockerDisposition.CARRIED, code
        assert len(reason) > 40, code


def test_PERSONAL_DATA_POLICY_REQUIRED_IS_CARRIED():
    """The one revision 17 lost. Losing it means a feature built from unlicensed personal data
    generates, and no amount of formula review makes the use lawful."""
    disposition, reason = ACTIVATION_BLOCKER_DISPOSITIONS[R.PERSONAL_DATA_POLICY_REQUIRED]
    assert disposition is BlockerDisposition.CARRIED
    assert "LICENCE" in reason


def test_every_row_states_a_REASON():
    """CARRIED or DROPPED without a reason is a decision nobody can review."""
    for code, (disposition, reason) in ACTIVATION_BLOCKER_DISPOSITIONS.items():
        assert isinstance(disposition, BlockerDisposition), code
        assert len(reason) > 40, code


# ══ the rule, applied consistently ═══════════════════════════════════════════════════════════════
def test_the_DROPPED_set_is_exactly_the_semantic_confirmation_gates():
    """The plan's rule: human semantic confirmation is dropped; legality and possibility are not.

    Three more since the four-stage plan's §6: readiness — a maturity PROJECTION that stopped
    authorizing executable actions (its enforcing facts gate by their own names) — and the two
    honest-absence codes the tri-state emits where a false engine claim used to stand.
    """
    dropped = {code for code, (d, _) in ACTIVATION_BLOCKER_DISPOSITIONS.items()
               if d is BlockerDisposition.DROPPED}
    assert dropped == {
        R.PROPOSED_METADATA_ONLY,
        R.SEMANTIC_AUTHORITY_INSUFFICIENT,
        R.FORMULA_NOT_REVIEWED,
        R.EXTERNAL_VALIDATION_OUTSTANDING,
        R.READINESS_NOT_MATERIALIZATION_READY,
        R.FORMULA_REVIEW_UNMEASURED,
        R.ENGINE_CAPABILITY_UNMEASURED,
    }


def test_READ_SCOPE_AND_BINDING_COMPLETENESS_ARE_CARRIED():
    """Named by the plan as carried, alongside the data-use licence."""
    for code in (R.EXECUTION_AUTHORITY_UNMET, R.EXECUTION_AUTHORITY_UNEVALUATED,
                 R.BINDING_NOT_BOUND):
        assert ACTIVATION_BLOCKER_DISPOSITIONS[code][0] is BlockerDisposition.CARRIED, code


def test_UNEVALUATED_read_scope_is_not_permission():
    """An unasked question has no affirmative answer."""
    assert "unasked question" in ACTIVATION_BLOCKER_DISPOSITIONS[
        R.EXECUTION_AUTHORITY_UNEVALUATED][1]


def test_RECIPE_REVIEW_NOT_CURRENT_IS_CARRIED_and_says_why():
    """It is the thing that JUSTIFIES dropping the semantic gates — dropping it too would remove
    the ground the whole disposition stands on."""
    disposition, reason = ACTIVATION_BLOCKER_DISPOSITIONS[R.RECIPE_REVIEW_NOT_CURRENT]
    assert disposition is BlockerDisposition.CARRIED
    assert "STANDS ON" in reason


def test_DROPPED_never_means_ignored():
    """The reasons say where each dropped gate went, rather than that it stopped mattering."""
    for code in (R.PROPOSED_METADATA_ONLY, R.SEMANTIC_AUTHORITY_INSUFFICIENT,
                 R.FORMULA_NOT_REVIEWED, R.EXTERNAL_VALIDATION_OUTSTANDING):
        reason = ACTIVATION_BLOCKER_DISPOSITIONS[code][1]
        assert any(phrase in reason for phrase in
                   ("replaces", "Replaced", "moved upstream", "not from the system")), code


def test_carried_blockers_filters_and_REFUSES_an_unknown_code():
    """Silently ignoring an unknown code is the defect this table exists to prevent."""
    assert carried_blockers((R.PROPOSED_METADATA_ONLY, R.BINDING_NOT_BOUND)) == (
        R.BINDING_NOT_BOUND,)
    with pytest.raises(KeyError):
        carried_blockers(("NOT_A_REAL_CODE",))


# ══ the verdict is a typed contract over stub records ════════════════════════════════════════════
def test_a_verdict_cannot_be_allowed_WITH_blockers():
    with pytest.raises(ValueError, match="proceed past a gate that refused"):
        EvaluatorVerdictV1(action=EvaluatorAction.GENERATE, allowed=True,
                           blockers=(R.BINDING_NOT_BOUND,))


def test_a_refusal_must_name_a_blocker():
    """Otherwise a caller cannot tell a refusal from a crash."""
    with pytest.raises(ValueError, match="cannot tell a refusal from a crash"):
        EvaluatorVerdictV1(action=EvaluatorAction.VERIFY, allowed=False, blockers=())


def test_A_VERDICT_CARRYING_AN_UNDISPOSED_CODE_IS_REFUSED():
    """A code with no CARRIED/DROPPED decision is exactly what revision 17 shipped."""
    with pytest.raises(ValueError, match="no disposition in this build"):
        EvaluatorVerdictV1(action=EvaluatorAction.GENERATE, allowed=False,
                           blockers=(R.TARGET_LEAKAGE_BLOCKED,))


def test_the_three_actions_are_named():
    assert {a.value for a in EvaluatorAction} == {"generate", "verify", "publish_sandbox"}


# ══ interfaces only — the implementations ship at S8/S9/S10 ══════════════════════════════════════
@pytest.mark.parametrize("protocol,method", [
    (GenerateEvaluator, "evaluate_generate"),
    (VerifyEvaluator, "evaluate_verify"),
    (PublishSandboxEvaluator, "evaluate_publish_sandbox"),
])
def test_each_evaluator_is_a_typed_PROTOCOL_with_no_implementation(protocol, method):
    """A stub returning a verdict would be an evaluator nobody wrote; freezing the interface early
    is what stops the shape moving once three stages depend on it."""
    assert hasattr(protocol, method)
    body = inspect.getsource(getattr(protocol, method))
    assert body.strip().endswith("..."), "the interface must not carry a decision"


def test_a_stub_record_satisfies_the_contract():
    """The typed-contract test the gate asks for."""

    class _StubGenerate:
        def evaluate_generate(self, *, generation_authorization_revision_id: str):
            return EvaluatorVerdictV1(
                action=EvaluatorAction.GENERATE, allowed=False,
                blockers=(R.PERSONAL_DATA_POLICY_REQUIRED,))

    stub: GenerateEvaluator = _StubGenerate()
    verdict = stub.evaluate_generate(generation_authorization_revision_id="gar-1")
    assert not verdict.allowed
    assert carried_blockers(verdict.blockers) == (R.PERSONAL_DATA_POLICY_REQUIRED,)
