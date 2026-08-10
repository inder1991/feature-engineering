"""BR-6 increment 3 — the v2 operation rule table: one row per aggregate, consulted everywhere.

Until this increment the operand/argument rules lived as special cases inside the schema checker;
three groups in, the table IS the grammar's operation semantics — one place that answers, per
operation: does it take an operand, does it take an argument, what additivity can its result
honestly claim, what KIND of value comes out, and is it event-time-ordered (an order-sensitive
operation is meaningless without the window's event clock, and an execution engine that cannot
order cannot run it — capability negotiation reads this).

The at-cutoff group lands with the table:

* ``last_known`` / ``first_known`` — the value the world held at (or first showed inside) the
  window, ordered by event time. SEMI-additive, exactly like the balances they exist to read: a
  last-known balance sums across accounts and NEVER across time.
* ``zscore`` — the standardized latest value: (last_known − mean) / stddev over ONE window and
  ONE operand. A single honest aggregate, not a three-formula composite a reviewer must
  re-derive; NON-additive, dimensionless.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.formula.schema import AdditivityClass
from featuregen.formula.schema_v2 import AggregateFunctionV2

# What kind of value an operation emits — the output-authority (BR-6's later increment) and the
# recipe contract's RESULT_CLASS_ADDITIVITY consume this, not ad-hoc suffix guessing.
RESULT_KINDS = ("operand_valued", "count", "duration", "dimensionless")


@dataclass(frozen=True, slots=True)
class OperationRuleV1:
    aggregation: AggregateFunctionV2
    operand_required: bool
    argument: str                     # "forbidden" | "percentile"
    additivity: AdditivityClass
    result_kind: str                  # RESULT_KINDS
    order_sensitive: bool             # needs the event clock to mean anything


_R = OperationRuleV1
OPERATION_RULES: dict[AggregateFunctionV2, OperationRuleV1] = {rule.aggregation: rule for rule in (
    _R(AggregateFunctionV2.SUM, True, "forbidden", AdditivityClass.ADDITIVE,
       "operand_valued", False),
    _R(AggregateFunctionV2.COUNT_ROWS, False, "forbidden", AdditivityClass.ADDITIVE,
       "count", False),
    _R(AggregateFunctionV2.COUNT_NON_NULL, True, "forbidden", AdditivityClass.ADDITIVE,
       "count", False),
    _R(AggregateFunctionV2.COUNT_DISTINCT, True, "forbidden", AdditivityClass.NON_ADDITIVE,
       "count", False),
    _R(AggregateFunctionV2.MIN, True, "forbidden", AdditivityClass.NON_ADDITIVE,
       "operand_valued", False),
    _R(AggregateFunctionV2.MAX, True, "forbidden", AdditivityClass.NON_ADDITIVE,
       "operand_valued", False),
    _R(AggregateFunctionV2.AVG, True, "forbidden", AdditivityClass.NON_ADDITIVE,
       "operand_valued", False),
    _R(AggregateFunctionV2.RECENCY, True, "forbidden", AdditivityClass.NON_ADDITIVE,
       "duration", True),
    _R(AggregateFunctionV2.STDDEV, True, "forbidden", AdditivityClass.NON_ADDITIVE,
       "operand_valued", False),
    _R(AggregateFunctionV2.PERCENTILE, True, "percentile", AdditivityClass.NON_ADDITIVE,
       "operand_valued", False),
    _R(AggregateFunctionV2.MEDIAN, True, "forbidden", AdditivityClass.NON_ADDITIVE,
       "operand_valued", False),
    # increment 3 — the at-cutoff group
    _R(AggregateFunctionV2.LAST_KNOWN, True, "forbidden", AdditivityClass.SEMI_ADDITIVE,
       "operand_valued", True),
    _R(AggregateFunctionV2.FIRST_KNOWN, True, "forbidden", AdditivityClass.SEMI_ADDITIVE,
       "operand_valued", True),
    _R(AggregateFunctionV2.ZSCORE, True, "forbidden", AdditivityClass.NON_ADDITIVE,
       "dimensionless", True),
)}


def operation_rule(aggregation: AggregateFunctionV2) -> OperationRuleV1:
    """Total over the enum by the exhaustiveness test — a member without a rule cannot ship."""
    return OPERATION_RULES[aggregation]
