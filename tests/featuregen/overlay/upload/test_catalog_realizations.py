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
