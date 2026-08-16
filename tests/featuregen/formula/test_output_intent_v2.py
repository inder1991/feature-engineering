"""C-A7 — the authored output intent, derived and provisional.

The gate is two claims: *"the intent is derivable from the expectation alone"* and *"a V3 result is
terminal without `OUTPUT_POLICY_RESOLVED`"*. The first is tested by giving the deriver nothing but a
proposal; the second by reading the stage table, since a stage that has not run must not be
representable as having run and agreed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from featuregen.formula.canonical_v3 import proposal_content_hash_v3
from featuregen.formula.output_intent_v2 import (
    OUTPUT_INTENT_CAPTURED,
    AuthoredOutputIntentV2,
    NumericShapeV2,
    derive_output_intent_v2,
)
from featuregen.formula.parse_v3 import parse_proposal_v3
from featuregen.formula.schema_v3 import FORMULA_SCHEMA_VERSION_V3

_GOLD_V2 = Path(__file__).parent / "gold_v2"
DIR_REF = "direction_sign:foundation-signed-by-indicator"
FX_REF = "currency_conversion:foundation-base-currency"


def _proposal(*, expected_output=..., refs=..., decimal=None):
    raw = json.loads((_GOLD_V2 / "01_avg_txn_amt_90d.json").read_text())["proposal"]
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    raw["body"]["expr"]["row_selections"] = []
    raw["body"]["expr"]["authority_refs"] = (
        {"direction_policy_ref": DIR_REF} if refs is ... else refs)
    if expected_output is not ...:
        raw["expected_output"] = expected_output
    if decimal is not None:
        raw["decimal"] = decimal
    return parse_proposal_v3(raw)


def _derive(proposal):
    return derive_output_intent_v2(proposal, proposal_hash=proposal_content_hash_v3(proposal))


# ══ THE GATE — derivable from the proposal ALONE ═════════════════════════════════════════════════
def test_the_deriver_takes_the_proposal_AND_NOTHING_ELSE():
    """An intent derivable from the expectation alone cannot carry a value the expectation does not
    contain — which is what stops it becoming a third statement about the output."""
    import inspect

    parameters = inspect.signature(derive_output_intent_v2).parameters
    assert set(parameters) == {"proposal", "proposal_hash"}


def test_the_same_proposal_derives_the_SAME_intent():
    proposal = _proposal()
    assert _derive(proposal).identity_payload() == _derive(proposal).identity_payload()


def test_the_intent_NAMES_the_proposal_it_came_from():
    """So an intent that travelled away from its formula can be caught instead of trusted."""
    proposal = _proposal()
    assert _derive(proposal).derived_from_proposal_hash == proposal_content_hash_v3(proposal)


def test_an_intent_with_no_source_proposal_is_refused():
    with pytest.raises(ValueError, match="second declaration in all but name"):
        AuthoredOutputIntentV2(
            unit=None, additivity=None, conversion_required=False, declared_conversion_ref="",
            target_currency=None, numeric_shape=NumericShapeV2(38, 2, "half_even", "error"),
            authored_expectation_present=False, derived_from_proposal_hash="  ")


# ══ advisory and structural, kept apart ══════════════════════════════════════════════════════════
def test_the_AUTHORED_half_comes_from_expected_output():
    intent = _derive(_proposal(expected_output={
        "output_type": "decimal", "unit": "monetary", "currency": "AED"}))
    assert intent.authored_expectation_present
    assert intent.unit == "monetary"
    assert intent.target_currency == "AED"


def test_A_DETERMINISTIC_RUN_DECLARES_NO_EXPECTATION_AND_SAYS_SO():
    """C-A5's producer sets `expected_output=None`. "The author expected nothing" is a fact, and an
    empty string would collapse it with "the author expected an empty currency"."""
    intent = _derive(_proposal(expected_output=None))
    assert intent.authored_expectation_present is False
    assert intent.unit is None
    assert intent.target_currency is None
    assert intent.numeric_shape is not None, "the STRUCTURAL half is still present"


def test_an_intent_cannot_carry_authored_values_while_denying_an_expectation():
    """One of the two would be untrue and a reader could not tell which."""
    with pytest.raises(ValueError, match="one of the two is untrue"):
        AuthoredOutputIntentV2(
            unit="monetary", additivity=None, conversion_required=False,
            declared_conversion_ref="", target_currency=None,
            numeric_shape=NumericShapeV2(38, 2, "half_even", "error"),
            authored_expectation_present=False, derived_from_proposal_hash="sha256:x")


def test_NO_ADDITIVITY_IS_INVENTED():
    """`ExpectedOutput` carries none — it is not something a model is asked to guess, and a default
    here would put a value into the intent that nobody authored."""
    assert _derive(_proposal(expected_output={
        "output_type": "decimal", "unit": "monetary", "currency": "AED"})).additivity is None


# ══ conversion is STRUCTURAL, and the two readings cannot disagree ═══════════════════════════════
def test_a_declared_conversion_is_read_from_the_FORMULA_not_the_guess():
    intent = _derive(_proposal(refs={"currency_conversion_ref": FX_REF}))
    assert intent.conversion_required is True
    assert intent.declared_conversion_ref == FX_REF


def test_no_declared_conversion_means_none_required():
    intent = _derive(_proposal())
    assert intent.conversion_required is False
    assert intent.declared_conversion_ref == ""


def test_THE_TWO_READINGS_OF_ONE_FACT_CANNOT_DISAGREE():
    """`conversion_required` and `declared_conversion_ref` are two readings of ONE structural fact;
    letting them differ is exactly the second declaration this type prevents."""
    shape = NumericShapeV2(38, 2, "half_even", "error")
    with pytest.raises(ValueError, match="two readings of ONE structural fact"):
        AuthoredOutputIntentV2(
            unit=None, additivity=None, conversion_required=True, declared_conversion_ref="",
            target_currency=None, numeric_shape=shape, authored_expectation_present=False,
            derived_from_proposal_hash="sha256:x")
    with pytest.raises(ValueError, match="two readings of ONE structural fact"):
        AuthoredOutputIntentV2(
            unit=None, additivity=None, conversion_required=False,
            declared_conversion_ref=FX_REF, target_currency=None, numeric_shape=shape,
            authored_expectation_present=False, derived_from_proposal_hash="sha256:x")


# ══ the desired numeric shape ════════════════════════════════════════════════════════════════════
def test_the_numeric_shape_is_the_formulas_DECLARED_decimal():
    intent = _derive(_proposal(decimal={
        "precision": 20, "scale": 4, "rounding": "half_even", "overflow": "error"}))
    assert intent.numeric_shape == NumericShapeV2(20, 4, "half_even", "error")


def test_the_shape_is_DESIRED_not_resolved():
    """Whether it survives contact with the operands' real types is C-C6's answer, and it can
    refuse — recording the intent separately is what lets that refusal say which side disagreed."""
    from featuregen.formula.schema import DecimalPolicy, OverflowBehavior, RoundingMode
    from featuregen.materialize.physical_types_v2 import (
        DecimalTypeV2,
        PhysicalTypeRefusalV2,
        product_sum_type_v2,
    )

    intent = _derive(_proposal(decimal={
        "precision": 38, "scale": 8, "rounding": "half_even", "overflow": "error"}))
    assert intent.numeric_shape.precision == 38

    refused = product_sum_type_v2(
        DecimalTypeV2(30, 4), DecimalTypeV2(20, 4),
        DecimalPolicy(precision=38, scale=8, rounding=RoundingMode.HALF_EVEN,
                      overflow=OverflowBehavior.ERROR),
        round_per_row=False)
    assert isinstance(refused, PhysicalTypeRefusalV2), "the desired shape can be refused"


# ══ a formula that would be converted two ways refuses ═══════════════════════════════════════════
def test_TWO_DIFFERENT_CONVERSIONS_IN_ONE_FORMULA_REFUSE():
    """Its halves would be converted by different policies and the result would be in neither
    currency cleanly."""
    raw = json.loads((_GOLD_V2 / "01_avg_txn_amt_90d.json").read_text())["proposal"]
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    expr = raw["body"]["expr"]
    expr["row_selections"] = []
    expr["authority_refs"] = {"currency_conversion_ref": FX_REF}
    ratio = {
        "final_operation": "ratio", "zero_denominator": "null",
        "numerator": {**expr},
        "denominator": {**expr, "authority_refs": {
            "currency_conversion_ref": "currency_conversion:other-policy"}},
    }
    raw["body"] = ratio
    proposal = parse_proposal_v3(raw)

    with pytest.raises(ValueError, match="different currency conversions"):
        _derive(proposal)


# ══ terminal WITHOUT output policy resolution ════════════════════════════════════════════════════
def test_OUTPUT_INTENT_CAPTURED_follows_EITHER_review_path():
    import inspect

    from featuregen.formula import replay_trace

    table = inspect.getsource(replay_trace)
    assert f'"{OUTPUT_INTENT_CAPTURED}": lambda value: value in {{' in table
    assert '"CRITIC_COMPLETED", "REVIEW_BYPASSED"},\n        "OUTPUT_POLICY_RESOLVED"' in table


def test_A_V3_RUN_IS_TERMINAL_WITHOUT_OUTPUT_POLICY_RESOLVED():
    """`TERMINAL` accepts any prior stage, so a run that captured an intent and stopped is legal.
    Resolving the intent against C1's governed facts is S5's, and a stage that has not run must not
    be represented as having run and agreed."""
    import inspect

    from featuregen.formula import replay_trace

    table = inspect.getsource(replay_trace)
    assert '"TERMINAL": lambda _value: True' in table
    # OUTPUT_POLICY_RESOLVED is not a prerequisite of TERMINAL anywhere
    assert '"TERMINAL": lambda value: value == "OUTPUT_POLICY_RESOLVED"' not in table


def test_output_policy_resolution_may_still_follow_a_captured_intent():
    """S5 does not have to re-run the review stages to resolve an intent that was captured."""
    import inspect

    from featuregen.formula import replay_trace

    assert '"CRITIC_COMPLETED", "REVIEW_BYPASSED", "OUTPUT_INTENT_CAPTURED"' in inspect.getsource(
        replay_trace)


# ══ C-A8 — the tool seam fails CLOSED ════════════════════════════════════════════════════════════
def test_OMITTING_A_TOOL_RUNNER_REFUSES_ON_BOTH_ORCHESTRATORS():
    """`author_formula`'s `tool_runner` defaults to `run_tool` — the V1 set — so omitting the kwarg
    did not disable tools, it QUIETLY SWAPPED them. Both entry points reach that same default, so a
    seam failing closed in one and open in the other would just move the defect."""
    from featuregen.formula.authoring_v2 import ToolRunnerRequiredV2, _require_tool_runner_v2
    from featuregen.formula.replay_authoring_v2 import ToolRunnerRequired, _require_tool_runner

    with pytest.raises(ToolRunnerRequired, match="falls back to `run_tool`, the V1 set"):
        _require_tool_runner(None)
    with pytest.raises(ToolRunnerRequiredV2, match="falls back to `run_tool`, the V1 set"):
        _require_tool_runner_v2(None)

    _require_tool_runner(lambda **kw: {})       # a supplied runner passes
    _require_tool_runner_v2(lambda **kw: {})


def test_the_runner_is_ALWAYS_PASSED_never_conditionally_omitted():
    """The old call site was `**({} if tool_runner is None else {...})`, which is precisely how the
    V1 default got reached."""
    import inspect

    from featuregen.formula import replay_authoring_v2

    source = inspect.getsource(replay_authoring_v2)
    assert 'tool_runner=tool_runner,' in source
    assert '**({} if tool_runner is None else {"tool_runner": tool_runner})' not in source


def test_run_authoring_v2_now_TAKES_a_tool_runner():
    """C-A8's first clause — the non-replay orchestrator had no such parameter at all."""
    import inspect

    from featuregen.formula.authoring_v2 import run_authoring_v2

    assert "tool_runner" in inspect.signature(run_authoring_v2).parameters
