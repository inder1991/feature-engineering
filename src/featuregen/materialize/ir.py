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
refused" would assert false semantics. What must fail is the GROUP OPERATION: one ref the governed
catalog does not describe anywhere returns a single ``COLUMN_NOT_GOVERNED`` (existence is decided
first — Task 12), one denied element anywhere returns a single ``READ_SCOPE_INSUFFICIENT``, and in
either case **no contract, group plan or project is produced**. There is no partial authorization and no per-feature bypass — which is why the success
value is a TOKEN (:class:`AuthorizedCompilation`) rather than a boolean: a downstream stage that
takes the token cannot be entered by a caller who skipped the gate.

**Read scope is the shipped predicate over BOTH sensitivity axes (migration 1032).** Gate 2 checks
``graph_node.visible_requires`` — the GENERATED column folding the raw file tag AND the governed
``effective_restriction`` floor into the classes a reader must hold — against
``allowed_classes(roles)``, via ``read_scope.materialization_anchor_visibility_predicate``. So a
governed-``restricted`` column whose file attested nothing refuses here, and a ``prohibited`` floor
is ungrantable at this gate — on a TABLE element as well as on a column one. §5.2 classification
still owns the contract-time answer: it re-reads the catalog and refuses a ``prohibited`` input with
``PROHIBITED_INPUT`` even for a group authorized before the ruling.

A TABLE-AWARE predicate and not the plain column one, because this read set is not all columns: it
carries the spine's own TABLE, and ``build_graph`` writes no sensitivity on a table node — so under
the column predicate the table half of this gate passed for every caller on every real catalog.
D11's derived rule (a table is visible iff the caller can see at least one of its columns) answers
that half. It is not the whole answer at THIS gate: D11's shared predicate SUBSTITUTES the derived
answer for the table row's own ``visible_requires``, and naming a table in a generated project is a
grant to SCAN it, so a governed floor set on the table itself would be discarded by exactly the
element class it was set on. Gate 2 therefore binds the conjunctive variant — the table's own
requirement AND a visible column — which branches on ``graph_node.kind`` itself, so ONE predicate
still covers both element kinds without minting a second rule here. See :func:`_hidden`.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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
from featuregen.materialize.joins import CrossCatalogJoinStepV1, CrosswalkJoinStepV1
from featuregen.materialize.spine import (
    SpineSourceDeclarationV1,
    SpineSpec,
    validate_spine_declaration,
)
from featuregen.overlay.upload.bridge_realization import (
    AdditionalKeyRequirementV1,
    AsOfIntervalRequirementV1,
    ExecutionTier,
    FixedValueReferencePredicateV1,
)
from featuregen.overlay.upload.bridge_store import (
    CurrentBridgeRealizationV1,
    executable_bridge_realizations,
    load_current_bridge_realizations,
    revalidate_bridge_realization,
)
from featuregen.overlay.upload.crosswalk_admission import AdmittedCrosswalkV1
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.read_scope import (
    allowed_classes,
    materialization_anchor_visibility_predicate,
)

__all__ = [
    "AuthorizedCompilation",
    "BridgeExecutionAuthorization",
    "FormulaExecutionIRV1",
    "ReadElementKind",
    "authorize_compilation",
    "authorize_execution_realizations",
    "bridge_realization_dependencies",
    "compile_ir",
    "crosswalk_execution_pins",
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
    JOIN_PREDICATE = "join_predicate"     # a column a step's own filter is applied to
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


#: The one purpose every read on this path is for. It is a value the applicability scope is checked
#: against, not a switch: a realization approved for another purpose is not approved for this one.
_BRIDGE_PURPOSE = "feature_generation"


def _executable_realizations_for_tier(
    conn: DbConn, *, environment_id: str, execution_tier: ExecutionTier,
) -> tuple[CurrentBridgeRealizationV1, ...]:
    """Every current realization this environment may execute AT THIS applicability tier.

    ``ExecutionTier`` scopes whether a directional bridge realization is approved for sandbox or
    production DATA (``bridge_realization.py:81``). It is emphatically **not** a run execution tier:
    there is one namespace here, it is baked into ``sandbox_execution_hash``, and nothing on this
    path mints or reads a second one.

    ``executable_bridge_realizations`` is the typed production reader and STAYS the production path
    — it fixes the tier at ``PRODUCTION`` in its own body and ``overlay/upload/`` is not this
    module's to change. Any other tier composes the same two typed readers with the tier passed
    through, which is that reader's own body and not a looser one: the realizations still have to be
    CURRENT and still have to pass full execution revalidation. Nothing here ever falls back to link
    availability, which is discovery evidence rather than execution authority.
    """
    if execution_tier is ExecutionTier.PRODUCTION:
        return executable_bridge_realizations(
            conn, purpose=_BRIDGE_PURPOSE, environment=environment_id)
    assessments = (
        revalidate_bridge_realization(
            conn, realization, purpose=_BRIDGE_PURPOSE, environment=environment_id,
            execution_tier=execution_tier)
        for realization in load_current_bridge_realizations(conn))
    return tuple(assessment.realization for assessment in assessments if assessment.executable)


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
    crosswalks: tuple[AdmittedCrosswalkV1, ...] = (),
    execution_tier: ExecutionTier = ExecutionTier.PRODUCTION,
    plan_envelope: Mapping[str, Any] | None = None,
) -> FormulaExecutionIRV1 | MaterializationRefused:
    """Compile ONE admitted feature into its complete executable plan, or refuse it (§2).

    A refusal is RETURNED, not raised: a refused feature is one governed verdict among the many a
    compilation collects. Checks run in this order, and the FIRST failure decides the code:

    1. the spine declaration, validated by Task 4 against the governed facts — a group with no
       attested population has nothing for its features to land on, so it is answered first;
    2. the formula's grain ENTITY against the population's;
    3. every body expression, in Child-1's own path order, via ``compile_expression``;
    4. **the frozen plan envelope, AFTER the IR is complete** (D-4, task B3) — see
       :func:`_validate_plan_envelope`. It runs last because everything it compares (the resolved
       read set, the compiled PIT window, the validated population) exists only once the first
       three have produced an IR.

    Only the reported code depends on that order; every branch refuses.

    ``plan_envelope`` is the plan the option was SERVED with, frozen at generation
    (``recipe_planning_lens.fold_frozen_binding_plan`` → ``semantic_option_decision.binding_plan``,
    migration 1066 → the work item, migration 1068). Compilation does not consume it as an input:
    it derives every answer exactly as it did before and then **refuses on divergence**, never
    substitutes. ``None`` — every run whose work item predates B2, and every direct caller — compiles
    byte-for-byte as before.

    The declaration is validated on every call rather than once per group. §4 requires it to be
    declared once per materialization CONTRACT, and that is enforced where the group exists — in
    :func:`authorize_compilation`, which refuses to authorize IRs compiled against a population
    other than the one it was handed.

    ``execution_tier`` is the bridge-realization APPLICABILITY scope this compilation reads at — is
    the join approved for production data, or only for sandbox data. It is not a run tier and it
    changes no identity: see :func:`_executable_realizations_for_tier`. It defaults to
    ``PRODUCTION``, which is what this entry point silently asserted before it was a parameter, so
    an existing caller compiles against exactly the realizations it always did. It is ignored when
    ``bridge_realizations`` is injected, because then no read happens at all.

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
        bridge_realizations = _executable_realizations_for_tier(
            conn,
            environment_id=inventory.environment_id,
            execution_tier=execution_tier,
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
            inventory=inventory, bridge_realizations=bridge_realizations,
            crosswalks=crosswalks, execution_tier=execution_tier)
        if isinstance(compiled, MaterializationRefused):
            return compiled
        expressions.append(compiled)

    body = formula.body
    compiled_ir = FormulaExecutionIRV1(
        feature_name=admitted.feature_name,
        formula_content_hash=admitted.formula_content_hash,
        final_operation=body.final_operation,
        # Only a ratio has one, and it changes the NUMBER a zero denominator produces (§6), so it
        # is carried rather than left for a renderer to default. `None` states "not a ratio" — it
        # is never a defaulted policy.
        zero_denominator=body.zero_denominator if isinstance(body, RatioBody) else None,
        grain_entity=formula.grain.entity,
        # Folded for the same reason `compile_expression` folds its refs: `formula_content_hash`
        # folds case, so a raw spelling here would fork one governed formula into two `ir_hash`es —
        # and the renderer's `_grain_key_columns` compares these against the (folded) expression
        # read set and the `hive_identifier`-lowered landing keys, which a raw spelling can match
        # neither of.
        grain_keys=tuple(_fold(key) for key in formula.grain.keys),
        expressions=tuple(expressions),
        spine=spine,
        output_policy=formula.output,
        authoring_run_id=admitted.authoring_run_id)
    divergence = _validate_plan_envelope(compiled_ir, plan_envelope)
    return compiled_ir if divergence is None else divergence


# ── D-4: the frozen plan envelope is CONSUMED, never re-derived (task B3) ────────────────────────


def _envelope_ref(catalog_source: str, ref: str) -> tuple[str, str, str, str]:
    """One ref as ``(source, schema, table, column)``, case-folded — from EITHER vocabulary.

    The two sides do not speak the same dialect and pretending they do is how this check would
    quietly pass everything. The compiler emits governed LOGICAL refs
    (``bank::public.txns.txn_amt``); the frozen envelope's ``read_set`` is the semantic context's
    OBJECT refs (``public.txns.txn_amt``) with the catalog carried once, beside them, as
    ``catalog_source`` — which is exactly why ``assemble_current_activation_state`` has to
    ``normalize_ref(frozen.plan_catalog_source, *ref.split('.')[-3:])`` before it can ask anything
    about them. Both shapes are accepted, and the LAST three path segments are what name the
    column, so a ref that carries its own source is not read as one whose schema is ``bank::public``.
    A RELATION ref (``bank::public.txns``) folds to an empty ``column`` rather than being shifted
    one segment left — read as a column it would name a table called ``txns`` in a schema called
    ``public``, and would then compare equal to nothing and unequal to everything.
    """
    source, sep, path = ref.partition("::")
    if not sep:
        source, path = catalog_source, ref
    parts = [part.strip().lower() for part in path.split(".")]
    if len(parts) >= 3:
        schema, table, column = parts[-3], parts[-2], parts[-1]
    elif len(parts) == 2:
        schema, table, column = parts[0], parts[1], ""
    else:
        schema, table, column = "", parts[0], ""
    return source.strip().lower(), schema, table, column


def _allowed_additions(ir: FormulaExecutionIRV1, catalog_source: str) -> set[tuple[str, ...]]:
    """The refs compilation legitimately reads that the human's plan never named.

    The comparison is a SUBSET, not an equality, and this function is the whole reason it is safe
    to be one — it says precisely which additions are permitted, so a join hop, a bridge endpoint
    or a filter column the human never saw is a divergence rather than an unremarked widening.

    Exactly three classes, and all three are structural rather than incidental:

    * **the SPINE's own columns.** ``fold_frozen_binding_plan``'s read set is the bound operand
      refs — the columns the recipe's roles resolved to. The population is a separate governed
      declaration (§4) validated at compile time, and its source relation, ordered keys, read set
      and availability column are read by every group compiled against it. The envelope names the
      population by table (``population_ref``), never by column, so its columns cannot be in the
      read set and their absence is not evidence of anything.
    * **each expression's AVAILABILITY column** (§8 rule 1). Every row an expression aggregates is
      admitted or excluded by it, so it is read whether or not anything else names it — and it is a
      governed catalog FACT about the table, not a role the recipe bound, so the semantic engine
      never had it to freeze.
    * **each expression's SOURCE RELATION** — the table itself, which enters the read set as a
      relation element (``RefRole.SOURCE_TABLE``) while the envelope names it once, by table, as
      ``source_table``. Comparing it here would ask the same question twice in two vocabularies;
      it is admission's check 7 that owns it, and that check has already run by the time an IR
      exists.
    """
    spine = ir.spine
    extra = {spine.source_table_ref, *spine.ordered_key_refs, *spine.read_set}
    if spine.availability_ref is not None:
        extra.add(spine.availability_ref)
    for expression in ir.expressions:
        extra.add(expression.pit.availability_ref)
        extra.update(ref.logical_ref for ref in expression.physical_read_set
                     if RefRole.SOURCE_TABLE in ref.roles)
    return {_envelope_ref(catalog_source, ref) for ref in extra}


def _validate_plan_envelope(
    ir: FormulaExecutionIRV1, envelope: Mapping[str, Any] | None
) -> MaterializationRefused | None:
    """``None``, or the divergence between what was compiled and what the human was shown (D-4).

    ⚠️ **NOTHING HERE SUBSTITUTES.** The IR is already complete when this runs; every check reads
    it and compares, and the only outcome other than ``None`` is a refusal naming BOTH sides. That
    is the same law ``fold_frozen_binding_plan`` applies to itself with ``BINDING_PLAN_DIVERGENCE``
    and ``govern.py`` applies at the governing write.

    ⚠️ **AN ABSENT ENVELOPE, AND AN ABSENT FIELD, ASSERT NOTHING.** A run with no envelope compiles
    exactly as it did before B3; a field the envelope left blank (``window`` is ``None`` for a recipe
    with no window parameter, ``population_ref`` is absent on an intent that declared none) is not
    a claim that compilation must match. Reading absence as an assertion would refuse the majority
    of genuine features for having been served by an earlier build.

    **What is NOT checked, and why — a correction to the task as authored.** B3 asks that "the
    compiled ``PitSpec``'s rendered clause must match ``envelope['pit']``". There is no such clause:
    ``PitSpec`` carries structured fields and renders no text anywhere in ``materialize/``, while
    ``envelope['pit']`` is the human-facing PROSE ``recipe_temporal_v2.compile_temporal`` writes
    (*"trailing 90d observation window over posting_ts events: (cutoff − 90d, cutoff], values
    knowable strictly at or before the cutoff"*). The two are not two spellings of one thing, and
    re-parsing that prose to manufacture a comparison would be the second derivation D-4 exists to
    forbid. What the two sides genuinely share is the WINDOW, which the envelope froze as a number,
    and that is compared below.
    """
    if not envelope:
        return None
    catalog_source = str(envelope.get("catalog_source") or "")

    declared_read_set = envelope.get("read_set") or ()
    if declared_read_set:
        frozen = {_envelope_ref(catalog_source, str(ref)) for ref in declared_read_set}
        compiled = {_envelope_ref(catalog_source, ref)
                    for ref in physical_read_set([ir], ir.spine)}
        unexplained = compiled - frozen - _allowed_additions(ir, catalog_source)
        if unexplained:
            return _refuse(
                CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE,
                f"{ir.feature_name} compiles to read "
                f"{sorted('.'.join(part for part in ref if part) for ref in unexplained)}, which "
                f"the frozen plan's read set does not name and which is neither a spine column nor "
                f"an availability gate: the human approved reading "
                f"{sorted(str(ref) for ref in declared_read_set)} in catalog "
                f"{catalog_source!r}")

    declared_population = str(envelope.get("population_ref") or "").strip().lower()
    if declared_population:
        _source, _schema, table, _column = _envelope_ref(
            catalog_source, ir.spine.source_table_ref)
        if table != declared_population:
            return _refuse(
                CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE,
                f"{ir.feature_name} lands on the population "
                f"{ir.spine.source_table_ref!r}, but the frozen plan the option was served with "
                f"declares population_ref={envelope.get('population_ref')!r}")

    declared_window = envelope.get("window")
    if isinstance(declared_window, int) and not isinstance(declared_window, bool):
        for expression in ir.expressions:
            if expression.pit.window_length != declared_window:
                return _refuse(
                    CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE,
                    f"{ir.feature_name}/{expression.expr_path} compiles a window of "
                    f"{expression.pit.window_length} {expression.pit.window_unit}, but the frozen "
                    f"plan the option was served with declares window={declared_window!r} "
                    f"({envelope.get('pit')!r})")
    return None


# ── Gate 2 (§1.3) ────────────────────────────────────────────────────────────────────────────────


def bridge_realization_dependencies(
    irs: Sequence[FormulaExecutionIRV1],
) -> tuple[tuple[str, str], ...]:
    """Every exact ``(realization_revision_id, dependency_snapshot_id)`` this group would execute.

    A CROSSWALK leg contributes here too, and that is what extends the
    :func:`authorize_execution_realizations` doctrine to crosswalk pins for free: a
    ``JoinLegPinV1`` that resolved through a governed bridge realization pins that realization and
    its dependency snapshot by side, so a leg whose current pointer moved between compilation and
    run preparation is refused by exactly the check a direct bridge is refused by. A same-catalog
    leg pins neither (``JoinLegPinV1`` refuses the pretence that every leg is an entity bridge) and
    so contributes nothing — its staleness is caught by the row-selection and observation pins the
    step carries instead.

    The two tuples are paired POSITIONALLY, which is the pin's own contract:
    ``dependency_snapshot_ids`` holds "one singular value per realization it pinned".
    """
    dependencies: set[tuple[str, str]] = set()
    for ir in irs:
        for expression in ir.expressions:
            for step in expression.join_plan.steps:
                if isinstance(step, CrossCatalogJoinStepV1):
                    dependencies.add(
                        (step.realization_revision_id, step.dependency_snapshot_id))
                elif isinstance(step, CrosswalkJoinStepV1):
                    dependencies.update(zip(
                        step.leg_realization_revision_ids,
                        step.leg_dependency_snapshot_ids,
                        strict=True))
    return tuple(sorted(dependencies))


def crosswalk_execution_pins(
    irs: Sequence[FormulaExecutionIRV1],
) -> tuple[tuple[str, str, str, str, str], ...]:
    """Every pinned crosswalk revision this group would execute, deduplicated and sorted.

    One tuple per two-leg traversal, in the order
    ``(execution, definition, mapping binding, mapping temporal policy, composed observation)``.
    Both legs of one crosswalk carry the same five values (they are the execution-level pins, not
    per-leg ones), so a two-step plan contributes one tuple rather than two.

    A blank stands for a pin the crosswalk legitimately does not carry — a mapping table with no
    declared temporal policy has no policy revision, and recording an empty string says that,
    where omitting the slot would let a reader mistake it for a different pin's value.
    """
    pins = {
        (
            step.crosswalk_execution_revision_id,
            step.crosswalk_definition_revision_id,
            step.mapping_binding_revision_id,
            step.mapping_temporal_policy_revision_id or "",
            step.composed_observation_revision_id or "",
        )
        for ir in irs
        for expression in ir.expressions
        for step in expression.join_plan.steps
        if isinstance(step, CrosswalkJoinStepV1)
    }
    return tuple(sorted(pins))


def authorize_execution_realizations(
    conn: DbConn,
    authorized: AuthorizedCompilation,
    *,
    environment_id: str,
    execution_tier: ExecutionTier = ExecutionTier.PRODUCTION,
) -> BridgeExecutionAuthorization | MaterializationRefused:
    """Revalidate every exact directional realization immediately before run preparation.

    The check resolves by revision, not by symmetric bridge fact or endpoint names.  If a current
    pointer advanced, a dependency changed, exact evidence expired, or the bridge lifecycle closed
    after compilation, the old revision is refused rather than silently replaced with a newer
    realization that was never rendered into this artifact.

    ``execution_tier`` is the applicability scope this run is authorized AT, and must be the one the
    compilation read at — a realization approved only for sandbox data is refused here at the
    ``PRODUCTION`` default with ``realization_execution_tier_mismatch``, which is the correct answer
    and not the bug. The bug was that it was the ONLY answer.
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
                purpose=_BRIDGE_PURPOSE,
                environment=environment_id,
                execution_tier=execution_tier,
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


def _step_predicate_refs(step: Any) -> tuple[str, ...]:
    """Every COLUMN a join step's own predicates are applied to, in the step's own address grammar.

    The vocabularies are closed and owned elsewhere — ``bridge_realization``'s three structured
    predicates and ``joins.MappingRowPredicateV1`` — so this reads them, it does not re-derive
    them. An unknown member raises rather than returning nothing: silently authorizing no column
    for a predicate this gate has never been taught is how a governance extension loses effect.
    """
    if isinstance(step, CrosswalkJoinStepV1):
        return tuple(predicate.column_ref for predicate in step.mapping_row_predicates)
    if not isinstance(step, CrossCatalogJoinStepV1):
        return ()
    refs: list[str] = []
    for predicate in step.predicates:
        if isinstance(predicate, FixedValueReferencePredicateV1):
            refs.append(predicate.logical_column_ref)
        elif isinstance(predicate, AsOfIntervalRequirementV1):
            refs.extend((predicate.effective_from_ref, predicate.effective_to_ref))
        elif isinstance(predicate, AdditionalKeyRequirementV1):
            refs.extend((predicate.from_logical_column_ref, predicate.to_logical_column_ref))
        else:  # pragma: no cover - the realization constructor closes the vocabulary
            raise AssertionError(
                f"unknown structured bridge predicate {type(predicate).__name__}: Gate 2 cannot "
                f"authorize the columns of a predicate it has never been taught")
    return tuple(refs)


def _union_of(irs: Sequence[FormulaExecutionIRV1], spine: SpineSpec) -> tuple[_ReadElement, ...]:
    """The COMPLETE physical read set of the group (§1.3), from every structural source there is."""
    return _union_elements([(ir.feature_name, ir.expressions) for ir in irs], spine)


def _union_elements(
    features: Sequence[tuple[str, Sequence[ExpressionExecutionIR]]], spine: SpineSpec,
) -> tuple[_ReadElement, ...]:
    """The same union over ``(feature_name, expressions)`` pairs — the LANGUAGE-NEUTRAL core.

    Split out so the V2 execution boundary unions a group's reads through THIS walk rather than a
    second one of its own. ``physical_read_set``'s warning — that deriving a read set twice gives
    the group two answers to "what does this feature read", and the narrower answer is the one the
    sensitivity class gets computed from — does not stop being true because the second deriver is a
    different formula version. Nothing here names a formula version; it sees only
    :class:`ExpressionExecutionIR`, which both boundaries reuse verbatim.
    """
    union = _Union()
    for feature_name, expressions in features:
        for expression in expressions:
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
                        f"{feature_name}/{expression.expr_path} carries "
                        f"{len(expression.join_plan.steps)} join step(s) and an EMPTY physical read "
                        f"set, so the catalog its endpoints belong to is unknowable: the IR was "
                        f"assembled wrongly, and guessing a catalog would authorize nodes in one "
                        f"and read them in another")
                for step in expression.join_plan.steps:
                    # ADDRESSED BY KIND, not by the expression's single catalog. A cross-catalog
                    # step and a crosswalk leg carry FULL governed logical refs that name their own
                    # catalog; qualifying those with `catalog_source` (the source relation's) would
                    # authorize a node in one catalog while the run reads it in another — and a
                    # crosswalk's second leg is precisely the case where the two differ, because
                    # the mapping dataset and the target endpoint need not live where the source
                    # relation does.
                    if isinstance(step, CrossCatalogJoinStepV1 | CrosswalkJoinStepV1):
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
                    # THE STEP'S PREDICATES ARE READS. A structured bridge predicate (a fixed
                    # value, an as-of interval, an additional key) and a crosswalk's mapping row
                    # rule are both applied on the cluster, so their columns are columns this group
                    # reads. Task 6 folds them into the expression read set as well; §1.3 names
                    # them here as their own element class for the same reason it names endpoints —
                    # a gate that could only see them through one path would stop seeing them the
                    # day that path changed.
                    for predicate_ref in _step_predicate_refs(step):
                        union.add(predicate_ref, ReadElementKind.JOIN_PREDICATE)

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


def physical_read_set_of(
    features: Sequence[tuple[str, Sequence[ExpressionExecutionIR]]], spine: SpineSpec,
) -> tuple[str, ...]:
    """:func:`physical_read_set` over ``(feature_name, expressions)`` pairs — version-neutral.

    The V2 execution boundary has no ``FormulaExecutionIRV1`` to hand this function and must not
    walk the expressions a second way to compensate. It unions through here: same core, same
    refusals, same sorted refs, so a V1 group and a V2 group of the same expressions produce the
    same read set by construction rather than by two implementations agreeing.
    """
    return _sorted_refs(_union_elements(features, spine))


def _hidden(
    conn: DbConn, elements: Sequence[_ReadElement], roles: tuple[str, ...]
) -> tuple[tuple[_ReadElement, ...], tuple[_ReadElement, ...]]:
    """``(hidden, missing)`` — what the read scope may not see, and what the catalog does not have.

    The shipped read-scope rule, genuinely INHERITED rather than re-implemented: one predicate over
    one parameter (``read_scope.allowed_classes``), exactly as ``read_scope.py``'s module contract
    prescribes and as every other read-scope call site binds it since migration 1032.
    ``visible_requires`` is the GENERATED column folding BOTH axes — the raw file tag AND the
    governed ``effective_restriction`` floor — so a governed-``restricted`` column whose file
    attested nothing (``sensitivity = NULL``, the shipped FTR shape) refuses here instead of
    compiling for a caller with no reader role. An untagged, unfloored node has
    ``visible_requires = '{}'``, contained in every allowed list, and stays visible to everyone;
    ``prohibited`` appears in no role map and fails CLOSED for every caller.

    **The predicate is TABLE-AWARE (D11), and that is a fix.** This gate's read set contains
    exactly ONE table element — ``SPINE_SOURCE``, the population's own relation (:func:`_union_of`)
    — alongside its columns. Every other relation a traversal touches enters as COLUMNS: a join
    step contributes its endpoint columns, and a crosswalk's mapping dataset contributes its join
    keys and its row-rule columns, each scope-checked individually. So the table branch below is
    about the spine source, and the spine source is precisely where ``build_graph`` never writes
    sensitivity: a table row's ``visible_requires`` is ``'{}'`` on every real catalog. Under the
    plain column predicate the table half of this gate therefore passed for everybody, on every
    catalog, always — a fully-restricted spine source authorized cleanly for a principal who could
    not read one of its columns.

    ``read_scope.materialization_anchor_visibility_predicate`` answers BOTH halves: the table's own
    ``visible_requires`` must be contained in the allowed list AND at least one of its columns must
    be visible (the ``catalogs.py:50-59`` derived shape D11 adopted). Conjunctive rather than
    D11's substitutive form because this gate grants a SCAN — see that function's own docstring for
    why the shared predicate is deliberately left alone. It branches on ``graph_node.kind`` itself,
    so ONE predicate serves both element kinds here and no second rule is minted, and it binds
    ``allowed_classes(roles)`` THREE times, which is the shape of the predicate rather than a
    call-site choice.

    A ref with no ``graph_node`` row at all is returned as MISSING (Task 12). The doctrine used to
    be the reverse — pass it through and let §11's L1 validation report ``COLUMN_ABSENT`` against
    the live metastore — but L1 sits on no production path, so a hallucinated column rendered
    cleanly and died on the cluster. The old objection (refusing here reported a missing column as
    an insufficient role, sending an operator after a privilege that would not help) is answered by
    a code of its own: the caller refuses missing refs as ``COLUMN_NOT_GOVERNED``, never as a role
    problem. Both verdicts come from the SAME single fetch per catalog_source — every governed row
    for the group's object refs, with its visibility — so hidden-ness and existence cannot be
    answered against two different catalog states.
    """
    allowed = allowed_classes(roles)
    by_source: dict[str, dict[str, _ReadElement]] = {}
    for element in elements:
        by_source.setdefault(element.catalog_source, {})[element.object_ref] = element

    hidden: list[_ReadElement] = []
    missing: list[_ReadElement] = []
    for catalog_source, indexed in by_source.items():
        rows = conn.execute(
            "SELECT lower(object_ref), "
            f"(NOT ({materialization_anchor_visibility_predicate('graph_node')})) AS hidden "
            "FROM graph_node "
            "WHERE catalog_source = %s AND lower(object_ref) = ANY(%s)",
            (allowed, allowed, allowed, catalog_source, list(indexed))).fetchall()
        # Fail CLOSED across duplicates: should the catalog ever hold two rows for one folded
        # object_ref, one hidden row hides the node — mirroring the old shape, where the query
        # returned hidden rows directly and any one of them sufficed.
        found: dict[str, bool] = {}
        for object_ref, is_hidden in rows:
            found[object_ref] = found.get(object_ref, False) or bool(is_hidden)
        hidden.extend(indexed[object_ref] for object_ref, is_hidden in found.items() if is_hidden)
        missing.extend(element for object_ref, element in indexed.items()
                       if object_ref not in found)
    return (tuple(sorted(hidden, key=lambda element: element.logical_ref)),
            tuple(sorted(missing, key=lambda element: element.logical_ref)))


def authorize_compilation(
    conn: DbConn,
    irs: Sequence[FormulaExecutionIRV1],
    spine: SpineSpec,
    *,
    roles: Iterable[str] = (),
) -> AuthorizedCompilation | MaterializationRefused:
    """Gate 2 — authorize the GROUP's complete physical read set, or refuse the whole compilation.

    Two verdicts, decided from ONE fetch per catalog source, in a FIXED order. Existence first: a
    ref the governed catalog does not describe returns ``COLUMN_NOT_GOVERNED`` naming every such
    ref (Task 12 — L1's ``COLUMN_ABSENT`` sits on no production path, so compile must not emit a
    read nobody governs). Then read scope: one denied element anywhere returns a single
    ``READ_SCOPE_INSUFFICIENT``. When one group carries both, existence wins — no grantable role
    can make an undescribed column readable, so a scope refusal would send the operator after a
    privilege that cannot help. Nothing partial is returned either way, so no contract, group plan
    or project can be derived from a refused group: the downstream chain takes an
    :class:`AuthorizedCompilation`, and a refusal is not one.

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
    hidden, missing = _hidden(conn, elements, roles_used)
    if missing:
        described = ", ".join(
            f"{element.logical_ref} ({'+'.join(kind.value for kind in element.kinds)})"
            for element in missing)
        return _refuse(
            CompilationRefusalCode.COLUMN_NOT_GOVERNED,
            f"the governed catalog does not describe {len(missing)} of {len(elements)} elements "
            f"of the group's complete physical read set across {len(group)} feature(s): "
            f"{described}. §11's L1 would report each of them as COLUMN_ABSENT against the live "
            f"metastore, but L1 sits on no production path, so compile refuses rather than emit a "
            f"read nobody governs. Existence is decided before read scope: no grantable role can "
            f"make an undescribed column readable, so when a group carries both an undescribed "
            f"ref and a hidden one, this refusal wins")
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
