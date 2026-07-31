"""Spec §7 — render the COMPLETE runnable project: the files, the wiring, the launch story.

Everything before this module was a pure function over governed metadata, checked by calling it.
This one produces **text that has to be a working Kedro project**, and the difference is the whole
risk of the task: ``ast.parse`` proves that a file is syntactically Python and proves nothing else.
L0 (§11.2) is what imports the project and builds the pipeline object, and it is deliberately a
later task — nothing here may be read as evidence that the project runs.

**What this module owns, and what it does not.** It owns the project SHELL and the WIRING: which
files exist, which datasets exist in which layer, which node reads and writes which dataset, how the
run is launched, which dependencies are pinned, and the identity the whole thing is sealed under.
It does **not** own a single line of compute. Node bodies arrive as :class:`RenderedNode` values —
Task 13's spine/projection/calculation, Task 14's gates and assembly — and this module refuses to
assemble a project whose wiring does not close (see :func:`_check_wiring`). The seam is deliberate:
the catalog is the only place a storage location may be written, so the module that writes the
catalog must be the module that decides the dataset names, and a node that invented its own would be
naming a location nobody could review.

**Storage locations are catalog configuration, never literals in node source.** A path spelled
inside a node cannot be redirected, cannot be reviewed alongside the rest of the environment's
configuration, and cannot be told apart from a computed value by anything reading the source. So
every location — the governed source tables, the staging area, the published table — is a catalog
entry, and the wiring check refuses a node that writes anything the catalog does not declare.

**The publication target is DERIVED, never parameterised.** It comes from
:func:`~featuregen.materialize.binding.physical_target_for`, which is built on
``binding.SANDBOX_NAMESPACE`` — the same constant :func:`derive_namespace` answers with. It is not a
run parameter, not a template variable and not an argument to this function, because every one of
those is a way for a run to write somewhere nobody bound (§10.1). It is also rendered **fail-closed**:
the published dataset's ``write_mode`` refuses to replace an existing table, because the publication
MECHANISM is not selectable until §10.3's probe passes (Task 16), and `INSERT OVERWRITE` is banned
outright by §10.

**Determinism.** The same compilation must render byte-identically, or ``generated_project_hash``
identifies the day a project was built rather than what it is. Every collection this module walks is
sorted before it is rendered, and nothing observes a clock, a path on this machine, an environment
variable, or the order a mapping happened to be built in.

**No ``pyspark`` and no ``kedro`` import.** Both appear only inside the strings below. This module
runs wherever the compiler runs; the artifact it emits runs on the cluster.

**What raises.** Nothing here is a governed verdict about a feature — a group that must not be
rendered was already refused by Gate 2, and this module *takes that gate's token* rather than
re-asking. What is left is malformed caller input: a node whose source does not define the function
the pipeline wires, two datasets that normalize to one name, a spine requirement for another table.
§14's closed vocabularies have no member for any of them, so they raise.
"""
from __future__ import annotations

import ast
import os
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.materialize.binding import physical_target_for
from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.group_plan import FeatureGroupPlanV1
from featuregen.materialize.identity import (
    GENERATED_LOCK_FILENAME,
    CompilationIdentity,
    SealedProject,
    build_compilation_identity,
    derive_namespace,
    seal_project,
)
from featuregen.materialize.inputs import PhysicalInputRequirement
from featuregen.materialize.inventory import EngineVersions
from featuregen.materialize.ir import AuthorizedCompilation
from featuregen.materialize.joins import CrossCatalogJoinStepV1
from featuregen.materialize.publish import PublisherSelection
from featuregen.materialize.render import RENDERER_VERSION
from featuregen.materialize.render._yaml import yaml_scalar
from featuregen.materialize.render.publish import publish_entry_body, published_dataset_name
from featuregen.overlay.upload.object_ref import parse_ref

__all__ = [
    "PIPELINE_NAME",
    "REQUIRED_RUN_PARAMETERS",
    "RENDERER_VERSION",
    "DatasetLayer",
    "ProjectDatasets",
    "RenderedNode",
    "feature_staging_path",
    "materialize_to",
    "project_datasets",
    "render_project",
]

#: The one pipeline this slice renders. Named once so the registry, the package path and the
#: ``kedro run --pipeline`` line in the README cannot drift apart.
PIPELINE_NAME = "materialize"

#: §11.1 — the run-scoped values execution must be GIVEN, in sorted order. The rendered hook refuses
#: a run that is missing one *or* carries one that is not here: "unexpected" matters as much as
#: "missing", because a parameter nobody planned for is a value the pipeline may read and nobody
#: prepared. None of them is defaulted anywhere in the rendered project — a default is what lets a
#: run proceed on a value run preparation never resolved.
REQUIRED_RUN_PARAMETERS: tuple[str, ...] = (
    "business_dt",
    "generation_id",
    "input_snapshots",
    "run_id",
    "sandbox_execution_hash",
    "staging_root",
)


class DatasetLayer(StrEnum):
    """§7's layer table, closed. ``model_input`` is Spec C and is deliberately absent.

    The layer is carried on every dataset rather than inferred from its name, because two of the
    rules that matter are stated in terms of it: nothing may be WRITTEN to ``RAW`` (they are the
    governed sources, read-only by construction), and exactly one dataset is in ``FEATURE`` (the
    published table).
    """

    RAW = "raw"
    INTERMEDIATE = "intermediate"
    PRIMARY = "primary"
    FEATURE_STAGING = "feature_staging"
    FEATURE = "feature"


@dataclass(frozen=True, slots=True)
class _CatalogEntry:
    """One catalog dataset: its name, its layer, and the YAML body already rendered.

    The body is rendered at construction rather than held as a mapping so there is exactly one
    place that decides how a location becomes YAML — a second renderer would be a second chance to
    quote a path differently.
    """

    name: str
    layer: DatasetLayer
    body: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectDatasets:
    """Every dataset the generated project declares, and the names the wiring must use.

    Task 13 and Task 14 build their nodes against THIS, so the names in ``inputs=``/``outputs=`` are
    the names the catalog declares. A node that spelled its own would be reading a dataset Kedro
    cannot resolve — discoverable only at run time, on the cluster.

    ``raw`` is keyed by the resolved physical ``"<schema>.<table>"``, which is what §3.5 resolved and
    what the metastore answers to; the governed logical ref is schema-flattened and would name a
    different table.
    """

    raw: Mapping[str, str]
    join_gates: Mapping[str, str]
    spine: str
    projections: Mapping[tuple[str, str], str]
    staging: Mapping[str, str]
    manifests: Mapping[str, str]
    assembled: str
    published: str

    def names(self) -> tuple[str, ...]:
        """Every declared dataset name, sorted."""
        return tuple(sorted({
            *self.raw.values(), *self.join_gates.values(), self.spine,
            *self.projections.values(), *self.staging.values(), *self.manifests.values(),
            self.assembled, self.published}))


@dataclass(frozen=True, slots=True)
class RenderedNode:
    """One node of the generated pipeline: its source, and its EXPLICIT wiring.

    ``inputs``/``outputs`` are dataset names, never positions, and they are carried on the node
    rather than inferred from the function's signature: Kedro binds them positionally, so a
    signature and a wiring that disagree produce a pipeline that runs and computes the wrong thing.

    ``imports`` are whole import statements. They are collected here rather than written into
    ``source`` so the rendered ``nodes.py`` can de-duplicate and sort them — two nodes that both need
    a ``DataFrame`` must not emit the import twice, and the order two nodes happened to be built in
    must not change the bytes.

    ``source`` must define ``func_name`` at the top level and must not import anything: an import
    inside the source escapes the de-duplication above, and a source whose function is named
    something else would wire a node to a function the module does not have.
    """

    name: str
    func_name: str
    source: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    imports: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.func_name, "func_name")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(
                f"the node for {self.func_name!r} has no name ({self.name!r}): a Kedro node's name "
                f"is how a failure, a resume and a `--node` selection all address it, and an "
                f"unnamed node is one an operator cannot re-run")
        _unique(self.inputs, f"node {self.name!r} inputs")
        _unique(self.outputs, f"node {self.name!r} outputs")
        if not self.outputs:
            raise ValueError(
                f"node {self.name!r} declares no output: a node that writes nothing contributes "
                f"nothing to the published table, and Kedro would run it for its side effects — "
                f"which is exactly the shape a storage location written in node source takes")
        for declared in self.imports:
            parsed = _parse(declared, f"node {self.name!r} import {declared!r}")
            if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.Import | ast.ImportFrom):
                raise ValueError(
                    f"node {self.name!r} declares {declared!r} as an import, and it is not one: "
                    f"the rendered module merges these into its import block, where a statement "
                    f"that is not an import would execute before anything else in the project")
        module = _parse(self.source, f"node {self.name!r} source")
        for statement in module.body:
            if isinstance(statement, ast.Import | ast.ImportFrom):
                raise ValueError(
                    f"node {self.name!r} imports inside its source: imports are declared on the "
                    f"node so the rendered module can de-duplicate and sort them, and one hidden in "
                    f"the body would be emitted once per node that needs it")
        defined = {statement.name for statement in module.body
                   if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)}
        if self.func_name not in defined:
            raise ValueError(
                f"node {self.name!r} wires {self.func_name!r}, and its source defines "
                f"{sorted(defined)}: the pipeline imports the function BY NAME, so the two must be "
                f"the same name or the generated project fails at import with the wiring intact")


# ── small checked helpers ────────────────────────────────────────────────────────────────────────


def _parse(source: str, what: str) -> ast.Module:
    if not isinstance(source, str):
        raise TypeError(f"{what} must be text, got {type(source).__name__}")
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"{what} is not valid Python ({exc})") from exc


def _identifier(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.isidentifier():
        raise ValueError(
            f"{what} must be a Python identifier, got {value!r}: it is emitted into generated "
            f"source, where anything else is a file that will not import")
    return value


def _unique(values: Sequence[str], what: str) -> tuple[str, ...]:
    items = tuple(values)
    duplicated = sorted({item for item in items if items.count(item) > 1})
    if duplicated:
        raise ValueError(
            f"{what} names {', '.join(duplicated)} more than once: Kedro binds a node's datasets "
            f"positionally, so a repeated name is an argument nobody can tell from another")
    return items


#: The ONE YAML-scalar decision, shared with ``render.publish`` since §10.3 gave the publication
#: target an entry of its own — see :mod:`featuregen.materialize.render._yaml`. Aliased rather than
#: re-implemented: this module renders every body at construction precisely so that exactly one
#: place decides how a location becomes YAML, and a second copy would undo that.
_quote = yaml_scalar


def _fold(value: str) -> str:
    return value.strip().lower()


# ── the datasets ─────────────────────────────────────────────────────────────────────────────────


def slug(value: str) -> str:
    """A ref fragment as a dataset-name segment: everything outside ``[a-z0-9_]`` becomes ``_``.

    Public because Task 13 derives its node and function names from the same fragments this derives
    dataset names from, and a second spelling of the rule is a node whose name and whose dataset
    disagree about which expression it computes.
    """
    return "".join(char if char.isalnum() or char == "_" else "_" for char in _fold(value))


def project_datasets(
    authorized: AuthorizedCompilation,
    plan: FeatureGroupPlanV1,
    *,
    spine_input: PhysicalInputRequirement,
) -> ProjectDatasets:
    """The dataset names of the generated project, derived from what was compiled (§7's layers).

    Exposed so Tasks 13 and 14 wire their nodes against the same names the catalog declares.

    Raises:
        TypeError: ``authorized`` is not Gate 2's token (§1.3: no project is produced from an
            unauthorized group), or ``spine_input`` is not a resolved requirement.
        ValueError: two datasets normalize to one name, or ``spine_input`` describes a table other
            than the declared population's.
    """
    if not isinstance(authorized, AuthorizedCompilation):
        raise TypeError(
            f"rendering requires Gate 2's AuthorizedCompilation, got {type(authorized).__name__}: "
            f"§1.3 says no contract, group plan or PROJECT is produced from an unauthorized group, "
            f"and a token is the only thing that shows the group's whole read set was authorized")
    if not isinstance(plan, FeatureGroupPlanV1):
        raise TypeError(
            f"rendering requires a FeatureGroupPlanV1, got {type(plan).__name__}: the plan is what "
            f"states which columns the published table carries")
    if not isinstance(spine_input, PhysicalInputRequirement):
        raise TypeError(
            f"the spine's physical input must be a PhysicalInputRequirement, got "
            f"{type(spine_input).__name__}: the population's table is resolved by §3.5 like every "
            f"other read, and a name accepted as text would read whichever table it happened to spell")

    _source, _schema, spine_table, _column = parse_ref(authorized.spine.source_table_ref)
    if _fold(spine_input.table) != _fold(spine_table):
        raise ValueError(
            f"the supplied spine input describes {spine_input.schema}.{spine_input.table} while the "
            f"declared population is {authorized.spine.source_table_ref}: the project would read its "
            f"entity population from a table §4 never declared, and every landing key would come "
            f"from a population nobody attested")

    raw: dict[str, str] = {}
    for requirement in (spine_input, *_expression_requirements(authorized)):
        key = f"{_fold(requirement.schema)}.{_fold(requirement.table)}"
        raw.setdefault(key, f"raw_{slug(requirement.schema)}__{slug(requirement.table)}")

    projections: dict[tuple[str, str], str] = {}
    join_gates: dict[str, str] = {}
    staging: dict[str, str] = {}
    manifests: dict[str, str] = {}
    for ir in sorted(authorized.irs, key=lambda compiled: compiled.feature_name):
        column = _column_of(plan, ir.feature_name)
        staging[column] = f"feature_staging_{column}"
        manifests[column] = f"feature_staging_manifest_{column}"
        for expression in sorted(ir.expressions, key=lambda e: e.expr_path):
            projections[(column, expression.expr_path)] = (
                f"intermediate_{column}__{slug(expression.expr_path)}")
            for step in expression.join_plan.steps:
                if isinstance(step, CrossCatalogJoinStepV1):
                    join_gates.setdefault(
                        step.realization_revision_id,
                        f"intermediate_bridge_{slug(step.realization_revision_id[:16])}",
                    )

    datasets = ProjectDatasets(
        raw=dict(sorted(raw.items())),
        join_gates=dict(sorted(join_gates.items())),
        spine="primary_spine",
        projections=dict(sorted(projections.items())),
        staging=dict(sorted(staging.items())),
        manifests=dict(sorted(manifests.items())),
        # §9's gates run AFTER assembly and BEFORE publication, so the assembled group needs a
        # declared home: an undeclared output is an in-memory dataset, and a gate that failed over
        # one would leave nothing an operator could inspect to see what it judged.
        assembled="feature_staging_assembled",
        published=published_dataset_name(plan))

    declared = [
        *sorted(datasets.raw.values()), *sorted(datasets.join_gates.values()), datasets.spine,
        *sorted(datasets.projections.values()), *sorted(datasets.staging.values()),
        *sorted(datasets.manifests.values()),
        datasets.assembled, datasets.published]
    _unique(declared, "the generated catalog")
    return datasets


def _expression_requirements(
    authorized: AuthorizedCompilation,
) -> tuple[PhysicalInputRequirement, ...]:
    """Every physical table the group's expressions read, in a plan-determined order.

    Taken from each expression's ``input_requirements`` — which §3.5 resolved and which already
    covers the join hops — rather than re-derived from the read set here. A second derivation would
    be a second chance to disagree about which table a governed ref names, and the disagreement
    would surface as a catalog entry for a table the pipeline never reads (or, worse, a read with no
    entry at all).

    The two sorts below are **provably unable to change any rendered byte** (recorded EQUIVALENT by
    Task 12's mutation run): the one consumer keys by ``(schema, table)`` and stores a value derived
    from nothing else, then sorts the mapping. They are kept because this function's *contract* is an
    order, and a consumer that read the sequence rather than the mapping would need it — but nothing
    here should be read as a test having established that.
    """
    ordered: list[PhysicalInputRequirement] = []
    for ir in sorted(authorized.irs, key=lambda compiled: compiled.feature_name):
        for expression in sorted(ir.expressions, key=lambda e: e.expr_path):
            ordered.extend(expression.input_requirements)
    return tuple(ordered)


def _column_of(plan: FeatureGroupPlanV1, feature_name: str) -> str:
    """The planned COLUMN a compiled feature occupies — read off the plan, never re-normalized.

    ``build_compilation_identity`` has already proved the two sides describe the same features, so a
    lookup that misses is a caller who assembled a plan and a group of IRs that never agreed.
    """
    for feature in plan.features:
        if feature.column_name == feature_name or feature.column_name == _fold(feature_name):
            return feature.column_name
    raise ValueError(
        f"the plan carries no column for the compiled feature {feature_name!r} (it plans "
        f"{[feature.column_name for feature in plan.features]}): a rendered project would stage a "
        f"feature the published table has no column for")


# ── the catalog ──────────────────────────────────────────────────────────────────────────────────


def _hive_entry(name: str, layer: DatasetLayer, *, database: str, table: str,
                comment: str) -> _CatalogEntry:
    """A Hive table. ``write_mode`` is ``errorifexists`` on EVERY one of them.

    On a ``raw`` source that is belt-and-braces: nothing writes to a governed source table, and the
    wiring check refuses a node that tries. On the PUBLISHED target it is load-bearing — §10 bans
    `INSERT OVERWRITE`, and §10.3 forbids selecting any publish mechanism before the executable
    probe has proved it, so a project rendered today must be unable to replace an existing partition
    at all. Task 16 replaces this entry with the mechanism its probe attested.
    """
    return _CatalogEntry(name=name, layer=layer, body=(
        f"  # {comment}",
        "  type: spark.SparkHiveDataset",
        f"  database: {_quote(database)}",
        f"  table: {_quote(table)}",
        '  write_mode: "errorifexists"',
        "  metadata:",
        "    kedro-viz:",
        f"      layer: {_quote(layer.value)}",
    ))


def _parquet_entry(name: str, layer: DatasetLayer, *, path: str, comment: str) -> _CatalogEntry:
    """A generation-scoped intermediate or staging output.

    ``staging_root`` is a prepared run parameter (§11.1) because staging paths are generation-scoped
    and immutable (§9): a path fixed at render time would be shared by every run of the artifact,
    and a re-run would land on top of the previous run's evidence.
    """
    return _CatalogEntry(name=name, layer=layer, body=(
        f"  # {comment}",
        "  type: spark.SparkDataset",
        f"  filepath: {_quote('${runtime_params:staging_root}/' + path)}",
        '  file_format: "parquet"',
        "  save_args:",
        '    mode: "errorifexists"',
        "  metadata:",
        "    kedro-viz:",
        f"      layer: {_quote(layer.value)}",
    ))


def _json_entry(name: str, layer: DatasetLayer, *, path: str, comment: str) -> _CatalogEntry:
    return _CatalogEntry(name=name, layer=layer, body=(
        f"  # {comment}",
        "  type: json.JSONDataset",
        f"  filepath: {_quote('${runtime_params:staging_root}/' + path)}",
        "  metadata:",
        "    kedro-viz:",
        f"      layer: {_quote(layer.value)}",
    ))


def feature_staging_path(column: str) -> str:
    """One feature's staging output, RELATIVE to ``staging_root``.

    Public because two things must agree about it: the catalog entry that decides where the dataset
    is written, and the ``StagingManifestV1`` the calculation node writes, whose ``output_location``
    states where the output went. A second spelling would be a manifest that names a path nothing
    wrote to — evidence about a location rather than about the output.

    The ROOT is never here. It arrives as ``${runtime_params:staging_root}`` in the catalog and as
    the ``staging_root`` run parameter in the node, because §9's staging area is generation-scoped
    and a root fixed at render time would be shared by every run of the artifact.
    """
    return f"feature_staging/{column}/data"


def _catalog_entries(
    datasets: ProjectDatasets,
    *,
    plan: FeatureGroupPlanV1,
    published_target: str,
    selection: PublisherSelection | None,
) -> tuple[_CatalogEntry, ...]:
    """Every dataset entry. ``selection`` decides ONLY the last one — the publication target.

    ``None`` renders Task 12's fail-closed Hive entry, which is the right artifact for a project
    rendered before anything was attested: it cannot publish over anything at all. A selection
    renders §10.3's attested mechanism instead (:func:`~featuregen.materialize.render.publish
    .publish_entry_body`). The default is the closed one, so a caller who omits the selection gets
    *less* capability rather than more.
    """
    entries: list[_CatalogEntry] = []
    for physical, name in sorted(datasets.raw.items()):
        schema, table = physical.split(".", 1)
        entries.append(_hive_entry(
            name, DatasetLayer.RAW, database=schema, table=table,
            comment=f"governed source, read-only: {physical}"))
    for revision_id, name in sorted(datasets.join_gates.items()):
        entries.append(_parquet_entry(
            name,
            DatasetLayer.INTERMEDIATE,
            path=f"intermediate/bridge/{revision_id}",
            comment=(
                "predicate-scoped target after the pre-computation fan-out gate for directional "
                f"realization {revision_id}"),
        ))
    for (column, expr_path), name in sorted(datasets.projections.items()):
        entries.append(_parquet_entry(
            name, DatasetLayer.INTERMEDIATE, path=f"intermediate/{column}/{slug(expr_path)}",
            comment=f"point-in-time projection for {column} / {expr_path} (§8)"))
    entries.append(_parquet_entry(
        datasets.spine, DatasetLayer.PRIMARY, path="primary/spine",
        comment="the declared entity population (§4) — one row per (keys…, business_dt)"))
    for column, name in sorted(datasets.staging.items()):
        entries.append(_parquet_entry(
            name, DatasetLayer.FEATURE_STAGING, path=feature_staging_path(column),
            comment=f"{column}: (keys…, business_dt, {column}) only — no system columns (§10.2)"))
    for column, name in sorted(datasets.manifests.items()):
        entries.append(_json_entry(
            name, DatasetLayer.FEATURE_STAGING, path=f"feature_staging/{column}/manifest.json",
            comment=f"StagingManifestV1 for {column}: §9's completeness evidence"))
    entries.append(_parquet_entry(
        datasets.assembled, DatasetLayer.FEATURE_STAGING, path="feature_staging/_assembled",
        comment="the assembled group — §9's gates run on THIS, and only a group that passes"
                " every one of them reaches the publication target"))
    if selection is None:
        database, table = published_target.split(".", 1)
        entries.append(_hive_entry(
            datasets.published, DatasetLayer.FEATURE, database=database, table=table,
            comment=f"the publication target, derived from the sandbox binding: {published_target}"))
    else:
        entries.append(_CatalogEntry(
            name=datasets.published, layer=DatasetLayer.FEATURE,
            body=publish_entry_body(plan, selection=selection)))
    return tuple(entries)


# ── the wiring ───────────────────────────────────────────────────────────────────────────────────


def _check_wiring(datasets: ProjectDatasets, nodes: Sequence[RenderedNode]) -> None:
    """Refuse a pipeline whose wiring does not close.

    Kedro discovers most of these at run time, on the cluster, after the session has started and
    the first source table has been read — which is the most expensive place to learn that a
    dataset name was misspelled. All of them are decidable here, from the plan.

    Six rules, each one a way a rendered project could be wrong while parsing perfectly:

    1. every output is a DECLARED dataset — a node writing an undeclared name writes to a Kedro
       ``MemoryDataset``, so the output vanishes when the run ends and nothing says so;
    2. nothing writes to a ``raw`` dataset — those are the governed sources §1.3 authorized this
       group to READ;
    3. no dataset is written twice — two nodes producing one name is Kedro's own error, but the
       damage is that the group's evidence would depend on which ran last;
    4. every non-raw dataset is written exactly once — a declared staging output nobody produces is
       a §9 gate failure discovered a run later;
    5. every raw dataset is read — a catalog entry nothing reads is a governed source the project
       claims and does not use, which is the same false statement in the other direction;
    6. every input is a declared dataset or a parameter reference;
    7. every bridge-gate output is consumed downstream. Merely running a sibling validation node
       beside a projection leaves the computation free to read the unchecked raw target.

    Node names and function names are also required to be unique: Kedro rejects duplicate node
    names, and two functions of one name in ``nodes.py`` would mean the second silently replaced the
    first.
    """
    declared = set(datasets.names())
    raw = set(datasets.raw.values())
    _unique([node.name for node in nodes], "the rendered pipeline's node names")
    _unique([node.func_name for node in nodes], "the rendered nodes module's function names")

    written: dict[str, str] = {}
    read: set[str] = set()
    for node in nodes:
        for name in node.outputs:
            if name not in declared:
                raise ValueError(
                    f"node {node.name!r} writes {name!r}, which the catalog does not declare: an "
                    f"undeclared output becomes an in-memory dataset that disappears when the run "
                    f"ends, and the pipeline would report success having published nothing")
            if name in raw:
                raise ValueError(
                    f"node {node.name!r} writes {name!r}, a raw governed source: Gate 2 authorized "
                    f"this group to READ its physical read set, and writing back into it is a "
                    f"different act nobody governed")
            clash = written.get(name)
            if clash is not None:
                raise ValueError(
                    f"nodes {clash!r} and {node.name!r} both write {name!r}: whichever ran last "
                    f"would decide what the group published, and neither is the answer")
            written[name] = node.name
        for name in node.inputs:
            if name.startswith("params:") or name == "parameters":
                continue
            if name not in declared:
                raise ValueError(
                    f"node {node.name!r} reads {name!r}, which the catalog does not declare: Kedro "
                    f"would hand it an empty in-memory dataset rather than the table it names")
            read.add(name)

    missing = sorted(declared - raw - set(written))
    if missing:
        raise ValueError(
            f"{len(missing)} declared dataset(s) are written by no node ({', '.join(missing)}): a "
            f"staging output nobody produces is a §9 completeness failure discovered a whole run "
            f"later, on the cluster")
    unread = sorted(raw - read)
    if unread:
        raise ValueError(
            f"{len(unread)} governed source table(s) are read by no node ({', '.join(unread)}): the "
            f"catalog would state a read this project does not perform, which is the same false "
            f"claim as a read it performs without declaring")
    bypassed_bridge_gates = sorted(set(datasets.join_gates.values()) - read)
    if bypassed_bridge_gates:
        raise ValueError(
            "bridge pre-computation gate output(s) are not consumed by a downstream node "
            f"({', '.join(bypassed_bridge_gates)}): the projection would still read the unchecked "
            "raw target, so the gate could fail beside—not before—the computation")
    if datasets.published not in written:
        raise ValueError(
            f"no node writes the publication target {datasets.published!r}: a project that computes "
            f"a group and publishes nothing is one an operator would have to inspect a warehouse to "
            f"discover")


# ── the rendered files ───────────────────────────────────────────────────────────────────────────

_DO_NOT_EDIT = "GENERATED by featuregen.materialize.render — do not edit."


def _python_header(summary: str, *, package: str) -> str:
    return (f'"""{summary}\n\n'
            f'{_DO_NOT_EDIT} Rendered for the package `{package}` by renderer version '
            f'{RENDERER_VERSION}.\n"""\n')


def _identity_literal(compilation: CompilationIdentity) -> str:
    """The compilation identity as a Python literal, sorted, four spaces deep.

    Rendered by hand rather than through ``json.dumps`` so nothing about the emitted bytes depends
    on a serializer's defaults. Every value is a hex hash or a list of them, so the literal is both
    valid Python and (by construction) free of anything that would need escaping — a test pins that
    by ``ast.literal_eval``-ing it back.
    """
    payload = compilation.identity_payload()
    lines = ["{"]
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, list):
            lines.append(f"    {key!r}: [")
            lines.extend(f"        {item!r}," for item in value)
            lines.append("    ],")
        else:
            lines.append(f"    {key!r}: {value!r},")
    lines.append("}")
    return "\n".join(lines)


def _render_package_init(compilation: CompilationIdentity, *, package: str) -> str:
    return (
        _python_header(
            "The generated materialization project (§7).\n\n"
            "Rendered files embed the COMPILATION identity only. `generated_project_hash` is\n"
            "computed over these bytes and lives in GENERATED.lock — in this file it would be a\n"
            "value the hash is taken over, which is the circularity §7's two phases remove.",
            package=package)
        + "\n"
        f'__version__ = "1.0"\n'
        "\n"
        "#: What the compilation chain decided, before a single file existed (§7).\n"
        f"COMPILATION_IDENTITY = {_identity_literal(compilation)}\n")


def _render_settings(package: str) -> str:
    return (
        _python_header("Kedro project settings for the generated materialization project.",
                       package=package)
        + "\n"
        f"from {package}.hooks import RunParametersHook, SparkSessionHook\n"
        "\n"
        "#: Both hooks are required, and both are §7's launch story rather than decoration:\n"
        "#: `SparkSessionHook` is the Spark session `kedro run` runs inside, and\n"
        "#: `RunParametersHook` is §11.1's refusal to run on parameters nobody prepared.\n"
        "#: Kedro executes hooks last-in-first-out, so the parameter check runs BEFORE the session\n"
        "#: is used and a refused run never touches the cluster.\n"
        "HOOKS = (SparkSessionHook(), RunParametersHook())\n"
        "\n"
        "CONFIG_LOADER_ARGS = {\n"
        '    "base_env": "base",\n'
        "    # The generated project ships exactly one configuration environment. Pointing the run\n"
        "    # environment at a `local` that does not exist would leave conf/base/spark.yml\n"
        "    # silently unmerged.\n"
        '    "default_run_env": "base",\n'
        '    "config_patterns": {"spark": ["spark*", "spark*/**"]},\n'
        "}\n")


def _render_pipeline_registry(package: str) -> str:
    return (
        _python_header("The project's pipelines.", package=package)
        + "from __future__ import annotations\n"
        "\n"
        "from kedro.pipeline import Pipeline\n"
        "\n"
        f"from {package}.pipelines.{PIPELINE_NAME} import create_pipeline\n"
        "\n"
        "\n"
        "def register_pipelines() -> dict[str, Pipeline]:\n"
        '    """Register the one pipeline this artifact contains.\n'
        "\n"
        "    Registered EXPLICITLY rather than by discovery: a rendered artifact is identified by\n"
        "    the hash of its bytes, so what it runs must be readable from those bytes and not from\n"
        "    whatever happens to be importable at run time.\n"
        '    """\n'
        f"    {PIPELINE_NAME} = create_pipeline()\n"
        f'    return {{"{PIPELINE_NAME}": {PIPELINE_NAME}, "__default__": {PIPELINE_NAME}}}\n')


def _render_hooks(package: str, required_parameters: tuple[str, ...]) -> str:
    required = "\n".join(f"        {name!r}," for name in required_parameters)
    return (
        _python_header("The two project hooks §7's launch story depends on.", package=package)
        + "from __future__ import annotations\n"
        "\n"
        "from typing import Any\n"
        "\n"
        "from kedro.framework.hooks import hook_impl\n"
        "from pyspark import SparkConf\n"
        "from pyspark.sql import SparkSession\n"
        "\n"
        "\n"
        "class SparkSessionHook:\n"
        '    """§7 — `kedro run` runs INSIDE a Spark session this hook configures.\n'
        "\n"
        "    `spark-submit` only places the run on the cluster; it does not decide the session the\n"
        "    pipeline computes in. Keeping the configuration here rather than in a submit command\n"
        "    means the session is part of the artifact whose hash is recorded, instead of a set of\n"
        "    flags typed on the day.\n"
        '    """\n'
        "\n"
        "    @hook_impl\n"
        "    def after_context_created(self, context: Any) -> None:\n"
        '        parameters = context.config_loader["spark"]\n'
        "        configuration = SparkConf().setAll(sorted(parameters.items()))\n"
        "        (\n"
        "            SparkSession.builder.config(conf=configuration)\n"
        "            .enableHiveSupport()\n"
        "            .getOrCreate()\n"
        "        )\n"
        "\n"
        "\n"
        "class RunParametersHook:\n"
        '    """§11.1 — execution reads EXACTLY the values run preparation resolved.\n'
        "\n"
        "    Resolving and validating partitions is worthless if the pipeline then reads whatever it\n"
        "    likes, so a run missing one of these refuses before a node executes. An UNEXPECTED\n"
        "    parameter refuses for the same reason: it is a value somebody passed that nothing here\n"
        "    planned to read, and silently ignoring it hides the disagreement.\n"
        '    """\n'
        "\n"
        "    REQUIRED_RUN_PARAMETERS = (\n"
        f"{required}\n"
        "    )\n"
        f"    GATE_CODE = {ValidationGateCode.RUN_PARAMETERS_MISSING.value!r}\n"
        "\n"
        "    @hook_impl\n"
        "    def before_pipeline_run(self, run_params: dict[str, Any], pipeline: Any,\n"
        "                            catalog: Any) -> None:\n"
        '        supplied = set((run_params or {}).get("runtime_params") or {})\n'
        "        expected = set(self.REQUIRED_RUN_PARAMETERS)\n"
        "        missing = sorted(expected - supplied)\n"
        "        unexpected = sorted(supplied - expected)\n"
        "        if missing or unexpected:\n"
        "            raise RuntimeError(\n"
        '                f"{self.GATE_CODE}: the run is missing {missing} and carries unexpected "\n'
        '                f"{unexpected}. Run preparation resolves every one of "\n'
        '                f"{sorted(expected)} and submission passes them; a run that reads anything "\n'
        '                f"else is not the run that was prepared."\n'
        "            )\n")


def _render_pipelines_init(package: str) -> str:
    return _python_header("The project's pipeline packages.", package=package)


def _render_materialize_init(package: str) -> str:
    return (
        _python_header(f"The `{PIPELINE_NAME}` pipeline.", package=package)
        + "from __future__ import annotations\n"
        "\n"
        f"from {package}.pipelines.{PIPELINE_NAME}.pipeline import create_pipeline\n"
        "\n"
        '__all__ = ["create_pipeline"]\n')


def _render_nodes(nodes: Sequence[RenderedNode], *, package: str) -> str:
    imports = sorted({statement for node in nodes for statement in node.imports})
    blocks = [node.source.rstrip("\n") for node in nodes]
    header = _python_header(
        f"The `{PIPELINE_NAME}` pipeline's node functions.\n\n"
        "Every storage location this pipeline touches is a CATALOG entry, so no path, table or\n"
        "database name appears below: a location written here could not be reviewed with the rest\n"
        "of the environment's configuration, and nothing reading the source could tell it apart\n"
        "from a computed value.",
        package=package)
    body = "\n".join(imports)
    return header + (f"\n{body}\n" if body else "") + "\n\n" + "\n\n\n".join(blocks) + "\n"


def _node_literal(node: RenderedNode) -> str:
    inputs = "[" + ", ".join(repr(name) for name in node.inputs) + "]" if node.inputs else "None"
    outputs = (repr(node.outputs[0]) if len(node.outputs) == 1
               else "[" + ", ".join(repr(name) for name in node.outputs) + "]")
    tags = ", ".join(repr(tag) for tag in sorted(node.tags))
    return (
        "            node(\n"
        f"                func={node.func_name},\n"
        f"                inputs={inputs},\n"
        f"                outputs={outputs},\n"
        f"                name={node.name!r},\n"
        f"                tags=[{tags}],\n"
        "            ),")


def _render_pipeline(nodes: Sequence[RenderedNode], *, package: str) -> str:
    imported = ",\n".join(f"    {func}" for func in sorted(node.func_name for node in nodes))
    return (
        _python_header(f"The `{PIPELINE_NAME}` pipeline's wiring.\n\n"
                       "Every node states its inputs and outputs EXPLICITLY. Kedro would infer an\n"
                       "order either way; what an explicit wiring adds is that the order is\n"
                       "readable from the artifact instead of from the run that executed it.",
                       package=package)
        + "from __future__ import annotations\n"
        "\n"
        "from kedro.pipeline import Pipeline, node\n"
        "\n"
        f"from {package}.pipelines.{PIPELINE_NAME}.nodes import (\n"
        f"{imported},\n"
        ")\n"
        "\n"
        "\n"
        "def create_pipeline(**kwargs) -> Pipeline:\n"
        f'    """The `{PIPELINE_NAME}` pipeline: raw → intermediate → primary → staging → feature."""\n'
        "    return Pipeline(\n"
        "        [\n"
        + "\n".join(_node_literal(node) for node in nodes) + "\n"
        "        ]\n"
        "    )\n")


def _render_catalog(entries: Sequence[_CatalogEntry]) -> str:
    lines = [
        f"# {_DO_NOT_EDIT}",
        "#",
        "# EVERY storage location the generated project touches is here and nowhere else. A path or",
        "# a table name written into node source could not be reviewed with the rest of the",
        "# environment's configuration, and nothing reading that source could tell it apart from a",
        "# computed value.",
        "#",
        "# The only tables named below are the group's authorized physical read set (§1.3), the",
        "# declared entity population (§4), this generation's staging area, and the ONE publication",
        "# target §10.1 binds the group to.",
        "#",
        "# Layers (§7): raw -> intermediate -> primary -> feature_staging -> feature.",
    ]
    layer = None
    for entry in entries:
        if entry.layer is not layer:
            layer = entry.layer
            lines.extend(["", f"# ── {layer.value} " + "─" * max(1, 70 - len(layer.value))])
        lines.append("")
        lines.append(f"{entry.name}:")
        lines.extend(entry.body)
    return "\n".join(lines) + "\n"


def _render_parameters(required_parameters: tuple[str, ...]) -> str:
    required = "\n".join(f"#   - {name}" for name in required_parameters)
    return (
        f"# {_DO_NOT_EDIT}\n"
        "#\n"
        "# This file declares NO parameter, and that is the point.\n"
        "#\n"
        "# Every run-scoped value arrives as a prepared run parameter (§11.1) — resolved by run\n"
        "# preparation against a live metastore and passed to `kedro run --params`:\n"
        f"{required}\n"
        "#\n"
        "# A default here would let a run proceed on a value nobody prepared, which is exactly the\n"
        "# failure `RunParametersHook` exists to prevent. The publication target is not here either:\n"
        "# it is DERIVED from the group's sandbox binding (§10.1) and appears only in the catalog, so\n"
        "# no run can redirect where the group publishes.\n")


def _render_logging() -> str:
    return (
        f"# {_DO_NOT_EDIT}\n"
        "#\n"
        "# Selected by pointing KEDRO_LOGGING_CONFIG at this file; Kedro's own default lives at\n"
        "# conf/logging.yml, and §7 places this project's under conf/base with the rest of its\n"
        "# configuration.\n"
        "version: 1\n"
        "disable_existing_loggers: false\n"
        "formatters:\n"
        "  simple:\n"
        '    format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"\n'
        "handlers:\n"
        "  console:\n"
        "    class: logging.StreamHandler\n"
        "    level: INFO\n"
        "    formatter: simple\n"
        "    stream: ext://sys.stdout\n"
        "loggers:\n"
        "  kedro:\n"
        "    level: INFO\n"
        "root:\n"
        "  level: INFO\n"
        "  handlers: [console]\n")


def _render_spark(package: str) -> str:
    return (
        f"# {_DO_NOT_EDIT}\n"
        "#\n"
        "# Read by SparkSessionHook (§7): `kedro run` runs inside the session these settings build.\n"
        "#\n"
        "# The session time zone is pinned to UTC deliberately. Every point-in-time boundary this\n"
        "# project computes carries its OWN governed time zone (§8), so a session zone inherited\n"
        "# from whichever JVM the run landed on would be a second, ungoverned clock — and one that\n"
        "# changes numbers rather than failing.\n"
        "#\n"
        "# Nothing here configures an overwrite mode. §10 bans `INSERT OVERWRITE` outright and the\n"
        "# publication mechanism is not selectable until §10.3's probe proves one, so a setting that\n"
        "# decided HOW an overwrite behaves would be a policy for an operation this project may not\n"
        "# perform.\n"
        f"spark.app.name: {_quote(package)}\n"
        'spark.sql.session.timeZone: "UTC"\n')


def _render_pyproject(package: str, *, engine_versions: EngineVersions) -> str:
    return (
        f"# {_DO_NOT_EDIT}\n"
        "\n"
        "[build-system]\n"
        'requires = ["setuptools"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        f"name = {_toml(package)}\n"
        f'version = "1.0"\n'
        f"# Pinned, not bounded: every version below is what the target environment was CAPTURED\n"
        f"# running (§0). A range would let the artifact install something the cluster does not run.\n"
        f"requires-python = {_toml('==' + engine_versions.python)}\n"
        "dependencies = [\n"
        f"    {_toml('kedro==' + engine_versions.kedro)},\n"
        f"    {_toml('kedro-datasets==' + engine_versions.kedro_datasets)},\n"
        f"    {_toml('pyspark==' + engine_versions.pyspark)},\n"
        "]\n"
        "\n"
        "[tool.kedro]\n"
        f"package_name = {_toml(package)}\n"
        f"project_name = {_toml(package)}\n"
        f"kedro_init_version = {_toml(engine_versions.kedro)}\n"
        'source_dir = "src"\n'
        "\n"
        "[tool.setuptools.packages.find]\n"
        'where = ["src"]\n'
        "namespaces = false\n")


def _toml(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_requirements(engine_versions: EngineVersions) -> str:
    return (
        f"# {_DO_NOT_EDIT}\n"
        "#\n"
        "# The three runtime pins, taken from ClusterInventoryV1.engine_versions — what the target\n"
        "# environment was captured RUNNING, not what resolves today.\n"
        "#\n"
        "# The cluster's own engines are recorded in README.md and are NOT installed by this\n"
        f"# project: hive {engine_versions.hive} · spark {engine_versions.spark} · metastore "
        f"{engine_versions.metastore} · java {engine_versions.java}.\n"
        f"kedro=={engine_versions.kedro}\n"
        f"kedro-datasets=={engine_versions.kedro_datasets}\n"
        f"pyspark=={engine_versions.pyspark}\n")


def _render_readme(
    plan: FeatureGroupPlanV1,
    compilation: CompilationIdentity,
    datasets: ProjectDatasets,
    *,
    package: str,
    environment_id: str,
    engine_versions: EngineVersions,
    published_target: str,
    required_parameters: tuple[str, ...],
) -> str:
    features = "\n".join(f"| `{feature.column_name}` | `{feature.physical_type.sql_type}` | "
                         f"`{feature.ir_hash}` |" for feature in plan.features)
    sources = "\n".join(f"| `{physical}` | `{name}` |"
                        for physical, name in sorted(datasets.raw.items()))
    parameters = "\n".join(f"- `{name}`" for name in required_parameters)
    return (
        f"# `{published_target}`\n"
        "\n"
        f"{_DO_NOT_EDIT} Regenerate it; editing it makes the project a different artifact from the\n"
        "one whose hash was recorded, with no record that it changed.\n"
        "\n"
        f"- **Group** `{plan.logical_group_name}` · **environment** `{environment_id}`\n"
        f"- **Publishes to** `{published_target}` — derived from the group's sandbox binding\n"
        "  (§10.1). It is not a parameter and not a template variable: there is no way to make a run\n"
        "  write anywhere else.\n"
        f"- **Renderer version** `{RENDERER_VERSION}` · **physical type policy** "
        f"`{plan.physical_type_policy_version}`\n"
        f"- **Materialization contract** `{compilation.materialization_contract_hash}`\n"
        f"- **Group plan** `{compilation.group_plan_hash}`\n"
        "\n"
        "## How this is launched\n"
        "\n"
        "```\n"
        f"kedro run --pipeline {PIPELINE_NAME} --params <prepared run parameters>\n"
        "```\n"
        "\n"
        "**`kedro run` is what runs the pipeline, and it runs inside a Spark session\n"
        "`SparkSessionHook` builds from `conf/base/spark.yml`.** That command is the whole of the\n"
        "launch, locally and on the cluster.\n"
        "\n"
        "`spark-submit` does one thing: it PLACES that run on the cluster. It does not create the\n"
        "session the pipeline computes in, and it is not where the session's configuration belongs —\n"
        "a flag typed into a submit command is not part of the artifact whose hash was recorded, so\n"
        "two runs of one artifact could compute differently with nothing to show for it. The exact\n"
        "`spark-submit` line for a given cluster is that cluster's, and this project does not\n"
        "invent one.\n"
        "\n"
        "## The run parameters this project refuses to run without\n"
        "\n"
        f"{parameters}\n"
        "\n"
        "Run preparation (§11.1) resolves them against a live metastore and submission passes them.\n"
        "`RunParametersHook` refuses a run that is missing one **or** carries one that is not listed,\n"
        f"with `{ValidationGateCode.RUN_PARAMETERS_MISSING.value}`.\n"
        "\n"
        "## Layers\n"
        "\n"
        "| Layer | What it holds |\n"
        "| --- | --- |\n"
        "| `raw` | the governed source tables, read-only |\n"
        "| `intermediate` | one point-in-time projection per feature expression (§8) |\n"
        "| `primary` | the declared entity population — the spine (§4) |\n"
        "| `feature_staging` | one output and one manifest per feature, computed independently (§9) |\n"
        f"| `feature` | `{published_target}` (Hive) |\n"
        "\n"
        "Each feature computes from `raw` independently. A duplicated scan is slower; an incorrectly\n"
        "shared filter or window produces a wrong number, and this slice does not share scans (§7).\n"
        "\n"
        "## Governed sources\n"
        "\n"
        "| Physical table | Catalog dataset |\n"
        "| --- | --- |\n"
        f"{sources}\n"
        "\n"
        "## Published feature columns\n"
        "\n"
        "| Column | Type | `ir_hash` |\n"
        "| --- | --- | --- |\n"
        f"{features}\n"
        "\n"
        "Plus the three system columns §10.2 adds once at assembly: `__generation_id`,\n"
        "`__generated_project_hash`, `__sandbox_execution_hash`.\n"
        "\n"
        "## Identity\n"
        "\n"
        f"`{package}.COMPILATION_IDENTITY` carries what the compilation chain decided, and every\n"
        f"rendered file embeds that and only that. The hash of this project's bytes lives in\n"
        f"`{GENERATED_LOCK_FILENAME}` and in **no other file** — computed over every file except the\n"
        "lock itself, which is what stops the identity being a value the bytes it identifies\n"
        "contain (§7).\n"
        "\n"
        f"Rendered for hive `{engine_versions.hive}`, spark `{engine_versions.spark}`, metastore\n"
        f"`{engine_versions.metastore}`, java `{engine_versions.java}`.\n")


# ── the public entry points ──────────────────────────────────────────────────────────────────────


def render_project(
    authorized: AuthorizedCompilation,
    plan: FeatureGroupPlanV1,
    *,
    environment_id: str,
    engine_versions: EngineVersions,
    spine_input: PhysicalInputRequirement,
    nodes: Sequence[RenderedNode],
    publisher_selection: PublisherSelection | None = None,
) -> SealedProject:
    """Render the complete project for an AUTHORIZED group, and seal it under its identity (§7).

    The compilation identity is DERIVED here (``build_compilation_identity``) rather than accepted,
    so the bytes cannot embed an identity belonging to a different compilation — and that derivation
    also re-checks every planned ``ir_hash`` against the IR supplied for that column.

    ``nodes`` are the compute and gate nodes Tasks 13 and 14 render. They are injected because a node
    that decided its own dataset names would be writing storage locations into source, and because
    the wiring — not the compute — is what this module is able to check. It checks it: see
    :func:`_check_wiring`.

    Args:
        authorized: Gate 2's token (§1.3). The project is rendered from ITS IRs and ITS spine.
        plan: the packing list, which decides the published columns and the logical group name.
        environment_id: the environment this artifact is rendered FOR, recorded in the README. It is
            not a location and cannot redirect a read; every location is a catalog entry.
        engine_versions: what that environment was captured RUNNING (§0) — the project's pins.
        spine_input: the resolved physical requirement for the declared population's table.
        nodes: the pipeline's nodes, in the order they should be read.
        publisher_selection: §10.3's evidence that a publish mechanism was PROVEN for this
            environment at these engine versions, as returned by
            :func:`~featuregen.materialize.publish.select_publisher`. Omitting it renders Task 12's
            fail-closed publication entry, which cannot publish over anything — the default is the
            *less* capable artifact, so a caller cannot obtain a publishing project by forgetting
            an argument. Supplying one that disagrees with ``environment_id`` or
            ``engine_versions`` is refused: a selection proved a mechanism for a specific cluster
            at specific versions, and rendering it into a project built for another is exactly the
            un-evidenced publication §10.3 forbids.

    Returns:
        A :class:`~featuregen.materialize.identity.SealedProject`: every rendered file plus
        ``GENERATED.lock``, and the two-phase identity they were sealed under.

    Raises:
        TypeError: ``authorized`` is not an ``AuthorizedCompilation``, ``plan`` is not a
            ``FeatureGroupPlanV1``, ``spine_input`` is not a ``PhysicalInputRequirement``, or
            ``engine_versions`` is not an ``EngineVersions``.
        ValueError: the wiring does not close, a node's source does not define the function it
            wires, two datasets normalize to one name, the spine requirement names another table,
            or the IRs and the plan do not describe the same features.
    """
    # Checked FIRST, before anything is derived from it: §1.3 says no project is produced from an
    # unauthorized group, and a check that ran after `build_compilation_identity` would report a
    # missing attribute rather than the refusal it is.
    if not isinstance(authorized, AuthorizedCompilation):
        raise TypeError(
            f"render_project requires Gate 2's AuthorizedCompilation, got "
            f"{type(authorized).__name__}: §1.3 says no contract, group plan or PROJECT is produced "
            f"from an unauthorized group, and a bare list of IRs never showed that the group's "
            f"complete physical read set was authorized")
    if not isinstance(engine_versions, EngineVersions):
        raise TypeError(
            f"render_project requires an EngineVersions, got {type(engine_versions).__name__}: the "
            f"project pins its dependencies from what the environment was captured running, and a "
            f"loose mapping would pin it to whatever a caller happened to spell")
    if not isinstance(environment_id, str) or not environment_id.strip():
        raise ValueError(
            f"render_project needs the environment this artifact is rendered for "
            f"({environment_id!r}): it is what the capability attestation (§10.3) and every "
            f"validation report are keyed on, and a blank one names no environment at all")
    if publisher_selection is not None:
        if not isinstance(publisher_selection, PublisherSelection):
            raise TypeError(
                f"render_project's publisher_selection must be a PublisherSelection, got "
                f"{type(publisher_selection).__name__}: §10.3 says the renderer consumes a "
                f"selection and never a bare mechanism, and a duck-typed stand-in is a mechanism "
                f"with a different spelling")
        if publisher_selection.environment_id != environment_id:
            raise ValueError(
                f"the publisher selection was made for environment "
                f"{publisher_selection.environment_id!r} while this project is rendered for "
                f"{environment_id!r}: a capability attestation is for the EXACT environment "
                f"(§10.3), so carrying one across would publish on a capability nobody probed for "
                f"here")
        if publisher_selection.engine_versions != engine_versions:
            raise ValueError(
                f"the publisher selection was made against hive "
                f"{publisher_selection.engine_versions.hive} / spark "
                f"{publisher_selection.engine_versions.spark} / metastore "
                f"{publisher_selection.engine_versions.metastore} while this project pins hive "
                f"{engine_versions.hive} / spark {engine_versions.spark} / metastore "
                f"{engine_versions.metastore}: a mechanism proven on one triple is not proven on "
                f"another, and rendering the selection into a project built for different runtimes "
                f"would state evidence that does not cover it")

    supplied = tuple(nodes)
    for node in supplied:
        if not isinstance(node, RenderedNode):
            raise TypeError(
                f"render_project takes RenderedNode values, got {type(node).__name__}: a node's "
                f"wiring is checked against the catalog, and a bare source string carries no wiring "
                f"to check")
    if not supplied:
        raise ValueError(
            "render_project was given no nodes: a project with an empty pipeline builds, imports "
            "and runs, and publishes nothing — the one failure mode a parse check cannot see")
    node_parameters = {
        name.removeprefix("params:")
        for node in supplied
        for name in node.inputs
        if name.startswith("params:")
    }
    required_parameters = tuple(sorted({
        *REQUIRED_RUN_PARAMETERS,
        *node_parameters,
    }))

    compilation = build_compilation_identity(authorized.irs, plan)
    datasets = project_datasets(authorized, plan, spine_input=spine_input)
    _check_wiring(datasets, supplied)

    package = f"{derive_namespace()}_{plan.logical_group_name}"
    published_target = physical_target_for(plan.logical_group_name)
    entries = _catalog_entries(datasets, plan=plan, published_target=published_target,
                               selection=publisher_selection)
    source_root = f"src/{package}"

    files = {
        "pyproject.toml": _render_pyproject(package, engine_versions=engine_versions),
        "requirements.lock": _render_requirements(engine_versions),
        "README.md": _render_readme(
            plan, compilation, datasets, package=package, environment_id=environment_id,
            engine_versions=engine_versions, published_target=published_target,
            required_parameters=required_parameters),
        "conf/base/catalog.yml": _render_catalog(entries),
        "conf/base/parameters.yml": _render_parameters(required_parameters),
        "conf/base/logging.yml": _render_logging(),
        "conf/base/spark.yml": _render_spark(package),
        f"{source_root}/__init__.py": _render_package_init(compilation, package=package),
        f"{source_root}/settings.py": _render_settings(package),
        f"{source_root}/pipeline_registry.py": _render_pipeline_registry(package),
        f"{source_root}/hooks.py": _render_hooks(package, required_parameters),
        f"{source_root}/pipelines/__init__.py": _render_pipelines_init(package),
        f"{source_root}/pipelines/{PIPELINE_NAME}/__init__.py": _render_materialize_init(package),
        f"{source_root}/pipelines/{PIPELINE_NAME}/nodes.py": _render_nodes(
            supplied, package=package),
        f"{source_root}/pipelines/{PIPELINE_NAME}/pipeline.py": _render_pipeline(
            supplied, package=package),
    }
    return seal_project(compilation, dict(sorted(files.items())))


def materialize_to(project: SealedProject, root: str | os.PathLike[str]) -> pathlib.Path:
    """Write a sealed project to a real directory, and return that directory.

    The directory must not already hold anything. A file left over from an earlier render would
    still be part of the project on disk while not being part of the project the lock was computed
    over, so L0's ``PROJECT_HASH_MISMATCH`` (§11.2) would fire against a difference nobody made in
    this render — and a stale ``nodes.py`` would be imported in preference to nothing at all.

    Raises:
        TypeError: ``project`` is not a :class:`~featuregen.materialize.identity.SealedProject`.
            Only ``seal_project`` produces one, and it is the only path that writes a lock.
        ValueError: ``root`` exists and is not an empty directory.
    """
    if not isinstance(project, SealedProject):
        raise TypeError(
            f"materialize_to writes a SealedProject, got {type(project).__name__}: a bare mapping "
            f"of files has no GENERATED.lock, and a project on disk without one is a project whose "
            f"hash nothing states")
    directory = pathlib.Path(root)
    if directory.exists():
        if not directory.is_dir():
            raise ValueError(f"{directory} is not a directory: a project is a tree of files")
        if any(directory.iterdir()):
            raise ValueError(
                f"{directory} is not empty: a file left from an earlier render would be part of the "
                f"project on disk and not part of the project the lock was computed over, so L0 "
                f"would report a hash mismatch nobody introduced in this render")
    for path, text in sorted(project.files.items()):
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return directory
