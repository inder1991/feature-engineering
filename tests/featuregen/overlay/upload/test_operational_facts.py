"""Delivery C1 Task 1 — the operational-facts adapter (12-axis OperationalValue).

Proves the adapter EXTENDS (never replaces) the shipped OperationalColumnFacts: the same VALUE, plus
the rich authority axes (influence / producer / strength / status / conflict / audit ids / fact
key+event / versions). Asserts the base fail-closed cases (no_decision / retired / conflict /
non-operational) and that ``status == "resolved"`` is exactly ``is_feature_eligible`` — authority is
read from the decision lifecycle + selected evidence, never manufactured.

Seeding mirrors the shipped suites: the real resolver (``resolve_and_project`` over seeded
``field_evidence``) for the decision-governed cases, and a direct graph_node write for the
SPECIALIZED_FACT (grain) case, exactly as test_field_resolution / test_column_authority do.
"""
from __future__ import annotations

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import InfluenceTier
from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import (
    canonical_hash,
    field_input_hash,
    record_field_evidence,
)
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import read_column_facts
from featuregen.overlay.upload.field_resolution import (
    FIELD_POLICY_VERSION,
    RESOLVER_VERSION,
    is_feature_eligible,
    resolve_and_project,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.operational_facts import OperationalValue, read_operational_value
from featuregen.overlay.upload.upload_catalog import table_ref

_SOURCE = "deposits"
_ROW = CanonicalRow(_SOURCE, "accounts", "balance", "numeric")
_REF = normalize_ref(_SOURCE, None, "accounts", "balance")
_OBJECT_REF = "public.accounts.balance"


def _seed(db, field_name, value, producer, strength):
    record_field_evidence(
        db, logical_ref=_REF, field_name=field_name, proposed_value=value,
        producer=producer, strength=strength, producer_ref="test-producer",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=_REF, field_name=field_name, material=value))


def _set_col(db, **cols):
    assignments = ", ".join(f"{k} = %s" for k in cols)
    db.execute(
        f"UPDATE graph_node SET {assignments} WHERE catalog_source = %s AND object_ref = %s",
        [*cols.values(), _SOURCE, _OBJECT_REF])


# 1. A governed additive column: resolved decision + source-attested load-bearing evidence.
def test_governed_additive_column_is_operational(db):
    build_graph(db, _SOURCE, [_ROW])
    _seed(db, "additivity", "non_additive", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])

    ov = read_operational_value(db, _REF, "additivity")
    assert isinstance(ov, OperationalValue)
    assert ov.status == "resolved"
    assert ov.influence is InfluenceTier.OPERATIONAL
    assert ov.producer is EvidenceProducer.SOURCE           # the SELECTED evidence's producer
    assert ov.strength is AssertionStrength.ATTESTED        # its strength (strongest selected)
    assert ov.selected_evidence_ids                         # non-empty
    assert ov.decision_event_id is not None
    assert ov.conflict_status == "resolved"
    assert ov.policy_version == FIELD_POLICY_VERSION
    assert ov.resolver_version == RESOLVER_VERSION
    # value is byte-for-byte the shipped OperationalColumnFacts.value.
    assert ov.value == read_column_facts(db, _REF, "additivity").value == "non_additive"
    # operational <=> feature-eligible (the strict superset invariant).
    assert is_feature_eligible(db, _REF, "additivity") is True


# 2. A grain column (SPECIALIZED_FACT): governed by the fact stream, fact axes populated.
def test_grain_column_governed_populates_fact_axes(db):
    build_graph(db, _SOURCE, [_ROW])
    _set_col(db, is_grain=True, grain_fact_event_id="evt_grain_1")

    ov = read_operational_value(db, _REF, "is_grain")
    assert ov.status == "resolved"
    assert ov.fact_event_id == "evt_grain_1"                # the audit link (== read_column_facts)
    assert ov.fact_key == fact_key(table_ref(_SOURCE, "accounts"), "grain")  # canonical fact key
    assert ov.value == "true" == read_column_facts(db, _REF, "is_grain").value
    assert ov.decision_event_id is None                     # fact-stream governed, not a decision
    assert ov.influence is InfluenceTier.DISPLAY            # no policy -> lowest tier


# 3. A RECOMMENDATION field (business_term): never operational, even with source-attested evidence.
def test_recommendation_field_never_operational(db):
    build_graph(db, _SOURCE, [_ROW])
    _seed(db, "business_term", "Account Balance", EvidenceProducer.SOURCE,
          AssertionStrength.ATTESTED)
    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])

    ov = read_operational_value(db, _REF, "business_term")
    assert ov.influence is InfluenceTier.RECOMMENDATION
    assert ov.status == "no_value"                          # non-operational (not a conflict)
    assert ov.status != "resolved"
    assert ov.conflict_status == "influence_not_operational"
    assert is_feature_eligible(db, _REF, "business_term") is False


# 4. No decision -> status="no_decision", producer/strength None, display value echoed.
def test_no_decision_reports_display_only(db):
    build_graph(db, _SOURCE, [_ROW])
    _set_col(db, additivity="non_additive")

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "no_decision"
    assert ov.value == read_column_facts(db, _REF, "additivity").value == "non_additive"
    assert ov.producer is None and ov.strength is None
    assert ov.decision_event_id is None
    assert ov.selected_evidence_ids == ()


# 5. A RETIRED (staled) decision -> status="retired", not served as load-bearing.
def test_retired_decision_not_served_operational(db):
    build_graph(db, _SOURCE, [_ROW])
    _set_col(db, additivity="non_additive")
    record_field_decision(
        db, logical_ref=_REF, field_name="additivity",
        event_type=FieldDecisionEventType.STALED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=None,
        load_bearing_value_hash=None, conflict_status="staled", reason_codes=["evidence_staled"],
        field_policy_version=FIELD_POLICY_VERSION, resolver_version=RESOLVER_VERSION,
        actor_ref=None, supersedes_event_id=None)

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "retired"
    assert ov.producer is None and ov.strength is None
    assert is_feature_eligible(db, _REF, "additivity") is False
    # the value may echo the flat display column, but the status shows it is NOT load-bearing.
    assert ov.value == "non_additive"


# 6. Conflict: two distinct values tied at the top strength -> status="conflict".
def test_conflict_when_top_strength_evidence_disagrees(db):
    build_graph(db, _SOURCE, [_ROW])
    _seed(db, "additivity", "additive", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    _seed(db, "additivity", "non_additive", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "conflict"
    assert ov.conflict_status == "conflict"
    assert is_feature_eligible(db, _REF, "additivity") is False


# The strict-superset invariant made explicit: OperationalValue.value mirrors OperationalColumnFacts
# for a governed field, and 'resolved' is exactly is_feature_eligible.
def test_value_mirrors_column_facts_and_resolved_iff_eligible(db):
    build_graph(db, _SOURCE, [_ROW])
    _seed(db, "logical_representation", "decimal", EvidenceProducer.PARSER,
          AssertionStrength.SUPPORTED)
    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])

    ov = read_operational_value(db, _REF, "logical_representation")
    facts = read_column_facts(db, _REF, "logical_representation")
    assert ov.value == facts.value                          # same value axis
    assert (ov.status == "resolved") is is_feature_eligible(db, _REF, "logical_representation")
    assert ov.producer is EvidenceProducer.PARSER
    assert ov.strength is AssertionStrength.SUPPORTED
