"""The renderer accepts a V2 plan and contract — proved by RENDERING, never by annotation.

This module previously pinned the OPPOSITE: `render_project`'s outer gate had been widened and the
renderer reported as accepting V2, while every entry point beneath it was still typed on V1, so a V2
plan raised at `published_dataset_name` before a line of code was emitted.

That is why nothing here reads a type hint. Each test drives a real V1 object and a real V2 object
through the same function and compares the OUTPUT. A signature change cannot make these pass; only
the renderer actually emitting the same code can.

**Why identical output is the right assertion.** The V1 and V2 plans differ in exactly one field —
`physical_type_policy_version` (an ordinal) versus `physical_type_policy` (a named policy) — and the
renderer reads neither when emitting nodes. Every attribute it touches is shared. So a V2 plan must
produce byte-identical code, and anything else means the renderer is branching on the language
somewhere it should not.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.boundary_v2 import FeatureGroupPlanV2
from featuregen.materialize.group_plan import FeatureGroupPlanV1, PlannedFeature
from featuregen.materialize.physical_types import PhysicalType
from featuregen.materialize.render.publish import published_dataset_name
from featuregen.materialize.render.renderable import (
    RenderableContract,
    RenderableIR,
    RenderablePlan,
)

_FEATURES = (PlannedFeature(
    column_name="posted_amount_30d", ir_hash="sha256:ir",
    physical_type=PhysicalType(sql_type="BIGINT", nullable=False,
                               rounding=None, overflow=None)),)

_SHARED = dict(logical_group_name="customer_txn_features",
               materialization_contract_hash="sha256:contract",
               entity_key_columns=("cif_id",), business_dt_column="business_dt",
               features=_FEATURES)


def _v1_plan() -> FeatureGroupPlanV1:
    return FeatureGroupPlanV1(**_SHARED, physical_type_policy_version=1)


def _v2_plan() -> FeatureGroupPlanV2:
    return FeatureGroupPlanV2(**_SHARED, physical_type_policy="formula-v2/physical-types@1")


# ══ PROVED BY RENDERING ════════════════════════════════════════════════════════════════════════
def test_THE_PUBLISH_TARGET_ACCEPTS_A_V2_PLAN_AND_NAMES_THE_SAME_DATASET():
    """The boundary that used to raise. `published_dataset_name` is the ONE definition of the
    publication target — `project_datasets` reads it from here rather than spelling the name twice —
    so if a V2 plan cannot reach it, nothing V2 can reach a catalog entry."""
    assert published_dataset_name(_v2_plan()) == published_dataset_name(_v1_plan())
    assert published_dataset_name(_v2_plan()) == "feature_customer_txn_features"


def test_A_PLAN_THAT_IS_NEITHER_IS_STILL_REFUSED():
    """Widening is not loosening. The gate accepts two named types and nothing else — a mapping that
    happens to carry the right keys is a caller assembling the call wrongly."""
    with pytest.raises(TypeError):
        published_dataset_name({"logical_group_name": "customer_txn_features"})   # type: ignore[arg-type]


def test_A_WHOLE_NODE_RENDERS_IDENTICALLY_FROM_A_V2_PLAN():
    """The strongest form: render real Spark source from a V1 plan and from a V2 plan and diff it.

    `render_assembly_node` is the node that reads the MOST off the plan — the published columns, the
    entity keys, the business-date column, the schema hash — so if any of those readings branched on
    the plan's generation, the emitted source would differ here first.

    Byte-identical is the correct expectation, not merely "both succeed": the one field the two
    plans disagree about is the physical-type policy, which the assembly node never reads.
    """
    from featuregen.materialize.render.nodes_gate import render_assembly_node

    kwargs = dict(spine_dataset="primary_spine", staging_datasets={"posted_amount_30d": "stg"},
                  manifest_datasets={"posted_amount_30d": "man"}, assembled_dataset="assembled")
    from_v1 = render_assembly_node(_v1_plan(), **kwargs)
    from_v2 = render_assembly_node(_v2_plan(), **kwargs)

    assert from_v2.source == from_v1.source
    assert from_v2.func_name == from_v1.func_name
    # And it is real emitted code, not an empty string that would compare equal to itself.
    assert "def " in from_v2.source and "posted_amount_30d" in from_v2.source


# ══ THE UNIONS ARE DECLARED ONCE ═══════════════════════════════════════════════════════════════
def test_THE_RENDERABLE_UNIONS_NAME_BOTH_GENERATIONS():
    """One home, so `nodes_compute`, `nodes_gate` and `publish` cannot drift about what they take —
    and so the union cannot be widened in one file while another still refuses."""
    for union, v1, v2 in (
        (RenderablePlan, "FeatureGroupPlanV1", "FeatureGroupPlanV2"),
        (RenderableContract, "MaterializationContractV1", "MaterializationContractV2"),
        (RenderableIR, "FormulaExecutionIRV1", "FormulaExecutionIRV2"),
    ):
        assert v1 in str(union) and v2 in str(union), union


def test_NO_RENDER_MODULE_STILL_GATES_ON_THE_V1_PLAN_ALONE():
    """The structural half. An `isinstance(plan, FeatureGroupPlanV1)` left anywhere under `render/`
    would refuse a V2 plan at that one call site while every annotation said otherwise — which is
    exactly the shape of the mistake this file was written to catch."""
    import inspect
    import pathlib
    import re

    from featuregen.materialize.render import nodes_compute

    root = pathlib.Path(inspect.getfile(nodes_compute)).parent
    offences = [
        f"{path.name}:{n}"
        for path in sorted(root.rglob("*.py"))
        for n, line in enumerate(path.read_text().split("\n"), 1)
        if re.search(r"isinstance\([^)]*,\s*FeatureGroupPlanV1\s*\)", line)
        or re.search(r"isinstance\([^)]*,\s*MaterializationContractV1\s*\)", line)
    ]
    assert offences == [], (
        f"these gates accept only the V1 shape: {offences}. Every one of them refuses a V2 plan or "
        f"contract regardless of what the signatures say")
