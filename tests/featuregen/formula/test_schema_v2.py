"""BR-6 increment 1 — the v2 schema and THE version dispatch, with v1 frozen by literal manifest.

The first test in this file is the plan's first step, mechanized: every Formula-v1 gold fixture's
pinned hash is FROZEN here as a literal — if adding v2 (or anything else, ever) moves one v1
byte, this fails before any reviewer has to notice. Then the v2 gate: version pins are DATA
(validated equal to the v2 constants, never inferred), avg/min/max join the vocabulary with the
same operand discipline, and the dispatch reads the declared version field and nothing else.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from featuregen.formula.parse_v2 import parse_proposal_v2, parse_versioned
from featuregen.formula.schema import SchemaError, TypedFormulaProposalV1
from featuregen.formula.schema_v2 import TypedFormulaProposalV2

_GOLD_V1 = Path(__file__).parent / "gold_fixtures"
_GOLD_V2 = Path(__file__).parent / "gold_v2"

# The Formula-v1 freeze (BR-6 step 1): every v1 gold fixture's pinned formula hash, as a LITERAL.
# Breaking this is breaking Formula-v1 immutability — plan invariant 3; there is no legitimate
# same-commit regeneration of THIS table short of a reviewed v1 re-versioning decision.
_V1_FROZEN_HASHES = {
    "01_sum_txn_amt_90d.json": "c9fdf481fea83671892f1b34f0b1780165c1d19a414e4462ffd7bad899c6bec9",
    "02_count_rows_txns_90d.json": "2a513d1199e31a849cd310f39fe9920ed6430e0a54417aa4790b7237f17cc58f",
    "03_count_non_null_merchant_90d.json": "ef08b156270bd5cb62f095877376ea9b568902acfe0a1d41873d83031cbec8a9",
    "04_count_distinct_merchant_90d.json": "f09abe419986f926ed2ba8b668959f4cfe92d77ae08d4ee931323d46a5903c84",
    "05_ratio_posted_amount_share_90d.json": "4ddc8c054d89dc50da2e30eba51de670c414a47ae28c88efeaf1a8d264af9fd5",
    "06_difference_amount_minus_fee_90d.json": "4da62c58f40a2d35def39799581ba1431d3256b1df45dd15daf7cf0f6091441e",
    "07_multi_source_is_unsupported.json": None,
    "08_over_deep_filter_is_invalid.json": None,
    "09_avg_is_unsupported_operation.json": None,
    "10_blocking_critic_wrong_slot.json": None,
    "11_ungoverned_type_needs_external_validation.json": None,
}


def test_every_formula_v1_gold_hash_is_frozen():
    fixtures = {p.name: json.loads(p.read_text()) for p in sorted(_GOLD_V1.glob("*.json"))}
    assert set(fixtures) == set(_V1_FROZEN_HASHES), \
        "the v1 gold corpus itself changed shape — v1 is frozen"
    for name, doc in fixtures.items():
        assert doc.get("expected_formula_hash") == _V1_FROZEN_HASHES[name], \
            f"{name}: the pinned v1 hash moved — Formula-v1 immutability is plan invariant 3"


def _ok_fixture(name: str) -> dict:
    return json.loads((_GOLD_V2 / name).read_text())["proposal"]


def test_the_new_group_parses_and_avg_graduates_from_v1_unsupported():
    """v1's gold corpus literally pins `avg` as an unsupported operation (fixture 09); under v2
    it is a first-class aggregate with the same operand discipline as sum."""
    proposal = parse_proposal_v2(_ok_fixture("01_avg_txn_amt_90d.json"))
    assert isinstance(proposal, TypedFormulaProposalV2)
    assert proposal.body.expr.aggregation.value == "avg"
    for name in ("02_max_balance_90d.json", "03_min_balance_90d.json",
                 "04_sum_txn_amt_90d_v2.json"):
        parse_proposal_v2(_ok_fixture(name))


def test_operand_discipline_holds_for_the_new_group():
    bad = json.loads((_GOLD_V2 / "05_avg_without_operand_invalid.json").read_text())
    assert bad["expected"] == "schema_error"
    with pytest.raises(SchemaError):
        parse_proposal_v2(bad["proposal"])
    with_operand_on_count_rows = _ok_fixture("01_avg_txn_amt_90d.json")
    with_operand_on_count_rows["body"]["expr"]["aggregation"] = "count_rows"
    with pytest.raises(SchemaError, match="count_rows carries no operand"):
        parse_proposal_v2(with_operand_on_count_rows)


def test_version_pins_are_data_not_inference():
    claimed_v1 = _ok_fixture("04_sum_txn_amt_90d_v2.json")
    claimed_v1["formula_schema_version"] = 1
    with pytest.raises(SchemaError):
        parse_proposal_v2(claimed_v1)   # the v2 parser refuses a v1 claim outright


def test_the_dispatch_reads_the_declared_version_and_nothing_else():
    v2 = parse_versioned(_ok_fixture("04_sum_txn_amt_90d_v2.json"))
    assert isinstance(v2, TypedFormulaProposalV2)
    v1_doc = json.loads((_GOLD_V1 / "01_sum_txn_amt_90d.json").read_text())["proposal"]
    v1 = parse_versioned(v1_doc)
    assert isinstance(v1, TypedFormulaProposalV1)
    # a version-less body is refused — never sniffed, however v1-shaped it looks
    unversioned = dict(v1_doc)
    unversioned.pop("formula_schema_version")
    with pytest.raises(SchemaError, match="never inferred from body shape"):
        parse_versioned(unversioned)
    with pytest.raises(SchemaError, match="never inferred"):
        parse_versioned({**v1_doc, "formula_schema_version": 3})


def test_the_distributional_group_parses_and_the_argument_is_disciplined():
    """Increment 2: recency / stddev / percentile / median. The aggregate argument is REQUIRED
    for percentile (p strictly inside (0,100)), FORBIDDEN everywhere else — a parameterized
    aggregate is declared, never smuggled into a label."""
    for name in ("07_recency_last_txn_90d.json", "08_stddev_txn_amt_90d.json",
                 "09_percentile_p95_txn_amt_90d.json", "10_median_txn_amt_90d.json"):
        parse_proposal_v2(_ok_fixture(name))
    p95 = parse_proposal_v2(_ok_fixture("09_percentile_p95_txn_amt_90d.json"))
    assert p95.body.expr.aggregation_argument == 95

    bare = json.loads((_GOLD_V2 / "11_percentile_without_argument_invalid.json").read_text())
    with pytest.raises(SchemaError, match="strictly between 0 and 100"):
        parse_proposal_v2(bare["proposal"])
    smuggled = json.loads((_GOLD_V2 / "12_argument_on_sum_invalid.json").read_text())
    with pytest.raises(SchemaError, match="takes no argument"):
        parse_proposal_v2(smuggled["proposal"])
    edge = _ok_fixture("09_percentile_p95_txn_amt_90d.json")
    edge["body"]["expr"]["aggregation_argument"] = 100
    with pytest.raises(SchemaError, match="strictly between"):
        parse_proposal_v2(edge)
