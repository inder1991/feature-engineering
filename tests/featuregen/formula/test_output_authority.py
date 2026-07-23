"""Task 6 — operation map + corrected additivity + C1 output-authority resolver.

The authoritative output policy is resolved ONLY from C1 governed facts (the ``per_expr_facts``
``OperationalValue`` bundles seeded through the REAL governed path by the Task-5 fixtures) — NEVER
from the proposal's advisory ``expected_output``. See ``docs/superpowers/specs/...child1...`` §B
(operation map), §C (the operation-specific required-field matrix over C1) and §D (corrected
additivity).
"""
from __future__ import annotations

import pytest

from featuregen.formula.operations import to_path_aggregation
from featuregen.formula.schema import AggregateFunction
from featuregen.overlay.upload.planner.multisource_contracts import PathAggregation


# ── §B operation → path-aggregation compatibility map ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        (AggregateFunction.SUM, PathAggregation.sum),
        (AggregateFunction.COUNT_ROWS, PathAggregation.count),
        (AggregateFunction.COUNT_NON_NULL, PathAggregation.count),
        (AggregateFunction.COUNT_DISTINCT, PathAggregation.count_distinct),
    ],
)
def test_to_path_aggregation_maps_the_supported_vocabulary(fn, expected):
    assert to_path_aggregation(fn) is expected
