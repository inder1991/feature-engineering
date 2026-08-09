from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceProducer,
)
from featuregen.overlay.field_authority import FieldEvidenceView
from featuregen.overlay.field_evidence import (
    FieldEvidence,
    canonical_hash,
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
    stale_source_evidence,
    to_view,
)
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref


def test_canonical_hash_is_order_independent():
    # key order must not change the hash — staleness/decisions key on this being stable.
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    # a different value is a different hash.
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_normalize_ref_preserves_schema_and_defaults_public():
    ref = normalize_ref("upload", "risk", "accounts", "balance")
    # schema is PRESERVED (not collapsed away) — a schema-qualified identity.
    assert "risk" in ref
    # stable: same inputs -> same ref.
    assert ref == normalize_ref("upload", "risk", "accounts", "balance")
    # a missing schema defaults to "public" (same identity as an explicit "public")...
    assert normalize_ref("upload", None, "accounts", "balance") == normalize_ref(
        "upload", "public", "accounts", "balance")
    # ...but is NOT the same identity as a different, explicit schema.
    assert normalize_ref("upload", None, "accounts", "balance") != ref
    # a table ref (no column) differs from a column ref under it.
    assert normalize_ref("upload", "risk", "accounts") != ref
    # round-trippable: parse_ref recovers the (normalized) components for both column and table refs.
    assert parse_ref(ref) == ("upload", "risk", "accounts", "balance")
    assert parse_ref(normalize_ref("upload", "risk", "accounts")) == (
        "upload", "risk", "accounts", None)


def test_field_input_hash_differs_per_field_for_the_same_row():
    logical_ref = normalize_ref("upload", "public", "accounts", "balance")
    definition_h = field_input_hash(
        logical_ref=logical_ref, field_name="definition", material="Ledger balance in cents")
    concept_h = field_input_hash(
        logical_ref=logical_ref, field_name="concept", material="balance")
    # the definition-field input and the concept-field input are distinct per field.
    assert definition_h != concept_h
    # stable for the same field input (so an unchanged re-upload does not look changed).
    assert definition_h == field_input_hash(
        logical_ref=logical_ref, field_name="definition", material="Ledger balance in cents")


def test_write_read_and_view_concept_llm_proposed(db):
    logical_ref = normalize_ref("upload", "public", "accounts", "balance")
    ih = field_input_hash(logical_ref=logical_ref, field_name="concept", material="balance")
    eid = record_field_evidence(
        db,
        logical_ref=logical_ref,
        field_name="concept",
        proposed_value="account_balance",
        producer=EvidenceProducer.LLM,
        strength=AssertionStrength.PROPOSED,
        producer_ref="pass_a:v1",
        source_snapshot_id="snap1",
        input_hash=ih,
        evidence_spans=("balance",),
        confidence_band="high",
    )
    assert eid.startswith("fev_")
    rows = read_active_field_evidence(db, logical_ref, "concept")
    assert len(rows) == 1
    ev = rows[0]
    assert isinstance(ev, FieldEvidence)
    assert ev.producer == "llm" and ev.strength == "proposed" and ev.lifecycle == "active"
    assert ev.proposed_value == "account_balance"
    assert ev.proposed_value_hash == canonical_hash("account_balance")
    assert ev.evidence_spans == ("balance",)          # TUPLE contract, not list
    assert ev.confidence_band == "high"
    assert ev.source_snapshot_id == "snap1" and ev.input_hash == ih

    view = to_view(ev)
    assert isinstance(view, FieldEvidenceView)
    # the enums are load-bearing on the view (not raw strings).
    assert view.producer is EvidenceProducer.LLM and view.strength is AssertionStrength.PROPOSED
    assert view.value == "account_balance" and view.evidence_id == eid


def test_stale_source_evidence_is_producer_scoped(db):
    logical_ref = normalize_ref("upload", "public", "accounts", "definition")
    h1 = field_input_hash(logical_ref=logical_ref, field_name="definition", material="old text")
    h_human = field_input_hash(logical_ref=logical_ref, field_name="definition", material="human text")
    h2 = field_input_hash(logical_ref=logical_ref, field_name="definition", material="new text")

    src = record_field_evidence(
        db, logical_ref=logical_ref, field_name="definition", proposed_value="old text",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.SUPPORTED,
        producer_ref="glossary", source_snapshot_id="snap1", input_hash=h1)
    human = record_field_evidence(
        db, logical_ref=logical_ref, field_name="definition", proposed_value="human text",
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref="alice", source_snapshot_id="snap1", input_hash=h_human)

    # a SOURCE re-upload with new input (h2) stales the OLD source row only.
    staled = stale_source_evidence(
        db, logical_ref=logical_ref, field_name="definition",
        producer=EvidenceProducer.SOURCE, keep_input_hash=h2)
    assert staled == 1

    active = {r.evidence_id: r for r in read_active_field_evidence(db, logical_ref, "definition")}
    # the human-confirmed row is UNTOUCHED — a source re-upload NEVER stales human evidence.
    assert human in active and active[human].lifecycle == "active"
    # the old source row is gone from the active set (now stale).
    assert src not in active


def test_stale_source_evidence_keeps_unchanged_input(db):
    # unchanged input (keep_input_hash == the row's input_hash) stales nothing (snapshot reuse).
    logical_ref = normalize_ref("upload", "public", "accounts", "definition")
    h1 = field_input_hash(logical_ref=logical_ref, field_name="definition", material="same text")
    record_field_evidence(
        db, logical_ref=logical_ref, field_name="definition", proposed_value="same text",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.SUPPORTED,
        producer_ref="glossary", source_snapshot_id="snap1", input_hash=h1)
    staled = stale_source_evidence(
        db, logical_ref=logical_ref, field_name="definition",
        producer=EvidenceProducer.SOURCE, keep_input_hash=h1)
    assert staled == 0
    assert len(read_active_field_evidence(db, logical_ref, "definition")) == 1


def test_a_changed_producer_configuration_stales_even_when_the_input_is_identical(db):
    """FIX 1 — the staleness rule must consider the PRODUCER'S CONFIGURATION, not just the input.

    MEASURED ON THE LIVE CATALOG (2026-08-09). A classifier's answer depends on two things: the
    column, and the closed VOCABULARY it must choose from. Only the first was keyed. So a vocabulary
    that gained the very concept a column needed (`bank_bic`, added 2026-07-31) could not dislodge a
    label chosen when that concept did not exist — the column had not changed, so the row was
    'reused' and a re-upload was a no-op. The evidence table had been RECORDING the vocabulary
    fingerprint in `producer_configuration_hash` all along and nothing ever read it.

    The scale that made it worth fixing: 209 of cib's 319 concept rows sat on superseded
    vocabularies four days AFTER a re-ingest, and 124 of 124 of ftr's did.

    Passing no `keep_configuration_hash` keeps the historical behaviour EXACTLY — the other callers
    (type attestation, table synth, the source re-upload path) key on input alone and must not start
    staling rows because a column they never configured reports NULL here.
    """
    logical_ref = normalize_ref("upload", "public", "accounts", "concept")
    material = {"table": "accounts", "column": "cust_num", "type": "text"}
    h = field_input_hash(logical_ref=logical_ref, field_name="concept", material=material)

    record_field_evidence(
        db, logical_ref=logical_ref, field_name="concept", proposed_value="counterparty_id",
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        producer_ref="run-july", producer_configuration_hash="vocab_JULY",
        source_snapshot_id="snap1", input_hash=h)

    # SAME input hash, SAME producer — only the vocabulary moved on.
    staled = stale_source_evidence(
        db, logical_ref=logical_ref, field_name="concept", producer=EvidenceProducer.LLM,
        keep_input_hash=h, keep_configuration_hash="vocab_TODAY")
    assert staled == 1, "a label produced under a superseded vocabulary must not survive as active"
    assert read_active_field_evidence(db, logical_ref, "concept") == []


def test_an_unchanged_configuration_still_reuses_the_row(db):
    """The other half: re-deriving on every ingest would be as wrong as never re-deriving."""
    logical_ref = normalize_ref("upload", "public", "accounts", "concept")
    h = field_input_hash(logical_ref=logical_ref, field_name="concept", material="same")
    record_field_evidence(
        db, logical_ref=logical_ref, field_name="concept", proposed_value="customer_id",
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        producer_ref="run-1", producer_configuration_hash="vocab_TODAY",
        source_snapshot_id="snap1", input_hash=h)
    assert stale_source_evidence(
        db, logical_ref=logical_ref, field_name="concept", producer=EvidenceProducer.LLM,
        keep_input_hash=h, keep_configuration_hash="vocab_TODAY") == 0
    assert len(read_active_field_evidence(db, logical_ref, "concept")) == 1


def test_omitting_the_configuration_keeps_the_historical_input_only_rule(db):
    """Callers that never configured a vocabulary must be untouched by this change."""
    logical_ref = normalize_ref("upload", "public", "accounts", "definition")
    h = field_input_hash(logical_ref=logical_ref, field_name="definition", material="text")
    record_field_evidence(
        db, logical_ref=logical_ref, field_name="definition", proposed_value="text",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.SUPPORTED,
        producer_ref="glossary", source_snapshot_id="snap1", input_hash=h)
    assert stale_source_evidence(
        db, logical_ref=logical_ref, field_name="definition", producer=EvidenceProducer.SOURCE,
        keep_input_hash=h) == 0
    assert len(read_active_field_evidence(db, logical_ref, "definition")) == 1
