"""Phase-3A Task 3A.5 — THE load-bearing characterization test: the graph resolver reproduces the deleted
_ENTITY_ROLLUP map EXACTLY, for EVERY pair in the full known_entities() vocabulary (not just the seven in
the roll-ups). EXPECTED is computed from the OLD map's semantics, frozen here since the map is deleted."""
from __future__ import annotations

from dataclasses import fields

from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility
from featuregen.overlay.upload.taxonomy.ranking import RankSignals  # existing (Task A2)
from featuregen.overlay.upload.taxonomy.ranking_signals import (
    EntityCompatibility as _ReExported,
)
from featuregen.overlay.upload.taxonomy.ranking_signals import (
    entity_compatibility,
)

_OLD_ROLLUP = {
    "account": "customer", "card_account": "customer", "transaction": "account",
    "facility": "obligor", "policy": "customer"}
_ENTITIES = tuple(sorted(known_entities()))


def _old_status(source: str, target: str) -> EntityCompatibility:
    if source == target:
        return EntityCompatibility.EXACT
    seen: set[str] = set()
    cur: str | None = source
    while cur is not None and cur not in seen:
        seen.add(cur)
        cur = _OLD_ROLLUP.get(cur)
        if cur == target:
            return EntityCompatibility.DERIVABLE
    return EntityCompatibility.UNKNOWN


def test_graph_reproduces_old_rollup_for_every_known_entity_pair():
    for source in _ENTITIES:
        for target in _ENTITIES:
            assert resolve_entity_compatibility(source, target, ENTITY_GRAPH).status \
                == _old_status(source, target), f"{source}->{target}"


def test_no_pair_produces_ambiguous():
    for source in _ENTITIES:
        for target in _ENTITIES:
            assert resolve_entity_compatibility(source, target, ENTITY_GRAPH).status \
                is not EntityCompatibility.AMBIGUOUS


def test_rank_signals_gained_no_graph_metadata():
    # The route serializes RankSignals-derived fields; 3A must not leak graph provenance into them.
    names = {f.name for f in fields(RankSignals)}
    assert "graph_version" not in names
    assert "paths_truncated" not in names
    assert "paths" not in names


def test_adapter_still_returns_the_bare_enum_reexported_from_ranking_signals():
    # entity_compatibility returns the EntityCompatibility MEMBER (not EntityCompatibilityResultV1),
    # and EntityCompatibility is still importable from ranking_signals (re-export preserved).
    from featuregen.overlay.upload.templates import Need, Template
    t = Template(id="t", family="f", intent="i", needs=(Need("entity", "customer_id"),),
                 params={}, aggregation="avg", additivity="additive",
                 explain="H", use_cases=(), pit="trailing window (as_of − {window}, as_of].")
    assert entity_compatibility(t, target_entity="customer") is _ReExported.EXACT
