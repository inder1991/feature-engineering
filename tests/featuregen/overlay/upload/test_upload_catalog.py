from featuregen.overlay.catalog import CatalogObject
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref


def test_fingerprint_has_table_and_column_objects():
    rows = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
            CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True)]
    cat = UploadCatalog("deposits", rows)
    fp = cat.fingerprint()
    assert cat.catalog_source == "deposits"
    assert "public.accounts" in fp
    assert "public.accounts.id" in fp
    assert "public.accounts.posted_at" in fp
    assert isinstance(fp["public.accounts"], CatalogObject)
    assert fp["public.accounts"].object_kind == "table"
    assert fp["public.accounts.id"].object_kind == "column"
    assert fp["public.accounts.id"].data_type == "integer"


def test_get_fact_and_owner_are_none():
    cat = UploadCatalog("deposits", [CanonicalRow("deposits", "accounts", "id", "integer")])
    ref = table_ref("deposits", "accounts")
    assert cat.get_fact(ref, "grain") is None
    assert cat.owner_of(ref) is None
