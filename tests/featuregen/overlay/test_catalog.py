from featuregen.overlay.catalog import (
    CatalogFact,
    CatalogObject,
    FixtureCatalog,
    PostgresCatalog,
)
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


def test_postgres_catalog_reads_structure_and_returns_no_ml_facts(db):
    # DDL runs inside the test transaction (rolled back on teardown); information_schema and
    # pg_catalog reflect the uncommitted table within the same transaction.
    with db.cursor() as cur:
        cur.execute(
            "CREATE TABLE overlay_cat_probe ("
            "  id bigint PRIMARY KEY,"
            "  posted_at timestamptz NOT NULL,"
            "  amount numeric"
            ")"
        )

    cat = PostgresCatalog(db, catalog_source="pg:core", schemas=("public",))
    objs = {o.object_ref: o for o in cat.list_objects()}

    # The table object exists, with a stable native oid from pg_catalog.
    assert "public.overlay_cat_probe" in objs
    table_obj = objs["public.overlay_cat_probe"]
    assert table_obj.object_kind == "table"
    assert table_obj.column is None
    assert table_obj.data_type is None
    assert table_obj.native_oid is not None and table_obj.native_oid.isdigit()

    # Columns exist, with correct information_schema data types.
    assert objs["public.overlay_cat_probe.id"].object_kind == "column"
    assert objs["public.overlay_cat_probe.id"].data_type == "bigint"
    assert objs["public.overlay_cat_probe.posted_at"].data_type == "timestamp with time zone"
    assert objs["public.overlay_cat_probe.amount"].data_type == "numeric"

    # A column's native_oid is the composite "<table_oid>:<attnum>" (overview pin 16) so the
    # column has a stable identity that survives a rename (see the rename test below). The
    # table_oid portion matches the owning table's native oid.
    posted_native = objs["public.overlay_cat_probe.posted_at"].native_oid
    assert posted_native is not None
    tbl_oid, sep, attnum = posted_native.partition(":")
    assert sep == ":" and tbl_oid == table_obj.native_oid and attnum.isdigit()

    # information_schema records NONE of the five ML fact types authoritatively.
    table_ref = CatalogObjectRef(
        catalog_source="pg:core", object_kind="table",
        schema="public", table="overlay_cat_probe",
    )
    col_ref = CatalogObjectRef(
        catalog_source="pg:core", object_kind="column",
        schema="public", table="overlay_cat_probe", column="posted_at",
    )
    for fact_type in (
        "availability_time", "grain", "scd_effective_dating", "approved_join", "policy_tag",
    ):
        assert cat.get_fact(table_ref, fact_type) is None
    assert cat.get_fact(col_ref, "policy_tag", use_case="fraud_scoring") is None

    # Ownership is not recorded by the structural catalog.
    assert cat.owner_of(table_ref) is None

    # fingerprint includes the table keyed by object_ref, carrying its oid (rename detection §8).
    fp = cat.fingerprint()
    assert "public.overlay_cat_probe" in fp
    assert fp["public.overlay_cat_probe"].native_oid == table_obj.native_oid


def test_postgres_catalog_column_native_oid_is_stable_across_rename(db):
    # A column's native_oid is "<table_oid>:<attnum>" (overview pin 16). pg_attribute.attnum is
    # fixed at creation and not reused on rename, so the SAME column keeps the SAME native_oid
    # after a RENAME COLUMN — letting change detection (Phase 7) track the rename instead of
    # degrading it to a drop+add.
    with db.cursor() as cur:
        cur.execute(
            "CREATE TABLE overlay_rename_probe ("
            "  id bigint PRIMARY KEY,"
            "  posted_at timestamptz NOT NULL"
            ")"
        )

    cat = PostgresCatalog(db, catalog_source="pg:core", schemas=("public",))
    before = {o.object_ref: o for o in cat.list_objects()}
    native_before = before["public.overlay_rename_probe.posted_at"].native_oid
    assert native_before is not None and ":" in native_before

    with db.cursor() as cur:
        cur.execute(
            "ALTER TABLE overlay_rename_probe RENAME COLUMN posted_at TO event_time"
        )

    after = {o.object_ref: o for o in cat.list_objects()}
    # The new name appears; the old name is gone.
    assert "public.overlay_rename_probe.event_time" in after
    assert "public.overlay_rename_probe.posted_at" not in after
    # ...but the native_oid is unchanged: same column identity across the rename.
    assert after["public.overlay_rename_probe.event_time"].native_oid == native_before
