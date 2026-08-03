"""Pass-A acceptance hook for the concept critic (ingestion-richness Task 2, steps 5/6).

The critic gates concept ACCEPTANCE for identifier-group assignments only. The contract under
test, exactly as the plan states it: a ``refuted`` identifier assignment never persists as the
current suggestion — the column resolves to the revise-pass result if accepted, else to the
non-identifier abstain (``unclassified``), and NEVER silently retains a previously-stored wrong
identifier. Refutations land their conflict codes in the stats detail AND in the column's
field-decision trail. Non-identifier assignments pass through byte-for-byte.

The ingest seam (stage ``enrich_concept_critic``) is covered at the bottom: counts when it ran,
``not_applicable`` when there was nothing identifier-shaped, ``skipped_no_client`` when the app
has no LLM provider.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import read_field_decisions
from featuregen.overlay.field_evidence import (
    read_active_field_evidence,
    record_field_evidence,
)
from featuregen.overlay.upload.attest.concept_critic import (
    CONCEPT_CRITIC_TASK,
    CONCEPT_REVISION_TASK,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash, enrich_concepts
from featuregen.overlay.upload.glossary_reader import read_glossary
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.stage_report import StageRecorder
from featuregen.overlay.upload.upload_identity import classify_upload

_TASK = "overlay.enrich.concept"

_HDR = ("physical_name,business_term,description_business_definition,data_domain,"
        "synonyms,bian_path,fibo_path\n")


def _glossary_csv(column: str, definition: str) -> str:
    return (_HDR
            + f"DPL_EIB_COMPLIANCE.BO_CIB_CUSTOMER.{column},{column} term,"
            + f"{definition},Party,,,\n")


def _classifier(h: str, concept: str, extra: dict | None = None) -> FakeLLM:
    return FakeLLM(script={
        _TASK: FakeResponse(output={"results": [{"ref": h, "concept": concept}]}),
        **(extra or {}),
    })


def _setup(column: str, definition: str, source: str = "cib"):
    upload = read_glossary(_glossary_csv(column, definition), source=source)
    bindings, _ = classify_upload(upload.rows)
    (row,) = upload.rows
    ref = normalize_ref(source, "DPL_EIB_COMPLIANCE", "BO_CIB_CUSTOMER", column)
    return upload, bindings, content_hash(row), ref


def test_refuted_identifier_never_persists_and_stales_the_stored_wrong_one(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload, bindings, h, ref = _setup("SOL_DESC", "Branch description")
    # A PRIOR run stored the wrong identifier as active LLM evidence — the exact live defect.
    record_field_evidence(
        db, logical_ref=ref, field_name="concept", proposed_value="branch_id",
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        producer_ref="overlay-enrichment", source_snapshot_id="snap-0",
        input_hash="prior-run-input")
    # The classifier re-proposes branch_id; no critic script -> the revise pass is unavailable and
    # the DETERMINISTIC conflict alone must refute.
    stats: dict = {}
    out = enrich_concepts(db, upload.rows, _classifier(h, "branch_id"), glossary=upload,
                          bindings=bindings, source_snapshot_id="snap-1", stats=stats)
    assert out[h] == "unclassified"                     # the non-identifier abstain, recorded
    # NEVER silent retention: the previously-stored wrong identifier is staled, not protected.
    assert read_active_field_evidence(db, ref, "concept") == []
    # The replacement reason lives in the column's field-decision trail, not only in one run's
    # stage detail.
    decisions = read_field_decisions(db, ref, "concept")
    assert decisions
    latest = decisions[-1]
    assert "name_or_description_not_identifier" in latest.reason_codes
    assert "concept_critic_refuted" in latest.reason_codes
    report = stats["concept_critic"]
    assert report["items"] == 1 and report["refuted"] == 1
    assert report["conflicts"][ref] == ["name_or_description_not_identifier"]


def test_revise_pass_result_replaces_the_refuted_identifier(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload, bindings, h, ref = _setup("COUNTER_PARTY_BIC", "SWIFT BIC of the counterparty bank")
    client = _classifier(h, "counterparty_id", extra={
        CONCEPT_REVISION_TASK: FakeResponse(output={"concept": "bank_bic", "reason_codes": []}),
    })
    stats: dict = {}
    out = enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                          source_snapshot_id="snap-1", stats=stats)
    assert out[h] == "bank_bic"                          # the revise-pass result, accepted
    active = read_active_field_evidence(db, ref, "concept")
    assert [e.proposed_value for e in active] == ["bank_bic"]
    assert stats["concept_critic"]["revised"] == 1


def test_every_high_impact_group_is_criticised_and_low_impact_passes_through(db, monkeypatch):
    """Joint Task 4 (d): the critic runs for EVERY high-impact proposal group — identifier,
    monetary, temporal, label — not identifiers alone. A low-impact display concept (categorical)
    still passes through untouched: it decides no join, no aggregation, no point-in-time semantics
    and no training target, so a second paid pass over it buys nothing."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    source = "cib"
    csv = (_HDR
           + "DPL_EIB_COMPLIANCE.BO_CIB_CUSTOMER.CIF_ID,CIF,Customer CIF,Party,,,\n"
           + "DPL_EIB_COMPLIANCE.BO_CIB_CUSTOMER.ACCT_BAL,Balance,Ledger balance,Deposits,,,\n"
           + "DPL_EIB_COMPLIANCE.BO_CIB_CUSTOMER.OPEN_DT,Open Date,Account opening date,Deposits,,,\n"
           + "DPL_EIB_COMPLIANCE.BO_CIB_CUSTOMER.CHURN_FLG,Churn,Customer churned,Deposits,,,\n"
           + "DPL_EIB_COMPLIANCE.BO_CIB_CUSTOMER.CTRY_CD,Country,Country of residence,Party,,,\n")
    upload = read_glossary(csv, source=source)
    bindings, _ = classify_upload(upload.rows)
    rows = {r.column: r for r in upload.rows}
    h = {c: content_hash(rows[c]) for c in
         ("CIF_ID", "ACCT_BAL", "OPEN_DT", "CHURN_FLG", "CTRY_CD")}
    proposals = {"CIF_ID": "customer_id", "ACCT_BAL": "monetary_stock",
                 "OPEN_DT": "origination_date", "CHURN_FLG": "outcome_label",
                 "CTRY_CD": "country_code"}
    client = FakeLLM(script={
        _TASK: FakeResponse(output={"results": [
            {"ref": h[c], "concept": v} for c, v in proposals.items()]}),
        CONCEPT_CRITIC_TASK: FakeResponse(output={"verdict": "supported", "reason_codes": []}),
    })
    stats: dict = {}
    out = enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                          source_snapshot_id="snap-1", stats=stats)
    for column, concept in proposals.items():
        assert out[h[column]] == concept, column         # every proposal is upheld
    report = stats["concept_critic"]
    # identifier + monetary + temporal + label were criticised; the categorical one was not.
    assert report["items"] == 4
    assert report["accepted"] == 4 and report["refuted"] == 0


def test_critic_provider_failure_abstains_and_keeps_the_proposal(db, monkeypatch):
    # A shape-CLEAN identifier whose critique call fails (no script) must abstain — the proposal
    # stands; a transient provider fault never evicts a plausible assignment.
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload, bindings, h, ref = _setup("CIF_ID", "Customer CIF")
    stats: dict = {}
    out = enrich_concepts(db, upload.rows, _classifier(h, "customer_id"), glossary=upload,
                          bindings=bindings, source_snapshot_id="snap-1", stats=stats)
    assert out[h] == "customer_id"
    assert [e.proposed_value for e in read_active_field_evidence(db, ref, "concept")] == [
        "customer_id"]
    assert stats["concept_critic"]["abstained"] == 1


# ── the ingest stage seam ─────────────────────────────────────────────────────────────────────────


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _stage(recorder: StageRecorder, stage: str):
    return next(r for r in recorder.reports if r.stage == stage)


def test_ingest_records_skipped_no_client(db):
    _seal_config()
    rec = StageRecorder()
    rows = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True)]
    ingest_upload(db, "deposits", rows, actor=_actor(),
                  now=datetime(2026, 7, 31, tzinfo=UTC), stage_recorder=rec)
    assert _stage(rec, "enrich_concept_critic").state == "skipped_no_client"


def test_ingest_records_not_applicable_with_no_high_impact_assignments(db, monkeypatch):
    """`not_applicable` still means "the critic had nothing in scope" — but scope is now the four
    HIGH-IMPACT groups (joint Task 4 item d), so the witness must be a low-impact concept. A
    monetary assignment, which this test used to rely on, is now squarely in scope."""
    _seal_config()
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    rec = StageRecorder()
    rows = [CanonicalRow("deposits", "accounts", "country_cd", "text")]
    h = content_hash(rows[0])
    client = FakeLLM(script={
        _TASK: FakeResponse(output={"results": [{"ref": h, "concept": "country_code"}]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": []}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
        "overlay.enrich.unit": FakeResponse(output={"results": []}),
    })
    ingest_upload(db, "deposits", rows, actor=_actor(), client=client,
                  now=datetime(2026, 7, 31, tzinfo=UTC), stage_recorder=rec)
    report = _stage(rec, "enrich_concept_critic")
    assert report.state == "not_applicable"


def test_ingest_records_the_critic_counts_and_conflicts(db, monkeypatch):
    _seal_config()
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload, bindings, _raw_h, ref = _setup("SOL_DESC", "Branch description", source="cib")
    del bindings
    # ``validate_rows`` normalizes row identity before enrichment, so the batch item ref is the
    # hash of the NORMALIZED row, not the raw-cased one the reader emitted.
    (row,) = upload.rows
    h = content_hash(replace(row, table=row.table.lower(), column=row.column.lower()))
    client = FakeLLM(script={
        _TASK: FakeResponse(output={"results": [{"ref": h, "concept": "branch_id"}]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.summary": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": []}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
        "overlay.enrich.unit": FakeResponse(output={"results": []}),
    })
    rec = StageRecorder()
    ingest_upload(db, "cib", upload.rows, actor=_actor(), client=client, glossary=upload,
                  now=datetime(2026, 7, 31, tzinfo=UTC), stage_recorder=rec)
    report = _stage(rec, "enrich_concept_critic")
    assert report.state == "succeeded"
    assert report.detail["refuted"] == 1
    assert report.detail["conflicts"][ref] == ["name_or_description_not_identifier"]
    # And the graph never shows the refuted identifier as the current suggestion.
    concept = db.execute(
        "SELECT concept FROM graph_node WHERE catalog_source='cib' "
        "AND object_ref='public.bo_cib_customer.sol_desc'").fetchone()[0]
    assert concept != "branch_id"
