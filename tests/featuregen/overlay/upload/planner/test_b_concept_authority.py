"""Phase 3C.2b-i-B · Task 5 — DB-backed tests for the bespoke concept-authority resolver.

Every outcome is established through the REAL writers (``record_field_evidence`` +
``flag_pending_revalidation``) — never a hand-built object we then assert on. The resolver reads
raw ``field_evidence`` directly (it does NOT wrap ``resolve_field_authority``, which is
RECOMMENDATION-capped for ``concept`` and would always return a null authority), so a test that
passed by accepting weaker-than-``(HUMAN,CONFIRMED)``/``(SOURCE,ATTESTED)`` evidence would be a
failure of this file, not a pass.
"""
from __future__ import annotations

import psycopg

from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.field_revalidation import flag_pending_revalidation
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.planner.b_concept_authority import (
    ConceptAuthority,
    ConceptAuthorityReason,
    ConceptRejection,
    PlannerConceptBinding,
    reason_to_b_disposition,
    resolve_planner_concept_binding,
)
from featuregen.overlay.upload.planner.b_dispositions import BDisposition


def _record(db, ref, value, producer, strength, lifecycle=EvidenceLifecycle.ACTIVE):
    """Write ONE real ``concept`` field-evidence row through the production writer."""
    return record_field_evidence(
        db,
        logical_ref=ref,
        field_name="concept",
        proposed_value=value,
        producer=producer,
        strength=strength,
        producer_ref=f"{EvidenceProducer(producer).value}:t5",
        source_snapshot_id="snap-t5",
        input_hash=field_input_hash(logical_ref=ref, field_name="concept", material=value),
        lifecycle=lifecycle,
    )


def _ref(table: str) -> str:
    return normalize_ref("upload_t5", "public", table, "amount")


# 1 — single HUMAN/CONFIRMED, in registry -> human_confirmed binding.
def test_single_human_confirmed_in_registry(db):
    ref = _ref("t1")
    _record(db, ref, "monetary_flow", EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, PlannerConceptBinding)
    assert res.authority is ConceptAuthority.human_confirmed
    assert res.authoritative_concept == "monetary_flow"
    assert res.evidence_ids  # non-empty
    assert res.evidence_set_hash and res.value_hash


# 2 — two ACTIVE human rows, distinct values -> conflict.
def test_conflicting_human_confirmed(db):
    ref = _ref("t2")
    _record(db, ref, "monetary_flow", EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)
    _record(db, ref, "monetary_stock", EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, ConceptRejection)
    assert res.reason is ConceptAuthorityReason.concept_authority_conflict


# 3 — no human + single SOURCE/ATTESTED -> source_attested binding.
def test_single_source_attested(db):
    ref = _ref("t3")
    _record(db, ref, "monetary_flow", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, PlannerConceptBinding)
    assert res.authority is ConceptAuthority.source_attested
    assert res.authoritative_concept == "monetary_flow"


# 4 — human beats a DIFFERING source (a diagnostic, NOT a conflict).
def test_human_beats_source_lower_authority_diagnostic(db):
    ref = _ref("t4")
    _record(db, ref, "monetary_flow", EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)
    _record(db, ref, "monetary_stock", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, PlannerConceptBinding)
    assert res.authority is ConceptAuthority.human_confirmed
    assert res.authoritative_concept == "monetary_flow"
    assert "lower_authority_disagreement" in res.diagnostics


# 5 — only LLM/PROPOSED (not an accepted pair) -> missing.
def test_only_llm_proposed_is_missing(db):
    ref = _ref("t5")
    _record(db, ref, "monetary_flow", EvidenceProducer.LLM, AssertionStrength.PROPOSED)
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, ConceptRejection)
    assert res.reason is ConceptAuthorityReason.concept_authority_missing


# 6 — no evidence at all -> missing.
def test_no_evidence_is_missing(db):
    res = resolve_planner_concept_binding(db, _ref("t6"))
    assert isinstance(res, ConceptRejection)
    assert res.reason is ConceptAuthorityReason.concept_authority_missing


# 7 — accepted-pair row SUPERSEDED, none active -> stale (folds to concept_authority_stale).
def test_superseded_only_is_stale(db):
    ref = _ref("t7")
    _record(db, ref, "monetary_flow", EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED,
            lifecycle=EvidenceLifecycle.SUPERSEDED)
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, ConceptRejection)
    assert res.reason is ConceptAuthorityReason.concept_evidence_stale
    assert reason_to_b_disposition(res.reason) is BDisposition.concept_authority_stale


# 8 — ACTIVE HUMAN/CONFIRMED + pending revalidation -> revalidation_pending (folds to stale).
def test_revalidation_pending_blocks(db):
    ref = _ref("t8")
    _record(db, ref, "monetary_flow", EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)
    flag_pending_revalidation(
        db, logical_ref=ref, field_name="concept",
        reason="material_changed", source_snapshot_id="snap-t5")
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, ConceptRejection)
    assert res.reason is ConceptAuthorityReason.concept_revalidation_pending
    assert reason_to_b_disposition(res.reason) is BDisposition.concept_authority_stale


# 9 — only REJECTED accepted-pair rows, none active -> missing (distinct query, same outcome).
def test_only_rejected_is_missing(db):
    ref = _ref("t9")
    _record(db, ref, "monetary_flow", EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED,
            lifecycle=EvidenceLifecycle.REJECTED)
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, ConceptRejection)
    assert res.reason is ConceptAuthorityReason.concept_authority_missing


# 10 — a confirmed concept absent from the registry -> not_in_registry.
def test_confirmed_value_not_in_registry(db):
    ref = _ref("t10")
    _record(db, ref, "totally_not_a_concept_xyz",
            EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, ConceptRejection)
    assert res.reason is ConceptAuthorityReason.concept_not_in_registry


# 11 — resolved concept differs from graph_node.concept -> non-blocking DISPLAY_CONCEPT_MISMATCH.
def test_display_concept_mismatch_diagnostic(db):
    ref = _ref("t11")
    _record(db, ref, "monetary_flow", EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)
    source, _schema, table, column = parse_ref(ref)
    db.execute(
        "INSERT INTO graph_node "
        "(catalog_source, object_ref, kind, table_name, column_name, concept) "
        "VALUES (%s, %s, 'column', %s, %s, %s)",
        (source, f"public.{table}.{column}", table, column, "monetary_stock"),
    )
    res = resolve_planner_concept_binding(db, ref)
    assert isinstance(res, PlannerConceptBinding)
    assert res.authoritative_concept == "monetary_flow"  # evidence wins, not the display value
    assert "DISPLAY_CONCEPT_MISMATCH" in res.diagnostics


# 12 — determinism: two resolutions of the same state yield equal hashes.
def test_determinism_of_hashes(db):
    ref = _ref("t12")
    _record(db, ref, "monetary_flow", EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)
    r1 = resolve_planner_concept_binding(db, ref)
    r2 = resolve_planner_concept_binding(db, ref)
    assert isinstance(r1, PlannerConceptBinding) and isinstance(r2, PlannerConceptBinding)
    assert r1.evidence_set_hash == r2.evidence_set_hash
    assert r1.value_hash == r2.value_hash
    assert r1.evidence_ids == r2.evidence_ids


# 13 — a DB read failure from the resolver's own queries -> technical_failure (fail-closed).
def test_db_failure_is_technical_failure():
    class _BoomConn:
        def cursor(self, *args, **kwargs):
            raise psycopg.OperationalError("boom")

        def execute(self, *args, **kwargs):
            raise psycopg.OperationalError("boom")

    res = resolve_planner_concept_binding(_BoomConn(), "upload_t5::public.t13.amount")
    assert isinstance(res, ConceptRejection)
    assert res.reason is ConceptAuthorityReason.technical_failure
    assert reason_to_b_disposition(res.reason) is BDisposition.technical_failure
