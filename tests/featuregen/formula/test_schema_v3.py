"""C-A2/C-A3 — the v3 wire schema: the semantic row selection, and V2 left byte-identical.

The point of a whole new type family is the last clause. ``canonical_v2._plain_v2`` serializes every
dataclass field, and ``test_canonical_v2::test_the_projection_is_field_exhaustive`` pins that: *"a
field added later is hash-bearing automatically"*. Putting ``row_selections`` on
``AggregateExpressionV2`` would therefore have re-hashed every stored V2 artifact — so these tests
assert both that v3 carries the field AND that v2's canonical bytes did not move.
"""
from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from featuregen.formula.canonical_v2 import proposal_content_hash_v2
from featuregen.formula.canonical_v3 import canonical_json_v3, proposal_content_hash_v3
from featuregen.formula.parse_v2 import parse_proposal_v2, parse_versioned
from featuregen.formula.parse_v3 import parse_proposal_v3
from featuregen.formula.schema import SchemaError
from featuregen.formula.schema_v2 import AggregateExpressionV2, TypedFormulaProposalV2
from featuregen.formula.schema_v3 import (
    FORMULA_SCHEMA_VERSION_V3,
    SELECTION_TOKENS,
    AggregateExpressionV3,
    SelectionKind,
    TypedFormulaProposalV3,
)

_GOLD_V2 = Path(__file__).parent / "gold_v2"
_DIR_REF = "direction_sign:foundation-signed-by-indicator"


def _v3_raw(*, selections=None, authority_refs=..., filter_node=None) -> dict:
    """A v3 proposal built from a reviewed v2 exemplar — only the v3 deltas differ."""
    raw = json.loads((_GOLD_V2 / "01_avg_txn_amt_90d.json").read_text())["proposal"]
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    expr = raw["body"]["expr"]
    expr["authority_refs"] = (
        {"direction_policy_ref": _DIR_REF} if authority_refs is ... else authority_refs)
    if selections is None:
        selections = [{"kind": "transaction_direction", "role": "direction",
                       "semantic_value": "debit"}]
    expr["row_selections"] = selections
    if filter_node is not None:
        expr["filter"] = filter_node
    return raw


# ── V2 IS UNTOUCHED — the reason v3 exists at all ───────────────────────────────────────────────
def test_every_v2_gold_hash_is_unchanged_by_v3_existing():
    """Recomputed here rather than trusting `test_canonical_v2` alone: if adding v3 had perturbed
    any shared v2 type, these pinned hashes would move."""
    docs = [json.loads(p.read_text()) for p in sorted(_GOLD_V2.glob("*.json"))]
    ok = [d for d in docs if d["expected"] == "ok"]
    assert len(ok) == 25
    for doc in ok:
        assert (proposal_content_hash_v2(parse_proposal_v2(doc["proposal"]))
                == doc["expected_proposal_hash"]), doc["case_id"]


def test_the_v2_expression_has_no_selection_field():
    """The structural guarantee: `row_selections` exists on v3 and NOWHERE on v2."""
    assert "row_selections" not in {f.name for f in fields(AggregateExpressionV2)}
    assert "row_selections" in {f.name for f in fields(AggregateExpressionV3)}


# ── dispatch ────────────────────────────────────────────────────────────────────────────────────
def test_parse_versioned_routes_3_to_v3():
    proposal = parse_versioned(_v3_raw())
    assert isinstance(proposal, TypedFormulaProposalV3)
    assert proposal.body.expr.row_selections[0].semantic_value == "debit"


def test_parse_versioned_still_routes_2_to_v2():
    raw = json.loads((_GOLD_V2 / "01_avg_txn_amt_90d.json").read_text())["proposal"]
    assert isinstance(parse_versioned(raw), TypedFormulaProposalV2)


def test_an_unknown_version_still_refuses_loudly():
    with pytest.raises(SchemaError):
        parse_versioned({**_v3_raw(), "formula_schema_version": 99})


# ── C-A3: the selection's rules ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("token", ["debit", "credit"])
def test_a_semantic_token_is_accepted(token):
    proposal = parse_proposal_v3(_v3_raw(selections=[
        {"kind": "transaction_direction", "role": "direction", "semantic_value": token}]))
    assert proposal.body.expr.row_selections[0].semantic_value == token


def test_a_physical_literal_refuses():
    """THE rule that makes the formula portable: `D` is this source's spelling, and it belongs to
    the policy realization. A closed token set is what makes the refusal decidable at all."""
    with pytest.raises(SchemaError):
        parse_proposal_v3(_v3_raw(selections=[
            {"kind": "transaction_direction", "role": "direction", "semantic_value": "D"}]))


def test_two_selections_sharing_kind_and_role_refuse():
    """A tuple, not a single value — but one role is governed once."""
    sel = {"kind": "transaction_direction", "role": "direction", "semantic_value": "debit"}
    with pytest.raises(SchemaError, match="duplicate selection"):
        parse_proposal_v3(_v3_raw(selections=[sel, {**sel, "semantic_value": "credit"}]))


def test_a_selection_without_its_policy_ref_refuses():
    """Rule 14: `AuthorityRefsV2` owns the reference. A selection declares INTENT; without the
    matching ref nothing can resolve it to a column and a value."""
    with pytest.raises(SchemaError, match="direction_policy_ref"):
        parse_proposal_v3(_v3_raw(authority_refs=None))


def test_a_selection_beside_a_filter_refuses_selection_filter_conflict():
    """The schema cannot prove WHICH column a filter touches, so a filter beside a selection is
    refused rather than risk applying direction twice or contradicting it."""
    predicate = {"kind": "predicate", "left": "authored::public.txns.direction",
                 "op": "equal", "right_literal": {"type": "string", "value": "D"},
                 "right_param": None, "right_set": None}
    with pytest.raises(SchemaError, match="SELECTION_FILTER_CONFLICT"):
        parse_proposal_v3(_v3_raw(filter_node=predicate))


def test_no_selection_is_still_a_valid_v3_proposal():
    """v3 must not force a selection: most features need none."""
    proposal = parse_proposal_v3(_v3_raw(selections=[], authority_refs=None))
    assert proposal.body.expr.row_selections == ()


def test_the_token_matrix_is_closed_per_kind():
    assert set(SELECTION_TOKENS) == set(SelectionKind)
    assert SELECTION_TOKENS[SelectionKind.TRANSACTION_DIRECTION] == {"debit", "credit"}


# ── canonicalization ────────────────────────────────────────────────────────────────────────────
def test_the_selection_is_hash_bearing():
    """Two proposals differing only in the selected direction must be different formulas."""
    debit = parse_proposal_v3(_v3_raw())
    credit = parse_proposal_v3(_v3_raw(selections=[
        {"kind": "transaction_direction", "role": "direction", "semantic_value": "credit"}]))
    assert proposal_content_hash_v3(debit) != proposal_content_hash_v3(credit)
    assert "row_selections" in canonical_json_v3(debit)


def test_the_v3_projection_is_field_exhaustive():
    """The same guarantee v2 pins: a field added to any v3 type is hash-bearing automatically."""
    proposal = parse_proposal_v3(_v3_raw())
    body = json.loads(canonical_json_v3(proposal))

    def walk(value, serialized):
        if is_dataclass(value) and not isinstance(value, type):
            for f in fields(value):
                assert f.name in serialized, f"{f.name} missing from canonical-v3"
                walk(getattr(value, f.name), serialized[f.name])
        elif isinstance(value, tuple):
            for item, item_s in zip(value, serialized, strict=True):
                walk(item, item_s)

    walk(proposal, body)
