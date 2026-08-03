"""Task 0S — the shared evidence-axis contracts at their frozen home (0F-4 / 0F-6, ledger §2).

``featuregen.contracts.evidence_axes`` owns ``EvidenceAuthorityV1``, ``SemanticValueV1``,
``AttributedLabelV1`` and ``AttributedTextV1``. It IMPORTS the three axis enums from
``featuregen.overlay.evidence`` (one definition — never a copy), serializes evidence for
Task 2, and embodies the 0F-4 rule-3 hash canonicalization: only (producer, strength,
lifecycle) enter semantic hashes; occurrence provenance never re-keys a revision.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.canonical import contract_hash_v1
from featuregen.contracts import evidence_axes
from featuregen.contracts.contract_versions import (
    register_contract_version,
    registered_contract_versions,
)
from featuregen.contracts.evidence_axes import (
    ATTRIBUTED_BASIS_VALUES,
    AttributedLabelV1,
    AttributedTextV1,
    EvidenceAuthorityV1,
    SemanticValueV1,
    attributed_label_to_json,
    attributed_text_to_json,
    canonical_evidence_axes,
    evidence_to_json,
    semantic_value_to_json,
)
from featuregen.overlay.evidence import AssertionStrength, EvidenceLifecycle, EvidenceProducer


def _evidence(producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
              lifecycle=EvidenceLifecycle.ACTIVE, producer_ref=None, evidence_id=None):
    return EvidenceAuthorityV1(producer=producer, strength=strength, lifecycle=lifecycle,
                               producer_ref=producer_ref, evidence_id=evidence_id)


# ── One definition: the axis enums are the existing overlay vocabularies, imported ──────────────


def test_axis_enums_are_the_overlay_evidence_objects_not_copies():
    assert evidence_axes.EvidenceProducer is EvidenceProducer
    assert evidence_axes.AssertionStrength is AssertionStrength
    assert evidence_axes.EvidenceLifecycle is EvidenceLifecycle


def test_frozen_field_lists_match_the_0f_freeze_exactly():
    assert [f.name for f in dataclasses.fields(EvidenceAuthorityV1)] == [
        "producer", "strength", "lifecycle", "producer_ref", "evidence_id"]
    assert [f.name for f in dataclasses.fields(SemanticValueV1)] == [
        "field_name", "value", "evidence", "resolution_status", "operational_influence"]
    assert [f.name for f in dataclasses.fields(AttributedLabelV1)] == [
        "id", "display_name", "basis", "evidence", "operational_influence", "source_refs"]
    assert [f.name for f in dataclasses.fields(AttributedTextV1)] == [
        "value", "basis", "evidence", "operational_influence", "source_refs"]


def test_contracts_are_immutable():
    ev = _evidence()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.producer = EvidenceProducer.HUMAN  # type: ignore[misc]


# ── Vocabulary enforcement ──────────────────────────────────────────────────────────────────────


def test_evidence_axis_values_are_coerced_to_the_owner_enums():
    ev = EvidenceAuthorityV1(producer="llm", strength="proposed", lifecycle="active",
                             producer_ref=None, evidence_id=None)  # type: ignore[arg-type]
    assert ev.producer is EvidenceProducer.LLM
    assert ev.strength is AssertionStrength.PROPOSED
    assert ev.lifecycle is EvidenceLifecycle.ACTIVE


def test_unknown_axis_value_fails_loudly():
    with pytest.raises(ValueError):
        EvidenceAuthorityV1(producer="oracle", strength="proposed", lifecycle="active",
                            producer_ref=None, evidence_id=None)  # type: ignore[arg-type]


def test_attributed_basis_vocabulary_is_frozen():
    assert ATTRIBUTED_BASIS_VALUES == frozenset(
        {"template_authored", "catalog_resolved", "human", "llm_proposed"})
    with pytest.raises(ValueError):
        AttributedTextV1(value="retail lending", basis="verified", evidence=(),
                         operational_influence=None, source_refs=())


def test_operational_influence_is_read_never_widened():
    for allowed in (None, "governed", "hint"):
        AttributedTextV1(value="x", basis="human", evidence=(),
                         operational_influence=allowed, source_refs=())
    with pytest.raises(ValueError):
        AttributedTextV1(value="x", basis="human", evidence=(),
                         operational_influence="load_bearing", source_refs=())
    with pytest.raises(ValueError):
        SemanticValueV1(field_name="domain", value="x", evidence=(),
                        resolution_status="resolved", operational_influence="authoritative")


# ── 0F-4 rule 3: occurrence provenance never enters a semantic hash ─────────────────────────────


def test_canonical_evidence_axes_excludes_occurrence_provenance():
    ev = _evidence(producer_ref="enrich-batch-7", evidence_id="eviu_01")
    assert canonical_evidence_axes((ev,)) == [
        {"producer": "llm", "strength": "proposed", "lifecycle": "active"}]


def test_replaying_identical_evidence_under_a_new_event_id_changes_no_hash():
    register_contract_version("task0s-evidence-probe", "1",
                              owner="tests.featuregen.contracts.test_evidence_axes")
    first = (_evidence(evidence_id="eviu_01", producer_ref="run-1"),)
    replayed = first + (_evidence(evidence_id="eviu_02", producer_ref="run-2"),)
    h1 = contract_hash_v1("task0s-evidence-probe", "1",
                          {"value": "x", "evidence": canonical_evidence_axes(first)})
    h2 = contract_hash_v1("task0s-evidence-probe", "1",
                          {"value": "x", "evidence": canonical_evidence_axes(replayed)})
    assert h1 == h2


def test_canonical_evidence_axes_is_order_independent_but_axis_sensitive():
    a = _evidence(producer=EvidenceProducer.SOURCE, strength=AssertionStrength.SUPPORTED)
    b = _evidence(producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED)
    assert canonical_evidence_axes((a, b)) == canonical_evidence_axes((b, a))
    stronger = _evidence(producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED)
    assert canonical_evidence_axes((a,)) != canonical_evidence_axes((stronger,))


# ── Wire serialization (Task 2 imports these; no local substitutes) ─────────────────────────────


def test_evidence_wire_shape_preserves_occurrence_provenance():
    ev = _evidence(producer_ref="enrich-batch-7", evidence_id="eviu_01")
    assert evidence_to_json(ev) == {
        "producer": "llm", "strength": "proposed", "lifecycle": "active",
        "producer_ref": "enrich-batch-7", "evidence_id": "eviu_01"}


def test_semantic_value_wire_shape():
    value = SemanticValueV1(field_name="authority_role", value="system_of_record",
                            evidence=(_evidence(),), resolution_status="proposed",
                            operational_influence=None)
    assert semantic_value_to_json(value) == {
        "field_name": "authority_role", "value": "system_of_record",
        "evidence": [{"producer": "llm", "strength": "proposed", "lifecycle": "active",
                      "producer_ref": None, "evidence_id": None}],
        "resolution_status": "proposed", "operational_influence": None}


def test_attributed_wire_shapes():
    label = AttributedLabelV1(id="uc_liquidity", display_name="Liquidity", basis="human",
                              evidence=(_evidence(producer=EvidenceProducer.HUMAN,
                                                  strength=AssertionStrength.CONFIRMED),),
                              operational_influence=None, source_refs=("tpl:t1",))
    assert attributed_label_to_json(label) == {
        "id": "uc_liquidity", "display_name": "Liquidity", "basis": "human",
        "evidence": [{"producer": "human", "strength": "confirmed", "lifecycle": "active",
                      "producer_ref": None, "evidence_id": None}],
        "operational_influence": None, "source_refs": ["tpl:t1"]}
    text = AttributedTextV1(value="retail lending", basis="llm_proposed", evidence=(),
                            operational_influence=None, source_refs=())
    assert attributed_text_to_json(text) == {
        "value": "retail lending", "basis": "llm_proposed", "evidence": [],
        "operational_influence": None, "source_refs": []}


# ── Bullet 5: the owner module registers its serialized contract versions at import ─────────────


def test_evidence_axes_contract_versions_are_registered_to_their_owner():
    registry = registered_contract_versions()
    for name in ("evidence-authority", "semantic-value", "attributed-label", "attributed-text"):
        assert registry[(name, "1")] == "featuregen.contracts.evidence_axes", name
