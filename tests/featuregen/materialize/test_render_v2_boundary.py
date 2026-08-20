"""Where the renderer ACTUALLY stops accepting V2 — pinned, because it was reported as closed.

`render_project` and `project_datasets` were widened to accept either language's token and plan, and
that was reported as the renderer accepting V2. The outer type gate was widened; the internals were
not. Every entry point below `render_project` is typed on `FeatureGroupPlanV1` and
`MaterializationContractV1`, so a V2 plan raises before any code is emitted.

These tests exist so the boundary is a MEASURED fact rather than a claim in a commit message, and so
that closing it is a deliberate act that turns them red rather than something a signature change can
appear to have done.
"""
from __future__ import annotations

import inspect

import pytest

from featuregen.materialize.boundary_v2 import FeatureGroupPlanV2
from featuregen.materialize.group_plan import PlannedFeature
from featuregen.materialize.physical_types import PhysicalType
from featuregen.materialize.render import nodes_compute, nodes_gate
from featuregen.materialize.render.publish import published_dataset_name


def _v2_plan() -> FeatureGroupPlanV2:
    return FeatureGroupPlanV2(
        logical_group_name="customer_txn_features",
        materialization_contract_hash="sha256:contract",
        entity_key_columns=("cif_id",),
        business_dt_column="business_dt",
        features=(PlannedFeature(
            column_name="posted_amount_30d", ir_hash="sha256:ir",
            physical_type=PhysicalType(sql_type="BIGINT", nullable=False,
                                       rounding=None, overflow=None)),),
        physical_type_policy="formula-v2/physical-types@1")


def test_THE_PUBLISH_TARGET_REFUSES_A_V2_PLAN():
    """The first boundary a V2 render would hit, exercised rather than read.

    `published_dataset_name` is the ONE definition of the publication target — `project_datasets`
    reads it from here rather than spelling the name twice — so a V2 plan cannot reach a catalog
    entry, and no amount of widening `render_project`'s signature changes that.
    """
    with pytest.raises(TypeError, match="needs a FeatureGroupPlanV1"):
        published_dataset_name(_v2_plan())


@pytest.mark.parametrize(("where", "func"), [
    ("render_spine_node", nodes_compute.render_spine_node),
    ("render_assembly_node", nodes_gate.render_assembly_node),
])
def test_THE_NODE_RENDERERS_ARE_TYPED_ON_THE_V1_PLAN(where, func):
    """Not a runtime probe — the ANNOTATION, because that is what has to change and what a reader
    checking "does the renderer take V2 yet" will look at."""
    plan = inspect.signature(func).parameters["plan"].annotation
    assert "FeatureGroupPlanV1" in str(plan), (
        f"{where} now annotates plan as {plan!r}. If the V2 plan is genuinely accepted this test "
        f"should be deleted along with §0.8 — but only once `published_dataset_name` and the "
        f"contract-typed entry points move too, or the widening is cosmetic again")


def test_the_CALCULATION_node_takes_a_V2_IR_and_still_a_V1_PLAN():
    """The precise shape of the half-migration: step 8 made the IR side language-neutral, and the
    PLAN side was never touched. Both facts in one place so neither is mistaken for the other."""
    params = inspect.signature(nodes_compute.render_calculation_node).parameters
    assert "RenderableIR" in str(params["ir"].annotation)          # V1|V2 — done in step 8
    assert "FeatureGroupPlanV1" in str(params["plan"].annotation)  # V1 only — not done
