from featuregen.overlay.upload.enrich_llm import (
    _MAX_COLUMN_PROFILES,
    _MAX_LEN_DEFAULT,
    MAX_DEFINITION_LEN,
    _item_egress_ok,
)


def _cols(n=2):
    return [{"column": f"c{i}", "type": "int", "concept": "amount",
             "business_definition": "the posted amount"} for i in range(n)]


def test_table_item_with_column_profiles_passes():
    assert _item_egress_ok({"table": "txn", "column_profiles": _cols()}) is True


def test_descriptor_with_forbidden_definition_key_fails():
    bad = [{"column": "c0", "type": "int", "definition": "leaky free text"}]
    assert _item_egress_ok({"table": "txn", "column_profiles": bad}) is False


def test_descriptor_with_non_string_value_fails():
    bad = [{"column": "c0", "type": "int", "concept": ["not", "a", "string"]}]
    assert _item_egress_ok({"table": "txn", "column_profiles": bad}) is False


def test_oversized_descriptor_value_fails():
    # A non-definition descriptor scalar stays at the tighter default; only the definition keys get
    # the wider window. Both bounds are read from the constants — the 2026-08-06 zero-truncation
    # raise moved them, and a literal here would have asserted the new admissible size is refused.
    assert _item_egress_ok(
        {"table": "txn", "column_profiles": [{"column": "c0", "type": "x" * _MAX_LEN_DEFAULT}]})
    bad = [{"column": "c0", "type": "x" * (_MAX_LEN_DEFAULT + 1)}]
    assert _item_egress_ok({"table": "txn", "column_profiles": bad}) is False


def test_descriptor_business_definition_allows_up_to_the_definition_bound():
    assert _MAX_LEN_DEFAULT < MAX_DEFINITION_LEN
    ok = [{"column": "c0", "business_definition": "x" * MAX_DEFINITION_LEN}]
    assert _item_egress_ok({"table": "txn", "column_profiles": ok}) is True
    bad = [{"column": "c0", "business_definition": "x" * (MAX_DEFINITION_LEN + 1)}]
    assert _item_egress_ok({"table": "txn", "column_profiles": bad}) is False


def test_too_many_descriptors_fails():
    """The COUNT bound. Sized off `_MAX_COLUMN_PROFILES` because that constant is also Pass B's
    narrow/wide router — the 2026-08-06 raise took it 64 -> 512, and a fixed 65 would have turned
    this into an assertion that a perfectly admissible item is refused."""
    assert _item_egress_ok({"table": "txn", "column_profiles": _cols(_MAX_COLUMN_PROFILES)}) is True
    assert _item_egress_ok(
        {"table": "txn", "column_profiles": _cols(_MAX_COLUMN_PROFILES + 1)}) is False


def test_existing_scalar_and_list_of_str_still_pass():
    assert _item_egress_ok({"table": "txn", "columns": ["a", "b"]}) is True
    assert _item_egress_ok({"table": "txn", "column": "c0"}) is True
