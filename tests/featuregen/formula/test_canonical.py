"""Discriminating tests for the §E canonicalization + formula_content_hash.

Every equal-hash assertion has a nearby different-hash control so no test can
pass vacuously (e.g. by a canonicalizer that hashes everything the same).
Variants are built with the factory constructors in ``factories`` — the schema
dataclasses are slotted, so ``__dict__`` tricks are impossible by design.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import replace

import pytest
from tests.featuregen.formula import factories as f

from featuregen.formula.canonical import canonical_json, formula_content_hash
from featuregen.formula.schema import (
    FilterBool,
    FilterBoolOp,
    SchemaError,
)

# ---- stability + shape ----


def test_hash_is_stable_across_independent_rebuilds():
    one = f.base_formula()
    two = f.base_formula()
    assert one is not two
    assert canonical_json(one) == canonical_json(two)
    assert formula_content_hash(one) == formula_content_hash(two)


def test_hash_is_sha256_hex_of_the_canonical_json_utf8():
    formula = f.base_formula()
    text = canonical_json(formula)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert formula_content_hash(formula) == digest
    assert len(digest) == 64 and digest == digest.lower()


def test_canonical_document_covers_exactly_the_identity_fields():
    document = json.loads(canonical_json(f.base_formula()))
    assert set(document) == {
        "formula_schema_version",
        "operation_grammar_version",
        "output_policy_version",
        "canonicalization_version",
        "grain",
        "body",
        "parameters",
        "decimal",
        "output",
    }


# ---- ordered slots (SEMANTIC: never sorted) ----


def test_ratio_slot_swap_changes_the_hash():
    body = f.ratio_of_sums()
    swapped = replace(body, numerator=body.denominator, denominator=body.numerator)
    assert formula_content_hash(f.base_formula(body=body)) != formula_content_hash(
        f.base_formula(body=swapped)
    )


def test_grain_key_reorder_changes_the_hash():
    forward = f.base_formula(grain=f.customer_grain(keys=(f.CIF_KEY_REF, f.ACCOUNT_KEY_REF)))
    reversed_ = f.base_formula(grain=f.customer_grain(keys=(f.ACCOUNT_KEY_REF, f.CIF_KEY_REF)))
    assert formula_content_hash(forward) != formula_content_hash(reversed_)


# ---- associative AND/OR: flatten nested same-op, then sort by child hash ----


def _with_filter(node):
    return f.base_formula(
        body=f.ratio_of_sums(numerator=f.sum_expression(filter_node=node))
    )


def test_associative_and_child_reorder_gives_the_same_hash():
    p1, p2, p3 = f.equal_predicate(), f.in_predicate(), f.not_null_predicate()
    one = _with_filter(FilterBool(op=FilterBoolOp.AND, children=(p1, p2, p3)))
    two = _with_filter(FilterBool(op=FilterBoolOp.AND, children=(p3, p1, p2)))
    assert canonical_json(one) == canonical_json(two)
    assert formula_content_hash(one) == formula_content_hash(two)
    # Control: AND vs OR over the same children is a DIFFERENT formula.
    as_or = _with_filter(FilterBool(op=FilterBoolOp.OR, children=(p1, p2, p3)))
    assert formula_content_hash(one) != formula_content_hash(as_or)


def test_nested_same_op_is_flattened_before_sorting():
    p1, p2, p3 = f.equal_predicate(), f.in_predicate(), f.not_null_predicate()
    flat = _with_filter(FilterBool(op=FilterBoolOp.AND, children=(p1, p2, p3)))
    left_nested = _with_filter(
        FilterBool(
            op=FilterBoolOp.AND,
            children=(FilterBool(op=FilterBoolOp.AND, children=(p1, p2)), p3),
        )
    )
    right_nested = _with_filter(
        FilterBool(
            op=FilterBoolOp.AND,
            children=(p3, FilterBool(op=FilterBoolOp.AND, children=(p2, p1))),
        )
    )
    assert formula_content_hash(left_nested) == formula_content_hash(flat)
    assert formula_content_hash(right_nested) == formula_content_hash(flat)
    # Control: an OR nested under the AND is NOT the same-op and never merges.
    or_nested = _with_filter(
        FilterBool(
            op=FilterBoolOp.AND,
            children=(FilterBool(op=FilterBoolOp.OR, children=(p1, p2)), p3),
        )
    )
    assert formula_content_hash(or_nested) != formula_content_hash(flat)


def test_not_is_never_flattened_or_collapsed():
    p1, p2 = f.equal_predicate(), f.in_predicate()
    plain = _with_filter(p1)
    double_negated = _with_filter(
        FilterBool(
            op=FilterBoolOp.NOT,
            children=(FilterBool(op=FilterBoolOp.NOT, children=(p1,)),),
        )
    )
    assert formula_content_hash(double_negated) != formula_content_hash(plain)
    # An AND below a NOT still gets its associative reordering.
    not_and_12 = _with_filter(
        FilterBool(
            op=FilterBoolOp.NOT,
            children=(FilterBool(op=FilterBoolOp.AND, children=(p1, p2)),),
        )
    )
    not_and_21 = _with_filter(
        FilterBool(
            op=FilterBoolOp.NOT,
            children=(FilterBool(op=FilterBoolOp.AND, children=(p2, p1)),),
        )
    )
    assert formula_content_hash(not_and_12) == formula_content_hash(not_and_21)


# ---- the four identity versions are all hash-bearing, independently ----


@pytest.mark.parametrize(
    "version_field",
    [
        "formula_schema_version",
        "operation_grammar_version",
        "output_policy_version",
        "canonicalization_version",
    ],
)
def test_each_identity_version_independently_changes_the_hash(version_field: str):
    base = f.base_formula()
    bumped = replace(base, **{version_field: 2})
    assert formula_content_hash(bumped) != formula_content_hash(base)


def test_the_four_version_bumps_are_mutually_distinct():
    base = f.base_formula()
    hashes = {formula_content_hash(base)}
    for version_field in (
        "formula_schema_version",
        "operation_grammar_version",
        "output_policy_version",
        "canonicalization_version",
    ):
        hashes.add(formula_content_hash(replace(base, **{version_field: 2})))
    assert len(hashes) == 5


# ---- LogicalRef normalization (object_ref `_norm`: strip + lower-case) ----


def test_ref_case_and_padding_normalize_to_the_same_hash():
    messy_table = " FTR :: Public . Comp_Financial_Tran_Repos_Dly "
    messy = f.base_formula(
        grain=f.customer_grain(
            keys=("FTR::PUBLIC.COMP_FINANCIAL_TRAN_REPOS_DLY.CIF_ID", f" {f.ACCOUNT_KEY_REF} ")
        ),
        body=f.ratio_of_sums(
            numerator=f.sum_expression(
                "FTR::Public.Comp_Financial_Tran_Repos_Dly.TRAN_AMT_AED",
                table_ref=messy_table,
                filter_node=f.equal_predicate(left=f.TRAN_TYPE_REF.upper()),
                window=f.trailing_90d_window(event_time_ref=f" {f.EVENT_TIME_REF.upper()} "),
            )
        ),
    )
    clean = f.base_formula(
        body=f.ratio_of_sums(numerator=f.sum_expression(filter_node=f.equal_predicate()))
    )
    assert formula_content_hash(messy) == formula_content_hash(clean)
    text = canonical_json(messy)
    assert f.AMOUNT_REF in text
    assert "FTR" not in text
    # Control: a genuinely different column is a different identity.
    other = f.base_formula(
        body=f.ratio_of_sums(
            numerator=f.sum_expression(f"{f.TABLE_REF}.tran_amt_usd", filter_node=f.equal_predicate())
        )
    )
    assert formula_content_hash(other) != formula_content_hash(clean)


# ---- IN right_set + ParameterDecl.allowed_set: sorted + deduplicated ----


def test_in_right_set_order_and_duplicates_do_not_change_the_hash():
    shuffled_with_dup = _with_filter(f.in_predicate(values=("branch", "atm", "atm")))
    sorted_form = _with_filter(f.in_predicate(values=("atm", "branch")))
    assert canonical_json(shuffled_with_dup) == canonical_json(sorted_form)
    assert formula_content_hash(shuffled_with_dup) == formula_content_hash(sorted_form)
    # Control: a different member set is a different identity.
    smaller = _with_filter(f.in_predicate(values=("atm",)))
    assert formula_content_hash(smaller) != formula_content_hash(sorted_form)


def test_allowed_set_order_and_duplicates_do_not_change_the_hash():
    shuffled = f.base_formula(
        parameters=(f.string_parameter("channel", allowed_set=("y", "x", "x")),)
    )
    sorted_form = f.base_formula(
        parameters=(f.string_parameter("channel", allowed_set=("x", "y")),)
    )
    assert formula_content_hash(shuffled) == formula_content_hash(sorted_form)
    # Control: a different member set is a different identity.
    different = f.base_formula(
        parameters=(f.string_parameter("channel", allowed_set=("x", "z")),)
    )
    assert formula_content_hash(different) != formula_content_hash(sorted_form)


# ---- parameters: sorted by name; duplicate names rejected ----


def test_parameter_declaration_order_does_not_change_the_hash():
    alpha, beta = f.string_parameter("alpha"), f.string_parameter("beta")
    assert formula_content_hash(f.base_formula(parameters=(alpha, beta))) == formula_content_hash(
        f.base_formula(parameters=(beta, alpha))
    )
    # Control: dropping a declaration is a different identity.
    assert formula_content_hash(f.base_formula(parameters=(alpha,))) != formula_content_hash(
        f.base_formula(parameters=(alpha, beta))
    )


def test_duplicate_parameter_names_are_rejected():
    twice = (
        f.string_parameter("channel", classification="internal"),
        f.string_parameter("channel", classification="confidential"),
    )
    with pytest.raises(SchemaError):
        canonical_json(f.base_formula(parameters=twice))
    with pytest.raises(SchemaError):
        formula_content_hash(f.base_formula(parameters=twice))


# ---- NFC Unicode normalization ----


def test_nfc_and_nfd_equivalent_strings_hash_the_same():
    nfc_value = unicodedata.normalize("NFC", "café")  # robust to source-file form
    nfd_value = unicodedata.normalize("NFD", nfc_value)
    assert nfc_value != nfd_value  # guard: the two source forms really differ
    via_nfc = _with_filter(f.equal_predicate(value=nfc_value))
    via_nfd = _with_filter(f.equal_predicate(value=nfd_value))
    assert canonical_json(via_nfc) == canonical_json(via_nfd)
    assert formula_content_hash(via_nfc) == formula_content_hash(via_nfd)
    # Control: a genuinely different literal is a different identity.
    other = _with_filter(f.equal_predicate(value="cafe"))
    assert formula_content_hash(other) != formula_content_hash(via_nfc)


def test_nfc_applies_to_grain_entity_too():
    entity = unicodedata.normalize("NFC", "entité")
    nfd_form = unicodedata.normalize("NFD", entity)
    assert entity != nfd_form
    nfc_entity = f.base_formula(grain=f.customer_grain(entity=entity))
    nfd_entity = f.base_formula(grain=f.customer_grain(entity=nfd_form))
    assert formula_content_hash(nfc_entity) == formula_content_hash(nfd_entity)
