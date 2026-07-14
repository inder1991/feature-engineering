from featuregen.overlay.dependencies import fact_dependencies


def _ref(source, table, col):
    return {"catalog_source": source, "object_kind": "column", "schema": "public",
            "table": table, "column": col}


def test_entity_bridge_dependencies_are_both_endpoints_under_own_source():
    value = {"entity_id": "customer",
             "left_ref": _ref("core", "customer_master", "customer_id"),
             "right_ref": _ref("crm", "customers", "customer_id")}
    deps = fact_dependencies("customer: ... <-> ...", "entity_bridge", value, "")
    assert deps == {
        ("core", "public.customer_master"),
        ("crm", "public.customers"),
        ("core", "public.customer_master.customer_id"),
        ("crm", "public.customers.customer_id"),
    }
