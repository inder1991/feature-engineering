"""HTTP-level tests for the FTR glossary dispatch (Task 3b — route wiring + inline-repair guard).

Contracts under test (round-4 resolutions #6/#9/#10):
- An exact-fingerprint FTR CSV posted to /uploads dispatches to the FTR adapter and ingests
  through the unchanged glossary spine (no LLM configured -> enrichment is skipped).
- A near-FTR file (the distinctive ``schema.table.column`` header present but the multiset is not
  the exact 17-column layout) is REJECTED with HTTP 400 and a fingerprint diagnostic (#10) —
  never silently mangled by another reader.
- Adapter-quarantined rows carry ``raw["_adapter"] == "ftr"`` (plus ``source_row`` provenance),
  and ``resolve_quarantine_row`` refuses inline resolution for them (#9): the sidecar
  (schema/term_type/taxonomy/facets) cannot be reconstructed from a repaired CanonicalRow, so the
  only durable fix is re-uploading the corrected FTR file.

Fixture strings mirror tests/featuregen/overlay/upload/test_ftr_adapter.py (inline, never read
from ~/Downloads).
"""
from __future__ import annotations

from tests.featuregen.api._helpers import upload_csv

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.ingest import resolve_quarantine_row

# ── Fixtures (mirroring test_ftr_adapter.py) ─────────────────────────────────────────────────────

_HDR = ("source_row,schema.table.column,term_name,description_business_definition,data_domain,"
        "term_type,related_business_process_l1,related_terms,related_business_process_l2,"
        "related_business_process_l3,synonyms_aliases,bian_level_1,bian_level_2,bian_level_3,"
        "bian_level_4,fibo_level_1,data_type\n")
_FTR_CSV = _HDR + (
    '18,DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.CUST_NAME,Customer Name,'
    '"Registered legal name of the counterparty.",Party,Dimension,Onboarding,KYC Alias;Screening Alias,'
    'KYC,Screening,Client Name|Account Holder,Party,Customer,Identification,Legal,'
    'fibo-be-le-lp:LegalPerson,VARCHAR\n'
    '19,DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.TXN_AMT,Transaction Amount,'
    '"The monetary amount of the transaction.",Payments,Measure,Settlement,Amount Alias,Clearing,,Amt,'
    'Payment,Transaction,Amount,,fibo-fbc:MonetaryAmount,DECIMAL\n'
    '20,DPL_EIB_COMPLIANCE.COMP_FIN_TRAN,Financial Transaction Repository,'
    '"Daily compliance transaction repository.",Compliance,Reference Data,,,,,,Reference,Table,,,,\n')

# The same upload plus one row whose term_type is OUTSIDE the closed vocabulary ("Mesure") — the
# adapter quarantines it at reader level, stamped with _adapter="ftr" + its source_row.
_FTR_CSV_BAD_TERM_TYPE = _FTR_CSV + (
    '21,DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.SETTLE_DT,Settlement Date,'
    '"The date the transaction settles.",Payments,Mesure,Settlement,,,,,Payment,Transaction,,,,'
    'DATE\n')

# Near-FTR (#10): the distinctive schema.table.column header is present, but one header is renamed
# (term_name -> business_name), so the multiset is not the exact FTR layout -> HTTP 400.
_NEAR_FTR_CSV = _FTR_CSV.replace("term_name,", "business_name,", 1)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="reviewer", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _ftr_quarantine_rows(conn, source: str):
    return conn.execute(
        "SELECT row_index, raw, reason FROM quarantine_row WHERE catalog_source = %s "
        "ORDER BY row_index", (source,)).fetchall()


# ── Dispatch: exact FTR ingests ──────────────────────────────────────────────────────────────────

def test_ftr_upload_ingests(client):
    res = upload_csv(client, "ftr", _FTR_CSV)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "ingested"


# ── Dispatch: near-FTR is a 400 with a fingerprint diagnostic (#10) ──────────────────────────────

def test_near_ftr_upload_rejected_with_diagnostic(client):
    res = upload_csv(client, "ftr", _NEAR_FTR_CSV)
    assert res.status_code == 400
    assert "FTR glossary format error" in res.json()["detail"]


# ── Quarantine provenance: _adapter="ftr" + source_row persist on the durable row (#9) ───────────

def test_ftr_quarantine_row_carries_adapter_tag_and_source_row(client, conn):
    res = upload_csv(client, "ftr", _FTR_CSV_BAD_TERM_TYPE)
    assert res.status_code == 200, res.text
    assert res.json()["quarantined"] == 1
    rows = _ftr_quarantine_rows(conn, "ftr")
    assert len(rows) == 1
    _, raw, reason = rows[0]
    assert "term_type" in reason
    assert raw["source_row"] == "21"        # provenance back to the file's own row id
    assert raw["_adapter"] == "ftr"         # the inline-repair guard's discriminator


# ── Inline repair is refused for FTR rows (#9) ───────────────────────────────────────────────────

def test_ftr_quarantine_row_refuses_inline_repair(client, conn):
    res = upload_csv(client, "ftr", _FTR_CSV_BAD_TERM_TYPE)
    assert res.status_code == 200, res.text
    (row_index, _, _), = _ftr_quarantine_rows(conn, "ftr")
    resolved, reason = resolve_quarantine_row(
        conn, "ftr", row_index, {"column": "settle_dt"}, actor=_actor())
    assert resolved is False
    assert "re-upload" in reason
    assert len(_ftr_quarantine_rows(conn, "ftr")) == 1   # still quarantined — nothing resolved
