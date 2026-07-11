"""Task 8 — field policies + resolve-and-project (the payoff / checkpoint).

Proves must-prove #4/#5/#6:
  * #4/#5 DISPLAY ≠ AUTHORITY — the flat ``graph_node`` column shows the resolver's DISPLAY value,
    but ``is_feature_eligible`` reads the DECISION's load-bearing value (present only when the
    operational rule + influence ceiling are satisfied), so an LLM-proposed concept is SHOWN yet is
    NOT feature-eligible.
  * #6 no safety/structural field is load-bearing from an LLM alone — the ``sensitivity`` floor
    RESTRICTS (``effective_restriction``) but does not CERTIFY (``classification_status='proposed'``),
    and a proposed-taxonomy ``additivity`` derivation never gates the operational value.

Seed (spec §5.1 field_evidence) for one column, then resolve_and_project and assert the projection.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import read_field_decisions
from featuregen.overlay.field_evidence import (
    canonical_hash,
    field_input_hash,
    record_field_evidence,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import is_feature_eligible, resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref

_SOURCE = "deposits"
_ROW = CanonicalRow(_SOURCE, "accounts", "balance", "numeric")
_REF = normalize_ref(_SOURCE, None, "accounts", "balance")
_OBJECT_REF = "public.accounts.balance"


def _seed(db, field_name, value, producer, strength):
    record_field_evidence(
        db,
        logical_ref=_REF,
        field_name=field_name,
        proposed_value=value,
        producer=producer,
        strength=strength,
        producer_ref="test-producer",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=_REF, field_name=field_name, material=value),
    )


@pytest.fixture
def resolved(db):
    """Seed the four canonical proposals (+ a proposed-taxonomy additivity), build the graph node,
    and resolve/project. Returns ``db`` with the projection applied."""
    build_graph(db, _SOURCE, [_ROW])
    _seed(db, "concept", "monetary_stock", EvidenceProducer.LLM, AssertionStrength.PROPOSED)
    _seed(db, "definition", "The ledger balance.", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    _seed(db, "logical_representation", "decimal", EvidenceProducer.PARSER, AssertionStrength.SUPPORTED)
    # A taxonomy-derived floor from a PROPOSED concept -> taxonomy/proposed (spec §3.2).
    _seed(db, "sensitivity_floor", "pii", EvidenceProducer.TAXONOMY, AssertionStrength.PROPOSED)
    _seed(db, "additivity", "semi_additive", EvidenceProducer.TAXONOMY, AssertionStrength.PROPOSED)
    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])
    return db


def _node(db, *cols):
    return db.execute(
        f"SELECT {', '.join(cols)} FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s",
        (_SOURCE, _OBJECT_REF),
    ).fetchone()


def test_display_concept_and_definition_projected_with_decision_link(resolved):
    concept, concept_decision_id, definition, definition_decision_id = _node(
        resolved, "concept", "concept_decision_id", "definition", "definition_decision_id"
    )
    assert concept == "monetary_stock"                       # DISPLAY value shown
    assert definition == "The ledger balance."
    # ...each carries a companion decision link pointing at the recorded decision.
    c_decision = read_field_decisions(resolved, _REF, "concept")[-1]
    assert concept_decision_id == c_decision.decision_event_id
    d_decision = read_field_decisions(resolved, _REF, "definition")[-1]
    assert definition_decision_id == d_decision.decision_event_id


def test_additivity_load_bearing_unresolved_from_proposed_concept(resolved):
    # §3.2: a taxonomy derivation from a PROPOSED concept is taxonomy/proposed and does NOT gate the
    # operational value — additivity is SHOWN but not load-bearing.
    additivity, additivity_decision_id = _node(resolved, "additivity", "additivity_decision_id")
    assert additivity == "semi_additive"                     # display still shows the derivation
    assert additivity_decision_id is not None
    decision = read_field_decisions(resolved, _REF, "additivity")[-1]
    assert decision.load_bearing_value_hash is None          # unresolved: authority insufficient
    assert is_feature_eligible(resolved, _REF, "additivity") is False


def test_sensitivity_floor_restricts_but_does_not_certify(resolved):
    effective_restriction, classification_status, sensitivity_decision_id = _node(
        resolved, "effective_restriction", "classification_status", "sensitivity_decision_id"
    )
    # pii floor maps to SENSITIVITY_ORDER 'restricted' and is NEVER lowered by an LLM/taxonomy proposal.
    assert effective_restriction == "restricted"
    # The floor RESTRICTS but does not CERTIFY: no source/human confirm -> classification stays proposed.
    assert classification_status == "proposed"
    assert sensitivity_decision_id is not None
    decision = read_field_decisions(resolved, _REF, "sensitivity")[-1]
    assert decision.display_value_hash == canonical_hash("restricted")  # the effective restriction
    assert decision.load_bearing_value_hash is None                     # not a certified classification


def test_decision_event_per_field_carries_both_effective_values(resolved):
    # An advisory field (concept): DISPLAY present, load-bearing absent.
    concept = read_field_decisions(resolved, _REF, "concept")[-1]
    assert concept.display_value_hash == canonical_hash("monetary_stock")
    assert concept.load_bearing_value_hash is None
    # An OPERATIONAL-limited field (logical_representation): a deterministic parser/supported signal
    # is load-bearing -> both effective values present.
    logical = read_field_decisions(resolved, _REF, "logical_representation")[-1]
    assert logical.display_value_hash == canonical_hash("decimal")
    assert logical.load_bearing_value_hash == canonical_hash("decimal")
    assert is_feature_eligible(resolved, _REF, "logical_representation") is True


def test_display_is_not_authority(resolved):
    # must-prove #4/#5: the flat display concept EXISTS...
    (concept,) = _node(resolved, "concept")
    assert concept == "monetary_stock"
    # ...AND the decision says it is NOT feature-eligible (an LLM proposal alone is never load-bearing).
    assert is_feature_eligible(resolved, _REF, "concept") is False
    # is_feature_eligible reads the DECISION, not the flat column: the load-bearing hash is absent.
    decision = read_field_decisions(resolved, _REF, "concept")[-1]
    assert decision.load_bearing_value_hash is None


def test_is_feature_eligible_false_when_no_decision(db):
    # A field with no decision at all is not feature-eligible (fail-closed).
    assert is_feature_eligible(db, _REF, "concept") is False
