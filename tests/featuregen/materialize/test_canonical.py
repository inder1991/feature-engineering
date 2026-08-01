"""Task 1 — ``materialize_hash`` is THE one hasher for the materialization package."""
from __future__ import annotations

import pytest

from featuregen.materialize.canonical import materialize_hash


def test_key_order_irrelevant():
    assert materialize_hash({"a": 1, "b": 2}) == materialize_hash({"b": 2, "a": 1})


def test_sha256_hex():
    h = materialize_hash({"a": 1})
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_values_distinguished():
    assert materialize_hash({"a": 1}) != materialize_hash({"a": 2})


def test_rejects_non_mapping():
    with pytest.raises(TypeError):
        materialize_hash([1])  # type: ignore[arg-type]


def test_nested_mapping_views_hash_like_plain_dicts():
    """The signature says ``Mapping`` — a nested read-only view is the same identity.

    ``filter_tree`` payloads arrive as ``Mapping`` and a caller handing out
    ``MappingProxyType`` views (the package's own read-only convention) must not
    fork the hash or crash inside it.
    """
    from types import MappingProxyType
    plain = {"filter": {"op": "and", "children": [{"left": "x"}]}}
    proxied = {"filter": MappingProxyType({"op": "and",
               "children": [MappingProxyType({"left": "x"})]})}
    assert materialize_hash(plain) == materialize_hash(proxied)
