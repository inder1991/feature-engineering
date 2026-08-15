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
