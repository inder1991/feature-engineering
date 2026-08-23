"""BR-6 close — output authority v2: the declarations get their teeth, and the compiler gets its
version guard. A per-row-currency monetary sum with no conversion policy REFUSES (the 63-recipe
audit class as a refusal, not a count); mixed-unit composites refuse; additivity folds to the
weakest term; and materialization admission refuses any formula that is not schema version 1 —
compiling v2 operations under v1 semantics would silently misread them.
"""
from __future__ import annotations

import json
from pathlib import Path

from featuregen.formula.output_authority_v2 import (
    CURRENCY_CONVERSION_UNDECLARED,
    MIXED_UNITS,
    FormulaOutputPolicyV2,
    InvalidOutputV2,
    OperandFactsV2,
    resolve_output_v2,
)
from featuregen.formula.parse_v2 import parse_proposal_v2
from featuregen.formula.schema_leaves import AdditivityClass

_GOLD_V2 = Path(__file__).parent / "gold_v2"


def _proposal(name: str):
    return parse_proposal_v2(json.loads((_GOLD_V2 / name).read_text())["proposal"])


_MONETARY_PER_ROW = OperandFactsV2(logical_type="decimal(38,6)", unit="monetary",
                                   currency="per_row")
_MONETARY_FIXED = OperandFactsV2(logical_type="decimal(38,6)", unit="monetary",
                                 currency="fixed:AED")


def test_a_per_row_currency_sum_without_a_conversion_policy_refuses():
    stripped = json.loads(
        (_GOLD_V2 / "30_posted_debit_amount_exemplar.json").read_text())["proposal"]
    stripped["body"]["expr"].pop("authority_refs")
    proposal = parse_proposal_v2(stripped)
    verdict = resolve_output_v2(
        proposal, {"authored::public.txns.txn_amt": _MONETARY_PER_ROW})
    assert isinstance(verdict, InvalidOutputV2)
    assert verdict.reason == CURRENCY_CONVERSION_UNDECLARED
    assert "never assumed" in verdict.detail


def test_the_exemplar_with_its_policies_resolves_as_converted_monetary():
    proposal = _proposal("30_posted_debit_amount_exemplar.json")
    policy = resolve_output_v2(
        proposal, {"authored::public.txns.txn_amt": _MONETARY_PER_ROW})
    assert isinstance(policy, FormulaOutputPolicyV2)
    assert policy.currency == "converted:policy:governed-rate-at-booking"
    assert policy.output_additivity is AdditivityClass.ADDITIVE
    assert policy.external_type_required is False


def test_a_fixed_currency_source_needs_no_conversion_and_says_which_currency():
    stripped = json.loads(
        (_GOLD_V2 / "30_posted_debit_amount_exemplar.json").read_text())["proposal"]
    stripped["body"]["expr"].pop("authority_refs")
    policy = resolve_output_v2(parse_proposal_v2(stripped),
                               {"authored::public.txns.txn_amt": _MONETARY_FIXED})
    assert isinstance(policy, FormulaOutputPolicyV2)
    assert policy.currency == "fixed:AED"


def test_result_kinds_type_the_output_and_additivity_folds_to_the_weakest():
    flag = resolve_output_v2(_proposal("23_any_match_dispute_flag.json"), {})
    assert (flag.output_type, flag.unit) == ("boolean", "flag")
    recency = resolve_output_v2(_proposal("07_recency_last_txn_90d.json"), {})
    assert (recency.output_type, recency.unit) == ("numeric", "days")
    # the working-capital signed sum: date_diff terms are non-additive → the sum is too
    cycle = resolve_output_v2(_proposal("34_working_capital_cycle_signed_sum.json"), {})
    assert cycle.output_additivity is AdditivityClass.NON_ADDITIVE
    # delta of two additive sums stays additive
    delta = resolve_output_v2(
        _proposal("17_delta_sum_current_minus_prev.json"),
        {"authored::public.txns.txn_amt": _MONETARY_FIXED})
    assert delta.output_additivity is AdditivityClass.ADDITIVE


def test_mixed_units_in_a_composite_refuse():
    doc = json.loads((_GOLD_V2 / "17_delta_sum_current_minus_prev.json").read_text())["proposal"]
    doc["body"]["subtrahend"]["aggregation"] = "count_rows"
    doc["body"]["subtrahend"]["operand"] = None
    verdict = resolve_output_v2(parse_proposal_v2(doc),
                                {"authored::public.txns.txn_amt": _MONETARY_FIXED})
    assert isinstance(verdict, InvalidOutputV2)
    assert verdict.reason == MIXED_UNITS


def test_materialization_admission_refuses_any_non_v1_schema_version():
    """BR-6's compiler guard, where materialization actually exists: the admission path consumes
    exactly Formula-v1; any other declared version refuses with FORMULA_SCHEMA_UNSUPPORTED
    rather than being silently compiled under v1 semantics."""
    from dataclasses import replace

    import pytest

    from featuregen.formula.parse import parse_proposal_v1
    from featuregen.materialize.admission import _verify_schema_version
    from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

    v1_doc = json.loads((Path(__file__).parent / "gold_fixtures"
                         / "01_sum_txn_amt_90d.json").read_text())["proposal"]
    proposal = parse_proposal_v1(v1_doc)
    from featuregen.formula.replay_authoring import _formula
    from featuregen.formula.schema import FormulaOutputPolicyV1
    from featuregen.formula.schema_leaves import AdditivityClass as A

    output = FormulaOutputPolicyV1(output_type="decimal(38,6)", unit=None, currency=None,
                                   output_additivity=A.NON_ADDITIVE,
                                   external_type_required=False)
    formula = _formula(proposal, output)
    _verify_schema_version(formula, "run-ok")          # v1 passes untouched
    imposter = replace(formula, formula_schema_version=2)
    with pytest.raises(MaterializationRefused) as caught:
        _verify_schema_version(imposter, "run-v2")
    assert caught.value.code is CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED
