from __future__ import annotations

from featuregen.overlay.upload.bridge_candidates import (
    BRIDGE_DERIVATION_VERSION,
    derive_bridge_candidates,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph


def _load(db, source, rows_and_concepts):
    rows = [r for r, _ in rows_and_concepts]
    build_graph(db, source, rows, concepts={content_hash(r): c for r, c in rows_and_concepts})


def _two_catalog_customer(db):
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("core", "customer_master", "segment", "text"), "categorical"),
    ])
    _load(db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])


def test_derive_bridge_same_entity_distinct_catalogs(db):
    _two_catalog_customer(db)
    cands = derive_bridge_candidates(db)
    assert len(cands) == 1
    c = cands[0]
    assert c.entity_id == "customer"
    assert (c.left_ref.catalog_source, c.left_ref.table, c.left_ref.column) == ("core", "customer_master", "customer_id")
    assert (c.right_ref.catalog_source, c.right_ref.table, c.right_ref.column) == ("crm", "customers", "customer_id")
    assert c.left_ref.object_kind == "column" and c.left_ref.schema == "public"
    assert c.data_type_family == "integer"
    assert c.left_is_grain is True and c.right_is_grain is True
    assert len(c.candidate_id) == 16   # deterministic sha256[:16]


def test_same_catalog_pair_is_not_a_bridge(db):
    _load(db, "solo", [
        (CanonicalRow("solo", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("solo", "accounts", "customer_id", "integer"), "customer_id"),
    ])
    assert derive_bridge_candidates(db) == ()


def test_different_entities_do_not_bridge(db):
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _load(db, "cards", [
        (CanonicalRow("cards", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    assert derive_bridge_candidates(db) == ()


def test_incompatible_type_family_does_not_bridge(db):
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _load(db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "text", is_grain=True), "customer_id"),
    ])
    assert derive_bridge_candidates(db) == ()


def test_deterministic_candidate_id_is_orientation_independent(db):
    _two_catalog_customer(db)
    id1 = derive_bridge_candidates(db)[0].candidate_id
    # rebuild in the other declaration order -> same unordered candidate id
    build_graph(db, "core", [], concepts={})   # clear
    build_graph(db, "crm", [], concepts={})
    _load(db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    assert derive_bridge_candidates(db)[0].candidate_id == id1


def _glossary_col(db, source, table, column, concept_name, declared_type, *, is_grain=False):
    """A GLOSSARY-sourced column: the row attests no physical type (``type='unknown'``) and the
    file's declared type lands in ``graph_node.declared_type`` — exactly what the FTR adapter
    produces (`to_glossary_upload` emits `CanonicalRow.type='unknown'` while the sidecar record
    carries `declared_type`)."""
    row = CanonicalRow(source, table, column, "unknown", is_grain=is_grain)
    build_graph(db, source, [row], concepts={content_hash(row): concept_name},
                declared_types={f"public.{table}.{column}": declared_type})


def test_glossary_declared_type_bridges_when_nothing_attested_a_type(db):
    """THE FTR CASE. A glossary upload attests no physical type, so `data_type` is 'unknown' for
    every column and only `declared_type` carries the file's own answer. Matching on `data_type`
    alone classified all of them as an unknown family and skipped them, which made a bridge
    candidate STRUCTURALLY IMPOSSIBLE for any glossary-sourced catalog — measured on the deployed
    FTR catalog: 126/126 columns `data_type='unknown'`, 113 of them `declared_type='string'`,
    including the one `customer_id` (`cif_id`) the cross-catalog story depends on."""
    _glossary_col(db, "ftr", "tran_repos", "cif_id", "customer_id", "string")
    _glossary_col(db, "cust", "customer_master", "customer_no", "customer_id", "string", is_grain=True)
    cands = derive_bridge_candidates(db)
    assert len(cands) == 1, "a declared string<->string identifier pair must bridge"
    assert cands[0].entity_id == "customer"
    assert cands[0].data_type_family == "text"       # 'string' -> text, same family both sides
    assert cands[0].type_basis == "declared"         # weaker basis, surfaced not hidden


def test_attested_type_still_wins_over_a_declared_one(db):
    """The fallback is a fallback: an attested `data_type` is the stronger authority and decides the
    family whenever it is present, so a declared value can never override or weaken it."""
    row = CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True)
    build_graph(db, "core", [row], concepts={content_hash(row): "customer_id"},
                declared_types={"public.customer_master.customer_id": "string"})
    _glossary_col(db, "crm", "customers", "customer_id", "customer_id", "integer")
    cands = derive_bridge_candidates(db)
    assert len(cands) == 1
    assert cands[0].data_type_family == "integer"    # attested 'integer' beat declared 'string'
    assert cands[0].type_basis == "mixed"            # one attested side, one declared


def test_a_declared_type_cannot_bridge_across_incompatible_families(db):
    """The safety rule the family check exists for survives the fallback: falling back to a declared
    value must not start matching text to integers."""
    _glossary_col(db, "ftr", "tran_repos", "cif_id", "customer_id", "string")
    _glossary_col(db, "cust", "customer_master", "customer_no", "customer_id", "bigint")
    assert derive_bridge_candidates(db) == ()


def test_an_unclassifiable_declared_type_still_does_not_bridge(db):
    """A declared value the platform cannot classify is no better than none: it stays out rather
    than being admitted as a wildcard that matches everything."""
    _glossary_col(db, "ftr", "tran_repos", "cif_id", "customer_id", "struct<a:int>")
    _glossary_col(db, "cust", "customer_master", "customer_no", "customer_id", "string")
    assert derive_bridge_candidates(db) == ()


def test_version_pinned():
    assert BRIDGE_DERIVATION_VERSION == "1.0.0"
