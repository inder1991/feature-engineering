"""Spec §2.1 / §3 / §3.3 / §3.5 — the compiled plan for ONE `AggregateExpression`.

**Per EXPRESSION, never per formula.** Child-1 gives every ``AggregateExpression`` its own
``source_relation``, ``filter`` AND ``window`` (verified interfaces §6), so a legal ratio may read
two tables, on two event-time columns, over two windows, under two availability bases. An IR holding
one PIT spec per *formula* cannot represent that grammar: it would apply one half's window to the
other and return a number rather than an error. So the unit of compilation here is the expression,
and :class:`ExpressionExecutionIR` is complete on its own.

**Static.** ``business_dt`` is unreachable from this module. The IR carries Task 5's
:class:`PhysicalInputRequirement` — partition COLUMNS and the DECLARED mapping — and never a
resolved partition set. Run preparation (§3.3, Task 15) turns a requirement plus a business date
into a ``PhysicalInputSnapshot``; if that arrived here instead, the generated project would change
every business date, which is exactly what a hash-identified artifact must not do.

**Reference completeness replaces a stage that does not exist (§2.1).** Plan rev 3 named a
``FormulaPlannerIntentV1``; it is not in the repository, and inventing one would be a new abstraction
standing in for a guarantee. The guarantee is kept directly instead: :func:`logical_refs_in` walks
the expression EXHAUSTIVELY, and every ref-shaped string it finds must either be consumed into the
physical read set or be explicitly classified NON-PHYSICAL with a stated reason. Anything else is
``UNACCOUNTED_LOGICAL_REF`` — including, one day, a new field on ``AggregateExpression`` this
compiler was never taught to read. The discriminating case is already in the grammar: a
``FilterPredicate`` has **no ``right_ref``** (§6), so its only column reference is ``left`` and a
comparison literal that merely LOOKS like a ref is a value, not a column.

**It owns no traversal and no resolution.** ``joins.plan_join`` is the one route to the governed
join planner and already refuses fan-out, unknown cardinality, unverified and denied paths; those
verdicts are PROPAGATED here, never softened or repaired. ``inputs.resolve_physical_identity`` is
the one route from a schema-flattened logical ref to a table on the cluster (§3.5). Asking either
question a second way here would be a second chance to answer it differently.

**Operand type evidence: read here because only here is it per-EXPRESSION (§6, Task 8.1).**
Child-1 collapses a whole formula to ONE logical word — a SUM's own operand, a DIFFERENCE's
*minuend*, and for a RATIO the constant ``"decimal"``, which describes no operand at all — so a
ratio's numerator and denominator, and a difference's subtrahend, are invisible to any stage holding
only ``FormulaOutputPolicyV1``. §6 requires EVERY arithmetic operand to be an exact numeric before a
``DECIMAL(p,s)`` may be published, and that question cannot be asked of a word that never described
the operand being asked about. So each compiled expression carries its own
:class:`OperandTypeEvidence`: the C1 ``logical_representation`` of ITS operand, read through the same
shipped reader the availability authority uses.

This module RECORDS; it does not judge. Nothing here refuses on a type — ``physical_types`` owns
§6's rule, and splitting "what the catalog says" from "what may therefore be published" keeps one
answer to each. The evidence is deliberately outside :meth:`ExpressionExecutionIR.identity_payload`
(see that method).

**Availability: authority from the projection, payload through its link.** The governed
``AVAILABILITY_TIME`` fact says which column carries knowledge time AND on what basis (``posted_at``
/ ``ingested_at`` / ``event_time_plus_lag`` + ``lag_hours``), and §8 rule 1 needs all of it to render
the point-in-time gate. But ``table_fact_projection`` projects only the fact's ``column`` — as
``is_as_of`` plus the ``availability_fact_event_id`` link — so ``basis`` and ``lag_hours`` are
unreachable from ``graph_node`` (the same shape of gap interfaces §18.3 records for ``GRAIN``'s
``is_unique``). Authority is therefore still taken from the shipped governed reader
(``read_operational_value(..., "is_as_of")``), and the payload is read back THROUGH the link the
projection wrote: the ``overlay_fact_state`` row must be VERIFIED, its ``confirmed_event_id`` must
equal the id on the column, its ``column`` must be the column that was flagged, and its value must
clear the overlay's own ``validate_fact_value`` schema. That is dereferencing a link governance
already blessed, not a second opinion about authority — ``resolve_fact`` is deliberately not called,
because it needs a registered ``CatalogAdapter`` and would re-decide authority here.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any

from featuregen.contracts.db import DbConn
from featuregen.formula.canonical import filter_plain
from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    FilterNode,
    FilterPredicate,
    ParameterRef,
    TypedLiteral,
)
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.inputs import PhysicalInputRequirement, derive_requirement
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.materialize.joins import (
    CrossCatalogJoinStepV1,
    CrosswalkJoinStepV1,
    JoinPlan,
    PhysicalIdentity,
    plan_cross_catalog_join,
    plan_crosswalk_join,
    plan_join,
)
from featuregen.overlay.facts import AVAILABILITY_TIME, FactValidationError, validate_fact_value
from featuregen.overlay.identity import fact_key as _fact_key_of
from featuregen.overlay.upload.bridge_realization import (
    AdditionalKeyRequirementV1,
    AsOfIntervalRequirementV1,
    ExecutionTier,
    FixedValueReferencePredicateV1,
)
from featuregen.overlay.upload.bridge_store import CurrentBridgeRealizationV1
from featuregen.overlay.upload.crosswalk_admission import AdmittedCrosswalkV1
from featuregen.overlay.upload.crosswalk_flag import crosswalk_execution_enabled
from featuregen.overlay.upload.crosswalk_observation import SOURCE_TO_TARGET, TARGET_TO_SOURCE
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.operational_facts import read_operational_value
from featuregen.overlay.upload.upload_catalog import table_ref as _catalog_table_ref

__all__ = [
    "BODY_PATHS",
    "AvailabilityBasis",
    "ExpressionExecutionIR",
    "NonPhysicalReason",
    "NonPhysicalRef",
    "OperandTypeEvidence",
    "OperandTypeStatus",
    "PhysicalRef",
    "PitSpec",
    "RefRole",
    "compile_expression",
    "expression_ir_hash",
    "join_key_ref",
    "logical_refs_in",
]

#: The body-path vocabulary Child-1 keys expressions by (`formula/schema.py::_body_expressions`,
#: interfaces §6). Reused rather than re-minted: a path this module invented would not match the
#: path the authoring trace, the staging output and Task 15's `RunInputRequest` all use.
BODY_PATHS: frozenset[str] = frozenset(
    {"body.expr", "body.numerator", "body.denominator", "body.minuend", "body.subtrahend"})

#: `read_operational_value` renders the boolean fact flags as strings (`column_authority._render`).
_RESOLVED = "resolved"
_GOVERNED_TRUE = "true"

#: The C1 field carrying a column's governed logical type. Named once: `authoring.py` pairs it with
#: `output_type` and `replay_authoring._facts` reads exactly this field to build `ExprFacts`, so an
#: operand type read under a different field name would not be the one Child-1 resolved over.
_LOGICAL_TYPE_FIELD = "logical_representation"

#: The C1 statuses in which the READ ITSELF failed closed — the decision head is ambiguous, its
#: hashes do not recompute, or the projection the read depends on is degraded/lagged. Mirrored from
#: `output_authority._HARD_FAIL_STATUSES` (which is private, and belongs to a module this one must
#: not couple to) and kept LOCAL + auditable, exactly as that module keeps its own copy.
#:
#: The distinction this set draws is the whole point of :class:`OperandTypeStatus`: "the catalog
#: holds no governed type decision for this column" is a governance GAP, while "the governed type
#: could not be read" is a degraded or tampered AUTHORITY. Collapsing them would report a forked
#: decision log as a missing attestation and send an operator to attest a column that already is.
_UNAVAILABLE_TYPE_STATUSES: frozenset[str] = frozenset(
    {"fork", "hash_mismatch", "projection_unavailable"})


class RefRole(StrEnum):
    """Why a column is read. Closed, and carried rather than inferred downstream.

    A later stage cannot recover this: `txn_dt` read as the window's clock and `txn_dt` read as the
    availability gate are the same column doing two jobs, and a renderer that guessed which one it
    was looking at could apply the wrong filter to it.
    """

    SOURCE_TABLE = "source_table"   # the relation itself, not a column
    OPERAND = "operand"
    FILTER_LEFT = "filter_left"     # a predicate's ONLY column reference (§6: there is no right_ref)
    EVENT_TIME = "event_time"
    AVAILABILITY = "availability"
    GRAIN_KEY = "grain_key"
    JOIN_KEY = "join_key"


class NonPhysicalReason(StrEnum):
    """Why a ref-shaped string reachable from the expression is NOT a column read (§2.1).

    Both members are positions in Child-1's own grammar where a string is DATA or a NAME, so the
    classification is a statement about the grammar rather than a judgement about the value.
    """

    #: A ``TypedLiteral.value`` — the right-hand side of a comparison. ``FilterPredicate`` has no
    #: ``right_ref`` field at all, so reading this as a column would join a table the formula never
    #: mentioned.
    COMPARISON_LITERAL = "comparison_literal"
    #: A ``ParameterRef.name`` — a declared run parameter, bound at execution, never a catalog
    #: column.
    RUN_PARAMETER = "run_parameter"


class OperandTypeStatus(StrEnum):
    """Why an expression's operand does or does not carry a governed logical type. Closed.

    Four members because three of them route to three different people, and the fourth is not a
    problem at all:

    * :attr:`GOVERNED` — C1 served the type as ``resolved``: a hash-verified governed decision
      projection. This is the ONLY status that carries a type, because it is the only one under
      which a word about the operand is a governed fact rather than an echo of a file declaration;
    * :attr:`UNGOVERNED` — the read succeeded and no governed decision backs it (``no_decision`` /
      ``no_value`` / ``not_operational`` / ``retired`` / ``conflict``). A governance GAP: somebody
      must attest the column's type;
    * :attr:`UNAVAILABLE` — the read itself failed closed (``fork`` / ``hash_mismatch`` /
      ``projection_unavailable``). Degraded or tampered AUTHORITY: the remedy is to repair the
      decision log or the projection, not to attest anything;
    * :attr:`NO_OPERAND` — Child-1's grammar gives this aggregate no operand at all
      (``operand is None`` IFF ``COUNT_ROWS``, ``schema.py`` [c9]), so there is nothing to type and
      nothing is wrong.
    """

    GOVERNED = "governed"
    UNGOVERNED = "ungoverned"
    UNAVAILABLE = "unavailable"
    NO_OPERAND = "no_operand"


@dataclass(frozen=True, slots=True)
class OperandTypeEvidence:
    """What the catalog says about ONE expression's operand type — a statement, not a verdict.

    ``logical_type`` is populated for :attr:`OperandTypeStatus.GOVERNED` and for nothing else. That
    asymmetry is deliberate: carrying an ungoverned word in the same slot as a governed one is how a
    file-declared ``numeric`` gets consumed as though somebody had attested it, and §6's rule then
    rests on a claim nobody made. A consumer that wants a type must therefore check the status.

    ``read_status`` is C1's own status word, carried verbatim so an operator-facing refusal can say
    WHICH read failed (``fork`` reads differently from ``projection_unavailable``) without this
    module re-describing a vocabulary it does not own. ``None`` only for :attr:`NO_OPERAND`, where no
    read was attempted.
    """

    operand_ref: str | None
    status: OperandTypeStatus
    logical_type: str | None
    read_status: str | None


class AvailabilityBasis(StrEnum):
    """The governed ``AVAILABILITY_TIME.basis`` vocabulary (interfaces §4), mirrored as a closed
    enum so a basis this slice cannot render is refused rather than passed through as text."""

    POSTED_AT = "posted_at"
    INGESTED_AT = "ingested_at"
    EVENT_TIME_PLUS_LAG = "event_time_plus_lag"


@dataclass(frozen=True, slots=True)
class PhysicalRef:
    """One column (or the relation itself) the expression reads, resolved onto the cluster.

    ``logical_ref`` is the governed identity — schema-flattened, so its schema segment carries no
    cluster meaning; ``schema``/``table`` are what Task 5's resolver RESOLVED. Both are kept because
    they answer different questions: governance is asked about the ref, the metastore about the
    physical pair, and collapsing them is how a compilation reads a different table than the one it
    governs (§3.5).

    ``roles`` is a tuple because one column legitimately does several jobs at once.
    """

    logical_ref: str
    catalog_source: str
    schema: str
    table: str
    column: str | None
    roles: tuple[RefRole, ...]

    def identity_payload(self) -> dict[str, Any]:
        return {"logical_ref": self.logical_ref, "catalog_source": self.catalog_source,
                "schema": self.schema, "table": self.table, "column": self.column,
                "roles": [role.value for role in self.roles]}


@dataclass(frozen=True, slots=True)
class NonPhysicalRef:
    """A ref-shaped string the compiler saw and deliberately did not read (§2.1).

    ``location`` is the path inside the expression, never the string itself: a comparison literal IS
    a data value, and manifests and refusals carry locations, counts, types and hashes — never data.
    """

    location: str
    reason: NonPhysicalReason

    def identity_payload(self) -> dict[str, Any]:
        return {"location": self.location, "reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class PitSpec:
    """THIS expression's point-in-time semantics (§3, §8): its clock, its gate, its window.

    Deliberately excludes ``empty_window`` and ``null_input``. Those decide what an EMPTY window
    evaluates to, which §8 rule 4 assigns to the formula's own policies — putting them in a type
    named "PIT" would blur the one boundary this type exists to draw.

    ``availability_lag_hours`` is a canonical STRING and is ``None`` unless the basis is
    ``event_time_plus_lag``. A string because the governed fact schema types it as a JSON number,
    and a float reaching an identity hash is a rounding decision nobody made; ``None`` because a lag
    is undefined for the other two bases and a default of ``0`` would look like a governed answer.
    """

    event_time_ref: str
    availability_ref: str
    availability_basis: AvailabilityBasis
    availability_lag_hours: str | None
    window_basis: str
    window_length: int
    window_unit: str
    window_start_inclusive: str
    window_end_inclusive: str
    window_timezone: str

    def identity_payload(self) -> dict[str, Any]:
        return {
            "event_time_ref": self.event_time_ref,
            "availability_ref": self.availability_ref,
            "availability_basis": self.availability_basis.value,
            "availability_lag_hours": self.availability_lag_hours,
            "window_basis": self.window_basis,
            "window_length": self.window_length,
            "window_unit": self.window_unit,
            "window_start_inclusive": self.window_start_inclusive,
            "window_end_inclusive": self.window_end_inclusive,
            "window_timezone": self.window_timezone,
        }


@dataclass(frozen=True, slots=True)
class ExpressionExecutionIR:
    """Everything needed to compute ONE expression, and nothing about a given run (§3).

    ``non_physical_refs`` records §2.1's classification so it can be inspected rather than trusted.
    It is deliberately OUTSIDE :meth:`identity_payload`: it explains a decision about the formula,
    it is not an input to the computation, and letting a new classification reason change every
    compiled hash would make an explanatory field identity-bearing.
    """

    expr_path: str
    physical_read_set: tuple[PhysicalRef, ...]
    join_plan: JoinPlan
    pit: PitSpec
    input_requirements: tuple[PhysicalInputRequirement, ...]
    aggregation: AggregateFunction
    filter_tree: Mapping[str, Any] | None
    non_physical_refs: tuple[NonPhysicalRef, ...]
    operand_type: OperandTypeEvidence

    def identity_payload(self) -> dict[str, Any]:
        """What this expression IS — no provenance, no run-time value.

        The join plan contributes its STEPS (which tables are joined, on which columns, fanning
        which way) and not its authority columns or ``roles_used``. Those record WHY the traversal
        was allowed and WHO asked; neither changes a single row of the result, and including them
        would split one computation into as many artifacts as there are approvers and readers.

        ``operand_type`` is excluded for a sharper reason than that. It is evidence about whether
        this expression may be PUBLISHED, not about what it computes: a SUM over a governed
        ``numeric`` and the same SUM over a governed ``bigint`` read the same rows and add them the
        same way, and giving them two identities would split one computation over a distinction the
        published ``DECIMAL(p,s)`` does not record. The consequence of the evidence — the resolved
        :class:`~featuregen.materialize.physical_types.PhysicalType` — enters ``group_plan_hash`` on
        its own (§6). Including the evidence here would also make a projection blip change
        ``ir_hash`` and fire §9's ``IR_HASH_MISMATCH`` gate against a computation nobody touched.
        """
        return {
            "expr_path": self.expr_path,
            "physical_read_set": [ref.identity_payload() for ref in self.physical_read_set],
            # A typed step contributes its OWN payload; the single-catalog planner's step is a bare
            # triple. Both crosswalk legs land here, so every crosswalk pin the execution revision
            # is content-addressed over — the definition, the mapping binding, the temporal policy,
            # the composed observation, both leg pins — is inside `ir_hash` by the one field that
            # names it. A single-hop plan's bytes are untouched: no key was added, no branch reached.
            "join_steps": [
                (
                    step.identity_payload()
                    if isinstance(step, CrossCatalogJoinStepV1 | CrosswalkJoinStepV1)
                    else [step.from_ref, step.to_ref, step.cardinality]
                )
                for step in self.join_plan.steps
            ],
            "pit": self.pit.identity_payload(),
            "input_requirements": [req.identity_payload() for req in self.input_requirements],
            "aggregation": self.aggregation.value,
            "filter_tree": self.filter_tree,
        }


def expression_ir_hash(ir: ExpressionExecutionIR) -> str:
    """The expression's content identity — ``materialize_hash`` is the one hasher (§14)."""
    return materialize_hash(ir.identity_payload())


# ── §2.1: the exhaustive reference walk ──────────────────────────────────────────────────────────


def logical_refs_in(node: object, path: str) -> tuple[tuple[str, str], ...]:
    """Every ref-shaped string reachable from ``node``, as ordered ``(location, ref)`` pairs.

    EXHAUSTIVE and structure-driven: it recurses through dataclass fields and sequences rather than
    reading the fields this compiler happens to know about. That is the whole point — a check that
    enumerated the same fields as the compiler would agree with it by construction and prove
    nothing, whereas this one fails the day a field carrying a ref is added upstream.

    What counts as ref-shaped is :func:`parse_ref`'s own answer, not a private pattern here: the
    question is "would anything downstream treat this string as a ref?", and it is only meaningful
    asked with the grammar the readers use. That is what keeps a timezone or a status code out.
    (``Enum`` members are short-circuited first so a closed vocabulary never reaches the parser at
    all; Child-1's vocabularies are ``StrEnum``s and therefore ARE strings. None of their values is
    currently ref-shaped, so the skip is a guard rather than a load-bearing filter — recorded
    honestly, since a test cannot distinguish it today.)

    It deliberately decides nothing. Whether a found ref is a read, a literal or a defect is decided
    by the caller against what the compilation actually consumed.

    Reported per LOCATION, not per string: two slots holding the same ref are two claims about it,
    and one of them may be the one nobody consumed.
    """
    return tuple(_walk(node, path))


def _walk(node: object, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, Enum):
        return                                   # a closed vocabulary, never a reference
    if isinstance(node, str):
        if _is_ref_shaped(node):
            yield path, node
        return
    if isinstance(node, bytes | bytearray) or isinstance(node, Mapping):
        return                                   # not part of Child-1's expression grammar
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for field in dataclasses.fields(node):
            yield from _walk(getattr(node, field.name), f"{path}.{field.name}")
        return
    if isinstance(node, Sequence):
        for index, item in enumerate(node):
            yield from _walk(item, f"{path}[{index}]")


def _is_ref_shaped(value: str) -> bool:
    """True iff ``value`` is something :func:`parse_ref` accepts as a logical ref.

    Deliberately the PARSER's own answer rather than a private pattern: the question "would anything
    downstream treat this string as a ref?" is only meaningful if it is asked with the same
    grammar the readers use.
    """
    try:
        parse_ref(value)
    except ValueError:
        return False
    return True


#: Where a ref-shaped string is NOT a column reference, keyed by the dataclass it sits on. Both are
#: positions in Child-1's grammar, so this is a statement about the grammar rather than a guess
#: about a value — and both are load-bearing: `FilterPredicate` has no `right_ref`, so the ONLY way
#: a ref can appear on the right of a comparison is as data.
_NON_PHYSICAL_SLOTS: Mapping[type, tuple[str, NonPhysicalReason]] = {
    TypedLiteral: ("value", NonPhysicalReason.COMPARISON_LITERAL),
    ParameterRef: ("name", NonPhysicalReason.RUN_PARAMETER),
}


def _classify_non_physical(node: object, path: str) -> Iterator[NonPhysicalRef]:
    """Walk the same structure, emitting the classification for each non-physical slot found."""
    if isinstance(node, Enum) or isinstance(node, str | bytes | bytearray):
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        slot = _NON_PHYSICAL_SLOTS.get(type(node))
        if slot is not None:
            field_name, reason = slot
            value = getattr(node, field_name, None)
            if isinstance(value, str) and _is_ref_shaped(value):
                yield NonPhysicalRef(location=f"{path}.{field_name}", reason=reason)
        for field in dataclasses.fields(node):
            yield from _classify_non_physical(getattr(node, field.name), f"{path}.{field.name}")
        return
    if isinstance(node, Sequence):
        for index, item in enumerate(node):
            yield from _classify_non_physical(item, f"{path}[{index}]")


# ── governed availability ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Availability:
    """The governed availability column of one table, with the basis its fact declares."""

    column: str
    basis: AvailabilityBasis
    lag_hours: str | None


def _refuse(code: CompilationRefusalCode, detail: str) -> MaterializationRefused:
    return MaterializationRefused(code, detail)


def _ungoverned_availability(table_ref: str, detail: str) -> MaterializationRefused:
    return _refuse(
        CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED,
        f"{table_ref}: {detail}. Every row this expression aggregates is admitted or excluded by "
        f"the point-in-time gate (§8 rule 1), so a gate applied on an unattested column — or on an "
        f"unattested basis — silently changes the number and reports nothing")


def _governed_availability(
    conn: DbConn, catalog_source: str, ref_schema: str, table: str, table_ref: str
) -> _Availability | MaterializationRefused:
    """The table's ONE governed availability column and the basis its fact declares.

    Two steps, in the shape ``spine._governed_grain_columns`` established: the flat ``is_as_of``
    flag enumerates the CANDIDATES (it is the only per-table index there is) and the shipped
    governed reader then decides which of them are actually governed. Enumerating on the flag alone
    would accept a file declaration — ``build_graph`` writes ``is_as_of`` straight from the upload —
    and asking the reader without the flag would mean guessing column names.

    The catalog's OWN ``column_name`` is threaded into the ref built for the reader rather than one
    rebuilt from elsewhere: the shipped readers disagree about whether an ``object_ref`` lookup
    folds case, and a rebuilt address is how a perfectly governed catalog reports "not governed".
    """
    rows = conn.execute(
        "SELECT column_name FROM graph_node "
        "WHERE catalog_source = %s AND lower(table_name) = %s AND kind = 'column' "
        "  AND is_as_of = true ORDER BY lower(column_name)",
        (catalog_source, table)).fetchall()

    governed: list[str] = []
    for (column,) in rows:
        ref = normalize_ref(catalog_source, ref_schema, table, column)
        read = read_operational_value(conn, ref, "is_as_of")
        if read.status == _RESOLVED and read.value == _GOVERNED_TRUE:
            governed.append(column)

    if not governed:
        return _ungoverned_availability(
            table_ref,
            f"no column carries a governed availability_time fact ({len(rows)} flagged as-of, "
            f"0 governed)")
    if len(governed) > 1:
        return _ungoverned_availability(
            table_ref,
            f"{len(governed)} columns carry a governed availability_time fact; the fact names "
            f"exactly ONE column, so two of them make the gate a choice nobody made")

    column = governed[0]
    value = _availability_fact_value(conn, catalog_source, table, column, table_ref)
    if isinstance(value, MaterializationRefused):
        return value
    basis = AvailabilityBasis(value["basis"])
    lag = value.get("lag_hours")
    return _Availability(
        column=column, basis=basis,
        # Rendered from the JSON number to a canonical string only for the basis that defines one.
        # An integral lag renders without its `.0` so a re-declaration as `36` and `36.0` is one lag.
        lag_hours=(_canonical_number(lag)
                   if basis is AvailabilityBasis.EVENT_TIME_PLUS_LAG else None))


def _operand_type_evidence(conn: DbConn, expr: AggregateExpression) -> OperandTypeEvidence:
    """The governed C1 logical type of THIS expression's operand, or a marker saying why there is
    none (§6, Task 8.1).

    One read of the shipped governed reader, classified into :class:`OperandTypeStatus`. Three
    things it deliberately does NOT do:

    * it does not fall back to ``graph_node.data_type``. That column is the flat projection C1 reads
      THROUGH its hash gate, and reading it directly would serve exactly the value C1 refused —
      turning a tampered projection into a governed-looking type;
    * it does not consult ``formula.output.output_type``. That word is Child-1's, derived from ONE
      operand per formula, and using it here would re-import the blind spot this function exists to
      close;
    * it does not refuse. A type nobody governed is a fact about the catalog; whether it may back a
      published column is §6's question, and ``physical_types`` owns it.
    """
    if expr.operand is None:
        # `operand is None` IFF `COUNT_ROWS` (schema.py [c9]) — nothing to type, nothing wrong.
        return OperandTypeEvidence(
            operand_ref=None, status=OperandTypeStatus.NO_OPERAND, logical_type=None,
            read_status=None)

    read = read_operational_value(conn, expr.operand, _LOGICAL_TYPE_FIELD)
    if read.status in _UNAVAILABLE_TYPE_STATUSES:
        return OperandTypeEvidence(
            operand_ref=expr.operand, status=OperandTypeStatus.UNAVAILABLE, logical_type=None,
            read_status=read.status)
    if read.status == _RESOLVED and read.value is not None:
        return OperandTypeEvidence(
            operand_ref=expr.operand, status=OperandTypeStatus.GOVERNED,
            logical_type=str(read.value), read_status=read.status)
    # A `resolved` status with no value cannot describe a type, so it degrades here rather than
    # being carried as a governed `None` a consumer would have to re-check.
    return OperandTypeEvidence(
        operand_ref=expr.operand, status=OperandTypeStatus.UNGOVERNED, logical_type=None,
        read_status=read.status)


def _canonical_number(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _availability_fact_value(
    conn: DbConn, catalog_source: str, table: str, column: str, table_ref: str
) -> dict[str, Any] | MaterializationRefused:
    """The governed fact's own value, reached THROUGH the link the projection wrote.

    ``table_fact_projection`` reads only ``value["column"]`` and stores it as the ``is_as_of`` flag
    plus a ``confirmed_event_id`` link; ``basis`` and ``lag_hours`` are never projected anywhere, so
    the gate §8 rule 1 specifies cannot be rendered from ``graph_node`` alone. Rather than infer a
    basis (an inference about a specific bank's specific feed, whose failure mode is a wrong number)
    the payload is dereferenced through that link, and every step of the dereference must agree:

    1. the column carries a link id at all (authority — already established by the caller);
    2. an ``overlay_fact_state`` row exists for the table's ``availability_time`` fact and is
       VERIFIED — the only servable overlay status;
    3. its ``confirmed_event_id`` is the id on the column, so this is THE fact that was projected
       and not a later, differently-confirmed one;
    4. its ``column`` is the column that was flagged, catching a stale projection in either
       direction;
    5. its value clears the overlay's own per-type schema, so ``event_time_plus_lag`` without a lag
       refuses instead of rendering a zero-hour lag.

    ``resolve_fact`` is deliberately not used: it requires a registered ``CatalogAdapter`` and would
    re-decide authority (catalog precedence, expiry, drift) — a second, possibly disagreeing opinion
    about a question the projection already answered.
    """
    link = conn.execute(
        "SELECT availability_fact_event_id FROM graph_node "
        "WHERE catalog_source = %s AND lower(table_name) = %s AND kind = 'column' "
        "  AND lower(column_name) = %s",
        (catalog_source, table, column.strip().lower())).fetchone()
    event_id = link[0] if link is not None else None
    if event_id is None:
        return _ungoverned_availability(
            table_ref, f"the governed availability column {column!r} carries no fact-event link")

    key = _fact_key_of(_catalog_table_ref(catalog_source, table), AVAILABILITY_TIME)
    row = conn.execute(
        "SELECT status, value, confirmed_event_id FROM overlay_fact_state WHERE fact_key = %s",
        (key,)).fetchone()
    if row is None:
        return _ungoverned_availability(
            table_ref, "no availability_time fact is recorded in the overlay read model, so the "
                       "basis the point-in-time gate must apply is unknown")
    status, value, confirmed_event_id = row
    if status != "VERIFIED":
        return _ungoverned_availability(
            table_ref, f"the availability_time fact is {status!r}, and VERIFIED is the only "
                       f"servable overlay status")
    if confirmed_event_id != event_id:
        return _ungoverned_availability(
            table_ref, "the availability_time fact recorded in the overlay read model is not the "
                       "one projected onto the column (confirmed-event ids differ), so its basis "
                       "describes a different confirmation")
    if not isinstance(value, dict):
        return _ungoverned_availability(
            table_ref, f"the availability_time fact value is a {type(value).__name__}, not an "
                       f"object")
    try:
        validate_fact_value(AVAILABILITY_TIME, value, None)
    except (FactValidationError, TypeError, ValueError) as exc:
        return _ungoverned_availability(
            table_ref, f"the availability_time fact value fails its governed schema ({exc})")
    if str(value.get("column", "")).strip().lower() != column.strip().lower():
        return _ungoverned_availability(
            table_ref, f"the availability_time fact names a different column than the projection "
                       f"flagged as {column!r}: one of the two is stale, and applying the gate to "
                       f"either would be applying it to a column nobody governed for it")
    return value


# ── compilation ──────────────────────────────────────────────────────────────────────────────────


class _ReadSet:
    """The read set under construction: one entry per ref, accumulating roles in first-seen order."""

    def __init__(self) -> None:
        self._roles: dict[str, list[RefRole]] = {}
        self._identity: dict[str, PhysicalIdentity] = {}
        self._column: dict[str, str | None] = {}

    def add(self, logical_ref: str, role: RefRole, identity: PhysicalIdentity,
            column: str | None) -> None:
        roles = self._roles.setdefault(logical_ref, [])
        if role not in roles:
            roles.append(role)
        self._identity[logical_ref] = identity
        self._column[logical_ref] = column

    @property
    def refs(self) -> frozenset[str]:
        return frozenset(self._roles)

    def build(self) -> tuple[PhysicalRef, ...]:
        return tuple(
            PhysicalRef(
                logical_ref=logical_ref,
                catalog_source=self._identity[logical_ref].catalog_source,
                schema=self._identity[logical_ref].schema,
                table=self._identity[logical_ref].table,
                column=self._column[logical_ref],
                roles=tuple(roles))
            for logical_ref, roles in self._roles.items())


def _table_ref_of(column_ref: str) -> str:
    """The TABLE ref a column ref belongs to, in the ref's OWN catalog-side form.

    The ref's schema segment is carried through rather than replaced with a constant ``public``:
    it is what the catalog stored, and it is what the inventory's declared logical-to-physical
    mapping is keyed by (§3.5 step 2).
    """
    source, schema, table, _column = parse_ref(column_ref)
    return normalize_ref(source, schema, table)


def _canonical_ref(ref: str) -> str:
    """``ref`` in the ONE canonical spelling — the same fold :func:`_table_ref_of` applies.

    ``parse_ref`` recovers the components and ``normalize_ref`` re-emits them stripped and
    case-folded, so ``RETAIL::PUBLIC.TXN.AMT`` and ``retail::public.txn.amt`` become one string.
    Idempotent by construction: a canonical ref parses back into already-folded components.

    A string ``parse_ref`` rejects is returned UNTOUCHED: this helper normalizes spellings, it does
    not validate refs, and the existing verdict for a malformed ref (a ``ValueError`` from
    resolution, or an ``UNACCOUNTED_LOGICAL_REF`` refusal) must keep coming from where it comes
    from today.
    """
    try:
        source, schema, table, column = parse_ref(ref)
    except ValueError:
        return ref
    return normalize_ref(source, schema, table, column)


def _normalized_filter(node: FilterNode) -> FilterNode:
    """The filter subtree with every predicate ``left`` folded — and NOTHING else touched.

    ``left`` is a predicate's ONLY column reference (§6: ``FilterPredicate`` has no ``right_ref``).
    A ``TypedLiteral.value`` is DATA and a ``ParameterRef.name`` is a declared parameter's name —
    §2.1's non-physical slots — and folding either would silently change what the filter compares
    against (``'D'`` and ``'d'`` are different values).
    """
    if isinstance(node, FilterPredicate):
        return dataclasses.replace(node, left=_canonical_ref(node.left))
    return dataclasses.replace(
        node, children=tuple(_normalized_filter(child) for child in node.children))


def _normalized_expression(expr: AggregateExpression) -> AggregateExpression:
    """``expr`` with every AUTHORING-SIDE logical ref folded to its canonical spelling.

    Applied ONCE, at the top of :func:`compile_expression` before the first ``tables.resolve``, so
    a raw spelling can never become a ``_Tables`` cache key (deriving one table's requirement twice
    into ``input_requirements``) or reach ``identity_payload()`` (forking ``ir_hash`` away from the
    case-folded ``formula_content_hash``). Deliberately NOT inside ``_Tables.resolve``: folding the
    cache key alone would leave the payload refs unfolded and only mask the fork.

    NORMALIZED here — the refs that arrive in the author's own casing (``graph_node.object_ref``
    keeps the upload's casing, and the formula tools hand raw refs to the authoring model):
    ``source_relation.table_ref``, ``operand``, ``window.event_time_ref`` and every filter
    predicate's ``left``. The grain keys arrive raw too, through the ``grain_keys`` parameter, and
    are folded beside this call.

    SKIPPED, because they are born canonical: ``availability_ref`` and every join-key ref are
    DERIVED downstream through ``normalize_ref`` / :func:`join_key_ref`; a bridge realization's
    predicate refs are canonicalized by its own constructors (``bridge_realization._column_ref``
    runs ``normalize_ref`` in ``__post_init__``). SKIPPED, because they are not refs:
    ``TypedLiteral.value`` and ``ParameterRef.name`` (see :func:`_normalized_filter`).
    """
    return dataclasses.replace(
        expr,
        operand=None if expr.operand is None else _canonical_ref(expr.operand),
        source_relation=dataclasses.replace(
            expr.source_relation, table_ref=_canonical_ref(expr.source_relation.table_ref)),
        window=dataclasses.replace(
            expr.window, event_time_ref=_canonical_ref(expr.window.event_time_ref)),
        filter=None if expr.filter is None else _normalized_filter(expr.filter))


class _Tables:
    """Per-table resolution and requirements, each derived at most once per compilation.

    Once, because both are catalog reads and two reads of a moving projection can disagree — and
    because a requirement derived twice for one table would enter identity twice.
    """

    def __init__(self, conn: DbConn, inventory: ClusterInventoryV1) -> None:
        self._conn = conn
        self._inventory = inventory
        self._identity: dict[str, PhysicalIdentity] = {}
        self._requirements: dict[str, PhysicalInputRequirement] = {}

    def resolve(self, table_ref: str) -> PhysicalIdentity | MaterializationRefused:
        """Resolve, and DERIVE the requirement in the same step.

        Fused on purpose: a table this compilation resolved is a table it will read, and a resolved
        table with no requirement would be a physical read nothing declared a partition mapping for.

        The identity is READ OFF Task 5's own answer rather than resolved a second time here —
        ``derive_requirement`` already ran ``resolve_physical_identity`` to build it. Calling the
        resolver again would be a second read of a projection that moves, and the two answers
        entering the same IR (one in the read set, one in the requirement) could disagree.
        """
        if table_ref in self._identity:
            return self._identity[table_ref]
        requirement = derive_requirement(self._conn, self._inventory, table_ref=table_ref)
        if isinstance(requirement, MaterializationRefused):
            return requirement
        identity = PhysicalIdentity(catalog_source=requirement.catalog_source,
                                    schema=requirement.schema, table=requirement.table)
        self._identity[table_ref] = identity
        self._requirements[table_ref] = requirement
        return identity

    def requirements(self, order: Sequence[str]) -> tuple[PhysicalInputRequirement, ...]:
        """The resolved requirements in READ order, de-duplicated.

        Order is stated by the caller rather than taken from when each table happened to be
        resolved: the join target must be resolved BEFORE the path can be planned, so resolution
        order puts the destination in front of the hops that reach it — an order no reader of the
        generated project would recognize. ``input_requirements`` enters ``identity_payload``, so
        the order must also be a function of the plan and nothing else, or one expression would
        compile to two hashes depending on the sequence of internal lookups.
        """
        seen: dict[str, None] = {}
        for table_ref in order:
            seen.setdefault(table_ref, None)
        return tuple(self._requirements[table_ref] for table_ref in seen
                     if table_ref in self._requirements)


def compile_expression(
    conn: DbConn,
    *,
    expr_path: str,
    expr: AggregateExpression,
    grain_keys: Iterable[str],
    roles: Iterable[str] = (),
    inventory: ClusterInventoryV1,
    bridge_realizations: tuple[CurrentBridgeRealizationV1, ...] = (),
    crosswalks: tuple[AdmittedCrosswalkV1, ...] = (),
    execution_tier: ExecutionTier = ExecutionTier.PRODUCTION,
) -> ExpressionExecutionIR | MaterializationRefused:
    """Compile ONE ``AggregateExpression`` into its executable plan, or refuse it (§3).

    A refusal is RETURNED, not raised: a refused expression is one governed verdict among the many
    a compilation collects. Checks run in this order, and the FIRST failure decides the code:

    1. physical resolution of the source relation (§3.5) and its declared layout (§3.4) — until the
       table is known, nothing else is answerable;
    2. governed ``AVAILABILITY_TIME`` for that table (§8 rule 1);
    3. the governed traversal from the source relation to the grain (§3.1–3.2), via ``plan_join``,
       whose refusals are propagated unchanged;
    4. reference completeness over the whole expression (§2.1) — checked LAST because it asks
       whether everything the expression mentions was consumed, and nothing is consumed until the
       steps above have run.

    The operand's governed type is then READ and CARRIED as :class:`OperandTypeEvidence`. It is not
    a fifth check: §6's exact-numeric rule is ``physical_types``', and a compiler that refused a type
    here would hold two opinions about one question.

    ``crosswalks`` are ADMITTED two-leg mapping-table traversals (Release C Task 12) this
    compilation may plan through, supplied by the caller exactly as ``bridge_realizations`` are —
    this module reads no store. An empty tuple is the shipped state and every byte of a
    direct-equality plan is unchanged by their presence in the signature.
    ``execution_tier`` is the applicability scope both kinds are read at (see
    ``ir._executable_realizations_for_tier``); it is not a run tier and changes no identity.

    Raises:
        ValueError: ``expr_path`` is outside Child-1's body-path vocabulary, or ``grain_keys`` is
            empty. Both are calls assembled wrongly rather than governed verdicts, and §14's closed
            vocabulary has no member for that — the line ``joins.plan_join`` draws for a
            cross-catalog identity.
    """
    if expr_path not in BODY_PATHS:
        raise ValueError(
            f"expr_path {expr_path!r} is not one of Child-1's body paths "
            f"({', '.join(sorted(BODY_PATHS))}): the path names the staging output this expression "
            f"produces, and one this compiler invented would not be the one anything else reads")
    grain = tuple(grain_keys)
    if not grain:
        raise ValueError(
            "grain_keys is empty: an aggregate with no grain has no key to reduce onto, and the "
            "spine it must land on is defined by those keys")

    # One governed formula, one `ir_hash`: `formula_content_hash` folds ref case, so every
    # authoring-side spelling is folded HERE, before the first `tables.resolve(...)` — a raw
    # spelling must never become a `_Tables` cache key or reach `identity_payload()`.
    expr = _normalized_expression(expr)
    grain = tuple(_canonical_ref(key) for key in grain)

    # FEATUREGEN_CROSSWALK_EXECUTION=0 keeps crosswalks discoverable and STRUCTURALLY
    # NON-EXECUTABLE (profile plan §9 Task 13). Dropped here rather than refused, and that is what
    # "structurally non-executable" MEANS: with the flag off this compilation plans exactly what it
    # planned before crosswalks existed and answers with exactly the refusal it answered then —
    # there is no crosswalk-shaped code path to reach and no crosswalk-shaped error to read. The
    # gate sits at THIS entry point rather than at `compile_ir`'s because this is the function that
    # consumes them, so a caller who reaches the planner some other way cannot bypass it.
    if crosswalks and not crosswalk_execution_enabled():
        crosswalks = ()

    roles_used = tuple(roles)
    tables = _Tables(conn, inventory)
    read_set = _ReadSet()

    source_table_ref = expr.source_relation.table_ref
    source_identity = tables.resolve(source_table_ref)
    if isinstance(source_identity, MaterializationRefused):
        return source_identity
    read_set.add(source_table_ref, RefRole.SOURCE_TABLE, source_identity, None)

    # Every column the expression itself names is a column of its own source relation — Child-1
    # enforces it for the operand, the event-time ref and every filter `left`
    # (`schema._require_contained_column`). That is what makes per-column resolution unnecessary
    # here, so it is VERIFIED rather than assumed: an operand on another table would otherwise be
    # recorded with the SOURCE table's physical identity, i.e. as a column of a table it is not in.
    own: list[tuple[str, str]] = []
    if expr.operand is not None:
        own.append((f"{expr_path}.operand", expr.operand))
    own.append((f"{expr_path}.window.event_time_ref", expr.window.event_time_ref))
    own.extend((f"{expr_path}.filter{suffix}", left)
               for suffix, left in _filter_left_refs(expr.filter))

    off_relation = sorted(location for location, ref in own
                          if not _is_source_relation(_table_ref_of(ref), source_identity))
    if off_relation:
        return _refuse(
            CompilationRefusalCode.UNACCOUNTED_LOGICAL_REF,
            f"{len(off_relation)} reference(s) name a column of a relation this expression does "
            f"not read: {', '.join(off_relation)}. Child-1 requires the operand, the event-time ref "
            f"and every filter `left` to be contained in the expression's own source_relation, so "
            f"a ref outside it has no table to be resolved against and nothing joins toward it")

    for location, ref in own:
        role = (RefRole.OPERAND if location.endswith(".operand")
                else RefRole.EVENT_TIME if location.endswith(".event_time_ref")
                else RefRole.FILTER_LEFT)
        read_set.add(ref, role, source_identity, _column_of(ref))

    source_catalog, source_schema, source_table, _column = parse_ref(source_table_ref)
    availability = _governed_availability(
        conn, source_catalog, source_schema, source_table, source_table_ref)
    if isinstance(availability, MaterializationRefused):
        return availability
    availability_ref = normalize_ref(source_catalog, source_schema, source_table,
                                     availability.column)
    read_set.add(availability_ref, RefRole.AVAILABILITY, source_identity, availability.column)

    planned = _plan_to_grain(conn, tables, read_set, source_identity=source_identity,
                             source_table_ref=source_table_ref,
                             source_catalog=source_catalog, grain=grain, roles=roles_used,
                             bridge_realizations=bridge_realizations,
                             crosswalks=crosswalks, execution_tier=execution_tier)
    if isinstance(planned, MaterializationRefused):
        return planned
    join, joined_tables = planned

    unaccounted = _refuse_unaccounted(expr, grain, expr_path, read_set.refs)
    if unaccounted is not None:
        return unaccounted

    # Read LAST, after every refusal: the evidence describes an expression that compiled, and a
    # catalog read performed for a plan that was then refused is a read nothing needed.
    operand_type = _operand_type_evidence(conn, expr)

    return ExpressionExecutionIR(
        expr_path=expr_path,
        physical_read_set=read_set.build(),
        join_plan=join,
        pit=PitSpec(
            event_time_ref=expr.window.event_time_ref,
            availability_ref=availability_ref,
            availability_basis=availability.basis,
            availability_lag_hours=availability.lag_hours,
            window_basis=expr.window.basis.value,
            window_length=expr.window.length,
            window_unit=expr.window.unit.value,
            window_start_inclusive=expr.window.start_inclusive.value,
            window_end_inclusive=expr.window.end_inclusive.value,
            window_timezone=expr.window.timezone),
        input_requirements=tables.requirements((source_table_ref, *joined_tables)),
        aggregation=expr.aggregation,
        filter_tree=(None if expr.filter is None
                     else filter_plain(expr.filter, f"{expr_path}.filter")),
        non_physical_refs=tuple(_classify_non_physical(expr, expr_path)),
        operand_type=operand_type)


def _column_of(column_ref: str) -> str | None:
    _source, _schema, _table, column = parse_ref(column_ref)
    return column


def _filter_left_refs(node: object) -> Iterator[tuple[str, str]]:
    """Every predicate's ``left`` as ``(location suffix, ref)`` — a filter's ONLY column references.

    Driven by the same exhaustive walk the completeness check uses, so the two cannot drift: a
    hand-rolled recursion here that missed a nesting level would silently drop a read AND leave the
    dropped ref unaccounted, which is one bug reported as two. (Interfaces §6: ``FilterPredicate``
    has no ``right_ref`` — the right-hand side is a literal, a parameter or a set of literals.)
    """
    for location, ref in logical_refs_in(node, ""):
        if location.endswith(".left"):
            yield location, ref


def _match_crosswalk(
    crosswalks: tuple[AdmittedCrosswalkV1, ...], source_table_ref: str, target_table_ref: str,
) -> tuple[AdmittedCrosswalkV1, str] | MaterializationRefused | None:
    """The ONE admitted crosswalk relating these two datasets, and WHICH WAY this traversal runs.

    A definition's two sides are canonically ordered (``A -> map -> B`` and ``B -> map -> A`` are
    one record), so which side the expression starts from is what names the direction — and the
    direction is what decides which measured verdict gates the plan. Nothing here reads a stored
    orientation, because there is none to read.

    More than one match REFUSES. §6.6's V1 conflict behaviour is ``refuse_on_multiple`` and
    ``crosswalk_admission`` already refuses two active mappings for one endpoint pair; a compiler
    that picked one anyway would make the answer depend on which definition it happened to read
    first, which is the same defect one layer down.
    """
    source, target = _fold(source_table_ref), _fold(target_table_ref)
    matched: list[tuple[AdmittedCrosswalkV1, str]] = []
    for admitted in crosswalks:
        left, right = (_fold(ref) for ref in admitted.endpoint_refs())
        if (source, target) == (left, right):
            matched.append((admitted, SOURCE_TO_TARGET))
        elif (source, target) == (right, left):
            matched.append((admitted, TARGET_TO_SOURCE))
    if not matched:
        return None
    if len(matched) > 1:
        return _refuse(
            CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
            f"{len(matched)} admitted crosswalks relate {source_table_ref} and {target_table_ref}: "
            f"with more than one active mapping the answer depends on which definition a compiler "
            f"happened to read, and no deduplication, first-row pick or tie-break is permitted to "
            f"make that go away (§6.6, refuse_on_multiple)")
    return matched[0]


def _plan_to_grain(
    conn: DbConn,
    tables: _Tables,
    read_set: _ReadSet,
    *,
    source_identity: PhysicalIdentity,
    source_table_ref: str,
    source_catalog: str,
    grain: tuple[str, ...],
    roles: tuple[str, ...],
    bridge_realizations: tuple[CurrentBridgeRealizationV1, ...],
    crosswalks: tuple[AdmittedCrosswalkV1, ...] = (),
    execution_tier: ExecutionTier = ExecutionTier.PRODUCTION,
) -> tuple[JoinPlan, tuple[str, ...]] | MaterializationRefused:
    """The governed traversal from the source relation to the grain, and the reads it implies.

    Grain keys are the only refs an expression may hold that are NOT columns of its source relation
    (Child-1 constrains everything else), so they are the only reason a join exists here.

    Returns the plan together with every OTHER table ref the plan makes this expression read, in
    traversal order — the intermediate hops included, since those are precisely the tables the
    caller never named and whose declared layout nobody else would check. For a crosswalk that
    includes the MAPPING dataset, which no formula ever names.
    """
    grain_tables: dict[str, None] = {}
    for key in grain:
        grain_tables.setdefault(_table_ref_of(key), None)

    off_source = [table_ref for table_ref in grain_tables
                  if not _is_source_relation(table_ref, source_identity)]
    if len(off_source) > 1:
        # STILL REFUSED, and for its original reason. The rule this states is about INDEPENDENT
        # joins: two of them need two fan-out verdicts and nothing measures the pair, so no single
        # plan can state what the combination does to row counts. A crosswalk is the case where
        # something does — see the three-way branch below, which is why that refusal moved and this
        # one did not.
        return _refuse(
            CompilationRefusalCode.GRAIN_PATH_NOT_GOVERNED,
            f"the grain keys sit on {len(off_source) + 1} different tables: this slice governs ONE "
            f"traversal per expression, and a grain assembled from two independent joins would need "
            f"two — each with its own fan-out verdict, which no single plan can state")

    target_table_ref = off_source[0] if off_source else None
    if target_table_ref is None:
        target_identity = source_identity
    else:
        parsed_source, _schema, _table, _column = parse_ref(target_table_ref)
        resolved = tables.resolve(target_table_ref)
        if isinstance(resolved, MaterializationRefused):
            return resolved
        target_identity = resolved

    # ── the traversal, in THREE kinds (Release C Task 12) ────────────────────────────────────────
    #
    # WHAT REPLACED THE EITHER/OR, AND WHY IT IS NOT A BYPASS. Until this task there were exactly
    # two branches — one same-catalog traversal OR one cross-catalog realization — and a mapping
    # table between two schemes fell through both. The adversarial review named the reason the
    # third was impossible, in this module's own words: a grain assembled from two joins "would
    # need two [fan-out verdicts], which no single plan can state". That was a true statement about
    # two INDEPENDENT joins and it is still true of them (the refusal above is unchanged).
    #
    # It is false about a crosswalk, and Task 11 is what made it false. A composed observation
    # measures the composition ITSELF — `source_to_target_max_matches` over the JOINED shape,
    # explicitly "never the product of the leg maxima (which is a bound)" — and admission turns
    # that measurement into ONE directional verdict per direction. So a two-leg traversal IS
    # plannable exactly when the admitted execution carries the requested direction's verdict and
    # the composed fan-out behind it: the plan then states one fan-out verdict for the traversal it
    # is planning, which is precisely the condition the old refusal demanded and could not meet.
    # The refusal's reason is satisfied here, not routed around.
    crosswalk: tuple[AdmittedCrosswalkV1, str] | None = None
    if target_table_ref is not None and crosswalks:
        matched = _match_crosswalk(crosswalks, source_table_ref, target_table_ref)
        if isinstance(matched, MaterializationRefused):
            return matched
        crosswalk = matched

    cross_catalog = _fold(target_identity.catalog_source) != _fold(source_catalog)
    if crosswalk is not None:
        admitted, direction = crosswalk
        mapping_table_ref = admitted.definition.mapping_dataset_ref
        mapping_identity = tables.resolve(mapping_table_ref)
        if isinstance(mapping_identity, MaterializationRefused):
            return mapping_identity
        plan = plan_crosswalk_join(
            admitted,
            direction=direction,
            from_identity=source_identity,
            to_identity=target_identity,
            mapping_identity=mapping_identity,
            execution_tier=execution_tier,
            roles=roles,
        )
    elif cross_catalog:
        matches = tuple(
            realization
            for realization in bridge_realizations
            if _identity_matches_realization(
                source_identity, target_identity, realization)
        )
        if len(matches) != 1:
            return _refuse(
                CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
                f"the cross-catalog grain route {source_catalog} -> "
                f"{target_identity.catalog_source} resolved to {len(matches)} current executable "
                "directional realizations; production compilation requires exactly one",
            )
        plan = plan_cross_catalog_join(
            matches[0],
            from_identity=source_identity,
            to_identity=target_identity,
            roles=roles,
        )
    else:
        plan = plan_join(conn, catalog_source=source_catalog, from_identity=source_identity,
                         to_identity=target_identity, roles=roles)
    if isinstance(plan, MaterializationRefused):
        return plan

    # A join KEY is a column the query reads, so it belongs in the read set Gate 2 authorizes —
    # otherwise a path would be joined on columns nobody was ever authorized to see. Every table on
    # the path is read too, including the intermediate hops the caller never named.
    read_order: dict[str, None] = {}
    for step in plan.steps:
        step_refs = (
            # A cross-catalog step and a crosswalk leg both carry FULL governed logical refs, which
            # already name their own catalog. Only the single-catalog planner's steps carry bare
            # graph-side addresses that have to be qualified with the catalog that planned them.
            tuple(ref for pair in step.column_pairs for ref in pair)
            if isinstance(step, CrossCatalogJoinStepV1 | CrosswalkJoinStepV1)
            else tuple(
                join_key_ref(source_catalog, ref)
                for ref in (step.from_ref, step.to_ref)
            )
        )
        for key_ref in step_refs:
            key_table_ref = _table_ref_of(key_ref)
            identity = tables.resolve(key_table_ref)
            if isinstance(identity, MaterializationRefused):
                return identity
            read_set.add(key_ref, RefRole.JOIN_KEY, identity, _column_of(key_ref))
            read_order.setdefault(key_table_ref, None)
        if isinstance(step, CrossCatalogJoinStepV1):
            for predicate in step.predicates:
                predicate_refs: tuple[str, ...]
                if isinstance(predicate, FixedValueReferencePredicateV1):
                    predicate_refs = (predicate.logical_column_ref,)
                elif isinstance(predicate, AsOfIntervalRequirementV1):
                    predicate_refs = (
                        predicate.effective_from_ref,
                        predicate.effective_to_ref,
                    )
                elif isinstance(predicate, AdditionalKeyRequirementV1):
                    predicate_refs = (
                        predicate.from_logical_column_ref,
                        predicate.to_logical_column_ref,
                    )
                else:  # pragma: no cover - the realization constructor closes the vocabulary
                    raise AssertionError("unknown structured bridge predicate")
                for predicate_ref in predicate_refs:
                    predicate_table_ref = _table_ref_of(predicate_ref)
                    identity = tables.resolve(predicate_table_ref)
                    if isinstance(identity, MaterializationRefused):
                        return identity
                    read_set.add(
                        predicate_ref,
                        RefRole.FILTER_LEFT,
                        identity,
                        _column_of(predicate_ref),
                    )
                    read_order.setdefault(predicate_table_ref, None)
        if isinstance(step, CrosswalkJoinStepV1):
            # THE MAPPING ROW RULE'S OWN COLUMNS ARE READS. The filter that decides which mapping
            # rows take part is applied on the cluster, so its effective-from / effective-to /
            # snapshot / current-flag columns are columns this group reads — and Gate 2 must
            # authorize them, or the traversal would filter on a column nobody was granted.
            for predicate in step.mapping_row_predicates:
                predicate_table_ref = _table_ref_of(predicate.column_ref)
                identity = tables.resolve(predicate_table_ref)
                if isinstance(identity, MaterializationRefused):
                    return identity
                read_set.add(predicate.column_ref, RefRole.FILTER_LEFT, identity,
                             _column_of(predicate.column_ref))
                read_order.setdefault(predicate_table_ref, None)

    for key in grain:
        key_table_ref = _table_ref_of(key)
        identity = tables.resolve(key_table_ref)
        if isinstance(identity, MaterializationRefused):
            return identity
        read_set.add(key, RefRole.GRAIN_KEY, identity, _column_of(key))
        read_order.setdefault(key_table_ref, None)
    return plan, tuple(read_order)


def _identity_matches_realization(
    source: PhysicalIdentity,
    target: PhysicalIdentity,
    realization: CurrentBridgeRealizationV1,
) -> bool:
    revision = realization.revision
    left = revision.from_endpoint.physical_binding
    right = revision.to_endpoint.physical_binding
    return (
        left is not None
        and right is not None
        and (
            _fold(source.catalog_source),
            _fold(source.schema),
            _fold(source.table),
        )
        == (
            _fold(left.identity.catalog_source),
            _fold(left.identity.schema),
            _fold(left.identity.table),
        )
        and (
            _fold(target.catalog_source),
            _fold(target.schema),
            _fold(target.table),
        )
        == (
            _fold(right.identity.catalog_source),
            _fold(right.identity.schema),
            _fold(right.identity.table),
        )
    )


def _fold(value: str) -> str:
    return value.strip().lower()


def _is_source_relation(table_ref: str, identity: PhysicalIdentity) -> bool:
    """Whether a grain key's table IS the expression's source relation.

    Compared against the RESOLVED identity, not the ref's own schema segment: the segment is
    catalog-side and schema-flattened (§3.5), so it cannot distinguish two tables and must not be
    trusted to. ``plan_join`` returns an empty, no-hop plan for a table joined to itself, so this
    only decides whether a second table needs resolving at all.
    """
    source, _schema, table, _column = parse_ref(table_ref)
    return (_fold(source), _fold(table)) == (_fold(identity.catalog_source), _fold(identity.table))


def join_key_ref(catalog_source: str, step_ref: str) -> str:
    """A join step's endpoint as a canonical logical COLUMN ref.

    The planner's steps carry the graph's own ``schema.table.column`` object refs and no catalog
    source (its BFS is single-catalog). The source is threaded in from the call that planned the
    path, and the schema segment is the step's OWN — not a hard-coded ``public``, which would be
    this module deciding what the graph stores.

    Public since Task 7: Gate 2 (§1.3) authorizes every join step's endpoint as its own element
    class, and an endpoint addressed by a second conversion there could name a different node than
    the one this module put in the read set — so the same node would be authorized under one
    address and read under another.
    """
    parts = step_ref.split(".")
    if len(parts) >= 3:
        schema, table, column = parts[0], parts[1], ".".join(parts[2:])
    elif len(parts) == 2:
        schema, table, column = "public", parts[0], parts[1]
    else:
        schema, table, column = "public", step_ref, ""
    return normalize_ref(catalog_source, schema, table, column or None)


def _refuse_unaccounted(
    expr: AggregateExpression, grain: tuple[str, ...], expr_path: str, read: frozenset[str]
) -> MaterializationRefused | None:
    """§2.1 — every ref reachable from the expression was READ or explicitly classified.

    The refusal names the LOCATION, never the string: a slot that holds an unconsumed ref may well
    hold a data value (that is one of the ways it gets there), and a refusal that echoed it would
    put data into an operator-facing message.
    """
    classified = {ref.location for ref in _classify_non_physical(expr, expr_path)}
    accounted = set(read)
    unaccounted = [
        location for location, ref in
        (*logical_refs_in(expr, expr_path),
         *((f"grain.keys[{i}]", key) for i, key in enumerate(grain)))
        if ref not in accounted and location not in classified]
    if not unaccounted:
        return None
    return _refuse(
        CompilationRefusalCode.UNACCOUNTED_LOGICAL_REF,
        f"{len(unaccounted)} reference(s) reachable from {expr_path} were neither compiled into "
        f"the physical read set nor classified as non-physical: {', '.join(sorted(unaccounted))}. "
        f"A reference nothing consumed is a reference nothing authorized, nothing resolved onto the "
        f"cluster and nothing will read — so the compiled plan would be quietly incomplete")
