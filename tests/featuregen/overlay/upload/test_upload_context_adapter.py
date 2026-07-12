import pytest
from featuregen.overlay.catalog import (
    current_catalog_adapter, register_catalog_adapter, _clear_catalog_adapter,
)
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.upload_catalog import (
    UploadContextAdapter, ensure_upload_catalog_adapter,
)


@pytest.fixture(autouse=True)
def _reset_adapter():
    _clear_catalog_adapter()
    yield
    _clear_catalog_adapter()


def test_ensure_registers_when_absent():
    with pytest.raises(RuntimeError):
        current_catalog_adapter()
    ensure_upload_catalog_adapter()
    assert isinstance(current_catalog_adapter(), UploadContextAdapter)


def test_adapter_owner_of_is_none_routes_to_governance():
    ref = CatalogObjectRef("src", "table", "public", "txn", None)
    assert UploadContextAdapter().owner_of(ref) is None
    assert UploadContextAdapter().get_fact(ref, "grain") is None


def test_ensure_is_idempotent_and_yields_to_existing():
    sentinel = UploadContextAdapter()
    register_catalog_adapter(sentinel)
    ensure_upload_catalog_adapter()  # must NOT clobber an already-registered adapter
    assert current_catalog_adapter() is sentinel
