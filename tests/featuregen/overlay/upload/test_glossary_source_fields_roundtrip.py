"""Task 3 Step 6b: the four LOST glossary fields become durable source evidence.

The A1 adapter captures all 17 CSV headers, but `_SOURCE_FIELDS` persisted only
definition/domain/business_term/bian_path/fibo_path — `term_type`, the business-process path
(L1–L3, joined), `related_terms` and the raw physical FQN survived only inside `llm_call`
payloads: prompt fuel, never catalog truth. They now round-trip upload → `source/attested`
field evidence (readable by the dossier), reconcile present→absent like every other source
field, and are never fabricated when the file did not declare them.
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.ingest import _SOURCE_FIELDS, _write_glossary_source_evidence

_REF = "cib::bo_dpl_cib.bo_cib_customer.cust_curr_ntb_flg"

_LOST_FIELDS = ("term_type", "process_path", "related_terms", "physical_fqn")


def _rec(**overrides) -> GlossaryRecord:
    base = dict(
        logical_ref=_REF,
        term_name="Customer Current New to Bank Flag",
        definition="Whether the customer is currently new to the bank.",
        domain="Customer",
        bian_path="Customer Management",
        fibo_path="fibo-fnd:CustomerFlag",
        term_type="dimension",
        process_path="Onboarding > KYC > Activation",
        related_terms=("Customer Since Date", "NTB Segment"),
        physical_fqn="BO_DPL_CIB.BO_CIB_CUSTOMER.CUST_CURR_NTB_FLG",
    )
    base.update(overrides)
    return GlossaryRecord(**base)


def _active_value(db, field: str):
    rows = read_active_field_evidence(db, _REF, field)
    return rows[-1].proposed_value if rows else None


def test_the_four_fields_are_in_the_reconciled_source_field_set():
    """`_SOURCE_FIELDS` drives present→absent staling; a field outside it can never be retired
    when a re-upload drops it (the Task-10 Important-3 class)."""
    for field in _LOST_FIELDS:
        assert field in _SOURCE_FIELDS, field


def test_each_lost_field_round_trips_to_source_attested_evidence(db):
    _write_glossary_source_evidence(db, logical_ref=_REF, rec=_rec(), snapshot_id="ing_t1")
    for field, expected in (
        ("term_type", "dimension"),
        ("process_path", "Onboarding > KYC > Activation"),
        ("related_terms", "Customer Since Date, NTB Segment"),   # tuple joined for evidence
        ("physical_fqn", "BO_DPL_CIB.BO_CIB_CUSTOMER.CUST_CURR_NTB_FLG"),
    ):
        rows = read_active_field_evidence(db, _REF, field)
        assert len(rows) == 1, field
        assert rows[0].proposed_value == expected
        assert rows[0].producer == "source"
        assert rows[0].strength == "attested"


def test_absent_in_file_stays_absent(db):
    """No fabricated empties: a record that never declared the fields writes NO evidence rows."""
    _write_glossary_source_evidence(
        db, logical_ref=_REF,
        rec=_rec(term_type="", process_path="", related_terms=(), physical_fqn=""),
        snapshot_id="ing_t1")
    for field in _LOST_FIELDS:
        assert read_active_field_evidence(db, _REF, field) == [], field


def test_a_reupload_that_drops_a_field_stales_it(db):
    """Present→absent reconciliation: yesterday's term_type must not stay load-bearing when the
    corrected file no longer asserts it."""
    _write_glossary_source_evidence(db, logical_ref=_REF, rec=_rec(), snapshot_id="ing_t1")
    _write_glossary_source_evidence(
        db, logical_ref=_REF, rec=_rec(term_type=""), snapshot_id="ing_t2")
    assert read_active_field_evidence(db, _REF, "term_type") == []
    assert _active_value(db, "process_path") == "Onboarding > KYC > Activation"   # kept


def test_full_ingest_round_trips_the_fields(db):
    """Upload → readable evidence through the REAL ingest path (no LLM client needed — source
    evidence is deterministic)."""
    from featuregen.overlay.upload.ingest import ingest_upload

    rows = [CanonicalRow("cib", "bo_cib_customer", "cust_curr_ntb_flg", "unknown")]
    glossary = GlossaryUpload(rows=rows, records=[_rec()])
    actor = IdentityEnvelope(subject="u", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))
    res = ingest_upload(db, "cib", rows, actor=actor,
                        now=datetime(2026, 7, 31, tzinfo=UTC), glossary=glossary)
    assert res.status == "ingested"
    assert _active_value(db, "term_type") == "dimension"
    assert _active_value(db, "related_terms") == "Customer Since Date, NTB Segment"
    assert _active_value(db, "physical_fqn") == "BO_DPL_CIB.BO_CIB_CUSTOMER.CUST_CURR_NTB_FLG"
    assert _active_value(db, "process_path") == "Onboarding > KYC > Activation"
