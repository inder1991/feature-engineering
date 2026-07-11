"""Phase-3B.1 — resolved, versioned per-need binding metadata.

Derives each recipe need's cross-catalog binding facts from GOVERNED metadata — the source-grain
CONSTRAINT (concept.entity_link), the temporal role (concept.pit_role), and the join role (the
template's EXPLICIT anchor) — never a column name or a need's tuple position. Resolved once + versioned
so a plan replays exactly. Behaviour-neutral: nothing consumes this until the 3B.3 planner."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.templates import ALL_TEMPLATES, Need, Template

NEED_METADATA_VERSION = "1.0.0"

# concept.pit_role -> the governed TemporalRole. pit_role IS the per-concept temporal semantics;
# 'maturity' is a business future-date (not a binding temporal anchor) -> NONE.
_PIT_ROLE_TO_TEMPORAL: dict[str, TemporalRole] = {
    "none": TemporalRole.NONE,
    "event": TemporalRole.EVENT_TIME,
    "as_of": TemporalRole.AS_OF_TIME,
    "system_time": TemporalRole.INGESTION_TIME,
    "effective": TemporalRole.VALID_FROM,
    "valid_time": TemporalRole.VALID_FROM,
    "maturity": TemporalRole.NONE,
}

DerivationSource = Literal["explicit_recipe", "concept_registry", "template_default"]


@dataclass(frozen=True, slots=True)
class ResolvedNeedMetadataV1:
    """One need's resolved binding metadata + where each field came from. Immutable; the planner reads it."""
    role: str
    concept: str
    allowed_source_grains: tuple[str, ...]
    join_role: JoinRole
    temporal_role: TemporalRole
    grain_source: DerivationSource
    join_role_source: DerivationSource
    temporal_role_source: DerivationSource


def _entity_of(need: Need) -> str | None:
    c = concept(need.concept)
    return c.entity_link if c is not None else None


def validate_template_anchor(template: Template) -> None:
    """Raise ``ValueError`` on an ambiguous or invalid source anchor. If ``source_entity_need_role`` is
    set it MUST name an entity-linked need (checked unconditionally). Otherwise >1 DISTINCT entity-linked
    need with no explicit anchor is ambiguous (0 or 1 distinct entity key is fine)."""
    entity_needs = [n for n in template.needs if _entity_of(n) is not None]
    if template.source_entity_need_role is not None:
        if template.source_entity_need_role not in {n.role for n in entity_needs}:
            raise ValueError(
                f"template {template.id!r}: source_entity_need_role "
                f"{template.source_entity_need_role!r} is not an entity-linked need")
        return
    distinct = {_entity_of(n) for n in entity_needs}
    if len(distinct) > 1:
        raise ValueError(
            f"template {template.id!r}: {len(distinct)} distinct entity keys "
            f"({sorted(str(e) for e in distinct)}) but no source_entity_need_role")


def _source_anchor_role(template: Template) -> str | None:
    """The need role carrying the source grain: the explicit override, else the single entity-linked need.

    A VALIDATED template resolves 0 or 1 ``SOURCE_ENTITY_KEY`` (never more) — the 3B.3 planner must not
    assume exactly one. (``validate_template_anchor`` counts DISTINCT entity keys while this counts entity
    NEEDS, so ≥2 needs on the SAME entity with no explicit anchor would validate yet return None here → 0
    anchors; ``test_exactly_one_source_anchor_per_recipe_at_most`` asserts the ≤1 bound.)"""
    if template.source_entity_need_role is not None:
        return template.source_entity_need_role
    entity_needs = [n for n in template.needs if _entity_of(n) is not None]
    return entity_needs[0].role if len(entity_needs) == 1 else None


def _derive_one(template: Template, need: Need, anchor_role: str | None) -> ResolvedNeedMetadataV1:
    c = concept(need.concept)
    entity_link = c.entity_link if c is not None else None
    pit_role = c.pit_role if c is not None else "none"

    if need.allowed_source_grains:
        grains: tuple[str, ...] = need.allowed_source_grains
        grain_source: DerivationSource = "explicit_recipe"
    elif entity_link is not None:
        grains, grain_source = (entity_link,), "concept_registry"
    else:
        grains, grain_source = (), "template_default"

    join_role: JoinRole
    jr_source: DerivationSource
    if need.join_role is not None:
        join_role, jr_source = need.join_role, "explicit_recipe"
    elif need.role == anchor_role:
        join_role, jr_source = JoinRole.SOURCE_ENTITY_KEY, "template_default"
    elif entity_link is not None:
        join_role, jr_source = JoinRole.INTERMEDIATE_ENTITY_KEY, "concept_registry"
    elif pit_role != "none":
        join_role, jr_source = JoinRole.TIME, "concept_registry"
    else:
        join_role, jr_source = JoinRole.MEASURE, "template_default"

    temporal_role: TemporalRole
    tr_source: DerivationSource
    if need.temporal_role is not None:
        temporal_role, tr_source = need.temporal_role, "explicit_recipe"
    else:
        temporal_role = _PIT_ROLE_TO_TEMPORAL.get(pit_role, TemporalRole.NONE)
        tr_source = "concept_registry"

    return ResolvedNeedMetadataV1(
        role=need.role, concept=need.concept, allowed_source_grains=grains,
        join_role=join_role, temporal_role=temporal_role,
        grain_source=grain_source, join_role_source=jr_source, temporal_role_source=tr_source)


def derive_need_metadata(template: Template) -> tuple[ResolvedNeedMetadataV1, ...]:
    """Resolve every need of a template. Raises (via ``validate_template_anchor``) on an ambiguous anchor."""
    validate_template_anchor(template)
    anchor = _source_anchor_role(template)
    return tuple(_derive_one(template, n, anchor) for n in template.needs)


# Resolved ONCE at import over the whole recipe corpus — the immutable registry the 3B.3 planner reads.
# Fails FAST (at import) if any recipe's source anchor is ambiguous. Each entry resolves 0 or 1
# SOURCE_ENTITY_KEY (never more; see _source_anchor_role) — the planner must not assume exactly one.
# Wrapped in MappingProxyType so the mapping itself is read-only (values are already immutable tuples).
RESOLVED_NEED_METADATA: Mapping[str, tuple[ResolvedNeedMetadataV1, ...]] = MappingProxyType(
    {t.id: derive_need_metadata(t) for t in ALL_TEMPLATES})


def derivation_report() -> tuple[dict[str, object], ...]:
    """One row per (template, need): the resolved fields + where each came from — for inspection/audit."""
    return tuple(
        {"template": tid, "role": m.role, "concept": m.concept,
         "allowed_source_grains": m.allowed_source_grains,
         "join_role": m.join_role.value, "temporal_role": m.temporal_role.value,
         "grain_source": m.grain_source, "join_role_source": m.join_role_source,
         "temporal_role_source": m.temporal_role_source}
        for tid, metas in RESOLVED_NEED_METADATA.items() for m in metas)
