"""C-C10 — subgraph requirements over C-C10a's closed vocabulary.

**Requirements are TOPOLOGY-DERIVED, never asserted alongside the plan.** A requirement that a
caller had to remember to apply is a requirement that gets forgotten on the one feature it mattered
for. Each requirement here names the operator whose PRESENCE triggers it: a graph containing an
as-of FX join must contain the duplicate-rate gate, the missing-rate gate and the multiplication,
because it joined rates — nobody has to declare that it did.

**Position matters as much as presence.** A duplicate-rate gate sitting on a disconnected branch
satisfies "contains a duplicate-rate gate" and protects nothing. Every required operator must
therefore be DOWNSTREAM of the trigger (so it sees the joined rows) and UPSTREAM of the terminal (so
its refusal can stop the feature). That pair of conditions is the "connected path" the plan asks
for, stated in terms a graph can answer.

**The base-currency bypass is an ABSENCE, not a node.** A fixed-base-currency feature contains no
as-of FX join, so the FX requirement never triggers. That is why the bypass is not a fourteenth
operator: adding one would create a node whose only job is to say a subgraph is missing, and then
the requirement would have to check for the node instead of the thing.

**Linked reversal is not four nodes.** The vocabulary has ONE reversal operator, so its four
required facts — as-of population, linkage, ambiguity gate, survivor — live in
:class:`~featuregen.materialize.operator_graph_v2.LinkedReversalSurvivorV2`'s payload and are
enforced by its own construction. Writing this module is what found that the payload was missing two
of them.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.materialize.operator_graph_v2 import (
    OperatorGraphV2,
    OperatorKindV2,
    OperatorNodeV2,
)

__all__ = [
    "FX_CONVERSION",
    "LINKED_REVERSAL",
    "PILOT_REQUIREMENTS",
    "RequirementFindingV2",
    "RequirementVerdictV2",
    "SubgraphRequirementV2",
    "check_subgraph_requirements_v2",
]


@dataclass(frozen=True, slots=True)
class SubgraphRequirementV2:
    """One named requirement: what the presence of ``triggered_by`` obliges a graph to contain."""

    name: str
    triggered_by: OperatorKindV2
    required: tuple[OperatorKindV2, ...]
    optional: tuple[OperatorKindV2, ...] = ()
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.triggered_by in self.required:
            raise ValueError(
                f"{self.name} lists its own trigger {self.triggered_by} as required: the trigger is "
                f"why the requirement applies, so requiring it is a tautology that always passes")
        overlap = set(self.required) & set(self.optional)
        if overlap:
            raise ValueError(
                f"{self.name} lists {sorted(o.value for o in overlap)} as both required and "
                f"optional: an operator is one or the other, and a reader cannot tell which rule "
                f"the graph was judged under")


#: An as-of FX join converted currency, so the two gates and the multiplication must be there.
#: ``QUOTE_INVERSION`` is OPTIONAL because whether a quote needs inverting is a property of how the
#: rate table publishes, not of the conversion.
FX_CONVERSION = SubgraphRequirementV2(
    name="FX_CONVERSION",
    triggered_by=OperatorKindV2.AS_OF_FX_JOIN,
    required=(OperatorKindV2.DUPLICATE_RATE_GATE, OperatorKindV2.MISSING_RATE_GATE,
              OperatorKindV2.DECIMAL_MULTIPLICATION),
    optional=(OperatorKindV2.QUOTE_INVERSION,),
    rationale=(
        "An as-of join amplifies silently when the rate side offers two rows for a key at an "
        "instant, and drops rows silently when it offers none. Both are wrong numbers rather than "
        "errors, so both gates are required wherever a rate was joined."),
)

#: The reversal requirement is about POSITION and payload completeness, not about sibling nodes —
#: the vocabulary has one reversal operator and its payload carries the other three facts.
LINKED_REVERSAL = SubgraphRequirementV2(
    name="LINKED_REVERSAL",
    triggered_by=OperatorKindV2.LINKED_REVERSAL_SURVIVOR,
    required=(),
    rationale=(
        "As-of population, linkage, ambiguity gate and survivor are payload facts of the one "
        "reversal operator, enforced at its construction. What this requirement adds is position: "
        "a survivor filter downstream of the aggregate would filter the ALREADY-SUMMED rows."),
)

PILOT_REQUIREMENTS: tuple[SubgraphRequirementV2, ...] = (FX_CONVERSION, LINKED_REVERSAL)


@dataclass(frozen=True, slots=True)
class RequirementFindingV2:
    """One way a graph failed a requirement — named, so a refusal points somewhere."""

    requirement: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class RequirementVerdictV2:
    satisfied: bool
    triggered: tuple[str, ...]
    findings: tuple[RequirementFindingV2, ...]

    def __post_init__(self) -> None:
        if self.satisfied and self.findings:
            raise ValueError(
                "a satisfied verdict cannot carry findings: the two fields would disagree, and a "
                "caller reading only `satisfied` would render a graph the check refused")
        if not self.satisfied and not self.findings:
            raise ValueError("a refusal with no findings tells an author nothing to fix")


MISSING_OPERATOR = "MISSING_OPERATOR"
DISCONNECTED_OPERATOR = "DISCONNECTED_OPERATOR"
TRIGGER_NOT_UPSTREAM = "TRIGGER_NOT_UPSTREAM"


def _by_id(graph: OperatorGraphV2) -> dict[str, OperatorNodeV2]:
    return {node.node_id: node for node in graph.nodes}


def _ancestors(graph: OperatorGraphV2, node_id: str) -> set[str]:
    """Every node ``node_id`` transitively READS (following ``inputs`` upstream), including itself."""
    nodes = _by_id(graph)
    seen: set[str] = set()
    stack = [node_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(nodes[current].inputs)
    return seen


def check_subgraph_requirements_v2(
    graph: OperatorGraphV2,
    requirements: tuple[SubgraphRequirementV2, ...] = PILOT_REQUIREMENTS,
) -> RequirementVerdictV2:
    """Check ``graph`` against every requirement its own topology triggers.

    A requirement whose trigger is absent is not checked and is not reported as passing — that is
    the base-currency bypass, and calling it a pass would suggest an FX subgraph was inspected and
    found sound when there was none.
    """
    nodes_by_kind: dict[OperatorKindV2, list[OperatorNodeV2]] = {}
    for node in graph.nodes:
        nodes_by_kind.setdefault(node.kind, []).append(node)

    terminal_ancestors = _ancestors(graph, graph.terminal.node_id)
    findings: list[RequirementFindingV2] = []
    triggered: list[str] = []

    for requirement in requirements:
        trigger_nodes = nodes_by_kind.get(requirement.triggered_by, [])
        if not trigger_nodes:
            continue                       # not triggered — the bypass, and NOT a pass
        triggered.append(requirement.name)

        for trigger in trigger_nodes:
            if trigger.node_id not in terminal_ancestors:
                findings.append(RequirementFindingV2(
                    requirement=requirement.name, code=TRIGGER_NOT_UPSTREAM,
                    detail=(f"{trigger.node_id} is not upstream of the terminal, so whatever it "
                            f"computes never reaches the published column — a subgraph nothing "
                            f"consumes cannot be judged sound or unsound")))
                continue

            downstream_of_trigger = {
                node.node_id for node in graph.nodes
                if trigger.node_id in _ancestors(graph, node.node_id)}

            for kind in requirement.required:
                present = nodes_by_kind.get(kind, [])
                if not present:
                    findings.append(RequirementFindingV2(
                        requirement=requirement.name, code=MISSING_OPERATOR,
                        detail=(f"the graph contains {requirement.triggered_by.value} but no "
                                f"{kind.value}. {requirement.rationale}")))
                    continue
                positioned = [
                    node for node in present
                    if node.node_id in downstream_of_trigger
                    and node.node_id in terminal_ancestors]
                if not positioned:
                    findings.append(RequirementFindingV2(
                        requirement=requirement.name, code=DISCONNECTED_OPERATOR,
                        detail=(f"the graph contains {kind.value}, but not on the path from "
                                f"{trigger.node_id} to the terminal: it does not see the rows "
                                f"{requirement.triggered_by.value} produced, or its refusal cannot "
                                f"stop the feature, so it protects nothing")))

    ordered = tuple(sorted(findings, key=lambda f: (f.requirement, f.code, f.detail)))
    return RequirementVerdictV2(
        satisfied=not ordered, triggered=tuple(sorted(triggered)), findings=ordered)
