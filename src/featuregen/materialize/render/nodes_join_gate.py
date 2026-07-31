"""Render the pre-computation safety gate for one directional cross-catalog realization.

The control plane revalidates lifecycle, binding revisions, dependencies and deterministic evidence
before it renders or dispatches a project.  This node checks the property only real rows can answer:
under the realization's exact predicates, can one source tuple match more than one target row?

It returns the predicate-scoped target unchanged.  The projection consumes that output, so the frame
whose uniqueness was checked is exactly the frame it joins.  No de-duplication is permitted: choosing
one of several target rows would hide the failed relationship and silently select business meaning.
"""
from __future__ import annotations

from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.joins import CrossCatalogJoinStepV1
from featuregen.materialize.render.nodes_compute import _dataset, _safe_text
from featuregen.materialize.render.project import RenderedNode, slug
from featuregen.overlay.upload.bridge_realization import (
    AdditionalKeyRequirementV1,
    AsOfIntervalRequirementV1,
    FixedValueReferencePredicateV1,
)
from featuregen.overlay.upload.object_ref import parse_ref

__all__ = ["render_join_precondition_node"]


def _column(logical_ref: str) -> str:
    _source, _schema, _table, column = parse_ref(logical_ref)
    if column is None:
        raise ValueError(f"bridge join predicate/key {logical_ref!r} is not a column")
    return column


def _table(logical_ref: str) -> tuple[str, str]:
    source, _schema, table, _column_name = parse_ref(logical_ref)
    return source, table


def _target_predicates(
    step: CrossCatalogJoinStepV1,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return safe target-frame expressions and their named prepared value references."""
    target_table = _table(step.to_ref)
    expressions: list[str] = []
    values: list[str] = []

    def value(name: str) -> str:
        values.append(name)
        return f"F.lit(bridge_predicate_values[{name!r}])"

    for predicate in step.predicates:
        if isinstance(predicate, AdditionalKeyRequirementV1):
            raise ValueError(
                f"realization {step.realization_revision_id} still has unresolved additional key "
                f"{predicate.reason_code!r}")
        if isinstance(predicate, FixedValueReferencePredicateV1):
            if _table(predicate.logical_column_ref) != target_table:
                continue
            expressions.append(
                f"(F.col({_column(predicate.logical_column_ref)!r}) == "
                f"{value(predicate.value_ref)})")
            continue
        if isinstance(predicate, AsOfIntervalRequirementV1):
            bounds = (
                _table(predicate.effective_from_ref),
                _table(predicate.effective_to_ref),
            )
            if bounds[0] != bounds[1]:
                raise ValueError(
                    f"as-of predicate {predicate.predicate_id!r} spans two logical tables")
            if bounds[0] != target_table:
                continue
            as_of = value(predicate.as_of_value_ref)
            effective_from = _column(predicate.effective_from_ref)
            effective_to = _column(predicate.effective_to_ref)
            expressions.append(
                f"((F.col({effective_from!r}) <= {as_of}) & "
                f"(F.col({effective_to!r}).isNull() | "
                f"(F.col({effective_to!r}) > {as_of})))")
            continue
        raise TypeError(  # pragma: no cover - the realization contract closes the vocabulary
            f"unsupported bridge predicate {type(predicate).__name__}")
    return tuple(expressions), tuple(sorted(set(values)))


def render_join_precondition_node(
    step: CrossCatalogJoinStepV1,
    *,
    target_dataset: str,
    validated_dataset: str,
) -> RenderedNode:
    """Render one exact N:1/1:1 target-uniqueness gate and its scoped target output."""
    if not isinstance(step, CrossCatalogJoinStepV1):
        raise TypeError(
            "the bridge precondition renderer requires CrossCatalogJoinStepV1; a generic edge "
            "does not carry the exact directional realization evidence this gate records")
    _dataset(target_dataset, "the bridge gate target dataset")
    _dataset(validated_dataset, "the bridge gate validated output")
    if step.cardinality not in {"N:1", "1:1"}:
        raise ValueError(
            f"directional realization {step.realization_revision_id} is {step.cardinality!r}; "
            "this gate only validates a traversal that must fan in")

    target_columns = tuple(_column(pair[1]) for pair in step.column_pairs)
    if not target_columns or len(set(target_columns)) != len(target_columns):
        raise ValueError("the directional realization target tuple is empty or duplicated")
    predicates, value_refs = _target_predicates(step)
    suffix = slug(step.realization_revision_id[:16])
    func_name = f"validate_bridge_{suffix}"
    node_name = f"validate_bridge_{suffix}"
    parameters = ["target: DataFrame"]
    inputs = [target_dataset]
    if value_refs:
        parameters.append("bridge_predicate_values: dict[str, object]")
        inputs.append("params:bridge_predicate_values")

    lines = [
        f"def {func_name}({', '.join(parameters)}) -> DataFrame:",
        '    """Validate and return the exact predicate-scoped target for one bridge revision."""',
    ]
    if value_refs:
        lines.extend([
            f"    required_values = {value_refs!r}",
            "    missing_values = sorted(",
            "        name for name in required_values if name not in bridge_predicate_values)",
            "    if missing_values:",
            "        raise RuntimeError(",
            f"            {ValidationGateCode.RUN_PARAMETERS_MISSING.value!r} + ",
            '            ": missing bridge predicate value reference(s): " + repr(missing_values))',
        ])
    lines.append("    scoped = target")
    lines.extend(f"    scoped = scoped.where({predicate})" for predicate in predicates)
    non_null = " & ".join(
        f"F.col({column!r}).isNotNull()" for column in target_columns)
    grouped = ", ".join(f"F.col({column!r})" for column in target_columns)
    lines.extend([
        f"    joinable = scoped.where({non_null})",
        f"    duplicate = joinable.groupBy({grouped}).count().where(",
        "        F.col('count') > F.lit(1)).limit(1).count()",
        "    if duplicate:",
        "        raise RuntimeError(",
        f"            {ValidationGateCode.JOIN_AMPLIFICATION.value!r} + ",
        "            ': target tuple is not unique for directional realization ' +",
        f"            {_safe_text(step.realization_revision_id, 'realization revision')!r})",
        "    return scoped",
    ])
    return RenderedNode(
        name=node_name,
        func_name=func_name,
        source="\n".join(lines) + "\n",
        inputs=tuple(inputs),
        outputs=(validated_dataset,),
        imports=(
            "from pyspark.sql import DataFrame",
            "from pyspark.sql import functions as F",
        ),
        tags=("intermediate", "bridge_precondition"),
    )
