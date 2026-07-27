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
    ParameterRef,
    TypedLiteral,
)
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.inputs import PhysicalInputRequirement, derive_requirement
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.materialize.joins import JoinPlan, PhysicalIdentity, plan_join
from featuregen.overlay.facts import AVAILABILITY_TIME, FactValidationError, validate_fact_value
from featuregen.overlay.identity import fact_key as _fact_key_of
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.operational_facts import read_operational_value
from featuregen.overlay.upload.upload_catalog import table_ref as _catalog_table_ref

__all__ = [
    "BODY_PATHS",
    "AvailabilityBasis",
    "ExpressionExecutionIR",
    "NonPhysicalReason",
    "NonPhysicalRef",
    "PhysicalRef",
    "PitSpec",
    "RefRole",
    "compile_expression",
    "expression_ir_hash",
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

    def identity_payload(self) -> dict[str, Any]:
        """What this expression IS — no provenance, no run-time value.

        The join plan contributes its STEPS (which tables are joined, on which columns, fanning
        which way) and not its authority columns or ``roles_used``. Those record WHY the traversal
        was allowed and WHO asked; neither changes a single row of the result, and including them
        would split one computation into as many artifacts as there are approvers and readers.
        """
        return {
            "expr_path": self.expr_path,
            "physical_read_set": [ref.identity_payload() for ref in self.physical_read_set],
            "join_steps": [[step.from_ref, step.to_ref, step.cardinality]
                           for step in self.join_plan.steps],
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
                             source_catalog=source_catalog, grain=grain, roles=roles_used)
    if isinstance(planned, MaterializationRefused):
        return planned
    join, joined_tables = planned

    unaccounted = _refuse_unaccounted(expr, grain, expr_path, read_set.refs)
    if unaccounted is not None:
        return unaccounted

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
        non_physical_refs=tuple(_classify_non_physical(expr, expr_path)))


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


def _plan_to_grain(
    conn: DbConn,
    tables: _Tables,
    read_set: _ReadSet,
    *,
    source_identity: PhysicalIdentity,
    source_catalog: str,
    grain: tuple[str, ...],
    roles: tuple[str, ...],
) -> tuple[JoinPlan, tuple[str, ...]] | MaterializationRefused:
    """The governed traversal from the source relation to the grain, and the reads it implies.

    Grain keys are the only refs an expression may hold that are NOT columns of its source relation
    (Child-1 constrains everything else), so they are the only reason a join exists here.

    Returns the plan together with every OTHER table ref the plan makes this expression read, in
    traversal order — the intermediate hops included, since those are precisely the tables the
    caller never named and whose declared layout nobody else would check.
    """
    grain_tables: dict[str, None] = {}
    for key in grain:
        grain_tables.setdefault(_table_ref_of(key), None)

    off_source = [table_ref for table_ref in grain_tables
                  if not _is_source_relation(table_ref, source_identity)]
    if len(off_source) > 1:
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
        if _fold(parsed_source) != _fold(source_catalog):
            return _refuse(
                CompilationRefusalCode.GRAIN_PATH_NOT_GOVERNED,
                f"the grain key table {target_table_ref} is in catalog {parsed_source!r} while the "
                f"source relation is in {source_catalog!r}: the governed join planner is "
                f"single-catalog, so no governed path between them exists to plan")
        resolved = tables.resolve(target_table_ref)
        if isinstance(resolved, MaterializationRefused):
            return resolved
        target_identity = resolved

    plan = plan_join(conn, catalog_source=source_catalog, from_identity=source_identity,
                     to_identity=target_identity, roles=roles)
    if isinstance(plan, MaterializationRefused):
        return plan

    # A join KEY is a column the query reads, so it belongs in the read set Gate 2 authorizes —
    # otherwise a path would be joined on columns nobody was ever authorized to see. Every table on
    # the path is read too, including the intermediate hops the caller never named.
    read_order: dict[str, None] = {}
    for step in plan.steps:
        for step_ref in (step.from_ref, step.to_ref):
            key_ref = _key_ref(source_catalog, step_ref)
            key_table_ref = _table_ref_of(key_ref)
            identity = tables.resolve(key_table_ref)
            if isinstance(identity, MaterializationRefused):
                return identity
            read_set.add(key_ref, RefRole.JOIN_KEY, identity, _column_of(key_ref))
            read_order.setdefault(key_table_ref, None)

    for key in grain:
        key_table_ref = _table_ref_of(key)
        identity = tables.resolve(key_table_ref)
        if isinstance(identity, MaterializationRefused):
            return identity
        read_set.add(key, RefRole.GRAIN_KEY, identity, _column_of(key))
        read_order.setdefault(key_table_ref, None)
    return plan, tuple(read_order)


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


def _key_ref(catalog_source: str, step_ref: str) -> str:
    """A join step's endpoint as a canonical logical COLUMN ref.

    The planner's steps carry the graph's own ``schema.table.column`` object refs and no catalog
    source (its BFS is single-catalog). The source is threaded in from the call that planned the
    path, and the schema segment is the step's OWN — not a hard-coded ``public``, which would be
    this module deciding what the graph stores.
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
