"""C-C10a — THE CLOSED PILOT OPERATOR GRAPH.

**Why this exists.** The V1 IR has no operator vocabulary at all: it carries expressions, a spine and
an output policy, and the *shape of the computation* lives in the renderer's node templates. That is
workable while every feature is one scan-filter-aggregate, and it stops being workable the moment a
feature needs an as-of FX join whose duplicate-rate and missing-rate gates are the difference
between a correct number and a quietly wrong one. Until the vocabulary is closed and each node's
identity is pinned, no topology question is decidable — you cannot ask "does this plan contain a
missing-rate gate" of a thing that has no nodes.

**Closed means closed.** :class:`OperatorKindV2` has exactly thirteen members and there is one
payload type per member, matched by :class:`OperatorNodeV2` at construction. A fourteenth operator
is a deliberate amendment to this module, not something a caller can introduce by passing a
different payload — which is the whole point of freezing a vocabulary that later subgraph
requirements (C-C10) are written against.

**What each payload is grounded in.** Nothing here invents a field it could borrow. The PIT node
carries :class:`~featuregen.materialize.expression_ir.PitSpec` verbatim; the policy nodes carry the
refs :class:`~featuregen.formula.schema_v2.AuthorityRefsV2` already declares; the multiplication
node carries :class:`~featuregen.formula.schema.DecimalPolicy`; the selection node carries C-A3b's
:class:`~featuregen.formula.schema_v3.SemanticRowSelectionV1`. A payload field with no existing
source would be a fact nobody governs.

**Node ids are CONTENT-DERIVED, not counted.** A counter makes a graph's identity depend on the
order its nodes happened to be appended, which is the defect Task 6 found one level down (ordering
by the sequence in which tables happened to RESOLVE silently changed the hash). Two structurally
identical nodes with identical inputs ARE one node here, and that is correct: they compute the same
thing from the same thing.

**Semantic values stay SEMANTIC.** A selection node says ``"debit"``, never the pilot ledger's
``"D"``. Resolving it is C-C8's policy realization, and a graph that resolved it here would bake one
ledger's encoding into every feature's identity.

**Not built here, deliberately.** ``realizes_occurrences`` (C-C8) is absent rather than empty — an
empty field would say a node realizes no policy, when the truth is that policy realization does not
exist. The base-currency identity-rate BYPASS is deliberately not a fourteenth kind: it is the
*absence* of the FX nodes, which is what C-C10 checks for.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.formula.schema_leaves import DecimalPolicy
from featuregen.formula.schema_v2 import AggregateFunctionV2, FinalOperationV2
from featuregen.formula.schema_v3 import SemanticRowSelectionV1
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.expression_ir import PitSpec

__all__ = [
    "AggregateV2",
    "AsOfFxJoinV2",
    "DecimalMultiplicationV2",
    "DuplicateRateGateV2",
    "EligibleStatusFilterV2",
    "GovernedScanV2",
    "GroupAssemblyV2",
    "LinkedReversalSurvivorV2",
    "MissingRateGateV2",
    "OperatorGraphV2",
    "OperatorKindV2",
    "OperatorNodeV2",
    "PitAvailabilityFilterV2",
    "QuoteInversionV2",
    "SemanticSelectionV2",
    "SpineLeftJoinV2",
    "graph_hash_v2",
]


class OperatorKindV2(StrEnum):
    """The CLOSED vocabulary — fourteen operators.

    Ordered as the pilot reads: scan, restrict to the point-in-time window, select the rows the
    recipe declared, apply the governed status and reversal policies, convert currency, do the
    arithmetic, aggregate, land the result on the declared population, and COMBINE the aggregates
    into the feature's final value.

    ``FINAL_COMBINE`` was added last, and its absence was a real gap rather than an oversight: with
    thirteen kinds the graph could describe every step of a feature EXCEPT the one that produces its
    value. A ratio's two aggregates had nowhere to be divided, so the graph could not claim to be
    the executable form of any feature at all — not merely of exotic ones. Recording capability per
    final operation also needs it: `ratio` and `signed_sum` are different renderer abilities, and
    without a kind to hang them on there was nowhere to say which one an engine has.
    """

    GOVERNED_SCAN = "governed_scan"
    PIT_AVAILABILITY_FILTER = "pit_availability_filter"
    SEMANTIC_SELECTION = "semantic_selection"
    ELIGIBLE_STATUS_FILTER = "eligible_status_filter"
    LINKED_REVERSAL_SURVIVOR = "linked_reversal_survivor"
    AS_OF_FX_JOIN = "as_of_fx_join"
    DUPLICATE_RATE_GATE = "duplicate_rate_gate"
    MISSING_RATE_GATE = "missing_rate_gate"
    QUOTE_INVERSION = "quote_inversion"
    DECIMAL_MULTIPLICATION = "decimal_multiplication"
    AGGREGATE = "aggregate"
    SPINE_LEFT_JOIN = "spine_left_join"
    GROUP_ASSEMBLY = "group_assembly"
    FINAL_COMBINE = "final_combine"


def _refs(values: tuple[str, ...], what: str) -> tuple[str, ...]:
    """Non-empty, non-blank, de-duplicated-check helper for ordered ref tuples."""
    if not values:
        raise ValueError(f"{what} is empty: a node that names no ref reads nothing")
    for value in values:
        if not value.strip():
            raise ValueError(f"{what} carries a blank ref: {values!r}")
    if len(set(values)) != len(values):
        raise ValueError(
            f"{what} names the same ref twice ({values!r}): order is identity-bearing here, so a "
            f"duplicate makes two different tuples mean one read set")
    return values


def _ref(value: str, what: str) -> str:
    if not value.strip():
        raise ValueError(
            f"{what} is blank: a governed reference that names nothing cannot be resolved against "
            f"any store, and carrying it would let a node claim a policy it does not have")
    return value


# ── the thirteen payloads ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GovernedScanV2:
    """Read a governed relation. ``column_refs`` are the FULL governed logical refs, in read order.

    Refs, never bare column names: a bare name is ambiguous across catalogs, and §1.3's read set is
    expressed in logical refs so that the gate and the scan cannot disagree about what was read.
    """

    table_ref: str
    column_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _ref(self.table_ref, "table_ref")
        _refs(self.column_refs, "column_refs")

    def identity_payload(self) -> dict[str, Any]:
        return {"table_ref": self.table_ref, "column_refs": list(self.column_refs)}


@dataclass(frozen=True, slots=True)
class PitAvailabilityFilterV2:
    """Admit only rows the cutoff could have seen — :class:`PitSpec` carried VERBATIM.

    Re-declaring the window here would create a second answer to "what is this expression's clock",
    and §8 rule 1 already assigns that answer to ``PitSpec``.
    """

    pit: PitSpec

    def identity_payload(self) -> dict[str, Any]:
        return {"pit": self.pit.identity_payload()}


@dataclass(frozen=True, slots=True)
class SemanticSelectionV2:
    """C-A3b's declared row selection — SEMANTIC (``"debit"``), never a ledger encoding."""

    selection: SemanticRowSelectionV1

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": str(self.selection.kind), "role": self.selection.role,
                "semantic_value": self.selection.semantic_value}


@dataclass(frozen=True, slots=True)
class EligibleStatusFilterV2:
    """Keep only rows the governed status policy calls eligible."""

    status_policy_ref: str

    def __post_init__(self) -> None:
        _ref(self.status_policy_ref, "status_policy_ref")

    def identity_payload(self) -> dict[str, Any]:
        return {"status_policy_ref": self.status_policy_ref}


@dataclass(frozen=True, slots=True)
class LinkedReversalSurvivorV2:
    """Keep the SURVIVOR of a linked reversal pair, under the governed reversal policy.

    C-C10 requires four facts of a linked-reversal subgraph — as-of population, linkage, ambiguity
    gate, survivor — and unlike FX they are not four nodes: the vocabulary has ONE reversal
    operator, so they are payload facts and this type must carry all of them or the requirement is
    unexpressible. (It did not, until C-C10 asked. Writing the requirement is what found the gap.)

    ``ambiguity_refusal_code`` is the reversal twin of the duplicate-rate gate. When a reversal
    links to more than one candidate the answer is a refusal, never a pick: choosing a survivor
    among ambiguous links silently changes a balance, and does it consistently enough to look right.
    """

    reversal_policy_ref: str
    as_of_population_ref: str
    linkage_key_refs: tuple[str, ...]
    ambiguity_refusal_code: str = "REVERSAL_LINK_AMBIGUOUS"

    def __post_init__(self) -> None:
        _ref(self.reversal_policy_ref, "reversal_policy_ref")
        _ref(self.as_of_population_ref, "as_of_population_ref")
        _refs(self.linkage_key_refs, "linkage_key_refs")
        _ref(self.ambiguity_refusal_code, "ambiguity_refusal_code")

    def identity_payload(self) -> dict[str, Any]:
        return {"reversal_policy_ref": self.reversal_policy_ref,
                "as_of_population_ref": self.as_of_population_ref,
                "linkage_key_refs": list(self.linkage_key_refs),
                "ambiguity_refusal_code": self.ambiguity_refusal_code}


@dataclass(frozen=True, slots=True)
class AsOfFxJoinV2:
    """Join the rate that was in force AS OF each row's event time.

    ``as_of_ref`` is the rate side's effective-time column. An FX join that matched on anything else
    would apply today's rate to a year-old row — the failure the load-bearing mid-window rate test
    (3.65 → 3.70 ⇒ 73.50) exists to catch.
    """

    currency_conversion_ref: str
    rate_table_ref: str
    as_of_ref: str
    rate_column_ref: str

    def __post_init__(self) -> None:
        _ref(self.currency_conversion_ref, "currency_conversion_ref")
        _ref(self.rate_table_ref, "rate_table_ref")
        _ref(self.as_of_ref, "as_of_ref")
        _ref(self.rate_column_ref, "rate_column_ref")

    def identity_payload(self) -> dict[str, Any]:
        return {"currency_conversion_ref": self.currency_conversion_ref,
                "rate_table_ref": self.rate_table_ref, "as_of_ref": self.as_of_ref,
                "rate_column_ref": self.rate_column_ref}


@dataclass(frozen=True, slots=True)
class DuplicateRateGateV2:
    """Refuse when the rate side offers MORE THAN ONE row for a key at an instant.

    Without this an as-of join silently amplifies: one transaction becomes two, and the sum is
    quietly wrong rather than loudly refused.
    """

    rate_key_refs: tuple[str, ...]
    refusal_code: str = "FX_RATE_AMBIGUOUS"

    def __post_init__(self) -> None:
        _refs(self.rate_key_refs, "rate_key_refs")
        _ref(self.refusal_code, "refusal_code")

    def identity_payload(self) -> dict[str, Any]:
        return {"rate_key_refs": list(self.rate_key_refs), "refusal_code": self.refusal_code}


@dataclass(frozen=True, slots=True)
class MissingRateGateV2:
    """Refuse when a row that NEEDS conversion finds no rate.

    A left join here would emit NULL and a sum would skip the row — which is exactly the silent
    omission D3 forbids: a mixed-currency population must refuse, never quietly drop its USD rows.
    """

    refusal_code: str = "FX_RATE_MISSING"

    def __post_init__(self) -> None:
        _ref(self.refusal_code, "refusal_code")

    def identity_payload(self) -> dict[str, Any]:
        return {"refusal_code": self.refusal_code}


@dataclass(frozen=True, slots=True)
class QuoteInversionV2:
    """Invert a quote when the rate table publishes the opposite direction."""

    from_currency: str
    to_currency: str

    def __post_init__(self) -> None:
        _ref(self.from_currency, "from_currency")
        _ref(self.to_currency, "to_currency")
        if self.from_currency == self.to_currency:
            raise ValueError(
                f"a quote inversion from {self.from_currency} to itself is not an inversion: the "
                f"identity rate is the base-currency BYPASS, which is the absence of these nodes")

    def identity_payload(self) -> dict[str, Any]:
        return {"from_currency": self.from_currency, "to_currency": self.to_currency}


@dataclass(frozen=True, slots=True)
class DecimalMultiplicationV2:
    """``amount × rate`` under a declared decimal policy — :class:`DecimalPolicy` carried verbatim.

    The rounding SITE is a node, not a formatting detail: rounding per row and rounding after the
    sum give different numbers, and which one happened has to be a fact the graph states.
    """

    decimal: DecimalPolicy
    round_per_row: bool

    def identity_payload(self) -> dict[str, Any]:
        return {
            "decimal": {
                "precision": self.decimal.precision, "scale": self.decimal.scale,
                "rounding": str(self.decimal.rounding), "overflow": str(self.decimal.overflow)},
            "round_per_row": self.round_per_row,
        }


@dataclass(frozen=True, slots=True)
class AggregateV2:
    """Collapse the surviving rows to one value per grain key."""

    function: AggregateFunctionV2
    operand_ref: str | None
    grain_key_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _refs(self.grain_key_refs, "grain_key_refs")
        if self.function is AggregateFunctionV2.COUNT_ROWS:
            if self.operand_ref is not None:
                raise ValueError(
                    "count_rows takes no operand: carrying one would suggest the count depended on "
                    "a column, and a reader could not tell which answer the engine produced")
        elif self.operand_ref is None or not self.operand_ref.strip():
            raise ValueError(f"{self.function} needs an operand ref")

    def identity_payload(self) -> dict[str, Any]:
        return {"function": str(self.function), "operand_ref": self.operand_ref,
                "grain_key_refs": list(self.grain_key_refs)}


@dataclass(frozen=True, slots=True)
class SpineLeftJoinV2:
    """Land the aggregate on the DECLARED population (D4).

    Left join FROM the spine, so a key with no in-window rows still produces a row. ``empty_value``
    is what that row carries — the pilot coalesces to ``"0"`` so ``ACC_ZERO`` reports 0.00 rather
    than vanishing. A transaction belonging to an unknown account cannot invent a population row,
    because the population is the LEFT side and nothing here adds to it.
    """

    spine_table_ref: str
    key_refs: tuple[str, ...]
    empty_value: str | None

    def __post_init__(self) -> None:
        _ref(self.spine_table_ref, "spine_table_ref")
        _refs(self.key_refs, "key_refs")

    def identity_payload(self) -> dict[str, Any]:
        return {"spine_table_ref": self.spine_table_ref, "key_refs": list(self.key_refs),
                "empty_value": self.empty_value}


@dataclass(frozen=True, slots=True)
class GroupAssemblyV2:
    """Assemble the group's published columns, in published order."""

    column_names: tuple[str, ...]

    def __post_init__(self) -> None:
        _refs(self.column_names, "column_names")

    def identity_payload(self) -> dict[str, Any]:
        return {"column_names": list(self.column_names)}


@dataclass(frozen=True, slots=True)
class FinalCombineV2:
    """Combine the body's aggregates into the feature's ONE value.

    The last step of every feature, and the one the vocabulary previously could not express. An
    IDENTITY takes a single term; a RATIO divides two under a declared zero-denominator policy; a
    DIFFERENCE subtracts; a SIGNED_SUM adds N terms under their declared signs.

    ``term_paths`` is ordered because the operations are not commutative — numerator before
    denominator, minuend before subtrahend. A set here would render `a/b` and `b/a` identically.
    """

    final_operation: FinalOperationV2
    term_paths: tuple[str, ...]
    zero_denominator: str | None = None

    def __post_init__(self) -> None:
        if not self.term_paths:
            raise ValueError(
                "a final combination with no terms produces no value: the node names the step that "
                "makes the feature, and one with nothing to combine is not that step")
        arity = {FinalOperationV2.IDENTITY: 1, FinalOperationV2.RATIO: 2,
                 FinalOperationV2.DIFFERENCE: 2}.get(self.final_operation)
        if arity is not None and len(self.term_paths) != arity:
            raise ValueError(
                f"{self.final_operation} combines exactly {arity} term(s), not "
                f"{len(self.term_paths)}: an arity the operation does not have would render as a "
                f"different calculation from the one the formula declared")
        if self.final_operation is FinalOperationV2.SIGNED_SUM and len(self.term_paths) < 2:
            raise ValueError(
                "a signed sum of one term is that term: recording it as a sum would claim a "
                "combination the formula did not ask for")
        # The zero-denominator policy belongs to division and to nothing else. Carrying one on an
        # identity would suggest a divide-by-zero decision was made where no division happens.
        if self.final_operation is FinalOperationV2.RATIO:
            if not (self.zero_denominator or "").strip():
                raise ValueError(
                    "a ratio must declare its zero-denominator policy: what a feature does when the "
                    "denominator is zero is a governed decision, and defaulting it silently picks "
                    "an answer nobody reviewed")
        elif self.zero_denominator is not None:
            raise ValueError(
                f"{self.final_operation} carries a zero-denominator policy but performs no "
                f"division")

    def identity_payload(self) -> dict[str, Any]:
        return {"final_operation": str(self.final_operation),
                "term_paths": list(self.term_paths),
                "zero_denominator": self.zero_denominator}


#: The ONE mapping from kind to payload type. ``OperatorNodeV2`` checks against it, so the
#: vocabulary cannot be widened by passing a payload the enum never promised.
_PAYLOAD_TYPE: dict[OperatorKindV2, type] = {
    OperatorKindV2.GOVERNED_SCAN: GovernedScanV2,
    OperatorKindV2.PIT_AVAILABILITY_FILTER: PitAvailabilityFilterV2,
    OperatorKindV2.SEMANTIC_SELECTION: SemanticSelectionV2,
    OperatorKindV2.ELIGIBLE_STATUS_FILTER: EligibleStatusFilterV2,
    OperatorKindV2.LINKED_REVERSAL_SURVIVOR: LinkedReversalSurvivorV2,
    OperatorKindV2.AS_OF_FX_JOIN: AsOfFxJoinV2,
    OperatorKindV2.DUPLICATE_RATE_GATE: DuplicateRateGateV2,
    OperatorKindV2.MISSING_RATE_GATE: MissingRateGateV2,
    OperatorKindV2.QUOTE_INVERSION: QuoteInversionV2,
    OperatorKindV2.DECIMAL_MULTIPLICATION: DecimalMultiplicationV2,
    OperatorKindV2.AGGREGATE: AggregateV2,
    OperatorKindV2.SPINE_LEFT_JOIN: SpineLeftJoinV2,
    OperatorKindV2.GROUP_ASSEMBLY: GroupAssemblyV2,
    OperatorKindV2.FINAL_COMBINE: FinalCombineV2,
}


# ── nodes and the graph ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OperatorNodeV2:
    """One operator: a kind, its typed payload, and its ORDERED inputs.

    ``node_id`` is derived from the kind, the payload's identity and the input ids, so it is stable
    across runs and independent of the order nodes were built in. Inputs are ordered because for a
    join the sides are not interchangeable, and an unordered input set would give a left join and a
    right join the same identity.
    """

    kind: OperatorKindV2
    payload: Any
    inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        expected = _PAYLOAD_TYPE.get(self.kind)
        if expected is None:
            raise ValueError(
                f"{self.kind!r} is not one of this vocabulary's {len(_PAYLOAD_TYPE)} operators: "
                f"the set is CLOSED, and adding one is an amendment to this module rather than "
                f"something a caller introduces")
        if not isinstance(self.payload, expected):
            raise TypeError(
                f"{self.kind} takes a {expected.__name__} payload, got "
                f"{type(self.payload).__name__}: the kind and the payload are one decision, and a "
                f"node whose payload disagreed with its kind would render as neither")
        for input_id in self.inputs:
            if not input_id.strip():
                raise ValueError(f"{self.kind} carries a blank input id")

    @property
    def node_id(self) -> str:
        """Content-derived and STABLE — never a counter (see the module docstring)."""
        digest = materialize_hash({
            "kind": self.kind.value,
            "payload": self.payload.identity_payload(),
            "inputs": list(self.inputs),
        })
        return f"{self.kind.value}:{digest[:16]}"

    def identity_payload(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "payload": self.payload.identity_payload(),
            "inputs": list(self.inputs),
        }


@dataclass(frozen=True, slots=True)
class OperatorGraphV2:
    """One feature's operator DAG, closed over C-C10a's vocabulary.

    Validated at construction rather than checked by whoever renders it: every input must name a
    node in this graph, the graph must be acyclic, and exactly one node may be terminal. A graph
    with two terminals computes two things and publishes one of them, which is the kind of defect
    that shows up as a missing column rather than an error.
    """

    nodes: tuple[OperatorNodeV2, ...]

    def __post_init__(self) -> None:
        if not self.nodes:
            raise ValueError("an operator graph with no nodes computes nothing")
        by_id: dict[str, OperatorNodeV2] = {}
        for node in self.nodes:
            if node.node_id in by_id:
                raise ValueError(
                    f"two nodes share the id {node.node_id!r}: ids are CONTENT-derived, so this "
                    f"means the same operator was added twice — one node, not two")
            by_id[node.node_id] = node

        consumed: set[str] = set()
        for node in self.nodes:
            for input_id in node.inputs:
                if input_id not in by_id:
                    raise ValueError(
                        f"{node.node_id} reads {input_id!r}, which is not a node in this graph: an "
                        f"input naming nothing is a dangling edge, and a renderer would emit a "
                        f"pipeline that cannot wire")
                consumed.add(input_id)

        self._check_acyclic(by_id)

        terminals = [node_id for node_id in by_id if node_id not in consumed]
        if len(terminals) != 1:
            raise ValueError(
                f"the graph has {len(terminals)} terminal nodes ({sorted(terminals)}): a feature "
                f"computes ONE thing, and a second terminal is a branch nobody publishes")

    @staticmethod
    def _check_acyclic(by_id: dict[str, OperatorNodeV2]) -> None:
        """Depth-first, with an explicit stack — a cycle is refused by NAME, not by recursion depth.

        **Honest status: today this is defence in depth, not a reachable refusal.** Because
        ``node_id`` is DERIVED from the inputs, building a cycle would require a node whose id
        depends on an id that depends on its own — a fixed point of SHA-256, which nobody is
        constructing by accident. The guard is kept because the property it protects is load-bearing
        and the thing making it free is one refactor away: the moment ``node_id`` becomes an
        assignable field, or ids arrive from a persisted graph rather than being recomputed, cycles
        become constructible and this is what catches them. It is cheap and it does not lie about
        what it does.
        """
        WHITE, GREY, BLACK = 0, 1, 2
        colour = dict.fromkeys(by_id, WHITE)
        for start in by_id:
            if colour[start] != WHITE:
                continue
            stack: list[tuple[str, bool]] = [(start, False)]
            while stack:
                node_id, leaving = stack.pop()
                if leaving:
                    colour[node_id] = BLACK
                    continue
                if colour[node_id] == GREY:
                    continue
                colour[node_id] = GREY
                stack.append((node_id, True))
                for input_id in by_id[node_id].inputs:
                    if colour[input_id] == GREY:
                        raise ValueError(
                            f"the graph contains a cycle through {input_id!r}: an operator that "
                            f"reads its own output has no evaluation order, and a renderer would "
                            f"either loop or silently pick one")
                    if colour[input_id] == WHITE:
                        stack.append((input_id, False))

    @property
    def terminal(self) -> OperatorNodeV2:
        """The one node nothing else reads — what this feature computes."""
        consumed = {input_id for node in self.nodes for input_id in node.inputs}
        return next(node for node in self.nodes if node.node_id not in consumed)

    @property
    def kinds(self) -> frozenset[OperatorKindV2]:
        """Which operators this graph contains — what C-C10's subgraph requirements ask about."""
        return frozenset(node.kind for node in self.nodes)

    def identity_payload(self) -> dict[str, Any]:
        """Nodes enter ordered BY NODE ID, never by tuple position.

        The tuple's order is how a builder happened to append; the graph is defined by its edges,
        and two builders producing the same edges must produce the same hash.
        """
        return {
            "operator_vocabulary_version": 1,
            "nodes": [node.identity_payload()
                      for node in sorted(self.nodes, key=lambda n: n.node_id)],
        }


def graph_hash_v2(graph: OperatorGraphV2) -> str:
    """The graph's content identity — ``materialize_hash`` is the one hasher (§14)."""
    return materialize_hash(graph.identity_payload())
