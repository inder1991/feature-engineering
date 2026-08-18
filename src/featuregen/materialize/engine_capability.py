"""D-5 — the kedro-pyspark engine's advertised capability, derived from its renderer.

Per D-5 this is CODE, not a table: an engine's capability describes what ``materialize/render/``
can emit, and that is a property of this build, not an operational fact somebody maintains. The
constant below is therefore BUILT from the renderer's own self-description
(:func:`~featuregen.materialize.render.nodes_compute.renderable_aggregations`) rather than typed
out — an aggregate the renderer cannot emit structurally cannot be advertised, and one it gains is
not advertised until this derivation sees it.

The two boolean advertisements are the dataclass's own fail-closed defaults, deliberately not
written here: ``materialize/render/`` contains no window-offset and no future-horizon rendering at
all, and ``EngineCapabilityV1`` says an engine that does not advertise a capability does not have
it. The test suite pins the absence — offset rendering added to the renderer without revisiting
this advertisement fails the build rather than staying silently unadvertised.
"""
from __future__ import annotations

from featuregen.formula.capability_v2 import EngineCapabilityV1
from featuregen.materialize.render.nodes_compute import renderable_aggregations

#: The one engine this platform renders for today. The id is an identity other rows point at
#: (work items, activation reads) — never parsed, never a version carrier.
KEDRO_PYSPARK_ENGINE_ID = "kedro-pyspark"

KEDRO_PYSPARK_ENGINE = EngineCapabilityV1(
    engine_id=KEDRO_PYSPARK_ENGINE_ID,
    supported_aggregations=frozenset(
        member.value for member in renderable_aggregations()),
)

_ENGINES: dict[str, EngineCapabilityV1] = {
    KEDRO_PYSPARK_ENGINE.engine_id: KEDRO_PYSPARK_ENGINE,
}


def engine_capability_for(engine_id: str) -> EngineCapabilityV1 | None:
    """The advertised capability of ``engine_id`` — ``None`` for an engine this build has never
    heard of, which every caller must treat as *unsupported*, never as a default."""
    return _ENGINES.get(engine_id)


def renderer_dispatch_surface() -> dict[tuple[str, str], bool]:
    """Every operator SIGNATURE this build's renderer can or cannot emit, derived from the renderer.

    The same discipline as :data:`KEDRO_PYSPARK_ENGINE` above, extended from aggregations to the
    whole operator vocabulary: an ability the renderer does not have is not advertised, and an
    ability it gains is not advertised until this derivation sees it. Nothing here is typed out.

    **Why `False` rows are written rather than omitted.** An absent row and a `False` row read the
    same to a caller that defaults, and differently to one that does not. Recording the negative
    makes "this build cannot do it" a fact in the table instead of an inference from silence — and
    it is the honest answer today for most of the vocabulary.

    The picture it produces is not flattering, which is the point: of fourteen kinds the renderer
    emits five, and of twenty-one aggregate functions it emits four.
    """
    from featuregen.formula.schema_v2 import AggregateFunctionV2, FinalOperationV2
    from featuregen.materialize.operator_graph_v2 import OperatorKindV2
    from featuregen.materialize.render.nodes_compute import _BODY_SLOTS, renderable_aggregations

    #: The kinds the renderer has a branch for. Named here because the renderer expresses them as
    #: node-rendering functions rather than as a table it can be asked — the one place in this
    #: derivation that is a reading of the renderer rather than a call into it.
    dispatchable_kinds = {
        OperatorKindV2.GOVERNED_SCAN, OperatorKindV2.PIT_AVAILABILITY_FILTER,
        OperatorKindV2.AGGREGATE, OperatorKindV2.SPINE_LEFT_JOIN,
        OperatorKindV2.GROUP_ASSEMBLY,
    }

    surface: dict[tuple[str, str], bool] = {
        (kind.value, "*"): kind in dispatchable_kinds for kind in OperatorKindV2
    }

    # AGGREGATE, per function — the distinction the typed signature exists for.
    emittable = {fn.value for fn in renderable_aggregations()}
    for function in AggregateFunctionV2:
        surface[("aggregate", function.value)] = function.value in emittable

    # FINAL_COMBINE, per operation. Derived from the renderer's own body-slot table: an operation
    # with no slots has no body path to render into, which is precisely `signed_sum` today.
    renderable_finals = {str(op) for op in _BODY_SLOTS}
    for operation in FinalOperationV2:
        surface[("final_combine", operation.value)] = operation.value in renderable_finals

    return surface
