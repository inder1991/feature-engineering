"""Spec A §7's node sequence — the ONE place the generated pipeline's wiring is described.

WHAT THIS IS, and why it did not exist. Every node renderer shipped with its own tests, and
``render_project`` refuses a wiring that does not close — but **nothing in ``src/`` had ever
assembled the sequence**. The only reference wiring was a test fixture
(``tests/featuregen/materialize/test_render_nodes_compute.py`` ``_wired_nodes``), and it omits the
join gate, so no full cross-catalog project had ever been assembled by anything. Task 3 left the
seam open on purpose (:class:`~featuregen.materialize.compile.chain.NodeAssembler`: "It is a
parameter rather than a call so that this module does not become the second place the wiring is
described"), and this module fills it. It lands beside ``chain.py`` rather than inside it for that
same reason: the chain orchestrates STAGES, and a chain that also described the wiring would be two
subjects in one file.

THIS MODULE IS A CALLER OF ``render/``, NEVER AN AUTHOR OF IT. It writes no node body, no dataset
name and no storage location. Every name it uses comes from
:func:`~featuregen.materialize.render.project.project_datasets` — which ``render_project``
re-derives and checks the wiring against — and every line of source comes from a renderer.

THE ONE DECISION THIS MODULE OWNS: **which frame a bridged projection reads.** Task 26 of the
codegen programme established that ``_check_wiring``'s rule is "the bridge gate's output is read by
SOME node", which is strictly weaker than "read by the projection that renders that hop". A project
in which the gate runs BESIDE the computation — a sibling node consuming its output while the
projection reads the unchecked raw table — passes every one of the renderer's seven rules and seals.
So the guarantee cannot be the renderer's, and it is made here instead, structurally:
:func:`_joined_datasets` resolves each reached table through :func:`_bridged_targets` FIRST, and
there is no path in this module that puts a bridged table's raw dataset into a projection's
``joined_datasets``. The gate's target is the raw table and its output is what the projection reads,
so the frame whose uniqueness was proved is exactly the frame the join consumes.

THE ORDER IS DATA FLOW, and it is deterministic. ``pipeline.py`` and ``nodes.py`` are rendered in
sequence order, so the order is part of ``generated_project_hash`` — two assemblies of one
compilation must produce byte-identical sequences or a group would get one identity per build. Every
collection walked below is sorted by a value the compilation carries (feature name, body path,
realization revision id), never by mapping insertion order. The sequence reads as the frames flow:
spine → bridge gates → projections → calculations → assembly → §9's gates. The gates come BEFORE the
projections that consume them because ``pipeline.py`` is read top to bottom, and a gate listed after
its consumer reads as though it ran afterwards — which is the exact confusion §3.2 exists to remove.
Kedro derives execution order from the datasets either way.

WHAT RAISES. Nothing here is a governed verdict about a feature: Gate 2 already authorized the group
and §14's closed vocabulary has no member for "the caller assembled this call wrongly". What is left
is a dataset map that does not cover the compilation it was derived from, or a group member with no
admitted formula — both ``ValueError``, both caught by no one, exactly as ``render/`` treats the same
class of defect.
"""
from __future__ import annotations

from collections.abc import Mapping

from featuregen.formula.schema import body_expressions
from featuregen.materialize.compile.chain import NodeAssemblyInputs
from featuregen.materialize.expression_ir import (
    ExpressionExecutionIR,
    PhysicalRef,
    RefRole,
)
from featuregen.materialize.group_plan import FeatureGroupPlanV1, PlannedFeature
from featuregen.materialize.ir import AuthorizedCompilation, FormulaExecutionIRV1
from featuregen.materialize.joins import CrossCatalogJoinStepV1
from featuregen.materialize.render.nodes_compute import (
    render_calculation_node,
    render_projection_node,
    render_spine_node,
)
from featuregen.materialize.render.nodes_gate import render_assembly_node, render_gate_node
from featuregen.materialize.render.nodes_join_gate import render_join_precondition_node
from featuregen.materialize.render.project import ProjectDatasets, RenderedNode

__all__ = ["assemble_nodes"]


def assemble_nodes(inputs: NodeAssemblyInputs, /) -> tuple[RenderedNode, ...]:
    """The complete ordered node sequence for one authorized compilation (§7).

    Satisfies :class:`~featuregen.materialize.compile.chain.NodeAssembler`, so it is passed to
    :func:`~featuregen.materialize.compile.chain.compile_feature_group` as ``assemble_nodes``.

    Args:
        inputs: everything the assembly needs and nothing it may decide for itself — Gate 2's token,
            the group plan, the one materialization contract, the admitted artifacts (for the
            formula-owned window policies ``PitSpec`` excludes), the population's resolved physical
            requirement, and the dataset names ``project_datasets`` derived.

    Returns:
        The nodes in the order they should be READ, ready for
        :func:`~featuregen.materialize.render.project.render_project`, which re-derives the same
        dataset names and refuses any wiring that does not close.

    Raises:
        ValueError: the dataset map does not cover the compilation (a governed source, a projection,
            a staging output, a manifest or a bridge gate is missing), a group member has no
            admitted formula, an expression names no single source relation, or two steps share a
            realization revision id and would render two different gates.
        TypeError: a renderer was handed something that is not the type it must be — every argument
            below comes from ``inputs``, so this is a caller who assembled ``NodeAssemblyInputs``
            wrongly.
    """
    plan, datasets, contract = inputs.plan, inputs.datasets, inputs.contract
    irs = sorted(inputs.authorized.irs, key=lambda ir: ir.feature_name)

    nodes: list[RenderedNode] = [render_spine_node(
        inputs.authorized.spine, plan, contract, spine_input=inputs.spine_input,
        source_dataset=_raw(datasets, inputs.spine_input.schema, inputs.spine_input.table),
        spine_dataset=datasets.spine)]

    nodes.extend(_join_gate_nodes(inputs.authorized, datasets))

    for ir in irs:
        column = _planned_column(plan, ir.feature_name)
        for expression in sorted(ir.expressions, key=lambda e: e.expr_path):
            source = _source_relation(expression)
            nodes.append(render_projection_node(
                expression, contract, feature_column=column,
                source_dataset=_raw(datasets, source.schema, source.table),
                projection_dataset=_projection(datasets, column, expression.expr_path),
                joined_datasets=_joined_datasets(expression, datasets)))

    for ir in irs:
        nodes.append(_calculation_node(ir, inputs))

    nodes.append(render_assembly_node(
        plan, spine_dataset=datasets.spine, staging_datasets=datasets.staging,
        manifest_datasets=datasets.manifests, assembled_dataset=datasets.assembled))
    nodes.append(render_gate_node(
        plan, assembled_dataset=datasets.assembled, published_dataset=datasets.published))
    return tuple(nodes)


# ── the bridge gates, and the frame the projection reads ─────────────────────────────────────────


def _join_gate_nodes(
    authorized: AuthorizedCompilation, datasets: ProjectDatasets,
) -> list[RenderedNode]:
    """One pre-computation gate per directional realization, ordered by revision id.

    Per REALIZATION and not per hop: ``project_datasets`` declares one validated dataset per
    ``realization_revision_id`` (``setdefault``), and two nodes writing one dataset is a wiring the
    renderer refuses — correctly, since whichever ran last would decide what the projections read.
    Two steps that share a revision id and would render DIFFERENT gates are refused here rather than
    silently reduced to whichever was seen first: one gate cannot validate two target frames, and
    the one that was dropped would be the one nothing checked.
    """
    rendered: dict[str, RenderedNode] = {}
    for ir in sorted(authorized.irs, key=lambda compiled: compiled.feature_name):
        for expression in sorted(ir.expressions, key=lambda e: e.expr_path):
            for step in expression.join_plan.steps:
                if not isinstance(step, CrossCatalogJoinStepV1):
                    continue
                target = _bridge_target(expression, step)
                node = render_join_precondition_node(
                    step,
                    target_dataset=_raw(datasets, target.schema, target.table),
                    validated_dataset=_gate(datasets, step.realization_revision_id))
                seen = rendered.setdefault(step.realization_revision_id, node)
                if seen != node:
                    raise ValueError(
                        f"two steps carry realization revision "
                        f"{step.realization_revision_id!r} and render different pre-computation "
                        f"gates ({seen.name!r} over {seen.inputs} and {node.name!r} over "
                        f"{node.inputs}): the catalog declares ONE validated dataset per revision, "
                        f"so one of the two target frames would be joined without ever having been "
                        f"checked")
    return [rendered[revision] for revision in sorted(rendered)]


def _joined_datasets(
    expression: ExpressionExecutionIR, datasets: ProjectDatasets,
) -> dict[str, str]:
    """Every table the traversal reaches, mapped to the frame the projection must read.

    THE guarantee this module exists to make. A table reached over a ``CrossCatalogJoinStepV1`` is
    wired to the GATE's output — the predicate-scoped target whose uniqueness was proved — and every
    other reached table to its governed raw source. There is no branch here that hands a bridged
    table's raw dataset to a projection, which is what makes "the gate ran before the computation"
    a property of the assembly rather than a hope about the wiring check (see the module docstring).

    The reached tables are taken from the READ SET rather than re-walked from the join plan: §1.3
    authorized a set of columns per table, ``_projection_plan`` refuses any expression whose read
    set spans a relation the traversal does not arrive at, and ``_hops`` refuses a path that
    revisits one — so the tables carrying a column, minus the source relation, ARE the hop targets,
    exactly and in every case. Re-deriving the hop list here would be a second answer to which
    tables a traversal reaches, and the renderer refuses a ``joined_datasets`` that disagrees with
    its own.

    Keyed by the FULL ``catalog_source::schema.table`` address, which is what makes the mapping
    unambiguous when two catalogs spell one physical pair the same way.
    """
    source = _physical(_source_relation(expression))
    bridged = _bridged_targets(expression)
    wired: dict[str, str] = {}
    for ref in expression.physical_read_set:
        if ref.column is None:
            continue
        key = _physical(ref)
        if key == source:
            continue
        revision = bridged.get(key)
        wired[f"{key[0]}::{key[1]}.{key[2]}"] = (
            _gate(datasets, revision) if revision is not None
            else _raw(datasets, ref.schema, ref.table))
    return dict(sorted(wired.items()))


def _bridged_targets(expression: ExpressionExecutionIR) -> dict[tuple[str, str, str], str]:
    """Which reached tables are arrived at over a bridge, and under which realization revision."""
    return {
        _physical(_bridge_target(expression, step)): step.realization_revision_id
        for step in expression.join_plan.steps
        if isinstance(step, CrossCatalogJoinStepV1)
    }


def _bridge_target(
    expression: ExpressionExecutionIR, step: CrossCatalogJoinStepV1,
) -> PhysicalRef:
    """The physical table one bridge hop ARRIVES at, resolved through the authorized read set.

    Resolved by looking the step's own ``column_pairs`` up in ``physical_read_set`` — the same
    address ``nodes_compute._full_endpoint`` resolves them by — because the step carries governed
    LOGICAL refs and the catalog entry is keyed by the RESOLVED physical pair (§3.5). Every member
    of the composite tuple is checked to land on one table: the assembly picks a dataset from this
    answer, so a tuple that spanned two would have half the join validated and half not.
    """
    by_ref = {ref.logical_ref: ref for ref in expression.physical_read_set}
    resolved: list[PhysicalRef] = []
    for _origin, target_ref in step.column_pairs:
        found = by_ref.get(target_ref)
        if found is None or found.column is None:
            raise ValueError(
                f"the bridge hop for realization {step.realization_revision_id} arrives on "
                f"{target_ref!r}, which the expression's authorized read set does not carry: §1.3 "
                f"records every join endpoint as its own read, so an endpoint missing from it is a "
                f"column the group would join on and was never granted")
        resolved.append(found)
    if len({_physical(ref) for ref in resolved}) != 1:
        raise ValueError(
            f"the bridge hop for realization {step.realization_revision_id} arrives on "
            f"{sorted({_physical(ref) for ref in resolved})}: a composite tuple spanning two "
            f"physical tables has no single frame to validate, so one side of the join would be "
            f"read without ever passing the pre-computation gate")
    return resolved[0]


# ── the per-feature calculation ──────────────────────────────────────────────────────────────────


def _calculation_node(ir: FormulaExecutionIRV1, inputs: NodeAssemblyInputs) -> RenderedNode:
    """One feature's calculation, with the two policies only its FORMULA can answer.

    ``empty_window`` and ``null_input`` are §8 rule 4's formula-owned declarations and
    :class:`~featuregen.materialize.expression_ir.PitSpec` excludes them deliberately, so they do
    not travel on the IR. They are read here from the admitted artifact through
    ``formula.body_expressions`` — Child-1's OWN enumeration, the same one ``compile_ir`` walks —
    because a second enumeration could disagree with it about how many expressions a body has or
    what each is called, and the path names the staging output every later stage reads.
    """
    admitted = inputs.admitted.get(ir.feature_name)
    if admitted is None:
        raise ValueError(
            f"the group compiles {ir.feature_name!r} and no admitted formula was supplied for it "
            f"(supplied: {sorted(inputs.admitted)}): §8 rule 4's empty-window and null-input "
            f"policies are the FORMULA's declarations and PitSpec excludes them, so a default here "
            f"would be this code answering a question the formula exists to answer — and `ignore` "
            f"and `propagate` produce different numbers from the same rows")
    column = _planned_column(inputs.plan, ir.feature_name)
    declared = body_expressions(admitted.formula.body)
    paths = [expression.expr_path for expression in ir.expressions]
    return render_calculation_node(
        ir, _planned_feature(inputs.plan, column), inputs.plan,
        empty_window={path: expression.window.empty_window for path, expression in declared},
        null_input={path: expression.window.null_input for path, expression in declared},
        projection_datasets={path: _projection(inputs.datasets, column, path) for path in paths},
        spine_dataset=inputs.datasets.spine,
        staging_dataset=_named(inputs.datasets.staging, column, "staging output"),
        manifest_dataset=_named(inputs.datasets.manifests, column, "staging manifest"))


# ── looking names up, and refusing when the map does not cover the compilation ───────────────────


def _physical(ref: PhysicalRef) -> tuple[str, str, str]:
    return (ref.catalog_source.strip().lower(), ref.schema.strip().lower(),
            ref.table.strip().lower())


def _source_relation(expression: ExpressionExecutionIR) -> PhysicalRef:
    """The ONE relation an expression projects from (§2.1 records it as a read of its own)."""
    relation = [ref for ref in expression.physical_read_set if RefRole.SOURCE_TABLE in ref.roles]
    if len({ref.logical_ref for ref in relation}) != 1:
        raise ValueError(
            f"the expression {expression.expr_path!r} names "
            f"{sorted({ref.logical_ref for ref in relation})} as its source relation: a projection "
            f"with no relation — or two — is not one table's rows, and the node would have no "
            f"single governed source to read")
    return relation[0]


def _raw(datasets: ProjectDatasets, schema: str, table: str) -> str:
    """The catalog entry for one governed source, keyed as ``project_datasets`` keys it."""
    key = f"{schema.strip().lower()}.{table.strip().lower()}"
    name = datasets.raw.get(key)
    if name is None:
        raise ValueError(
            f"the catalog declares no dataset for the governed source {key!r} (it declares "
            f"{sorted(datasets.raw)}): every physical read of this compilation is a catalog entry, "
            f"and a node wired to a name nothing declares would be handed an empty in-memory "
            f"dataset rather than the table it means")
    return name


def _gate(datasets: ProjectDatasets, realization_revision_id: str) -> str:
    name = datasets.join_gates.get(realization_revision_id)
    if name is None:
        raise ValueError(
            f"the catalog declares no bridge gate dataset for realization revision "
            f"{realization_revision_id!r} (it declares {sorted(datasets.join_gates)}): the "
            f"validated frame is `project_datasets`' answer, and a name invented here would be a "
            f"storage location decided by the compiler rather than by the catalog")
    return name


def _projection(datasets: ProjectDatasets, column: str, expr_path: str) -> str:
    name = datasets.projections.get((column, expr_path))
    if name is None:
        raise ValueError(
            f"the catalog declares no point-in-time projection for {column}/{expr_path} (it "
            f"declares {sorted(datasets.projections)}): §7 gives every expression its own "
            f"projection, and an expression with none has no frame for its calculation to read")
    return name


def _named(mapping: Mapping[str, str], column: str, what: str) -> str:
    name = mapping.get(column)
    if name is None:
        raise ValueError(
            f"the catalog declares no {what} for the planned column {column!r} (it declares "
            f"{sorted(mapping)}): §9 proves completeness per published column, so a column with "
            f"nothing to write is one the gate could only ever report as missing")
    return name


def _planned_column(plan: FeatureGroupPlanV1, feature_name: str) -> str:
    """The COLUMN a compiled feature occupies, read off the plan.

    Mirrors ``render.project._column_of``, which is private: ``project_datasets`` keys its
    projections, staging outputs and manifests by the PLAN's column, so an assembly that keyed them
    by anything else would look up names the catalog does not declare. That the rule has to be
    spelled twice is a finding about ``render/``'s surface, not a licence to normalize here — the
    plan is the authority on both sides and neither side derives a name.
    """
    return _planned_feature(plan, feature_name).column_name


def _planned_feature(plan: FeatureGroupPlanV1, feature_name: str) -> PlannedFeature:
    folded = feature_name.strip().lower()
    for feature in plan.features:
        if feature.column_name in (feature_name, folded):
            return feature
    raise ValueError(
        f"the plan carries no column for the compiled feature {feature_name!r} (it plans "
        f"{[feature.column_name for feature in plan.features]}): a rendered project would stage a "
        f"feature the published table has no column for")
