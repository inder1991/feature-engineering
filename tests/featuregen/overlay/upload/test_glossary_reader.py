from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, validate_rows
from featuregen.overlay.upload.glossary_reader import (
    GlossaryRecord,
    GlossaryUpload,
    is_glossary_csv,
    read_glossary,
)
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE

# An FTR-shaped glossary CSV (spec §U): a schema-qualified physical FQN + business meaning, no
# physical type. Two COLUMN terms (3-part FQN) + one TABLE term (2-part FQN). Inline fixture — the
# task brief forbids reading ~/Downloads.
_GLOSSARY_CSV = (
    "physical_name,business_term,description_business_definition,data_domain,"
    "synonyms,bian_path,fibo_path\n"
    "DPL_EIB_COMPLIANCE.COMP_REPOS_DLY.CUST_NAME,Customer Name,"
    "The legal name of the customer as registered.,Party,"
    "Client Name;Account Holder,Party/Customer,fibo-be-le-lp:LegalPerson\n"
    "DPL_EIB_COMPLIANCE.COMP_REPOS_DLY.ACCT_BAL,Account Balance,"
    "The ledger balance of the account.,Deposits,"
    "Balance,Product/CurrentAccount,fibo-fbc:Balance\n"
    "DPL_EIB_COMPLIANCE.COMP_REPOS_DLY,Compliance Repository,"
    "Daily compliance repository table.,Compliance,,Reference/Table,\n"
)

_GLOSSARY_HEADERS = ["physical_name", "business_term", "description_business_definition",
                     "data_domain", "synonyms", "bian_path", "fibo_path"]
_CANONICAL_HEADERS = ["source", "table", "column", "type", "definition", "sensitivity"]


def test_is_glossary_csv_true_for_glossary_headers():
    assert is_glossary_csv(_GLOSSARY_HEADERS) is True


def test_is_glossary_csv_false_for_canonical_headers():
    assert is_glossary_csv(_CANONICAL_HEADERS) is False


def test_returns_a_glossary_upload():
    up = read_glossary(_GLOSSARY_CSV, source="ftr")
    assert isinstance(up, GlossaryUpload)


def test_three_part_row_is_a_column_with_unknown_type():
    up = read_glossary(_GLOSSARY_CSV, source="ftr")
    # Only the two COLUMN terms become CanonicalRows; the 2-part table term does not.
    assert len(up.rows) == 2
    cust = next(r for r in up.rows if r.column == "CUST_NAME")
    assert cust.table == "COMP_REPOS_DLY"
    assert cust.type == UNKNOWN_TYPE == "unknown"          # sentinel, NEVER ""
    assert cust.type != ""
    assert cust.definition == "The legal name of the customer as registered."
    assert cust.source == "ftr"


def test_schema_is_preserved_in_the_records_logical_ref():
    up = read_glossary(_GLOSSARY_CSV, source="ftr")
    cust = next(r for r in up.records if r.term_name == "Customer Name")
    # The schema-preserving normalized ref includes the schema segment (spec §5.1).
    assert cust.logical_ref == normalize_ref(
        "ftr", "DPL_EIB_COMPLIANCE", "COMP_REPOS_DLY", "CUST_NAME")
    assert "dpl_eib_compliance" in cust.logical_ref
    assert cust.is_table is False


def test_two_part_fqn_is_a_table_record_not_a_column():
    up = read_glossary(_GLOSSARY_CSV, source="ftr")
    # No CanonicalRow for the table term.
    assert all(r.column != "" and "." not in r.table for r in up.rows)
    table_rec = next(r for r in up.records if r.term_name == "Compliance Repository")
    assert table_rec.is_table is True
    assert table_rec.logical_ref == normalize_ref("ftr", "DPL_EIB_COMPLIANCE", "COMP_REPOS_DLY")
    # A table ref has no column segment.
    assert table_rec.logical_ref.count(".") == 1


def test_record_carries_semantic_sidecar_fields():
    up = read_glossary(_GLOSSARY_CSV, source="ftr")
    cust = next(r for r in up.records if r.term_name == "Customer Name")
    assert isinstance(cust, GlossaryRecord)
    assert cust.definition == "The legal name of the customer as registered."
    assert cust.domain == "Party"
    assert cust.synonyms == ("Client Name", "Account Holder")
    assert cust.bian_path == "Party/Customer"
    assert cust.fibo_path == "fibo-be-le-lp:LegalPerson"


def test_one_record_per_input_row_keyed_by_normalize_ref():
    up = read_glossary(_GLOSSARY_CSV, source="ftr")
    assert len(up.records) == 3                               # 2 columns + 1 table term
    assert len({r.logical_ref for r in up.records}) == 3      # each keyed by a distinct ref


def test_invalid_fqn_yields_a_quarantinable_row_and_no_record():
    csv = (
        "physical_name,business_term,description_business_definition,bian_path,fibo_path\n"
        "CUST_NAME,Customer Name,A name.,Party/Customer,fibo:X\n"   # 1-part = no resolvable identity
    )
    up = read_glossary(csv, source="ftr")
    assert up.records == []                                   # cannot key a record without a table
    assert len(up.rows) == 1
    assert up.rows[0].table == "" and up.rows[0].column == ""  # missing identity -> quarantines


def test_read_glossary_rows_all_pass_under_glossary_profile():
    up = read_glossary(_GLOSSARY_CSV, source="ftr")
    vr = validate_rows(up.rows, "ftr", profile=FTR_GLOSSARY_PROFILE)
    assert len(vr.good) == 2                                  # both column terms accepted
    assert vr.quarantined == []
