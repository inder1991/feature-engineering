from featuregen.overlay.upload.enrich_llm import _item_egress_ok


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
    # A non-definition descriptor scalar stays capped at 200 (only business_definition gets 600).
    bad = [{"column": "c0", "type": "x" * 201}]
    assert _item_egress_ok({"table": "txn", "column_profiles": bad}) is False


def test_descriptor_business_definition_allows_up_to_600():
    ok = [{"column": "c0", "business_definition": "x" * 600}]
    assert _item_egress_ok({"table": "txn", "column_profiles": ok}) is True
    bad = [{"column": "c0", "business_definition": "x" * 601}]
    assert _item_egress_ok({"table": "txn", "column_profiles": bad}) is False


def test_too_many_descriptors_fails():
    assert _item_egress_ok({"table": "txn", "column_profiles": _cols(65)}) is False


def test_existing_scalar_and_list_of_str_still_pass():
    assert _item_egress_ok({"table": "txn", "columns": ["a", "b"]}) is True
    assert _item_egress_ok({"table": "txn", "column": "c0"}) is True
