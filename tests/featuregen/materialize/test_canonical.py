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


# ── Task 24: the hasher is PINNED, not merely self-consistent ────────────────────────────────────
# Every assertion above is relative (`f(x) == f(x)` / `f(x) != f(y)`), so a
# `json.dumps(sort_keys=True)` impostor would pass all of them while silently forking the identity
# of every sealed artifact. The absolute vectors below refuse that.


def test_pinned_rfc8785_vector():
    # A hasher that drifts from RFC 8785 (unicode escaping, whitespace, nested key order) silently
    # forks the identity of every sealed artifact. This digest was computed at 1f8fb766 and must
    # never change. (`json.dumps(sort_keys=True)` emits `"é"` and `", "` / `": "` separators —
    # both fail this vector.)
    #
    # One deviation from the task brief: its payload named n=1234567890123456789, which the
    # vendored JCS REFUSES by design (IntegerDomainError: |n| > 2**53 - 1 loses precision as an
    # IEEE 754 double) — pinned in test_out_of_domain_integer_is_refused_not_hashed. `n` here is
    # the largest integer the hasher accepts.
    payload = {"b": [1, 2.5, "é"], "a": {"nested": {"y": 1, "x": None}},
               "n": 9007199254740991}
    assert materialize_hash(payload) == (
        "bf02619b62d332af5ecf4ef79e5dbb4556c8bc169a9d4f4b0926a50b8e7ce76c")


def test_pinned_vector_es6_float_form_and_utf16_key_order():
    # The vector the first one cannot see: a compact ensure_ascii=False json.dumps impostor emits
    # byte-identical output for that payload. Here it diverges on three independent RFC 8785 axes —
    # ES6 integral floats (`5` not `5.0`), ES6 exponents (`1e-7` not `1e-07`), and UTF-16 code-unit
    # key sorting (the non-BMP emoji's surrogate 0xD83D sorts BEFORE the BMP 0xFF01; code-point
    # sorting puts it after). Canonical bytes:
    #   {"\xf0\x9f\x98\x80":true,"\xef\xbc\x81":[5,1e-7]}
    # Computed at 1f8fb766; must never change.
    payload = {"！": [5.0, 1e-7], "\U0001F600": True}
    assert materialize_hash(payload) == (
        "f48a5233c6f0b3a64995e1f665991c6982dfe9b2b5d7d7414cce084aa16fdc7e")


def test_out_of_domain_integer_is_refused_not_hashed():
    # `json.dumps` happily serializes 1234567890123456789; RFC 8785 numbers are IEEE 754 doubles,
    # so the hasher must refuse rather than mint an identity that a double-based reimplementation
    # would silently round to a DIFFERENT identity.
    from featuregen.formula._jcs import CanonicalizationError

    with pytest.raises(CanonicalizationError):
        materialize_hash({"n": 1234567890123456789})


def test_nested_key_order_is_irrelevant_too():
    # test_key_order_irrelevant only exercises the TOP level; JCS sorts at every depth.
    assert materialize_hash({"a": {"x": 1, "y": 2}}) == materialize_hash({"a": {"y": 2, "x": 1}})
