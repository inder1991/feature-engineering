"""Phase-3B.3b — cross-catalog assembly: eligibility, source-entity resolution, semantic paths, the
physical-transition physics, and the bounded frontier search. Read-only, deterministic."""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.need_metadata import ResolvedNeedMetadataV1, derive_need_metadata
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    EntityCompatibility,
    EntitySemanticPathV1,
)
from featuregen.overlay.upload.templates import Template


@dataclass(frozen=True, slots=True)
class EligibilityV1:
    eligible: bool
    source_entity: str | None
    reason: ReasonCode | None


def _resolved(template: Template) -> tuple[ResolvedNeedMetadataV1, ...]:
    """The GOVERNED per-need resolution (3B.1) — reuse it; never re-derive source grain from concepts here.
    ``derive_need_metadata`` is the pure function behind the ``RESOLVED_NEED_METADATA`` corpus registry and
    raises ``ValueError`` on an ambiguous anchor (the caller treats that as not-eligible)."""
    return derive_need_metadata(template)


def resolve_source_entity(template: Template) -> str | None:
    """The recipe's single source-grain entity, from the GOVERNED 3B.1 resolution: the sole need resolved to
    ``JoinRole.SOURCE_ENTITY_KEY`` and its single ``allowed_source_grain``. 0-or-many source keys, a source key
    with 0-or-many grains, or an ambiguous anchor -> None (never guessed from whichever catalog bound)."""
    try:
        metas = _resolved(template)
    except ValueError:
        return None
    sources = [m for m in metas if m.join_role is JoinRole.SOURCE_ENTITY_KEY]
    if len(sources) != 1:
        return None
    grains = sources[0].allowed_source_grains
    return grains[0] if len(grains) == 1 else None


def ingredient_eligibility(template: Template) -> EligibilityV1:
    """3B.3b handles SOURCE-GRAIN ingredients only. A recipe with no single governed source grain is SKIPPED
    (eligible=False, reason=None — not a rejection; it stays an ingredient-binding-only tier-1 candidate). A
    REQUIRED need governed to a single grain DIFFERENT from the source (a second entity that would need its own
    roll-up, e.g. a resolved ``INTERMEDIATE_ENTITY_KEY``) -> unsupported_multi_grain_ingredients. Optional needs
    and entity-neutral MEASURE/TIME needs (unconstrained grains) never gate."""
    source = resolve_source_entity(template)
    if source is None:
        return EligibilityV1(False, None, None)
    by_role = {m.role: m for m in _resolved(template)}
    for need in template.needs:
        if need.optional:
            continue
        m = by_role.get(need.role)
        if m is None:
            continue
        grains = m.allowed_source_grains
        if len(grains) == 1 and grains[0] != source:
            return EligibilityV1(False, source, ReasonCode.unsupported_multi_grain_ingredients)
    return EligibilityV1(True, source, None)


def semantic_rollup_paths(source_entity: str, target_entity: str
                          ) -> tuple[tuple[EntitySemanticPathV1, ...], EntityCompatibility]:
    """The governed roll-up paths source->target. EXACT (source==target) -> (); DERIVABLE -> one path;
    AMBIGUOUS -> >=2; UNKNOWN -> ()."""
    res = resolve_entity_compatibility(source_entity, target_entity, ENTITY_GRAPH)
    return res.paths, res.status
