from featuregen.overlay.catalog import CatalogFact, CatalogObject, FixtureCatalog
from featuregen.overlay.identity import CatalogObjectRef


def test_fixture_catalog_objects_facts_and_owner():
    cat = FixtureCatalog(catalog_source="pg:core")
    txn = CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema="core", table="transactions"
    )
    posted = CatalogObjectRef(
        catalog_source="pg:core", object_kind="column",
        schema="core", table="transactions", column="posted_at",
    )
    cat.add_object(CatalogObject(
        object_ref="core.transactions", object_kind="table", schema="core",
        table="transactions", column=None, data_type=None, native_oid="16500",
    ))
    cat.add_object(CatalogObject(
        object_ref="core.transactions.posted_at", object_kind="column", schema="core",
        table="transactions", column="posted_at",
        data_type="timestamp with time zone", native_oid=None,
    ))

    # list_objects returns exactly what was added.
    objs = {o.object_ref: o for o in cat.list_objects()}
    assert set(objs) == {"core.transactions", "core.transactions.posted_at"}
    assert objs["core.transactions.posted_at"].data_type == "timestamp with time zone"

    # get_fact returns a CatalogFact carrying the per-fact authoritative flag.
    cat.set_fact(posted, "availability_time",
                 {"column": "posted_at", "basis": "posted_at"}, authoritative=True)
    cat.set_fact(txn, "grain", {"columns": ["id"], "is_unique": True}, authoritative=False)

    avail = cat.get_fact(posted, "availability_time")
    assert avail == CatalogFact(value={"column": "posted_at", "basis": "posted_at"},
                                authoritative=True)
    assert cat.get_fact(txn, "grain").authoritative is False
    # use_case participates in fact identity: a policy_tag without the use_case is a miss.
    cat.set_fact(posted, "policy_tag", {"decision": "deny", "basis": "pii"},
                 authoritative=True, use_case="fraud_scoring")
    assert cat.get_fact(posted, "policy_tag", use_case="fraud_scoring").value == {
        "decision": "deny", "basis": "pii"}
    assert cat.get_fact(posted, "policy_tag") is None
    # unknown fact -> None.
    assert cat.get_fact(posted, "scd_effective_dating") is None

    # owner_of returns the recorded owner, else None.
    cat.set_owner(txn, "user:alice")
    assert cat.owner_of(txn) == "user:alice"
    assert cat.owner_of(posted) is None

    # fingerprint is object_ref -> CatalogObject for change detection.
    fp = cat.fingerprint()
    assert fp["core.transactions"].native_oid == "16500"
    assert set(fp) == {"core.transactions", "core.transactions.posted_at"}
