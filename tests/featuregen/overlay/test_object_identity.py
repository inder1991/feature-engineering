from __future__ import annotations

from featuregen.overlay.catalog import CatalogObject, FixtureCatalog
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.object_identity import (
    LogicalObjectRef,
    ObjectBinding,
    ObjectIdentityStatus,
    ProviderObjectRef,
    classify_identity,
    may_attach,
    resolve_object_identity,
)


def _balance_ref(catalog_source: str = "pg:core") -> CatalogObjectRef:
    return CatalogObjectRef(
        catalog_source=catalog_source,
        object_kind="column",
        schema="public",
        table="accounts",
        column="balance",
    )


# --- pure classifier ---------------------------------------------------------------------
# The classifier is the ONLY place the AMBIGUOUS branch is exercised: a single Postgres catalog
# resolves a ref to 0 or 1 native object, so >1 candidates can only arise from a FUTURE
# glossary/cross-provider adapter. We test it directly rather than via the real adapter.


def test_classify_single_candidate_is_exact() -> None:
    assert classify_identity(("16500:2",)) is ObjectIdentityStatus.EXACT


def test_classify_no_candidates_is_unresolved() -> None:
    assert classify_identity(()) is ObjectIdentityStatus.UNRESOLVED


def test_classify_multiple_candidates_is_ambiguous() -> None:
    assert classify_identity(("16500:2", "20999:2")) is ObjectIdentityStatus.AMBIGUOUS


# --- logical / provider wrappers ---------------------------------------------------------


def test_logical_and_provider_wrappers_roundtrip() -> None:
    lr = LogicalObjectRef("cat1", "public", "accounts", "balance")
    pr = ProviderObjectRef("ftr_glossary", "snap1", "public.accounts.balance")
    assert lr.logical_catalog_id == "cat1"
    assert lr.column == "balance"
    assert pr.provider_id == "ftr_glossary"
    assert pr.provider_snapshot_id == "snap1"
    assert pr.native_ref == "public.accounts.balance"


# --- resolver over the REAL FixtureCatalog test double -----------------------------------


def test_resolve_exact_via_fixture_catalog() -> None:
    adapter = FixtureCatalog(catalog_source="pg:core")
    adapter.add_object(
        CatalogObject(
            object_ref="public.accounts.balance",
            object_kind="column",
            schema="public",
            table="accounts",
            column="balance",
            data_type="numeric",
            native_oid="16500:2",
        )
    )
    binding = resolve_object_identity(adapter, _balance_ref())
    assert binding.status is ObjectIdentityStatus.EXACT
    assert binding.candidates == ("16500:2",)
    assert binding.logical_ref == LogicalObjectRef("pg:core", "public", "accounts", "balance")
    assert may_attach(binding) is True


def test_resolve_unresolved_when_object_absent() -> None:
    adapter = FixtureCatalog(catalog_source="pg:core")
    binding = resolve_object_identity(adapter, _balance_ref())
    assert binding.status is ObjectIdentityStatus.UNRESOLVED
    assert binding.candidates == ()
    assert binding.logical_ref is None
    assert may_attach(binding) is False


def test_resolve_unresolved_when_native_oid_missing() -> None:
    # Present in the catalog but with no stable native id -> zero candidates -> UNRESOLVED,
    # NOT EXACT: an object we can name but cannot pin to a native identity is not attachable.
    adapter = FixtureCatalog(catalog_source="pg:core")
    adapter.add_object(
        CatalogObject(
            object_ref="public.accounts.balance",
            object_kind="column",
            schema="public",
            table="accounts",
            column="balance",
            data_type="numeric",
            native_oid=None,
        )
    )
    binding = resolve_object_identity(adapter, _balance_ref())
    assert binding.status is ObjectIdentityStatus.UNRESOLVED
    assert binding.candidates == ()
    assert binding.logical_ref is None


# --- attach guard ------------------------------------------------------------------------


def test_may_attach_only_for_exact_and_aliased() -> None:
    assert may_attach(ObjectBinding(None, ObjectIdentityStatus.EXACT, ("x",))) is True
    assert may_attach(ObjectBinding(None, ObjectIdentityStatus.ALIASED, ("x",))) is True
    assert may_attach(ObjectBinding(None, ObjectIdentityStatus.AMBIGUOUS, ("x", "y"))) is False
    assert may_attach(ObjectBinding(None, ObjectIdentityStatus.UNRESOLVED, ())) is False
