"""Tests for the FTR glossary adapter module (Task 3a — module only, no route dispatch).

Contracts under test (round-4 resolutions #1/#3/#7/#10/#12):
- ``is_ftr_glossary`` is an EXACT normalized header-multiset match — a missing, extra, or
  duplicated header disqualifies the file.
- ``ftr_fingerprint_error`` returns a specific near-FTR diagnostic (missing/extra/duplicate
  normalized headers) ONLY when the FTR-distinctive ``schema.table.column`` header is present.
- The OPERATIONAL ``CanonicalRow.type`` is ALWAYS ``UNKNOWN_TYPE`` (#1); the FTR-declared SQL type
  survives only as bounded, non-operational ``GlossaryRecord.declared_type`` metadata (#3).
- ``term_type`` is matched against the versioned closed vocabulary ``TERM_TYPE_VOCAB_V1``; an
  unknown value quarantines its row with a bounded, redacted value in the reason (#7).
- ``source_row`` must be a non-empty integer, unique (as parsed int) across the upload (#12).
- Every definition is sanitized at parse time (an unhandled sample clause blanks the field, the
  row still ingests); every other free-text field is PII-redacted.

Fixture is inline (never read from ~/Downloads); definitions with commas are quoted.
"""
from __future__ import annotations

from featuregen.intake.redaction import REDACTION_VERSION
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE
from featuregen.overlay.upload.ftr_adapter import (
    TERM_TYPE_VOCAB_V1,
    PreparedFtrUpload,
    ftr_fingerprint_error,
    is_ftr_glossary,
    read_ftr_glossary,
    to_glossary_upload,
)
from featuregen.overlay.upload.glossary_reader import GlossaryUpload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.sanitize import SANITIZER_VERSION

# ── Fixture (plan Task 3; row 20's term_type is in-vocab per resolution #7) ──────────────────────

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

_FTR_HEADERS = _HDR.strip().split(",")


def _row(source_row="18", fqn="DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.CUST_NAME",
         term_name="Customer Name", definition='"Registered legal name of the counterparty."',
         domain="Party", term_type="Dimension", l1="Onboarding",
         related="KYC Alias;Screening Alias", l2="KYC", l3="Screening",
         synonyms="Client Name|Account Holder", b1="Party", b2="Customer", b3="Identification",
         b4="Legal", fibo="fibo-be-le-lp:LegalPerson", data_type="VARCHAR") -> str:
    """One FTR CSV data line (``definition`` is passed pre-quoted when it contains commas)."""
    return ",".join([source_row, fqn, term_name, definition, domain, term_type, l1, related, l2,
                     l3, synonyms, b1, b2, b3, b4, fibo, data_type]) + "\n"


# ── Fingerprint: exact multiset ──────────────────────────────────────────────────────────────────

def test_is_ftr_glossary_exact_headers():
    assert is_ftr_glossary(_FTR_HEADERS) is True


def test_is_ftr_glossary_tolerates_case_space_underscore_variants():
    variants = [h.upper().replace("_", " ") for h in _FTR_HEADERS]
    assert is_ftr_glossary(variants) is True


def test_is_ftr_glossary_rejects_duplicate_header():
    assert is_ftr_glossary([*_FTR_HEADERS, "data_type"]) is False


def test_is_ftr_glossary_rejects_missing_and_extra_headers():
    assert is_ftr_glossary(_FTR_HEADERS[:-1]) is False
    assert is_ftr_glossary([*_FTR_HEADERS, "surprise"]) is False


def test_ftr_fingerprint_error_on_near_ftr_headers():
    near = ["term_label" if h == "term_name" else h for h in _FTR_HEADERS]
    assert is_ftr_glossary(near) is False
    msg = ftr_fingerprint_error(near)
    assert msg is not None
    assert "termname" in msg      # the missing normalized header is named
    assert "termlabel" in msg     # the extra normalized header is named


def test_ftr_fingerprint_error_on_duplicate_header():
    msg = ftr_fingerprint_error([*_FTR_HEADERS, "data_type"])
    assert msg is not None
    assert "duplicate" in msg and "datatype" in msg


def test_ftr_fingerprint_error_none_when_not_ftr_shaped():
    assert ftr_fingerprint_error(["source", "table", "column", "type"]) is None
    assert ftr_fingerprint_error(_FTR_HEADERS) is None   # exact FTR is not an error


# ── Happy path: mapping ──────────────────────────────────────────────────────────────────────────

def test_read_ftr_glossary_counts():
    p = read_ftr_glossary(_FTR_CSV, source="ftr")
    assert isinstance(p, PreparedFtrUpload)
    assert len(p.rows) == 2 and len(p.records) == 3
    assert p.quarantined == []


def test_read_ftr_glossary_mapping():
    p = read_ftr_glossary(_FTR_CSV, source="ftr")
    rec = p.records[0]
    assert rec.schema == "DPL_EIB_COMPLIANCE"
    assert rec.physical_fqn == "DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.CUST_NAME"
    assert rec.logical_ref == normalize_ref("ftr", "DPL_EIB_COMPLIANCE", "COMP_FIN_TRAN",
                                            "CUST_NAME")
    assert rec.term_name == "Customer Name"
    assert rec.term_type == "dimension"
    assert rec.bian_path == "Party / Customer / Identification / Legal"
    assert rec.process_path == "Onboarding / KYC / Screening"
    assert rec.synonyms == ("Client Name", "Account Holder")
    assert rec.related_terms == ("KYC Alias", "Screening Alias")
    assert rec.source_row == "18"
    assert rec.definition == "Registered legal name of the counterparty."


def test_operational_type_is_unknown_declared_type_retained():
    p = read_ftr_glossary(_FTR_CSV, source="ftr")
    assert all(r.type == UNKNOWN_TYPE for r in p.rows)          # resolution #1
    assert p.records[0].declared_type == "varchar"              # lowercased, bounded (resolution #3)
    assert p.records[1].declared_type == "decimal"
    assert p.records[2].declared_type == ""                     # table term declares no type


def test_declared_type_outside_sql_token_bound_is_dropped():
    csv_text = _HDR + _row(data_type="fibo:not-a-type!")        # fails ^[a-z0-9 _()]+$
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.records[0].declared_type == ""
    assert p.rows[0].type == UNKNOWN_TYPE


def test_source_row_stamped_on_rows_and_records():
    p = read_ftr_glossary(_FTR_CSV, source="ftr")
    assert [r.source_row for r in p.rows] == ["18", "19"]
    assert [r.source_row for r in p.records] == ["18", "19", "20"]


def test_table_term_yields_record_not_row():
    p = read_ftr_glossary(_FTR_CSV, source="ftr")
    table_rec = p.records[2]
    assert table_rec.is_table is True
    assert table_rec.term_type == "reference_data"
    assert len(p.rows) == 2   # no CanonicalRow for the 2-part table term


def test_envelope_versions_and_clean_fixture_count():
    p = read_ftr_glossary(_FTR_CSV, source="ftr")
    assert p.sanitizer_version == SANITIZER_VERSION
    assert p.redaction_version == REDACTION_VERSION
    assert p.sanitized_count == 0     # nothing stripped/blanked/redacted in the clean fixture


def test_to_glossary_upload_reuses_prepared_lists():
    p = read_ftr_glossary(_FTR_CSV, source="ftr")
    g = to_glossary_upload(p)
    assert isinstance(g, GlossaryUpload)
    assert g.rows is p.rows and g.records is p.records and g.quarantined is p.quarantined


# ── Quarantine: duplicate FQN ────────────────────────────────────────────────────────────────────

def test_duplicate_normalized_fqn_quarantines_both_rows():
    csv_text = _HDR + _row(source_row="18") + _row(
        source_row="19", fqn="dpl_eib_compliance.comp_fin_tran.cust_name")   # case-variant dup
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.rows == [] and p.records == []
    assert len(p.quarantined) == 2
    assert all("duplicate" in q.message and "fqn" in q.message.lower() for q in p.quarantined)


def test_quarantine_indices_disjoint_from_row_indices():
    csv_text = _HDR + _row(source_row="18") + _row(
        source_row="19", fqn="DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.TXN_AMT") + _row(
        source_row="20", fqn="DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.TXN_AMT")
    p = read_ftr_glossary(csv_text, source="ftr")
    assert len(p.rows) == 1      # CUST_NAME survives; the TXN_AMT pair is quarantined
    assert sorted(q.row_index for q in p.quarantined) == [1, 2]   # starts AT len(rows)


# ── Quarantine: term_type vocabulary (resolution #7) ─────────────────────────────────────────────

def test_term_type_vocab_is_the_versioned_closed_set():
    assert TERM_TYPE_VOCAB_V1 == frozenset(
        {"measure", "dimension", "code_value", "reference_data", "business_term"})


def test_unknown_term_type_quarantined_with_bounded_value_in_reason():
    csv_text = _HDR + _row(term_type="Mesure")
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.rows == [] and p.records == []
    assert len(p.quarantined) == 1
    assert "mesure" in p.quarantined[0].message
    assert "term_type" in p.quarantined[0].message


def test_blank_term_type_is_not_declared_and_passes():
    csv_text = _HDR + _row(term_type="")
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.quarantined == []
    assert p.records[0].term_type == ""


# ── Quarantine: source_row (resolution #12) ──────────────────────────────────────────────────────

def test_non_integer_source_row_quarantined():
    csv_text = _HDR + _row(source_row="abc")
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.rows == [] and p.records == []
    assert len(p.quarantined) == 1
    assert "source_row" in p.quarantined[0].message


def test_duplicate_source_row_quarantines_both_rows():
    csv_text = _HDR + _row(source_row="18") + _row(
        source_row="18", fqn="DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.TXN_AMT")
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.rows == [] and p.records == []
    assert len(p.quarantined) == 2
    assert all("source_row" in q.message for q in p.quarantined)


# ── Sanitize at parse time ───────────────────────────────────────────────────────────────────────

def test_unhandled_sample_clause_blanks_definition_but_row_survives():
    # v2 value-shape gate trigger: a residual "observed entries" marker + all-caps entity values.
    csv_text = _HDR + _row(
        definition='"Counterparty name; observed entries include ARTKOM FZE and NORDIC AS."')
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.quarantined == []
    assert len(p.rows) == 1 and p.rows[0].definition == ""
    assert p.records[0].definition == ""
    assert p.sanitized_count >= 1


def test_recognized_sample_clause_stripped_and_facets_kept():
    definition = ('"Customer account number. The sample profile is NUMERIC, with representative '
                  'values such as 3708484836801; 3708446902413; 3708454004701, which supports '
                  'interpretation of the field."')
    csv_text = _HDR + _row(definition=definition)
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.quarantined == []
    assert "3708484836801" not in p.rows[0].definition
    assert "3708484836801" not in p.records[0].definition
    assert p.records[0].logical_representation == "numeric_string"
    assert p.records[0].semantic_type == "identifier"
    assert p.sanitized_count >= 1


# ── Structural mirrors of read_glossary ──────────────────────────────────────────────────────────

def test_unresolvable_fqn_yields_identity_less_row_and_no_record():
    csv_text = _HDR + _row(fqn="no_dots_here")
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.records == [] and p.quarantined == []
    assert len(p.rows) == 1
    assert p.rows[0].table == "" and p.rows[0].column == ""   # validate_rows will quarantine


def test_multi_schema_fold_collision_quarantines_both_rows():
    csv_text = _HDR + _row(source_row="18", fqn="SCHEMA_A.COMP_FIN_TRAN.CUST_NAME") + _row(
        source_row="19", fqn="SCHEMA_B.COMP_FIN_TRAN.CUST_NAME")
    p = read_ftr_glossary(csv_text, source="ftr")
    assert p.rows == [] and p.records == []
    assert len(p.quarantined) == 2
    assert all("schema collision" in q.message for q in p.quarantined)
