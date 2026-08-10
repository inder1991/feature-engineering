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
    p95 = _proposal("09_percentile_p95_txn_amt_90d.json")
    assert classify_formula_capability_v2(avg, engine=v1_only_engine) == "unsupported_engine"
    assert classify_formula_capability_v2(avg, engine=full_engine) == "ok"
    assert classify_formula_capability_v2(total, engine=v1_only_engine) == "ok"
    # the distributional group is likewise per-engine — a full-vocabulary engine advertises it
    assert classify_formula_capability_v2(p95, engine=full_engine) == "unsupported_engine", \
        "this engine never advertised percentile — advertisement is explicit, never assumed"
    # authoring-time (no engine) never claims materializability — grammar-ok regardless
    assert classify_formula_capability_v2(avg) == "ok"
    assert classify_formula_capability_v2(p95) == "ok"


def test_window_offsets_are_an_engine_capability_too():
    """Increment 4: an engine that can aggregate but cannot shift windows honestly refuses a lag
    formula — advertisement covers structure, not just operations."""
    lag = _proposal("16_lag_prev_period_sum.json")
    no_offset = EngineCapabilityV1(engine_id="basic",
                                   supported_aggregations=frozenset({"sum"}))
    with_offset = EngineCapabilityV1(engine_id="offset-capable",
                                     supported_aggregations=frozenset({"sum"}),
                                     supports_window_offset=True)
    assert classify_formula_capability_v2(lag, engine=no_offset) == "unsupported_engine"
    assert classify_formula_capability_v2(lag, engine=with_offset) == "ok"
    assert classify_formula_capability_v2(lag) == "ok", "grammar-ok regardless of engines"


def test_future_horizons_are_an_engine_capability_too():
    runoff = _proposal("27_future_maturity_runoff_sum.json")
    trailing_only = EngineCapabilityV1(engine_id="trailing-only",
                                       supported_aggregations=frozenset({"sum"}))
    forward = EngineCapabilityV1(engine_id="forward-capable",
                                 supported_aggregations=frozenset({"sum"}),
                                 supports_future_horizon=True)
    assert classify_formula_capability_v2(runoff, engine=trailing_only) == "unsupported_engine"
    assert classify_formula_capability_v2(runoff, engine=forward) == "ok"
    assert classify_formula_capability_v2(runoff) == "ok"
