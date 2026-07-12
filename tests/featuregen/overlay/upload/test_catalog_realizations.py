"""Phase-3B.2A — deterministic catalog-realization derivation over declared joins."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.catalog_realizations import (
    NormalizedRealization,
    cardinality_from_token,
    invert_cardinality,
    normalize_realization,
)
from featuregen.overlay.upload.taxonomy.entity_registry import global_relationship_for
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality


def test_cardinality_token_mapping():
    assert cardinality_from_token("N:1") is Cardinality.MANY_TO_ONE
    assert cardinality_from_token("1:N") is Cardinality.ONE_TO_MANY
    assert cardinality_from_token("1:1") is Cardinality.ONE_TO_ONE
    assert cardinality_from_token(None) is Cardinality.MANY_TO_ONE   # unstated -> the common FK default
    with pytest.raises(ValueError, match="unknown cardinality"):
        cardinality_from_token("weird")


def test_invert_cardinality():
    assert invert_cardinality(Cardinality.MANY_TO_ONE) is Cardinality.ONE_TO_MANY
    assert invert_cardinality(Cardinality.ONE_TO_MANY) is Cardinality.MANY_TO_ONE
    assert invert_cardinality(Cardinality.ONE_TO_ONE) is Cardinality.ONE_TO_ONE


def test_global_relationship_lookup():
    rel = global_relationship_for("account", "customer")
    assert rel is not None and rel.relationship_id == "account_to_customer"
    assert global_relationship_for("customer", "account") is None      # not a declared global direction


def test_normalize_forward_orientation_binds():
    # account-grain -> customer-grain join, declared N:1, matches global account->customer (many_to_one)
    rel = global_relationship_for("account", "customer")
    out = normalize_realization(
        from_object_grain="account", to_object_grain="customer",
        declared=Cardinality.MANY_TO_ONE, global_rel=rel)
    assert out == NormalizedRealization(
        relationship_id="account_to_customer", declared_cardinality=Cardinality.MANY_TO_ONE,
        conflict=False, reversed_authoring=False)


def test_normalize_reverse_orientation_inverts_cardinality():
    # the SAME account->customer relationship, but the join was authored customer-grain -> account-grain
    # with 1:N; normalization detects the reverse orientation and inverts the cardinality to compare.
    rel = global_relationship_for("account", "customer")
    out = normalize_realization(
        from_object_grain="customer", to_object_grain="account",
        declared=Cardinality.ONE_TO_MANY, global_rel=rel)
    assert out.relationship_id == "account_to_customer" and out.reversed_authoring is True
    assert out.conflict is False


def test_normalize_cardinality_conflict_fails_closed():
    # account->customer global is many_to_one; a join declaring many_to_many contradicts it
    rel = global_relationship_for("account", "customer")
    out = normalize_realization(
        from_object_grain="account", to_object_grain="customer",
        declared=Cardinality.MANY_TO_MANY, global_rel=rel)
    assert out.conflict is True and out.relationship_id == "account_to_customer"


def test_normalize_no_global_relationship_is_local():
    out = normalize_realization(
        from_object_grain="account", to_object_grain="account",
        declared=Cardinality.ONE_TO_ONE, global_rel=None)
    assert out is None      # unmapped grain pair -> caller records a catalog_local_relationship + proposal


from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_realizations import (
    key_entity,
    object_grain,
    table_of,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph


def _accounts_customer_catalog(conn) -> None:
    # accounts: grain = account (account_id is_grain), plus a customer_id FK column; customer_master:
    # grain = customer. A join accounts.customer_id -> customer_master.customer_id (N:1).
    catalog = [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "customer_id", "integer",
                      joins_to="customer_master.customer_id", cardinality="N:1"), "customer_id"),
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("core", "customer_master", "segment", "text"), "categorical"),
    ]
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog}
    build_graph(conn, "core", rows, concepts=concepts)


def test_table_of_strips_column():
    assert table_of("public.accounts.customer_id") == "public.accounts"


def test_object_grain_is_the_grain_column_entity(db):
    _accounts_customer_catalog(db)
    # accounts' grain column is account_id -> entity account; customer_master's is customer_id -> customer
    assert object_grain(db, "core", "public.accounts") == "account"
    assert object_grain(db, "core", "public.customer_master") == "customer"


def test_key_entity_is_the_join_column_concept_entity(db):
    _accounts_customer_catalog(db)
    # the join key column accounts.customer_id has concept customer_id -> entity customer (NOT account)
    assert key_entity(db, "core", "public.accounts.customer_id") == "customer"


def test_object_grain_none_when_no_grain_column(db):
    from featuregen.overlay.upload.canonical import CanonicalRow as CR
    rows = [CR("x", "t", "c", "text")]
    build_graph(db, "x", rows, concepts={content_hash(rows[0]): "categorical"})
    assert object_grain(db, "x", "public.t") is None


from featuregen.overlay.upload.catalog_realizations import (
    REALIZATION_DERIVATION_VERSION,
    derive_catalog_realizations,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import RealizationAuthority


def test_derive_binds_the_accounts_customer_realization(db):
    _accounts_customer_catalog(db)
    result = derive_catalog_realizations(db, "core")
    # the account->customer hop is realized by the customer_id join key
    assert len(result.realizations) == 1
    r = result.realizations[0]
    assert r.relationship_id == "account_to_customer"
    assert (r.from_object_grain, r.to_object_grain) == ("account", "customer")   # object grains
    assert (r.from_key_entity, r.to_key_entity) == ("customer", "customer")      # join-KEY entity
    assert r.from_object_ref == "public.accounts" and r.to_object_ref == "public.customer_master"
    assert r.declared_cardinality is Cardinality.MANY_TO_ONE
    assert r.reversed_authoring is False
    assert r.authority is RealizationAuthority.DECLARED_JOIN
    assert result.conflicts == () and result.local_relationships == ()


def test_reverse_authored_realization_is_physically_consistent(db):
    # A join authored FROM a customer-grain table TO an account-grain table (physical 1:N: one customer,
    # many accounts). It binds to the account_to_customer global via the REVERSE lookup — the stored record
    # must stay in the physical orientation (grains customer->account, declared 1:N) with reversed_authoring
    # True, NOT re-oriented to the global's many_to_one.
    catalog = [
        (CanonicalRow("rev", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("rev", "customers", "acct_id", "integer",
                      joins_to="accounts.account_id", cardinality="1:N"), "account_id"),
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ]
    rows = [r for r, _ in catalog]
    build_graph(db, "rev", rows, concepts={content_hash(r): c for r, c in catalog})
    result = derive_catalog_realizations(db, "rev")
    assert len(result.realizations) == 1 and result.conflicts == () and result.local_relationships == ()
    r = result.realizations[0]
    assert r.relationship_id == "account_to_customer"
    assert (r.from_object_grain, r.to_object_grain) == ("customer", "account")   # PHYSICAL, not re-oriented
    assert r.declared_cardinality is Cardinality.ONE_TO_MANY                       # physical fanout, not inverted
    assert r.reversed_authoring is True                                            # reverse vs relationship_id


def test_cardinality_conflict_is_surfaced_not_bound(db):
    # same catalog but the join declares 1:1 (contradicts global account->customer many_to_one)
    catalog = [
        (CanonicalRow("c2", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("c2", "accounts", "customer_id", "integer",
                      joins_to="customer_master.customer_id", cardinality="1:1"), "customer_id"),
        (CanonicalRow("c2", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
    ]
    rows = [r for r, _ in catalog]
    build_graph(db, "c2", rows, concepts={content_hash(r): c for r, c in catalog})
    result = derive_catalog_realizations(db, "c2")
    assert result.realizations == ()                       # NOT bound as valid
    assert len(result.conflicts) == 1
    assert result.conflicts[0].relationship_id == "account_to_customer"


def test_unmapped_grain_pair_is_local_plus_proposal(db):
    # a join whose grain pair has NO global relationship -> catalog_local + a proposal
    catalog = [
        (CanonicalRow("c3", "widgets", "widget_id", "integer", is_grain=True), "product_id"),
        (CanonicalRow("c3", "widgets", "merchant_id", "integer",
                      joins_to="merchants.merchant_id", cardinality="N:1"), "merchant_id"),
        (CanonicalRow("c3", "merchants", "merchant_id", "integer", is_grain=True), "merchant_id"),
    ]
    rows = [r for r, _ in catalog]
    build_graph(db, "c3", rows, concepts={content_hash(r): c for r, c in catalog})
    result = derive_catalog_realizations(db, "c3")
    assert result.realizations == () and result.conflicts == ()
    assert len(result.local_relationships) == 1 and len(result.proposals) == 1
    assert result.proposals[0].proposed_from_entity == "product"      # widgets grain
    assert result.proposals[0].proposed_to_entity == "merchant"


def test_fingerprint_is_stable_and_composite(db):
    _accounts_customer_catalog(db)
    fp1 = derive_catalog_realizations(db, "core").fingerprint
    fp2 = derive_catalog_realizations(db, "core").fingerprint
    assert fp1 == fp2 and len(fp1) == 64                              # sha256 hex, deterministic
    assert REALIZATION_DERIVATION_VERSION == "1.0.0"
