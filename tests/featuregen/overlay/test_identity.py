from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    ColumnPair,
    display_object_ref,
    fact_key,
    proposal_fingerprint,
)

TX = CatalogObjectRef(
    catalog_source="pg:core", object_kind="table", schema="core", table="transactions"
)
CUST = CatalogObjectRef(
    catalog_source="pg:core", object_kind="table", schema="core", table="customers"
)
ACCT = CatalogObjectRef(
    catalog_source="pg:core", object_kind="table", schema="core", table="accounts"
)


def test_fact_key_is_deterministic_64_hex():
    k1 = fact_key(TX, "grain")
    k2 = fact_key(TX, "grain")
    assert k1 == k2
    assert len(k1) == 64 and all(c in "0123456789abcdef" for c in k1)


def test_fact_key_is_case_and_whitespace_normalized():
    noisy = CatalogObjectRef(
        catalog_source="PG:CORE", object_kind="Table", schema=" Core ", table="TRANSACTIONS"
    )
    assert fact_key(noisy, "grain") == fact_key(TX, "grain")


def test_two_different_joins_on_one_source_table_get_distinct_keys():
    join_to_customers = ApprovedJoinRef(
        from_ref=TX,
        to_ref=CUST,
        column_pairs=(ColumnPair("customer_id", "id"),),
        cardinality="N:1",
    )
    join_to_accounts = ApprovedJoinRef(
        from_ref=TX,
        to_ref=ACCT,
        column_pairs=(ColumnPair("account_id", "id"),),
        cardinality="N:1",
    )
    assert fact_key(join_to_customers, "approved_join") != fact_key(
        join_to_accounts, "approved_join"
    )


def test_reordering_a_column_list_independently_does_not_alias_two_joins():
    # Pairs MUST be sorted as units. Join A pairs (a->x),(b->y); Join B pairs (a->y),(b->x).
    # If from-cols and to-cols were sorted independently both would canonicalize to
    # froms=[a,b] tos=[x,y] and collide. Pair-unit sorting keeps them distinct.
    join_a = ApprovedJoinRef(
        from_ref=TX,
        to_ref=CUST,
        column_pairs=(ColumnPair("a", "x"), ColumnPair("b", "y")),
        cardinality="N:1",
    )
    join_b = ApprovedJoinRef(
        from_ref=TX,
        to_ref=CUST,
        column_pairs=(ColumnPair("a", "y"), ColumnPair("b", "x")),
        cardinality="N:1",
    )
    assert fact_key(join_a, "approved_join") != fact_key(join_b, "approved_join")


def test_pair_order_does_not_matter_when_pairs_are_the_same_units():
    j1 = ApprovedJoinRef(
        from_ref=TX,
        to_ref=CUST,
        column_pairs=(ColumnPair("a", "x"), ColumnPair("b", "y")),
        cardinality="N:1",
    )
    j2 = ApprovedJoinRef(
        from_ref=TX,
        to_ref=CUST,
        column_pairs=(ColumnPair("b", "y"), ColumnPair("a", "x")),
        cardinality="N:1",
    )
    assert fact_key(j1, "approved_join") == fact_key(j2, "approved_join")


def test_use_case_changes_key_for_policy_facts():
    col = CatalogObjectRef(
        catalog_source="pg:core",
        object_kind="column",
        schema="core",
        table="customers",
        column="ssn",
    )
    assert fact_key(col, "policy_tag", use_case="fraud") != fact_key(
        col, "policy_tag", use_case="marketing"
    )


def test_display_object_ref_is_human_readable():
    col = CatalogObjectRef(
        catalog_source="pg:core",
        object_kind="column",
        schema="core",
        table="transactions",
        column="posted_at",
    )
    assert display_object_ref(col) == "core.transactions.posted_at"
    assert display_object_ref(TX) == "core.transactions"


def test_proposal_fingerprint_ignores_key_order_but_tracks_value_and_thresholds():
    a = proposal_fingerprint(
        {"columns": ["x", "y"], "is_unique": True}, profile_version="p1", thresholds={"min": 0.99}
    )
    b = proposal_fingerprint(
        {"is_unique": True, "columns": ["x", "y"]}, profile_version="p1", thresholds={"min": 0.99}
    )
    c = proposal_fingerprint(
        {"columns": ["x", "y"], "is_unique": True}, profile_version="p1", thresholds={"min": 0.90}
    )
    assert a == b
    assert a != c
