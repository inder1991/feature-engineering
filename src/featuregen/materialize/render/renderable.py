"""What this renderer accepts — the three unions, in one place with no imports back into `render/`.

There is ONE renderer, and it emits code for both formula languages. These aliases say which objects
it takes; they live here rather than in `nodes_compute` because `publish` and `nodes_gate` need them
too and `nodes_compute` already imports from `project`, which imports `publish` — a union defined
there closes that loop.

**The unions are measured, not assumed.** Across `render/` and `compile/wiring.py` the only contract
attributes read are ``ordered_keys`` and ``pit_semantics``; the V1 and V2 contracts differ in exactly
ONE field (``physical_type_policy_version``, an ordinal, versus ``physical_type_policy``, a named
policy) and the plans differ in that same one. Every attribute the renderer actually touches is
shared, which is why this is a union rather than a second renderer.

▲ **The mistake this replaces.** An earlier commit widened `render_project`'s OUTER gate to accept a
V2 token and plan and reported the renderer as accepting V2. It did not: every entry point beneath it
was still typed on V1, so a V2 plan raised at `published_dataset_name` before a line of code was
emitted. A signature change that looks like a capability change is the failure mode here, so the
acceptance is proved by RENDERING a project from a V2 plan and contract and comparing the emitted
source — never by reading an annotation.
"""
from __future__ import annotations

from featuregen.materialize.boundary_v2 import FeatureGroupPlanV2, FormulaExecutionIRV2
from featuregen.materialize.contract import MaterializationContractV1
from featuregen.materialize.contract_v2 import MaterializationContractV2
from featuregen.materialize.group_plan import FeatureGroupPlanV1
from featuregen.materialize.ir import FormulaExecutionIRV1

__all__ = ["RenderableContract", "RenderableIR", "RenderablePlan"]

#: V1 and V2 carry the same ten IR fields under the same names (V2 adds two), so every function
#: reads them structurally and none branches on which arrived.
RenderableIR = FormulaExecutionIRV1 | FormulaExecutionIRV2
RenderablePlan = FeatureGroupPlanV1 | FeatureGroupPlanV2
RenderableContract = MaterializationContractV1 | MaterializationContractV2
