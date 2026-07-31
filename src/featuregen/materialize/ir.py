"""Spec §1.3 / §2 / §3 — the per-FEATURE compiled IR, and Gate 2 over the whole GROUP.

**What this module assembles.** One :class:`ExpressionExecutionIR` per body path (Task 6), the
validated spine (Task 4), and Child-1's own output policy, joined into one
:class:`FormulaExecutionIRV1` with a content hash. It plans nothing itself: expression compilation,
join planning, physical resolution and spine validation are owned elsewhere and their verdicts are
PROPAGATED unchanged. Asking any of those questions a second way here would be a second chance to
answer it differently.

**The output policy is CARRIED, never re-derived (§2).** ``FormulaOutputPolicyV1`` is resolved by
Child-1 over C1 governed facts — additivity from a governed operand plus a partition proof, the
output type from the operand's governed ``logical_representation`` (interfaces §7). Re-resolving it
here would produce a *second* opinion about a governed question, and the second opinion would be
computed from whatever this stage happens to know, which is less. That is precisely how an
advisory guess gets laundered into governed authority, so the policy is passed through by reference
and this module does not import the resolver at all.

**Gate 2 is GROUP-WIDE (§1.3).** Gate 1 cannot authorize reads, because availability columns, join
hops, bridge tables and the spine are only discovered during compilation. So authorization runs
AFTER the IR is complete, over the UNION of:

* every ``PhysicalRef`` in every expression of every feature;
* the spine's source table, its ordered keys, and every column the spine itself reads;
* every join step's endpoints — taken from the PLAN, not only from the read set;
* every availability column — taken from each expression's ``PitSpec`` and from the spine.

The last two are derived from their own structural source even though Task 6 also folds them into
the read set. That redundancy is the point: §1.3 names them as element classes of the union, and a
gate that could only see them through one path would stop seeing them the day that path changed.

**A per-feature signature would be wrong in a way that matters.** A public feature genuinely *is*
individually authorized, so a per-feature gate would authorize it and a test asserting "every IR is
refused" would assert false semantics. What must fail is the GROUP OPERATION: one denied element
anywhere returns a single ``READ_SCOPE_INSUFFICIENT``, and **no contract, group plan or project is
produced**. There is no partial authorization and no per-feature bypass — which is why the success
value is a TOKEN (:class:`AuthorizedCompilation`) rather than a boolean: a downstream stage that
takes the token cannot be entered by a caller who skipped the gate.

**Two sensitivity axes, and this gate owns exactly one (interfaces §2).** ``graph_node.sensitivity``
is the read-scope TAG (``pii``, ``restricted``) checked against ``allowed_sensitivities(roles)`` —
that is Gate 2. ``graph_node.effective_restriction`` is the ordered restriction LEVEL, and a
``prohibited`` input is refused during §5.2 classification with ``PROHIBITED_INPUT``. Conflating
them would give the wrong code and make one of the two axes unreachable.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.contracts.db import DbConn
from featuregen.formula.schema import (
    FinalOperation,
    FormulaOutputPolicyV1,
    RatioBody,
    ZeroDenominator,
    body_expressions,
)
from featuregen.materialize.admission import AdmittedFeature
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import (
    ExpressionExecutionIR,
    RefRole,
    compile_expression,
    join_key_ref,
)
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.materialize.joins import CrossCatalogJoinStepV1
from featuregen.materialize.spine import (
    SpineSourceDeclarationV1,
    SpineSpec,
    validate_spine_declaration,
)
from featuregen.overlay.upload.bridge_store import (
    CurrentBridgeRealizationV1,
    executable_bridge_realizations,
    load_current_bridge_realizations,
    revalidate_bridge_realization,
)
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.read_scope import allowed_sensitivities

__all__ = [
    "AuthorizedCompilation",
    "BridgeExecutionAuthorization",
    "FormulaExecutionIRV1",
    "ReadElementKind",
    "authorize_compilation",
    "authorize_execution_realizations",
    "bridge_realization_dependencies",
    "compile_ir",
    "ir_hash",
    "physical_read_set",
]


class ReadElementKind(StrEnum):
    """WHY an element is part of the group's physical read set. Closed, and reported in refusals.

    An operator told only "you may not read this column" cannot tell whether the remedy is a
    different formula, a different population or a different join — and the four answers route to
    four different people. The kind is carried, never inferred downstream.
    """

    EXPRESSION_READ = "expression_read"   # an operand, a filter `left`, an event time, a grain key…
    JOIN_ENDPOINT = "join_endpoint"       # a column the governed traversal joins ON
    AVAILABILITY = "availability"         # the column the point-in-time gate is applied to
    SPINE_SOURCE = "spine_source"         # the population's own table
    SPINE_KEY = "spine_key"               # an ordered key of the population
    SPINE_READ = "spine_read"             # any other column the spine itself reads


@dataclass(frozen=True, slots=True)
class FormulaExecutionIRV1:
    """One feature's complete compiled plan — and nothing about a given run (§2, §3.3).

    ``expressions`` is one :class:`ExpressionExecutionIR` per body path, reused VERBATIM from Task 6
    so this module and that one cannot disagree about what an expression is.

    ``output_policy`` is Child-1's object, carried by reference (see the module docstring).

    ``authoring_run_id`` is PROVENANCE and is deliberately outside :meth:`identity_payload`: the
    same governed artifact authored twice is one computation, and letting the run id in would split
    a materialization group by who happened to author its members.
    """

    feature_name: str
    formula_content_hash: str
    final_operation: FinalOperation
    zero_denominator: ZeroDenominator | None
    grain_entity: str
    grain_keys: tuple[str, ...]
    expressions: tuple[ExpressionExecutionIR, ...]
    spine: SpineSpec
    output_policy: FormulaOutputPolicyV1
    authoring_run_id: str

    def identity_payload(self) -> dict[str, Any]:
        """What this feature IS — no provenance, no run-time value.

        Expressions enter ordered by their BODY PATH, never by tuple position. Each entry already
        carries its own path, so which half of a ratio the tuple happens to hold first is not a fact
        about the computation — and Task 6 found the matching defect one level down, where ordering
        by the sequence in which tables happened to RESOLVE silently changed the hash.

        ``formula_content_hash`` already covers the formula-side fields repeated below (the body
        shape, the grain, the output policy). They are repeated because the IR is read on its own
        by later stages, and an identity that could only be interpreted by fetching the formula
        would push every reader back to the object this one exists to summarize.

        Deliberately TOTAL: no parsing, no catalog read, nothing that can raise. It is called while
        building a hash, where an exception would be indistinguishable from a governed refusal.
        """
        return {
            "feature_name": self.feature_name,
            "formula_content_hash": self.formula_content_hash,
            "final_operation": self.final_operation.value,
            "zero_denominator": (None if self.zero_denominator is None
                                 else self.zero_denominator.value),
            "grain_entity": self.grain_entity,
            "grain_keys": list(self.grain_keys),
            "expressions": [expression.identity_payload()
                            for expression in sorted(self.expressions,
                                                     key=lambda e: e.expr_path)],
            "spine": self.spine.identity_payload(),
            "output_policy": {
                "output_type": self.output_policy.output_type,
                "unit": self.output_policy.unit,
                "currency": self.output_policy.currency,
                "output_additivity": self.output_policy.output_additivity.value,
                "external_type_required": self.output_policy.external_type_required,
            },
        }


def ir_hash(ir: FormulaExecutionIRV1) -> str:
    """The feature's content identity — ``materialize_hash`` is the one hasher (§14)."""
    return materialize_hash(ir.identity_payload())


@dataclass(frozen=True, slots=True)
class AuthorizedCompilation:
    """The TOKEN Gate 2 issues, and the only way into §2's downstream chain.

    A boolean would be a suggestion; a token is a precondition a later stage can require in its own
    signature. There is no partial variant and no per-feature field, because §1.3 admits neither:
    the group is authorized as one thing or refused as one thing.

    ``authorized_refs`` is the complete union that was checked, sorted and de-duplicated — the same
    read set §5.2's classification is derived over, recorded here so that derivation does not have
    to re-walk (and possibly re-derive differently) what the gate already assembled.
    """

    irs: tuple[FormulaExecutionIRV1, ...]
    spine: SpineSpec
    authorized_refs: tuple[str, ...]
    roles_used: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BridgeExecutionAuthorization:
    """Final control-plane check for the exact bridge revisions a run will execute.

    Compilation and rendering may take long enough for a bridge, its exact observation or one of
    its physical bindings to become stale.  This token is therefore minted separately, immediately
    before run preparation.  It is bound to the compiled IR hashes, environment and exact
    ``(realization_revision_id, dependency_snapshot_id)`` pairs.  Human review is deliberately not
    represented: deterministic execution safety, rather than endorsement, is the gate.
    """

    ir_hashes: tuple[str, ...]
    environment_id: str
    realization_dependencies: tuple[tuple[str, str], ...]


# ── compilation ──────────────────────────────────────────────────────────────────────────────────


def _refuse(code: CompilationRefusalCode, detail: str) -> MaterializationRefused:
    return MaterializationRefused(code, detail)


def _fold(value: str) -> str:
    return value.strip().lower()


def compile_ir(
    conn: DbConn,
    admitted: AdmittedFeature,
    *,
    roles: Iterable[str] = (),
    spine_decl: SpineSourceDeclarationV1 | None,
    inventory: ClusterInventoryV1,
    bridge_realizations: tuple[CurrentBridgeRealizationV1, ...] | None = None,
) -> FormulaExecutionIRV1 | MaterializationRefused:
    """Compile ONE admitted feature into its complete executable plan, or refuse it (§2).

    A refusal is RETURNED, not raised: a refused feature is one governed verdict among the many a
    compilation collects. Checks run in this order, and the FIRST failure decides the code:

    1. the spine declaration, validated by Task 4 against the governed facts — a group with no
       attested population has nothing for its features to land on, so it is answered first;
    2. the formula's grain ENTITY against the population's;
    3. every body expression, in Child-1's own path order, via ``compile_expression``.

    Only the reported code depends on that order; every branch refuses.

    The declaration is validated on every call rather than once per group. §4 requires it to be
    declared once per materialization CONTRACT, and that is enforced where the group exists — in
    :func:`authorize_compilation`, which refuses to authorize IRs compiled against a population
    other than the one it was handed.

    Raises:
        featuregen.formula.schema.SchemaError: ``formula.body`` is not one of Child-1's three body
            shapes. A body outside the discriminated union is a forged object rather than a governed
            verdict, and §14's closed vocabulary has no member for it — the line ``plan_join`` draws
            for a cross-catalog identity.
    """
    roles_used = tuple(roles)
    if bridge_realizations is None:
        # This is the production compilation entry point.  Callers may inject a frozen set in
        # deterministic unit tests, but omission must not mean "there are no bridges": that would
        # make every cross-catalog formula fail even though a current executable realization
        # exists, and would encourage callers to bypass the typed reader.
        bridge_realizations = executable_bridge_realizations(
            conn,
            purpose="feature_generation",
            environment=inventory.environment_id,
        )
    spine = validate_spine_declaration(conn, spine_decl, roles=roles_used)
    if isinstance(spine, MaterializationRefused):
        return spine

    formula = admitted.formula
    if _fold(formula.grain.entity) != _fold(spine.entity):
        return _refuse(
            CompilationRefusalCode.GRAIN_PATH_NOT_GOVERNED,
            f"the feature is computed at {formula.grain.entity!r} grain while the declared "
            f"population is {spine.entity!r} ({spine.source_table_ref}): the rows would be keyed by "
            f"something the spine does not contain, so there is no governed path from this "
            f"feature's grain to that population")

    expressions: list[ExpressionExecutionIR] = []
    for expr_path, expr in body_expressions(formula.body):
        compiled = compile_expression(
            conn, expr_path=expr_path, expr=expr, grain_keys=formula.grain.keys, roles=roles_used,
            inventory=inventory, bridge_realizations=bridge_realizations)
        if isinstance(compiled, MaterializationRefused):
            return compiled
        expressions.append(compiled)

    body = formula.body
    return FormulaExecutionIRV1(
        feature_name=admitted.feature_name,
        formula_content_hash=admitted.formula_content_hash,
        final_operation=body.final_operation,
        # Only a ratio has one, and it changes the NUMBER a zero denominator produces (§6), so it
        # is carried rather than left for a renderer to default. `None` states "not a ratio" — it
        # is never a defaulted policy.
        zero_denominator=body.zero_denominator if isinstance(body, RatioBody) else None,
        grain_entity=formula.grain.entity,
        grain_keys=tuple(formula.grain.keys),
        expressions=tuple(expressions),
        spine=spine,
        output_policy=formula.output,
        authoring_run_id=admitted.authoring_run_id)


# ── Gate 2 (§1.3) ────────────────────────────────────────────────────────────────────────────────


def bridge_realization_dependencies(
    irs: Sequence[FormulaExecutionIRV1],
) -> tuple[tuple[str, str], ...]:
    dependencies = {
        (step.realization_revision_id, step.dependency_snapshot_id)
        for ir in irs
        for expression in ir.expressions
        for step in expression.join_plan.steps
        if isinstance(step, CrossCatalogJoinStepV1)
    }
    return tuple(sorted(dependencies))


def authorize_execution_realizations(
    conn: DbConn,
    authorized: AuthorizedCompilation,
    *,
    environment_id: str,
) -> BridgeExecutionAuthorization | MaterializationRefused:
    """Revalidate every exact directional realization immediately before run preparation.

    The check resolves by revision, not by symmetric bridge fact or endpoint names.  If a current
    pointer advanced, a dependency changed, exact evidence expired, or the bridge lifecycle closed
    after compilation, the old revision is refused rather than silently replaced with a newer
    realization that was never rendered into this artifact.
    """
    if not isinstance(authorized, AuthorizedCompilation):
        raise TypeError(
            "authorize_execution_realizations requires Gate 2's AuthorizedCompilation")
    if not isinstance(environment_id, str) or not environment_id.strip():
        raise ValueError("environment_id must not be blank")

    required = bridge_realization_dependencies(authorized.irs)
    if required:
        current_by_dependency = {
            (
                realization.revision.realization_revision_id,
                realization.revision.dependency_snapshot_id,
            ): realization
            for realization in load_current_bridge_realizations(conn)
        }
        for dependency in required:
            realization = current_by_dependency.get(dependency)
            if realization is None:
                return _refuse(
                    CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
                    "the compiled directional bridge realization is no longer the current "
                    f"revision: {dependency[0]} / {dependency[1]}",
                )
            assessment = revalidate_bridge_realization(
                conn,
                realization,
                purpose="feature_generation",
                environment=environment_id,
            )
            if not assessment.executable:
                return _refuse(
                    CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
                    f"directional bridge realization {dependency[0]} failed final execution "
                    f"revalidation: {', '.join(assessment.reason_codes)}",
                )

    return BridgeExecutionAuthorization(
        ir_hashes=tuple(sorted(ir_hash(ir) for ir in authorized.irs)),
        environment_id=environment_id,
        realization_dependencies=required,
    )


@dataclass(frozen=True, slots=True)
class _ReadElement:
    """One node of the group's complete physical read set, with every reason it is read."""

    logical_ref: str
    catalog_source: str
    object_ref: str
    kinds: tuple[ReadElementKind, ...]


class _Union:
    """The group's read set under construction: one entry per NODE, accumulating kinds.

    Keyed by the catalog-side ``(catalog_source, object_ref)`` pair rather than by the logical ref,
    because that pair is what addresses a ``graph_node`` — and a node authorized under one spelling
    and read under another is a node nobody authorized.
    """

    def __init__(self) -> None:
        self._kinds: dict[tuple[str, str], list[ReadElementKind]] = {}
        self._refs: dict[tuple[str, str], str] = {}

    def add(self, logical_ref: str, kind: ReadElementKind) -> None:
        source, schema, table, column = parse_ref(logical_ref)
        key = (_fold(source), _fold(".".join([schema, table, *([column] if column else [])])))
        kinds = self._kinds.setdefault(key, [])
        if kind not in kinds:
            kinds.append(kind)
        self._refs.setdefault(key, logical_ref)

    def elements(self) -> tuple[_ReadElement, ...]:
        return tuple(
            _ReadElement(logical_ref=self._refs[key], catalog_source=key[0], object_ref=key[1],
                         kinds=tuple(kinds))
            for key, kinds in self._kinds.items())


def _catalog_of(expression: ExpressionExecutionIR) -> str | None:
    """The catalog a compiled expression reads in.

    Taken from the expression's OWN read set — the source relation's entry for preference — because
    the join planner is single-catalog and a step ref carries no source of its own. ``None`` only
    when the read set is empty, which is a malformed IR the caller assembled.
    """
    for ref in expression.physical_read_set:
        if RefRole.SOURCE_TABLE in ref.roles:
            return ref.catalog_source
    if expression.physical_read_set:
        return expression.physical_read_set[0].catalog_source
    return None


def _union_of(irs: Sequence[FormulaExecutionIRV1], spine: SpineSpec) -> tuple[_ReadElement, ...]:
    """The COMPLETE physical read set of the group (§1.3), from every structural source there is."""
    union = _Union()
    for ir in irs:
        for expression in ir.expressions:
            for ref in expression.physical_read_set:
                union.add(ref.logical_ref, ReadElementKind.EXPRESSION_READ)

            # From the PLAN, not only from the read set. Task 6 folds endpoints in as well, but
            # §1.3 names them as their own element class: a path is joined ON these columns, and a
            # gate that could only see them through one path would stop seeing them if that path
            # changed.
            if expression.join_plan.steps:
                catalog_source = _catalog_of(expression)
                if catalog_source is None:
                    raise ValueError(
                        f"{ir.feature_name}/{expression.expr_path} carries "
                        f"{len(expression.join_plan.steps)} join step(s) and an EMPTY physical read "
                        f"set, so the catalog its endpoints belong to is unknowable: the IR was "
                        f"assembled wrongly, and guessing a catalog would authorize nodes in one "
                        f"and read them in another")
                for step in expression.join_plan.steps:
                    if isinstance(step, CrossCatalogJoinStepV1):
                        for pair in step.column_pairs:
                            for endpoint in pair:
                                union.add(
                                    endpoint,
                                    ReadElementKind.JOIN_ENDPOINT,
                                )
                    else:
                        for endpoint in (step.from_ref, step.to_ref):
                            union.add(join_key_ref(catalog_source, endpoint),
                                      ReadElementKind.JOIN_ENDPOINT)

            # Every row this expression aggregates is admitted or excluded by the availability
            # column (§8 rule 1), so it is read whether or not anything else names it.
            union.add(expression.pit.availability_ref, ReadElementKind.AVAILABILITY)

    union.add(spine.source_table_ref, ReadElementKind.SPINE_SOURCE)
    for key_ref in spine.ordered_key_refs:
        union.add(key_ref, ReadElementKind.SPINE_KEY)
    for read_ref in spine.read_set:
        union.add(read_ref, ReadElementKind.SPINE_READ)
    if spine.availability_ref is not None:
        union.add(spine.availability_ref, ReadElementKind.AVAILABILITY)
    return union.elements()


def _sorted_refs(elements: Sequence[_ReadElement]) -> tuple[str, ...]:
    """The elements' logical refs, sorted and de-duplicated — ONE expression of "what was read"."""
    return tuple(sorted({element.logical_ref for element in elements}))


def physical_read_set(
    irs: Sequence[FormulaExecutionIRV1], spine: SpineSpec
) -> tuple[str, ...]:
    """§1.3's COMPLETE physical read set for ``irs`` over ``spine``, as sorted logical refs.

    The same union Gate 2 authorizes, exposed because §5.2 classifies a read set PER FEATURE and so
    needs it for a single IR — ``physical_read_set((ir,), ir.spine)`` — which the group-wide
    authorization does not produce. Deriving it a second way in the classification stage would give
    the group two answers to "what does this feature read", and the narrower answer would be the one
    the sensitivity class was computed from.

    Raises:
        ValueError: an IR carries join steps with an empty read set (see
            :func:`authorize_compilation`).
    """
    return _sorted_refs(_union_of(irs, spine))


def _hidden(
    conn: DbConn, elements: Sequence[_ReadElement], roles: tuple[str, ...]
) -> tuple[_ReadElement, ...]:
    """The elements the supplied read scope may not see.

    The shipped read-scope rule, inherited rather than re-implemented (``read_scope.py``, and the
    same shape ``spine._resolve_nodes`` applies): a node's ``sensitivity`` tag is visible only to a
    caller whose roles grant it, and an UNTAGGED node is visible to everyone. The tag is compared
    without folding, so a tag outside ``SENSITIVITY_ROLES`` — including one that differs only in
    case — is granted by no role and fails CLOSED.

    A ref with no ``graph_node`` row at all is treated as untagged, i.e. authorized. It carries no
    tag to hide behind, so nothing sensitive is being let through; what it is, is a read of a column
    the catalog does not describe, which §11's L1 validation reports as ``COLUMN_ABSENT`` against
    the live metastore. Refusing it here would report a missing column as an insufficient role and
    send an operator to request a privilege that would not help.
    """
    allowed = allowed_sensitivities(roles)
    by_source: dict[str, dict[str, _ReadElement]] = {}
    for element in elements:
        by_source.setdefault(element.catalog_source, {})[element.object_ref] = element

    hidden: list[_ReadElement] = []
    for catalog_source, indexed in by_source.items():
        rows = conn.execute(
            "SELECT lower(object_ref), sensitivity FROM graph_node "
            "WHERE catalog_source = %s AND lower(object_ref) = ANY(%s)",
            (catalog_source, list(indexed))).fetchall()
        for object_ref, sensitivity in rows:
            if sensitivity is not None and sensitivity not in allowed:
                hidden.append(indexed[object_ref])
    return tuple(sorted(hidden, key=lambda element: element.logical_ref))


def authorize_compilation(
    conn: DbConn,
    irs: Sequence[FormulaExecutionIRV1],
    spine: SpineSpec,
    *,
    roles: Iterable[str] = (),
) -> AuthorizedCompilation | MaterializationRefused:
    """Gate 2 — authorize the GROUP's complete physical read set, or refuse the whole compilation.

    One denied element anywhere returns a single ``READ_SCOPE_INSUFFICIENT``. Nothing partial is
    returned, so no contract, group plan or project can be derived from a refused group: the
    downstream chain takes an :class:`AuthorizedCompilation`, and a refusal is not one.

    Raises:
        ValueError: the group is empty, an IR was compiled against a different population than the
            supplied spine, or an IR carries join steps with no read set. All three are calls
            assembled wrongly rather than governed verdicts — §14's closed vocabulary has no member
            for that, and the alternatives are worse than an exception: an authorization token over
            no features is a permit for nothing, and a group authorized against a population its
            members were not compiled over would authorize reads nobody performs while performing
            reads nobody authorized.
    """
    group = tuple(irs)
    if not group:
        raise ValueError(
            "authorize_compilation was called with no features: an authorization token over an "
            "empty group is a permit for nothing, and the next stage cannot tell it apart from a "
            "group that was genuinely authorized")

    declared = spine.identity_payload()
    mismatched = [ir.feature_name for ir in group if ir.spine.identity_payload() != declared]
    if mismatched:
        raise ValueError(
            f"{len(mismatched)} of {len(group)} IRs were compiled against a different spine "
            f"declaration than the one supplied ({', '.join(sorted(mismatched))}): §4 declares the "
            f"population once per materialization contract, and authorizing one population while "
            f"the features read another authorizes reads nobody performs")

    roles_used = tuple(roles)
    elements = _union_of(group, spine)
    hidden = _hidden(conn, elements, roles_used)
    if hidden:
        described = ", ".join(
            f"{element.logical_ref} ({'+'.join(kind.value for kind in element.kinds)})"
            for element in hidden)
        return _refuse(
            CompilationRefusalCode.READ_SCOPE_INSUFFICIENT,
            f"the supplied read scope hides {len(hidden)} of {len(elements)} elements of the "
            f"group's complete physical read set across {len(group)} feature(s): {described}. "
            f"Gate 2 is group-wide: a group is published as one row per key, so one unreadable "
            f"element refuses the whole compilation rather than dropping a feature — there is no "
            f"partial authorization and no per-feature bypass")

    return AuthorizedCompilation(
        irs=group, spine=spine, authorized_refs=_sorted_refs(elements), roles_used=roles_used)
