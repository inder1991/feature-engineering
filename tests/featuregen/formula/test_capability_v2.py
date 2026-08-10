"""BR-6 increment 1 — capability classification and engine negotiation: a grammar gap and an
engine gap are DIFFERENT verdicts, and neither is ever approximated."""
from __future__ import annotations

import json
from pathlib import Path

from featuregen.formula.capability_v2 import (
    EngineCapabilityV1,
    classify_formula_capability_v2,
)
from featuregen.formula.parse_v2 import parse_proposal_v2

_GOLD_V2 = Path(__file__).parent / "gold_v2"


def _proposal(name: str):
    return parse_proposal_v2(json.loads((_GOLD_V2 / name).read_text())["proposal"])


def test_the_increment_one_vocabulary_is_ok_at_authoring_time():
    for name in ("01_avg_txn_amt_90d.json", "02_max_balance_90d.json",
                 "03_min_balance_90d.json", "04_sum_txn_amt_90d_v2.json"):
        assert classify_formula_capability_v2(_proposal(name)) == "ok"


def test_cross_source_stays_unsupported_never_approximated():
    doc = json.loads((_GOLD_V2 / "06_cross_source_ratio_unsupported.json").read_text())
    assert doc["expected"] == "unsupported_capability"
    proposal = parse_proposal_v2(doc["proposal"])
    assert classify_formula_capability_v2(proposal) == "unsupported_capability"


def test_an_engine_advertises_and_the_verdict_distinguishes_engine_from_grammar():
    """A legacy engine that only materializes the v1 four can still run a v2 sum — and honestly
    cannot run a v2 avg: unsupported_engine, the MATERIALIZATION_BLOCKED input, distinct from
    unsupported_capability (a grammar gap)."""
    v1_only_engine = EngineCapabilityV1(
        engine_id="legacy-sql",
        supported_aggregations=frozenset({"sum", "count_rows", "count_non_null",
                                          "count_distinct"}))
    full_engine = EngineCapabilityV1(
        engine_id="kedro-spark",
        supported_aggregations=frozenset({"sum", "count_rows", "count_non_null",
                                          "count_distinct", "min", "max", "avg"}))
    avg = _proposal("01_avg_txn_amt_90d.json")
    total = _proposal("04_sum_txn_amt_90d_v2.json")
    assert classify_formula_capability_v2(avg, engine=v1_only_engine) == "unsupported_engine"
    assert classify_formula_capability_v2(avg, engine=full_engine) == "ok"
    assert classify_formula_capability_v2(total, engine=v1_only_engine) == "ok"
    # authoring-time (no engine) never claims materializability — avg is grammar-ok regardless
    assert classify_formula_capability_v2(avg) == "ok"
