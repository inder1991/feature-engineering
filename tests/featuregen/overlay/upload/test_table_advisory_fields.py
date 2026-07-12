"""Task 8 — advisory table-field policies + display projection (migration 0986).

``table_role``/``primary_entity``/``event_or_snapshot`` are SHOWN on the table graph_node but can
NEVER be load-bearing: their policies carry the RECOMMENDATION influence ceiling, so however strong
the evidence, ``is_feature_eligible`` stays False (display ≠ authority, must-prove #4/#5). The
existing ``resolve_and_project`` is ref-shape agnostic — a TABLE logical_ref (``column=None``)
projects onto the ``public.<table>`` graph_node exactly like a column ref does.
"""
from __future__ import annotations

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import InfluenceTier, ResolutionMode
from featuregen.overlay.field_decision import read_field_decisions
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_policies import policy_for
from featuregen.overlay.upload.field_resolution import is_feature_eligible, resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref

_SOURCE = "deposits"
_ROW = CanonicalRow(_SOURCE, "transactions", "amount", "numeric")
_TABLE_REF = normalize_ref(_SOURCE, None, "transactions")   # TABLE ref: column=None
_TABLE_OBJECT_REF = "public.transactions"


def test_table_role_is_recommendation_ceilinged():
    p = policy_for("table_role")
    assert p is not None
    assert p.influence_max is InfluenceTier.RECOMMENDATION      # never load-bearing
    assert p.resolution_mode is ResolutionMode.GENERIC_FIELD


def test_primary_entity_is_recommendation_ceilinged():
    p = policy_for("primary_entity")
    assert p is not None and p.influence_max is InfluenceTier.RECOMMENDATION


def test_event_or_snapshot_is_recommendation_ceilinged():
    p = policy_for("event_or_snapshot")
    assert p is not None
    assert p.influence_max is InfluenceTier.RECOMMENDATION
    assert p.resolution_mode is ResolutionMode.GENERIC_FIELD


def _seed_table_evidence(db, field_name, value):
    record_field_evidence(
        db,
        logical_ref=_TABLE_REF,
        field_name=field_name,
        proposed_value=value,
        producer=EvidenceProducer.LLM,
        strength=AssertionStrength.PROPOSED,
        producer_ref="test-producer",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=_TABLE_REF, field_name=field_name, material=value),
    )


def test_table_role_projected_to_table_node_but_never_feature_eligible(db):
    """Display ≠ authority on a TABLE node: the table graph_node SHOWS table_role='fact' with a
    decision link, yet the RECOMMENDATION ceiling keeps load-bearing unresolved — the table is NOT
    feature-eligible however the evidence looks."""
    build_graph(db, _SOURCE, [_ROW])   # creates the 'public.transactions' table node too
    _seed_table_evidence(db, "table_role", "fact")
    _seed_table_evidence(db, "primary_entity", "account")
    resolve_and_project(db, source=_SOURCE, logical_refs=[_TABLE_REF])

    table_role, table_role_decision_id, primary_entity = db.execute(
        "SELECT table_role, table_role_decision_id, primary_entity FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s",
        (_SOURCE, _TABLE_OBJECT_REF),
    ).fetchone()
    assert table_role == "fact"                                  # DISPLAY value shown on the table node
    assert primary_entity == "account"
    decision = read_field_decisions(db, _TABLE_REF, "table_role")[-1]
    assert table_role_decision_id == decision.decision_event_id  # display ≠ authority pointer
    # ...and the RECOMMENDATION ceiling means the load-bearing value stays unresolved.
    assert decision.load_bearing_value_hash is None
    assert is_feature_eligible(db, _TABLE_REF, "table_role") is False
    assert is_feature_eligible(db, _TABLE_REF, "primary_entity") is False
