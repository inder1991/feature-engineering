"""C-C7 — occurrence-based policy requirements, against the SHAPE-based function they replace.

The plan's gate is two claims: *"a country filter needs no reversal policy; an operand whose C1
facts are not `monetary`/`per_row` needs no currency occurrence"*. Both are asserted here directly
against `required_policy_kinds`, so the difference between the old answer and the new one is a test
rather than a docstring.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.parse_v3 import parse_proposal_v3
from featuregen.formula.policy_occurrences import (
    PolicyOccurrenceSetV1,
    PolicyOccurrenceV1,
    derive_policy_occurrences,
    occurrence_set_hash,
    required_policy_kinds_v2,
)
from featuregen.formula.schema_v3 import FORMULA_SCHEMA_VERSION_V3
from featuregen.overlay.upload.banking_policies import required_policy_kinds

_GOLD_V2 = Path(__file__).parent / "gold_v2"
DIR_REF = "direction_sign:foundation-signed-by-indicator"
STATUS_REF = "eligible_status:foundation-posted-events"
FX_REF = "currency_conversion:foundation-base-currency"
ENV = "hdfc-local"
DATASET = "hdfc::public.transactions"


def _expr(*, selections=None, refs=..., filter_node=None):
    raw = json.loads((_GOLD_V2 / "01_avg_txn_amt_90d.json").read_text())["proposal"]
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    expr = raw["body"]["expr"]
    expr["authority_refs"] = {"direction_policy_ref": DIR_REF} if refs is ... else refs
    expr["row_selections"] = selections if selections is not None else []
    if filter_node is not None:
        expr["filter"] = filter_node
    return parse_proposal_v3(raw).body.expr


DEBIT_SELECTION = [{"kind": "transaction_direction", "role": "direction",
                    "semantic_value": "debit"}]
COUNTRY_FILTER = {"kind": "predicate", "left": "authored::public.txns.country_code",
                  "op": "equal", "right_literal": {"type": "string", "value": "AE"}}


# ══ THE GATE — occurrence-based beats shape-based, in both directions ════════════════════════════
def test_A_COUNTRY_FILTER_NEEDS_NO_REVERSAL_POLICY():
    """The shape-based function says a filtered formula needs `reversal_correction`. A predicate on
    `country_code` has nothing to do with reversals — the schema cannot even see WHICH column a
    filter touches, so inferring a policy need from a filter's presence invents a requirement from
    something unreadable."""
    assert "reversal_correction" in required_policy_kinds(filtered=True)

    expression = _expr(filter_node=COUNTRY_FILTER)
    needed = required_policy_kinds_v2(expression, OperandFactsV2(unit="monetary",
                                                                 currency="fixed:AED"))
    assert "reversal_correction" not in needed
    assert needed == frozenset()


def test_a_NON_per_row_operand_needs_no_currency_occurrence():
    """The gate's second clause. A count, or an amount already in one fixed currency, has nothing
    to convert."""
    expression = _expr()
    for facts in (OperandFactsV2(unit="count", currency=""),
                  OperandFactsV2(unit="monetary", currency="fixed:AED"),
                  OperandFactsV2()):
        assert "currency_conversion" not in required_policy_kinds_v2(expression, facts), facts


def test_a_PER_ROW_MONETARY_operand_DOES_need_a_currency_occurrence():
    """The other side of the same rule, so the test proves a discriminator rather than a constant."""
    needed = required_policy_kinds_v2(
        _expr(), OperandFactsV2(unit="monetary", currency="per_row"))
    assert needed == frozenset({"currency_conversion"})


def test_a_row_selection_requires_the_kind_that_resolves_it():
    needed = required_policy_kinds_v2(
        _expr(selections=DEBIT_SELECTION), OperandFactsV2(unit="monetary", currency="fixed:AED"))
    assert needed == frozenset({"direction_sign"})


def test_the_shape_based_function_and_the_occurrence_based_one_DISAGREE_by_design():
    """Recorded rather than assumed: if these ever agreed, C-C7 would be a rename."""
    shape = set(required_policy_kinds(filtered=True, monetary=True))
    occurrence = set(required_policy_kinds_v2(
        _expr(filter_node=COUNTRY_FILTER), OperandFactsV2(unit="monetary", currency="fixed:AED")))
    assert shape != occurrence
    assert shape - occurrence == {"eligible_status", "reversal_correction", "direction_sign"}


# ══ occurrences name every part of their own proof ═══════════════════════════════════════════════
def test_an_occurrence_names_where_what_and_in_which_environment():
    occurrences = derive_policy_occurrences(
        {"body.expr": _expr(refs={"direction_policy_ref": DIR_REF, "status_policy_ref": STATUS_REF})},
        bound_datasets={"body.expr": DATASET}, environment_id=ENV)

    assert {o.policy_kind for o in occurrences.occurrences} == {"direction_sign", "eligible_status"}
    one = next(o for o in occurrences.occurrences if o.policy_kind == "direction_sign")
    assert one.expr_path == "body.expr"
    assert one.policy_ref_field == "direction_policy_ref"
    assert one.semantic_role == "direction"
    assert one.bound_dataset == DATASET
    assert one.environment_id == ENV


def test_the_semantic_ROLE_is_separate_from_the_FIELD_NAME():
    """C-C8 keys realization families on the ROLE, so a rename of the wire field must not silently
    become a change to what the policy DOES."""
    (occurrence,) = derive_policy_occurrences(
        {"body.expr": _expr(refs={"currency_conversion_ref": FX_REF})},
        bound_datasets={"body.expr": DATASET}, environment_id=ENV).occurrences
    assert occurrence.policy_ref_field == "currency_conversion_ref"
    assert occurrence.semantic_role == "currency_conversion"
    assert occurrence.policy_ref_field != occurrence.semantic_role


def test_the_same_policy_in_TWO_ENVIRONMENTS_is_two_occurrences():
    """The physical dataset behind them differs, so a realization proved for one is not proved for
    the other."""
    args = ({"body.expr": _expr()}, )
    local = derive_policy_occurrences(*args, bound_datasets={"body.expr": DATASET},
                                      environment_id="hdfc-local")
    prod = derive_policy_occurrences(*args, bound_datasets={"body.expr": DATASET},
                                     environment_id="hdfc-prod")
    assert occurrence_set_hash(local) != occurrence_set_hash(prod)


def test_an_expression_with_no_refs_produces_no_occurrences():
    empty = derive_policy_occurrences(
        {"body.expr": _expr(refs=None)}, bound_datasets={"body.expr": DATASET},
        environment_id=ENV)
    assert empty.occurrences == ()


def test_an_unbound_expression_is_REFUSED():
    """An occurrence names the physical dataset its policy applies to; one that could not would be
    a requirement with nowhere to be met."""
    with pytest.raises(ValueError, match="no bound dataset"):
        derive_policy_occurrences({"body.expr": _expr()}, bound_datasets={}, environment_id=ENV)


def test_a_blank_environment_is_refused():
    with pytest.raises(ValueError, match="per-environment"):
        derive_policy_occurrences({"body.expr": _expr()},
                                  bound_datasets={"body.expr": DATASET}, environment_id="  ")


# ══ the set is durable and order-independent ═════════════════════════════════════════════════════
def test_the_set_is_ordered_by_HASH_not_by_walk_order():
    """Ordering by discovery makes identity depend on a walk, and a walk changes for reasons that
    are not about the formula."""
    refs = {"direction_policy_ref": DIR_REF, "status_policy_ref": STATUS_REF,
            "reversal_policy_ref": "reversal_correction:foundation-flag-or-code"}
    occurrences = derive_policy_occurrences(
        {"body.expr": _expr(refs=refs)}, bound_datasets={"body.expr": DATASET},
        environment_id=ENV).occurrences
    assert [o.occurrence_hash for o in occurrences] == sorted(o.occurrence_hash for o in occurrences)


def test_a_DUPLICATE_occurrence_is_refused():
    """An occurrence is identified by policy, bound column and environment, so a duplicate is one
    place counted twice and would make a realization look doubly required."""
    one = PolicyOccurrenceV1(
        expr_path="body.expr", policy_ref_field="direction_policy_ref",
        policy_kind="direction_sign", policy_ref=DIR_REF, semantic_role="direction",
        bound_dataset=DATASET, bound_column=f"{DATASET}.txn_amt", environment_id=ENV)
    with pytest.raises(ValueError, match="same occurrence twice"):
        PolicyOccurrenceSetV1(occurrences=(one, one))


@pytest.mark.parametrize("blank", ["expr_path", "policy_kind", "policy_ref", "semantic_role",
                                   "bound_dataset", "environment_id", "policy_ref_field"])
def test_an_occurrence_missing_its_own_location_is_refused(blank):
    kwargs = dict(expr_path="body.expr", policy_ref_field="direction_policy_ref",
                  policy_kind="direction_sign", policy_ref=DIR_REF, semantic_role="direction",
                  bound_dataset=DATASET, bound_column="c", environment_id=ENV)
    kwargs[blank] = "  "
    with pytest.raises(ValueError, match="names no place"):
        PolicyOccurrenceV1(**kwargs)


def test_the_set_answers_the_questions_a_realization_stage_asks():
    occurrences = derive_policy_occurrences(
        {"body.expr": _expr(refs={"direction_policy_ref": DIR_REF, "status_policy_ref": STATUS_REF})},
        bound_datasets={"body.expr": DATASET}, environment_id=ENV)
    assert occurrences.kinds() == {"direction_sign", "eligible_status"}
    assert len(occurrences.for_expression("body.expr")) == 2
    assert occurrences.for_expression("body.nowhere") == ()
