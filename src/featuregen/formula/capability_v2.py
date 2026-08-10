"""BR-6 increment 1 — v2 capability classification and execution-engine negotiation.

Two distinct questions, deliberately two arguments of one function:

* **Is this proposal inside the v2 GRAMMAR's capability?** (authoring-time; ``engine=None``) —
  the v1 single-source rule carries over verbatim (cross-source formulas stay unsupported until a
  governed multisource capability is accepted), and the aggregate vocabulary is increment 1's.
* **Can THIS engine run it?** (execution-time; ``engine`` given) — an engine ADVERTISES the
  operations it supports (`EngineCapabilityV1`), and an authorable formula an engine cannot run is
  ``unsupported_engine`` — the MATERIALIZATION_BLOCKED readiness input, distinct from a grammar
  gap. Authorable has never meant materializable; this is where the difference becomes a value.

Total on well-formed proposals: verdicts, never exceptions — a capability gap is a designed
answer, not an error.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from featuregen.formula.schema_v2 import (
    AggregateFunctionV2,
    TypedFormulaProposalV2,
    body_expressions_v2,
)

CapabilityVerdictV2 = Literal["ok", "unsupported_capability", "unsupported_engine"]


@dataclass(frozen=True, slots=True)
class EngineCapabilityV1:
    """One execution engine's advertised support: which v2 aggregates it can materialize. The
    engine declares; the classifier compares — nothing is assumed from an engine's name."""

    engine_id: str
    supported_aggregations: frozenset[str]
    # increment 4: window offsets (lag/delta shapes) are an ENGINE capability too — shifting a
    # window back k periods needs engine support just like an aggregate does. Default False:
    # an engine advertises it or offset formulas are unsupported_engine on it.
    supports_window_offset: bool = False


def classify_formula_capability_v2(
    proposal: TypedFormulaProposalV2,
    engine: EngineCapabilityV1 | None = None,
) -> CapabilityVerdictV2:
    """Classify a structurally valid v2 proposal. Grammar first (single source, known
    aggregates); then, when an engine is given, its advertisement."""
    sources: set[str] = set()
    used: set[str] = set()
    for expr in body_expressions_v2(proposal.body):
        sources.add(str(expr.source_relation.table_ref).split("::", 1)[0])
        used.add(expr.aggregation.value)
        if expr.operand is not None:
            sources.add(str(expr.operand).split("::", 1)[0])
    if len(sources) > 1:
        return "unsupported_capability"
    if not used <= {member.value for member in AggregateFunctionV2}:
        return "unsupported_capability"
    if engine is not None:
        if not used <= engine.supported_aggregations:
            return "unsupported_engine"
        uses_offset = any(expr.window.offset_periods > 0
                          for expr in body_expressions_v2(proposal.body))
        if uses_offset and not engine.supports_window_offset:
            return "unsupported_engine"
    return "ok"
