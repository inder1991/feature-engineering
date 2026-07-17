from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, validate_rows
from featuregen.overlay.upload.glossary_reader import (
    GlossaryRecord,
    GlossaryUpload,
    _split_fqn,
    is_glossary_csv,
    join_path,
    read_glossary,
    split_list,
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


def test_fqn_with_an_empty_component_has_no_resolvable_identity():
    # `schema..column` used to be silently REINTERPRETED (#26): the empty component was filtered out
    # BEFORE the arity check, so a malformed 3-part FQN collapsed into a valid-looking 2-part TABLE
    # term. An empty dot-separated piece means the identity is NOT resolvable — reject the whole FQN.
    for bad in ("schema..column", ".a.b", "a.b.", "a..b", "..", " . . "):
        assert _split_fqn(bad) == (None, None, None), f"{bad!r} must not resolve"


def test_valid_fqns_still_parse_after_the_empty_component_guard():
    assert _split_fqn("schema.table.column") == ("schema", "table", "column")
    assert _split_fqn("schema.table") == ("schema", "table", None)
    assert _split_fqn(" s . t . c ") == ("s", "t", "c")       # per-part strip still applies
    assert _split_fqn("no_dots") == (None, None, None)        # 1-part stays unresolvable
    assert _split_fqn("") == (None, None, None)


def test_fqn_with_an_empty_component_quarantines_and_emits_no_record():
    csv = (
        "physical_name,business_term,description_business_definition,bian_path,fibo_path\n"
        "DPL_EIB_COMPLIANCE..CUST_NAME,Customer Name,A name.,Party/Customer,fibo:X\n"
    )
    up = read_glossary(csv, source="ftr")
    assert up.records == []                                   # no sidecar for a malformed FQN
    assert len(up.rows) == 1
    assert up.rows[0].table == "" and up.rows[0].column == ""  # identity-less -> quarantined
    vr = validate_rows(up.rows, "ftr", profile=None)
    assert vr.good == []


def test_read_glossary_rows_all_pass_under_glossary_profile():
    up = read_glossary(_GLOSSARY_CSV, source="ftr")
    vr = validate_rows(up.rows, "ftr", profile=FTR_GLOSSARY_PROFILE)
    assert len(vr.good) == 2                                  # both column terms accepted
    assert vr.quarantined == []


# --- optional declared physical type (`data_type` column) ----------------------------------------
# A glossary MAY carry the source column's SQL type; used when present (lowercased), else UNKNOWN_TYPE.
# It is a DECLARED value — a structural source (OpenMetadata/DDL) stays the stronger authority.
_GLOSSARY_CSV_WITH_TYPE = (
    "physical_name,business_term,description_business_definition,data_domain,"
    "synonyms,bian_path,fibo_path,data_type\n"
    "DPL_EIB_COMPLIANCE.COMP_REPOS_DLY.FORACID,Customer Account Number,"
    "The account-level identifier.,Compliance,"
    "Account Number,Payments/TransactionRecord,fibo-fbc:Account,VARCHAR\n"
    "DPL_EIB_COMPLIANCE.COMP_REPOS_DLY.TRAN_DATE,Transaction Date,"
    "The date of the transaction.,Compliance,"
    ",Payments/TransactionRecord,fibo-fbc:Date,NUMBER\n"
    "DPL_EIB_COMPLIANCE.COMP_REPOS_DLY.NOTE_TXT,Note Text,"
    "A free-text note.,Compliance,"
    ",Payments/TransactionRecord,fibo-fbc:Text,\n"           # data_type cell left BLANK
)

_GLOSSARY_HEADERS_WITH_TYPE = _GLOSSARY_HEADERS + ["data_type"]


def test_declared_data_type_is_read_and_lowercased():
    up = read_glossary(_GLOSSARY_CSV_WITH_TYPE, source="ftr")
    foracid = next(r for r in up.rows if r.column == "FORACID")
    tran = next(r for r in up.rows if r.column == "TRAN_DATE")
    assert foracid.type == "varchar"                         # VARCHAR declared -> lowercased
    assert tran.type == "number"


def test_blank_data_type_cell_falls_back_to_unknown():
    up = read_glossary(_GLOSSARY_CSV_WITH_TYPE, source="ftr")
    note = next(r for r in up.rows if r.column == "NOTE_TXT")
    assert note.type == UNKNOWN_TYPE                          # column present, cell blank -> default


def test_absent_data_type_column_stays_unknown():
    # The original fixture has NO data_type column: every row keeps the unknown sentinel (back-compat).
    up = read_glossary(_GLOSSARY_CSV, source="ftr")
    assert all(r.type == UNKNOWN_TYPE for r in up.rows)


def test_data_type_column_does_not_flip_the_glossary_profile():
    # Detection keys on business-term/BIAN/FIBO presence + table/column ABSENCE, not on a type column,
    # so adding data_type must NOT reclassify a glossary as a technical CSV.
    assert is_glossary_csv(_GLOSSARY_HEADERS_WITH_TYPE) is True


def test_declared_type_rows_still_pass_under_glossary_profile():
    up = read_glossary(_GLOSSARY_CSV_WITH_TYPE, source="ftr")
    vr = validate_rows(up.rows, "ftr", profile=FTR_GLOSSARY_PROFILE)
    assert len(vr.good) == 3                                  # a declared type is accepted, not rejected
    assert vr.quarantined == []


# --- #9 — multi-schema fold collision (the flat graph is single-schema) ---------------------------
# The graph node ref is `public.<table>.<column>` (schema hardcoded), so two glossary rows from
# DIFFERENT schemas sharing (table, column) would silently fold into ONE node (last-writer-wins).
# Fail closed: the reader — the only place the schema is still in hand — quarantines BOTH rows.

_COLLISION_CSV = (
    "physical_name,business_term,description_business_definition,data_domain,bian_path,fibo_path\n"
    "sales.orders.id,Order Id,The sales order identifier.,Sales,,\n"
    "hr.orders.id,Order Id,The HR order identifier.,HR,,\n"
)


def test_multi_schema_fold_collision_quarantines_both_rows():
    up = read_glossary(_COLLISION_CSV, source="ftr")
    assert up.rows == []                                      # neither row may reach the graph
    assert up.records == []                                   # no sidecar for a quarantined identity
    assert len(up.quarantined) == 2
    for e in up.quarantined:
        assert "schema collision" in e.message
        assert "orders.id" in e.message
        assert "sales" in e.message and "hr" in e.message     # both schemas named for the reviewer
        assert "single-schema" in e.message
        assert e.row is not None and e.row.table == "orders" and e.row.column == "id"
    # Indexes are unique and start AT len(rows), disjoint from validate_rows' 0..len(rows)-1 space
    # (quarantine_row PKs on (catalog_source, row_index), so an overlap would abort the ingest tx).
    assert sorted(e.row_index for e in up.quarantined) == [len(up.rows), len(up.rows) + 1]


def test_same_schema_repeated_is_not_a_collision():
    csv = (
        "physical_name,business_term,description_business_definition,data_domain,bian_path,fibo_path\n"
        "sales.orders.id,Order Id,The sales order identifier.,Sales,,\n"
        "SALES.orders.id,Order Id,The sales order identifier.,Sales,,\n"   # case-variant SAME schema
    )
    up = read_glossary(csv, source="ftr")
    assert up.quarantined == []                               # one schema -> no false collision
    assert len(up.rows) == 2                                  # validate_rows dedups them downstream
    vr = validate_rows(up.rows, "ftr", profile=FTR_GLOSSARY_PROFILE)
    assert len(vr.good) == 1 and vr.quarantined == []


def test_distinct_columns_across_schemas_ingest_normally():
    csv = (
        "physical_name,business_term,description_business_definition,data_domain,bian_path,fibo_path\n"
        "sales.orders.id,Order Id,The sales order identifier.,Sales,,\n"
        "hr.customers.name,Customer Name,The HR customer name.,HR,,\n"    # no (table, column) fold
    )
    up = read_glossary(csv, source="ftr")
    assert up.quarantined == []
    assert {(r.table, r.column) for r in up.rows} == {("orders", "id"), ("customers", "name")}
    assert len(up.records) == 2


def test_collision_quarantine_does_not_touch_clean_rows_in_the_same_file():
    csv = (
        "physical_name,business_term,description_business_definition,data_domain,bian_path,fibo_path\n"
        "sales.orders.id,Order Id,The sales order identifier.,Sales,,\n"
        "hr.orders.id,Order Id,The HR order identifier.,HR,,\n"
        "public.accounts.balance,Account Balance,The ledger balance.,Deposits,,\n"
    )
    up = read_glossary(csv, source="ftr")
    assert len(up.quarantined) == 2                           # only the colliding pair
    assert [(r.table, r.column) for r in up.rows] == [("accounts", "balance")]
    assert [r.term_name for r in up.records] == ["Account Balance"]


# --- Shared whitelisted transforms (Task 1): join_path / split_list -------------------------------


def test_join_path_joins_ordered_levels_dropping_blanks():
    assert join_path(["Party", "", "Customer"]) == "Party / Customer"


def test_join_path_drops_whitespace_only_parts():
    assert join_path(["Party", "   ", "Customer", ""]) == "Party / Customer"


def test_join_path_of_all_blank_parts_is_empty():
    assert join_path(["", "  ", ""]) == ""


def test_join_path_honors_a_custom_separator():
    assert join_path(["A", "B"], sep="/") == "A/B"


def test_split_list_splits_on_any_listed_delimiter():
    assert split_list("A; B | C") == ("A", "B", "C")


def test_split_list_strips_and_drops_empties():
    assert split_list(" A ;; B ;  ") == ("A", "B")


def test_split_list_of_blank_input_is_empty():
    assert split_list("") == ()
    assert split_list("  ") == ()


def test_split_list_honors_custom_delimiters():
    assert split_list("A, B; C", delimiters=(",", ";")) == ("A", "B", "C")


def test_split_list_does_not_split_on_commas_by_default():
    # Commas inside a quoted CSV cell are legit characters, not list separators.
    assert split_list("Smith, John; Doe, Jane") == ("Smith, John", "Doe, Jane")


def test_glossary_record_new_fields_default_empty():
    rec = GlossaryRecord(logical_ref="ftr.public.t.c", term_name="T", definition="D")
    assert rec.source_row == ""
    assert rec.term_type == ""
    assert rec.process_path == ""
    assert rec.related_terms == ()
    assert rec.schema == ""
    assert rec.physical_fqn == ""
    assert rec.logical_representation == ""
    assert rec.semantic_type == ""
