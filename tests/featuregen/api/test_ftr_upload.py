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
- The quarantine surface is SAMPLE-SAFE against a row that genuinely carried a sample (whole-branch
  M4): a row whose definition holds a recognized sample clause AND which quarantines for a
  NON-definition reason persists only the SANITIZED definition in ``quarantine_row.raw``.

Since R5-1 the term_type vocabulary is OPEN (an unknown value like ``Mesure`` or the real file's
``Regulatory Term`` INGESTS — see test_ftr_acceptance.py), so the adapter-level quarantine
triggers exercised here are the ones that STILL quarantine: an UNRESOLVABLE FQN (R5-7), a
DUPLICATE ``source_row`` (provenance rule), and a MALFORMED row width (R5-6).

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

# The same upload plus one row whose FQN cannot resolve (no dots — neither a 3-part column nor a
# 2-part table identity): the adapter quarantines it at reader level (R5-7), stamped with
# _adapter="ftr" + its source_row, the raw FQN preserved in the reason for the re-upload fix.
_FTR_CSV_BAD_FQN = _FTR_CSV + (
    '21,no_dots_here,Settlement Date,'
    '"The date the transaction settles.",Payments,Business Term,Settlement,,,,,Payment,'
    'Transaction,,,,DATE\n')

# The same upload plus one MALFORMED-WIDTH row (R5-6): the definition is UNQUOTED and carries a
# comma, so csv sees 18 cells and every later field shifts one column right (csv.DictReader files
# the overflow under its None key) — the adapter quarantines it as read, nothing shifted ingests.
_FTR_CSV_MALFORMED_WIDTH = _FTR_CSV + (
    '21,DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.SETTLE_DT,Settlement Date,'
    'The date the transaction settles, net of adjustments,Payments,Business Term,Settlement,'
    ',,,,Payment,Transaction,,,,DATE\n')

# Near-FTR (#10): the distinctive schema.table.column header is present, but one header is renamed
# (term_name -> business_name), so the multiset is not the exact FTR layout -> HTTP 400.
_NEAR_FTR_CSV = _FTR_CSV.replace("term_name,", "business_name,", 1)

# Whole-branch M4: a row that BOTH carries a RECOGNIZED sample clause in its definition (synthetic
# scrub token ARTKOM — never a real value; the clause shape mirrors the acceptance fixture, a
# verified strip_sample_values path) AND quarantines for a NON-definition reason — a DUPLICATE
# source_row (18, already claimed by the CUST_NAME row; every member of the duplicate group fails
# closed). The durable quarantine_row must persist only the SANITIZED definition — the raw sample
# values may never reach any persistence surface.
_SAMPLE_TOKEN = "ARTKOM"
_FTR_CSV_SAMPLE_IN_QUARANTINE = _FTR_CSV + (
    '18,DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.CPTY_NAME,Counterparty Name,'
    '"Registered counterparty name. The sample profile is TEXT, with representative values '
    f'such as {_SAMPLE_TOKEN} GLOBAL FZE; NORDIC HOLDINGS AS, which supports interpretation.",'
    'Party,Dimension,Onboarding,,,,,Party,Customer,,,,VARCHAR\n')


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
    res = upload_csv(client, "ftr", _FTR_CSV_BAD_FQN)
    assert res.status_code == 200, res.text
    assert res.json()["quarantined"] == 1
    rows = _ftr_quarantine_rows(conn, "ftr")
    assert len(rows) == 1
    _, raw, reason = rows[0]
    assert "unresolvable FQN" in reason
    assert "no_dots_here" in reason         # the raw FQN the uploader must see to fix the file
    assert raw["source_row"] == "21"        # provenance back to the file's own row id
    assert raw["_adapter"] == "ftr"         # the inline-repair guard's discriminator


# ── Quarantine surface is sample-safe for a row that GENUINELY carried a sample (M4) ─────────────

def test_ftr_quarantined_sample_bearing_row_persists_only_sanitized_definition(client, conn):
    # Self-guard: the raw upload really DOES carry the synthetic sample token — fixture drift
    # would otherwise turn the absence assertions below into a vacuous pass.
    assert _SAMPLE_TOKEN in _FTR_CSV_SAMPLE_IN_QUARANTINE

    res = upload_csv(client, "ftr", _FTR_CSV_SAMPLE_IN_QUARANTINE)
    assert res.status_code == 200, res.text
    # BOTH members of the duplicate source_row group fail closed: CUST_NAME and CPTY_NAME.
    assert res.json()["quarantined"] == 2

    rows = _ftr_quarantine_rows(conn, "ftr")
    assert len(rows) == 2                    # NON-VACUOUS: the durable surface holds the rows
    (cpty_reason,) = [reason for _, raw, reason in rows if raw.get("column") == "CPTY_NAME"]
    assert "duplicate source_row" in cpty_reason   # the NON-definition reason, as staged
    # Positive control: the SAME probe shape DOES see this row's raw (its column identity), so the
    # token-absence probes below scan a surface that provably contains the quarantined row.
    assert conn.execute(
        "SELECT count(*) FROM quarantine_row WHERE catalog_source = %s AND raw::text ILIKE %s",
        ("ftr", "%CPTY_NAME%")).fetchone()[0] >= 1
    # The durable raw persisted the SANITIZED definition, not the raw sample.
    assert conn.execute(
        "SELECT count(*) FROM quarantine_row WHERE catalog_source = %s AND raw::text ILIKE %s",
        ("ftr", f"%{_SAMPLE_TOKEN}%")).fetchone()[0] == 0
    # Strongest form: no column of any durable quarantine row (reason included) carries the token.
    assert conn.execute(
        "SELECT count(*) FROM quarantine_row t WHERE t.catalog_source = %s AND t::text ILIKE %s",
        ("ftr", f"%{_SAMPLE_TOKEN}%")).fetchone()[0] == 0


# ── Inline repair is refused for FTR rows (#9) ───────────────────────────────────────────────────

def test_ftr_quarantine_row_refuses_inline_repair(client, conn):
    res = upload_csv(client, "ftr", _FTR_CSV_MALFORMED_WIDTH)
    assert res.status_code == 200, res.text
    (row_index, _, reason), = _ftr_quarantine_rows(conn, "ftr")
    assert "malformed row width" in reason               # 18 cells — nothing shifted ingested
    resolved, reason = resolve_quarantine_row(
        conn, "ftr", row_index, {"column": "settle_dt"}, actor=_actor())
    assert resolved is False
    assert "re-upload" in reason
    assert len(_ftr_quarantine_rows(conn, "ftr")) == 1   # still quarantined — nothing resolved
