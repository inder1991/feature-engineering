"""Child-1 §B — FormulaOperation → ``b_operation`` path-aggregation compatibility map.

Maps the frozen ``AggregateFunction`` vocabulary onto the EXISTING path-aggregation vocabulary
(``planner.multisource_contracts.PathAggregation``) so a TypedFormula expression can be checked
against, and (later) compiled onto, the shipped single-source aggregation machinery — WITHOUT
widening ``SupportedOperation`` / ``b_operation`` in place (§B forbids that).

§B mapping (verbatim):

* ``SUM``            → ``PathAggregation.sum``
* ``COUNT_ROWS``     → ``PathAggregation.count``            (a row count)
* ``COUNT_NON_NULL`` → ``PathAggregation.count``            (a non-null count is still a ``count``)
* ``COUNT_DISTINCT`` → ``PathAggregation.count_distinct``

``COUNT_ROWS`` / ``COUNT_NON_NULL`` are the Child-1 SPLIT of ``b_operation``'s generic ``count``; they
share the ``count`` path aggregation but differ in additivity/authority (§C/§D). ``min`` / ``max`` /
``avg`` / ``stddev`` / ``take_latest`` are OUT of the Child-1 vocabulary; ``RATIO`` / ``DIFFERENCE`` are
``FinalOperation``s (the body shape), never an ``AggregateFunction``, so they never reach this map.
``None`` for anything unmapped (``unsupported ≠ invalid``).
"""
from __future__ import annotations

from featuregen.formula.schema import AggregateFunction
from featuregen.overlay.upload.planner.multisource_contracts import PathAggregation

__all__ = ["to_path_aggregation"]

# §B — the ONLY supported FormulaOperation → PathAggregation edges. A function absent here is
# UNSUPPORTED (mapped to None), never INVALID.
_PATH_AGGREGATION: dict[AggregateFunction, PathAggregation] = {
    AggregateFunction.SUM: PathAggregation.sum,
    AggregateFunction.COUNT_ROWS: PathAggregation.count,
    AggregateFunction.COUNT_NON_NULL: PathAggregation.count,
    AggregateFunction.COUNT_DISTINCT: PathAggregation.count_distinct,
}


def to_path_aggregation(fn: AggregateFunction) -> PathAggregation | None:
    """The §B path aggregation for an ``AggregateFunction``, or ``None`` if it is unsupported.

    ``None`` (not an exception) for an unmapped function: an operation the path-aggregation vocabulary
    does not carry is UNSUPPORTED, not INVALID — the caller decides the disposition (§F)."""
    return _PATH_AGGREGATION.get(fn)
