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


def test_version_pinned():
    assert BRIDGE_DERIVATION_VERSION == "1.0.0"
