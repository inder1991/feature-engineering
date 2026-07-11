from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
    read_evidence,
    write_evidence,
)


def test_evidence_carries_producer_strength_lifecycle_and_linkage(db):
    eid = write_evidence(
        db, fact_key="fk1", table_snapshot_at=None, row_count=0, sample_size=0, profile_version="p1",
        thresholds_used={}, metric_values={}, created_by={"subject": "s"},
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        lifecycle=EvidenceLifecycle.ACTIVE, producer_configuration_hash="cfg",
        producer_item_ref="h1", evidence_spans=("balance",))
    ev = read_evidence(db, eid)
    assert ev.producer == "llm" and ev.strength == "proposed" and ev.lifecycle == "active"
    assert ev.producer_configuration_hash == "cfg" and ev.producer_item_ref == "h1"
    assert ev.evidence_spans == ("balance",)          # TUPLE contract, not list


def test_legacy_write_defaults(db):
    ev = read_evidence(db, write_evidence(
        db, fact_key="fk2", table_snapshot_at=None, row_count=1, sample_size=1, profile_version="p1",
        thresholds_used={}, metric_values={}, created_by={}))
    assert ev.producer == "profiler" and ev.strength == "supported" and ev.lifecycle == "active"


def test_assertion_strength_has_all_four_members():
    from featuregen.overlay.evidence import AssertionStrength
    assert [s.value for s in AssertionStrength] == ["proposed", "supported", "attested", "confirmed"]
