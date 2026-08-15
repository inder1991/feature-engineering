"""The v2 recipe-authoring seams: the frozen facts reader, the expectation validator, the tools.

The successor charter's increment 1 — the three things the replay-shaped v2 orchestrator needs
that the v1 seams structurally cannot give it (A3's plan defect 1 named all three). Every test
here is written so that the OBVIOUS wrong implementation fails it:

* a facts bundle keyed by v1's internal body PATH resolves every operand to empty facts —
  ``test_the_v2_facts_bundle_is_keyed_by_ref_not_by_body_path`` proves the ref keying by feeding
  the bundle to the real ``resolve_output_v2``, so a path-keyed bundle produces the WRONG policy
  rather than merely a differently-shaped dict;
* a validator that accepts a v1-shaped proposal, or that ignores any of the five keys v2 added,
  is caught one key at a time;
* a tool runner that answers ``list_supported_operations`` out of the v1 enum, or validates a v2
  draft with ``parse_proposal_v1``, is caught by name.
"""
from __future__ import annotations

from copy import deepcopy

import pytest
from tests.featuregen.formula.authoring_fixtures import (
    REF_AMT,
    REF_CIF,
    REF_DT,
    REF_FEE,
    REF_STATUS,
    TABLE_REF,
)

from featuregen.formula.output_authority_v2 import (
    FormulaOutputPolicyV2,
    OperandFactsV2,
    resolve_output_v2,
)
from featuregen.formula.parse_v2 import parse_proposal_v2
from featuregen.formula.recipe_authoring import (
    FrozenRecipeReadContext,
    recipe_expectation_validator_v2,
    recipe_tool_runner_v2,
)
from featuregen.formula.schema_v2 import AggregateFunctionV2, FinalOperationV2

# ── the shapes ───────────────────────────────────────────────────────────────────────────────────


def _window(event_time_ref: str = REF_DT, **overrides) -> dict:
    return {"event_time_ref": event_time_ref, "basis": "trailing", "length": 90, "unit": "day",
            "start_inclusive": "inclusive", "end_inclusive": "exclusive",
            "timezone": "Asia/Dubai", "empty_window": "null", "null_input": "ignore",
            "offset_periods": 0, **overrides}


def _expr(aggregation: str = "sum", operand: str | None = REF_AMT, **overrides) -> dict:
    return {"aggregation": aggregation, "operand": operand,
            "source_relation": {"table_ref": TABLE_REF}, "filter": None,
            "window": _window(), "aggregation_argument": None,
            "second_operand": None, "authority_refs": None, **overrides}


def _raw(body: dict | None = None, **overrides) -> dict:
    return {"formula_schema_version": 2, "operation_grammar_version": 1,
            "canonicalization_version": 1,
            "grain": {"entity": "account", "keys": [REF_CIF]},
            "body": body if body is not None else {
                "final_operation": "identity", "expr": _expr()},
            "parameters": [], "expected_output": None, "allocation_policy_ref": "",
            "decimal": {"precision": 38, "scale": 6, "rounding": "half_even",
                        "overflow": "error"},
            **overrides}


def _expected_expression(path: str = "body.expr", **overrides) -> dict:
    """One expectation expression in the WORK ITEM's dialect — the plain projection of
    ``BoundExpressionExpectationV2``, all twelve keys, which is what the worker hands the
    validator."""
    return {
        "expression_path": path,
        "aggregation": "sum",
        "operand_ref": REF_AMT,
        "second_operand_ref": None,
        "source_relation_ref": TABLE_REF,
        "event_time_ref": REF_DT,
        "window_length": 90,
        "window": {key: value for key, value in _window().items()
                   if key not in ("event_time_ref", "length")} | {
            "event_time_role": "clock", "length_parameter": "window_days"},
        "aggregation_argument": None,
        "authority_refs": None,
        "term_name": "",
        "term_sign": 0,
        **overrides,
    }


def _expectation(**overrides) -> dict:
    return {
        "formula_schema_version": "formula-v2",
        "final_operation": "identity",
        "expressions": [_expected_expression()],
        "grain_entity": "account",
        "grain_key_refs": [REF_CIF],
        "decimal": {"precision": 38, "scale": 6, "rounding": "half_even", "overflow": "error"},
        "policy_version": 1,
        **overrides,
    }


def _violations(raw: dict, expectation: dict | None = None) -> tuple[str, ...]:
    return recipe_expectation_validator_v2(
        expectation if expectation is not None else _expectation())(parse_proposal_v2(raw))


# ── the frozen facts reader ──────────────────────────────────────────────────────────────────────


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _SnapshotConnection:
    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    def execute(self, query, parameters):
        assert "catalog_metadata_snapshot_item" in query
        self.queries += 1
        return _Rows(self.rows)


def _snapshot(*, currency: str = "fixed:AED", unit: str = "monetary",
              status: str = "resolved", refs=(REF_AMT, REF_FEE, REF_DT, REF_CIF)
              ) -> FrozenRecipeReadContext:
    rows = []
    for ref in refs:
        rows.extend([
            (ref, "logical_representation", {"value": "decimal"},
             {"status": "resolved", "authority": "governed"}),
            (ref, "unit", {"value": unit}, {"status": status, "authority": "governed"}),
            (ref, "currency", {"value": currency}, {"status": status, "authority": "governed"}),
            (ref, "is_grain", {"value": "true" if ref == REF_CIF else "false"},
             {"status": "resolved", "authority": "governed"}),
        ])
    return FrozenRecipeReadContext.load(
        _SnapshotConnection(rows), "snapshot-v2", frozenset(refs))


def test_the_v2_facts_bundle_is_keyed_by_ref_not_by_body_path() -> None:
    """A3's plan defect 4, at the FROZEN reader. Proved through the real resolver rather than by
    inspecting keys: a bundle keyed v1's way ("body.expr") resolves every operand to empty facts,
    so the monetary output below would come back with no currency at all."""
    context = _snapshot()
    facts, failures = context.formula_facts_v2(parse_proposal_v2(_raw()))

    assert set(facts) == {REF_AMT}, "keyed by the operand's logical_ref"
    assert facts[REF_AMT] == OperandFactsV2(
        logical_type="decimal", unit="monetary", currency="fixed:AED")
    assert failures == ()

    resolved = resolve_output_v2(parse_proposal_v2(_raw()), facts)
    assert isinstance(resolved, FormulaOutputPolicyV2)
    assert (resolved.unit, resolved.currency) == ("monetary", "fixed:AED")

    # ...and the v1 keying, which is the mistake this test exists to catch.
    path_keyed = {"body.expr": facts[REF_AMT]}
    mis_resolved = resolve_output_v2(parse_proposal_v2(_raw()), path_keyed)
    assert isinstance(mis_resolved, FormulaOutputPolicyV2)
    assert mis_resolved.currency == "", "a path-keyed bundle assembles a policy out of nothing"


def test_the_frozen_reader_reads_the_second_operand_v1_has_no_notion_of() -> None:
    """``date_diff``'s second column is an operand like any other: unread, its governed facts are
    silently empty and the output policy is assembled over half the evidence."""
    body = {"final_operation": "identity",
            "expr": _expr("date_diff_avg", REF_AMT, second_operand=REF_FEE)}
    facts, failures = _snapshot().formula_facts_v2(parse_proposal_v2(_raw(body)))
    assert set(facts) == {REF_AMT, REF_FEE}
    assert failures == ()


def test_a_governed_read_that_failed_closed_is_attributed_not_silently_empty() -> None:
    """The same three fail-closed statuses the live reader treats as missing authority. The fact
    is EMPTY and the failure names the ref and the field — a guess is never assembled from a read
    the C1 gates refused to serve."""
    context = _snapshot(status="fork")
    facts, failures = context.formula_facts_v2(parse_proposal_v2(_raw()))
    assert facts[REF_AMT].unit == "" and facts[REF_AMT].currency == ""
    assert {(f.operand, f.field) for f in failures} == {(REF_AMT, "unit"), (REF_AMT, "currency")}


def test_a_grain_key_whose_governance_failed_closed_is_a_failure_too() -> None:
    rows = [
        (REF_AMT, "logical_representation", {"value": "decimal"},
         {"status": "resolved", "authority": "governed"}),
        (REF_AMT, "unit", {"value": "count"}, {"status": "resolved", "authority": "governed"}),
        (REF_AMT, "currency", {"value": ""}, {"status": "resolved", "authority": "governed"}),
        (REF_CIF, "is_grain", {"value": "true"},
         {"status": "hash_mismatch", "authority": "governed"}),
    ]
    context = FrozenRecipeReadContext.load(
        _SnapshotConnection(rows), "snapshot-v2", frozenset({REF_AMT, REF_CIF}))
    _facts, failures = context.formula_facts_v2(parse_proposal_v2(_raw()))
    assert [(f.operand, f.field, f.reason) for f in failures] == [
        (REF_CIF, "is_grain", "hash_mismatch")]


def test_the_v1_facts_reader_is_untouched() -> None:
    """The frozen half: ``formula_facts`` still returns v1 ``ExprFacts`` keyed by body path, and
    its bytes are what live work items were sealed against."""
    from tests.featuregen.formula.test_parse import AMT, CUSTOMER_KEY, raw_unary_proposal

    from featuregen.formula.parse import parse_proposal_v1

    rows = [(ref, field, {"value": "decimal"}, {"status": "resolved", "authority": "governed"})
            for ref in (AMT, CUSTOMER_KEY)
            for field in ("logical_representation", "additivity", "unit", "currency", "is_grain")]
    context = FrozenRecipeReadContext.load(
        _SnapshotConnection(rows), "snapshot-v1", frozenset({AMT, CUSTOMER_KEY}))
    per_expr, grain = context.formula_facts(parse_proposal_v1(raw_unary_proposal()))
    assert set(per_expr) == {"body.expr"}
    assert grain[CUSTOMER_KEY].status == "resolved"


# ── the v2 expectation validator ─────────────────────────────────────────────────────────────────


def test_an_exact_v2_proposal_preserves_its_expectation() -> None:
    assert _violations(_raw()) == ()


@pytest.mark.parametrize(("mutation", "code"), [
    ({"aggregation": "count_distinct"}, "AGGREGATION_NOT_PRESERVED"),
    ({"operand": REF_FEE}, "OPERAND_NOT_PRESERVED"),
    ({"source_relation": {"table_ref": "authored::public.other"},
      "operand": "authored::public.other.txn_amt",
      "window": _window("authored::public.other.txn_dt")},
     "SOURCE_RELATION_NOT_PRESERVED"),
])
def test_a_substituted_v1_shaped_key_is_named(mutation, code) -> None:
    """The seven keys v1 also preserves — restated over v2 so a v2 run is no weaker."""
    assert code in _violations(_raw({"final_operation": "identity", "expr": _expr(**mutation)}))


def test_a_substituted_window_length_or_clock_is_named() -> None:
    body = {"final_operation": "identity",
            "expr": _expr(window=_window(REF_STATUS, length=30))}
    violations = _violations(_raw(body))
    assert "WINDOW_LENGTH_NOT_PRESERVED" in violations
    assert "EVENT_TIME_NOT_PRESERVED" in violations


@pytest.mark.parametrize("key", [
    "basis", "unit", "start_inclusive", "end_inclusive", "timezone", "empty_window", "null_input",
])
def test_every_window_policy_key_is_preserved(key) -> None:
    replacement = {"basis": "calendar_period", "unit": "month", "start_inclusive": "exclusive",
                   "end_inclusive": "inclusive", "timezone": "UTC", "empty_window": "zero",
                   "null_input": "propagate"}[key]
    body = {"final_operation": "identity", "expr": _expr(window=_window(**{key: replacement}))}
    assert "WINDOW_POLICY_NOT_PRESERVED" in _violations(_raw(body))


def test_offset_periods_is_a_window_key_the_v1_validator_does_not_have() -> None:
    """v2's lag/delta composition. The v1 validator's expected-policy projection lists seven keys
    and this is not one of them, so a shifted window would ride through unnamed."""
    body = {"final_operation": "identity", "expr": _expr(window=_window(offset_periods=3))}
    assert "WINDOW_POLICY_NOT_PRESERVED" in _violations(_raw(body))


def test_a_second_operand_substitution_is_named() -> None:
    expectation = _expectation(expressions=[_expected_expression(
        aggregation="date_diff_avg", second_operand_ref=REF_DT)])
    exact = _raw({"final_operation": "identity",
                  "expr": _expr("date_diff_avg", REF_AMT, second_operand=REF_DT)})
    assert _violations(exact, expectation) == ()
    substituted = _raw({"final_operation": "identity",
                        "expr": _expr("date_diff_avg", REF_AMT, second_operand=REF_STATUS)})
    assert "SECOND_OPERAND_NOT_PRESERVED" in _violations(substituted, expectation)


def test_an_aggregation_argument_substitution_is_named() -> None:
    expectation = _expectation(expressions=[_expected_expression(
        aggregation="percentile", aggregation_argument=95)])
    exact = _raw({"final_operation": "identity",
                  "expr": _expr("percentile", REF_AMT, aggregation_argument=95)})
    assert _violations(exact, expectation) == ()
    moved = _raw({"final_operation": "identity",
                  "expr": _expr("percentile", REF_AMT, aggregation_argument=99)})
    assert "AGGREGATION_ARGUMENT_NOT_PRESERVED" in _violations(moved, expectation)


def test_an_authority_ref_substitution_is_named() -> None:
    """The governed policies the expression computes under are IDENTITY-bearing: dropping the
    currency-conversion ref changes which rows the number is even about."""
    refs = {"status_policy_ref": "policy.posted", "direction_policy_ref": "",
            "reversal_policy_ref": "", "currency_conversion_ref": "policy.fx.eod"}
    expectation = _expectation(expressions=[_expected_expression(authority_refs=refs)])
    exact = _raw({"final_operation": "identity", "expr": _expr(authority_refs=dict(refs))})
    assert _violations(exact, expectation) == ()

    dropped = deepcopy(refs)
    dropped["currency_conversion_ref"] = ""
    mutated = _raw({"final_operation": "identity", "expr": _expr(authority_refs=dropped)})
    assert "AUTHORITY_REFS_NOT_PRESERVED" in _violations(mutated, expectation)

    absent = _raw({"final_operation": "identity", "expr": _expr(authority_refs=None)})
    assert "AUTHORITY_REFS_NOT_PRESERVED" in _violations(absent, expectation)


def test_a_blank_authority_block_never_reaches_the_validator_at_all() -> None:
    """The stronger law, found by trying it: ``AuthorityRefsV2.__post_init__`` refuses four blanks
    outright (*"authority_refs with every ref blank is a lie — omit the block instead"*), so the
    validator's ``None``-vs-``{}`` distinction can never be tested through a parsed proposal. The
    projection keeps the distinction anyway, because a work item's stored expectation is a DICT
    and nothing re-parses it."""
    from featuregen.formula.schema import SchemaError

    blank = {"status_policy_ref": "", "direction_policy_ref": "",
             "reversal_policy_ref": "", "currency_conversion_ref": ""}
    with pytest.raises(SchemaError, match="every ref blank is a lie"):
        parse_proposal_v2(_raw({"final_operation": "identity",
                                "expr": _expr(authority_refs=blank)}))

    expectation = _expectation(expressions=[_expected_expression(authority_refs=blank)])
    assert "AUTHORITY_REFS_NOT_PRESERVED" in _violations(_raw(), expectation)


def test_a_different_combiner_refuses_whole() -> None:
    """The combiner decides the body SHAPE, so every positional comparison downstream would be
    meaningless — v1 refuses whole here too."""
    ratio = {"final_operation": "ratio", "numerator": _expr(),
             "denominator": _expr("count_rows", None), "zero_denominator": "null"}
    assert _violations(_raw(ratio)) == ("FINAL_OPERATION_NOT_PRESERVED",)


def test_a_ratio_body_preserves_both_expressions_independently() -> None:
    expectation = _expectation(final_operation="ratio", expressions=[
        _expected_expression("body.numerator"),
        _expected_expression("body.denominator", aggregation="count_rows", operand_ref=None)])
    exact = _raw({"final_operation": "ratio", "numerator": _expr(),
                  "denominator": _expr("count_rows", None), "zero_denominator": "null"})
    assert _violations(exact, expectation) == ()
    swapped = _raw({"final_operation": "ratio", "numerator": _expr(operand=REF_FEE),
                    "denominator": _expr("count_rows", None), "zero_denominator": "null"})
    assert "OPERAND_NOT_PRESERVED" in _violations(swapped, expectation)


def test_a_signed_sum_preserves_its_term_names_and_signs() -> None:
    expectation = _expectation(final_operation="signed_sum", expressions=[
        _expected_expression("body.terms[0].expr", term_name="credits", term_sign=1),
        _expected_expression("body.terms[1].expr", term_name="debits", term_sign=-1,
                             operand_ref=REF_FEE)])
    terms = [{"name": "credits", "sign": 1, "expr": _expr()},
             {"name": "debits", "sign": -1, "expr": _expr(operand=REF_FEE)}]
    assert _violations(_raw({"final_operation": "signed_sum", "terms": terms}),
                       expectation) == ()

    flipped = deepcopy(terms)
    flipped[1]["sign"] = 1
    assert "TERM_SIGN_NOT_PRESERVED" in _violations(
        _raw({"final_operation": "signed_sum", "terms": flipped}), expectation)

    renamed = deepcopy(terms)
    renamed[0]["name"] = "inflows"
    assert "TERM_NAME_NOT_PRESERVED" in _violations(
        _raw({"final_operation": "signed_sum", "terms": renamed}), expectation)


def test_the_grain_decimal_and_parameters_are_preserved() -> None:
    assert "GRAIN_ENTITY_NOT_PRESERVED" in _violations(
        _raw(grain={"entity": "customer", "keys": [REF_CIF]}))
    assert "GRAIN_KEYS_NOT_PRESERVED" in _violations(
        _raw(grain={"entity": "account", "keys": [REF_STATUS]}))
    assert "DECIMAL_POLICY_NOT_PRESERVED" in _violations(
        _raw(decimal={"precision": 18, "scale": 2, "rounding": "half_even",
                      "overflow": "error"}))


def test_an_unauthored_filter_is_named() -> None:
    body = {"final_operation": "identity",
            "expr": _expr(filter={"kind": "predicate", "op": "is_not_null", "left": REF_AMT})}
    assert "UNAUTHORED_FILTER" in _violations(_raw(body))


def test_an_expectation_the_rule_table_refuses_is_shape_invalid_not_preserved() -> None:
    """A degraded expectation must never be "preserved" by an equally degraded proposal. The
    validator re-asks the operation rule table the question the binder answered at capture."""
    unknown = _expectation(expressions=[_expected_expression(aggregation="median_of_medians")])
    assert _violations(_raw(), unknown) == ("EXPECTATION_SHAPE_INVALID",)

    argument_on_a_sum = _expectation(
        expressions=[_expected_expression(aggregation_argument=95)])
    assert _violations(_raw(), argument_on_a_sum) == ("EXPECTATION_SHAPE_INVALID",)

    operandless_sum = _expectation(expressions=[_expected_expression(operand_ref=None)])
    assert _violations(_raw(), operandless_sum) == ("EXPECTATION_SHAPE_INVALID",)


def test_an_expectation_whose_paths_are_not_canonical_is_shape_invalid() -> None:
    assert _violations(_raw(), _expectation(
        expressions=[_expected_expression("body.numerator")])) == ("EXPECTATION_SHAPE_INVALID",)


def test_the_expectation_expression_count_must_match_the_combiner() -> None:
    two_for_identity = _expectation(
        expressions=[_expected_expression(), _expected_expression()])
    assert _violations(_raw(), two_for_identity) == ("EXPECTATION_SHAPE_INVALID",)


def test_the_v1_validator_still_refuses_a_v2_proposal_which_is_why_v2_needs_its_own() -> None:
    """The whole reason this sibling exists. Handing a v2 proposal to the v1 validator is not a
    near miss — the v1 validator is typed and written for ``UnaryBody`` and reports the FIRST
    thing it can, which would become a durable ``invalid_formula → REJECTED`` about a recipe
    nobody ever failed to author."""
    from featuregen.formula.recipe_authoring import recipe_expectation_validator

    v1_shaped = {
        "final_operation": "identity", "grain_entity": "account",
        "grain_key_refs": [REF_CIF],
        "expressions": [{"aggregation": "sum", "operand_ref": REF_AMT,
                         "source_relation_ref": TABLE_REF, "event_time_ref": REF_DT,
                         "window_length": 90, "window": {}}],
        "decimal": {"precision": 38, "scale": 6, "rounding": "half_even", "overflow": "error"},
    }
    assert recipe_expectation_validator(v1_shaped)(parse_proposal_v2(_raw())) == (
        "FINAL_OPERATION_NOT_PRESERVED",)


# ── the v2 tool runner ───────────────────────────────────────────────────────────────────────────


def test_the_v2_tool_runner_answers_in_the_v2_grammar() -> None:
    """v1's ``list_supported_operations`` answers out of the v1 ``AggregateFunction`` enum — a
    grammar the model on a v2 run is not authoring in."""
    runner = recipe_tool_runner_v2(frozenset({REF_AMT}))
    answer = runner(object(), "list_supported_operations", {})
    names = {item["name"] for item in answer["aggregate_functions"]}
    assert names == {fn.value for fn in AggregateFunctionV2}
    assert set(answer["final_operations"]) == {op.value for op in FinalOperationV2}
    # ...and the v2 vocabulary is genuinely wider, so the two answers are not interchangeable.
    from featuregen.formula.schema import AggregateFunction

    assert names - {fn.value for fn in AggregateFunction}


def test_the_v2_tool_runner_calls_a_valid_v2_draft_valid() -> None:
    """v1's ``validate_draft_formula`` runs ``parse_proposal_v1``, so it would answer ``invalid``
    for this exact draft — teaching the model to abandon a correct proposal."""
    runner = recipe_tool_runner_v2(frozenset({REF_AMT}))
    assert runner(object(), "validate_draft_formula", {"proposal": _raw()}) == {
        "verdict": "ok", "detail": None, "operation_grammar_version": 1}

    from featuregen.formula.recipe_authoring import recipe_tool_runner

    v1_answer = recipe_tool_runner(frozenset({REF_AMT}))(
        None, "validate_draft_formula", {"proposal": _raw()})
    assert v1_answer["verdict"] == "invalid", "which is exactly why the sibling exists"


def test_the_v2_tool_runner_keeps_the_frozen_ref_gate_and_the_closed_tool_set() -> None:
    context = _snapshot()
    runner = recipe_tool_runner_v2(frozenset({REF_AMT}), frozen_context=context)
    assert runner(object(), "search_columns", {"query": "amount"}) == {
        "error": "tool is unavailable for frozen recipe authoring"}
    assert runner(object(), "get_column_metadata", {"logical_ref": REF_STATUS}) == {
        "error": "logical_ref is outside the frozen recipe bindings"}
    metadata = runner(object(), "get_column_metadata", {"logical_ref": REF_AMT})
    assert metadata["found"] is True and metadata["logical_ref"] == REF_AMT


def test_a_malformed_draft_is_invalid_with_a_bounded_detail() -> None:
    runner = recipe_tool_runner_v2(frozenset({REF_AMT}))
    assert runner(object(), "validate_draft_formula", {"proposal": "not an object"}) == {
        "error": "validate_draft_formula requires an object 'proposal'"}
    answer = runner(object(), "validate_draft_formula", {"proposal": {"body": {}}})
    assert answer["verdict"] == "invalid" and len(answer["detail"]) <= 500
