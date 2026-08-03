"""Task 0S — ``featuregen.canonical`` is THE neutral JCS hasher (shared ledger §3 / 0F-4).

``jcs_sha256`` is the exact hasher extracted from ``materialize.canonical.materialize_hash``;
``materialize_hash`` delegates to it BYTE-IDENTICALLY. Every golden below was captured from the
PRE-extraction implementation at worktree HEAD f0fe82a0 — a digest drift here means the
extraction rewrote existing hashes, which Task 0S forbids.
"""
from __future__ import annotations

import pytest

from featuregen.canonical import contract_hash_v1, jcs_sha256
from featuregen.contracts.contract_versions import (
    ContractVersionError,
    register_contract_version,
)
from featuregen.materialize.canonical import materialize_hash

# ── Goldens captured from the pre-change materialize_hash (commands in the Task 0S report) ──────
_PRE_EXTRACTION_GOLDENS = {
    "empty": (
        {},
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    ),
    "simple": (
        {"a": 1},
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862",
    ),
    # The two RFC-8785 vectors already pinned in tests/featuregen/materialize/test_canonical.py.
    "rfc8785_vector": (
        {"b": [1, 2.5, "é"], "a": {"nested": {"y": 1, "x": None}}, "n": 9007199254740991},
        "bf02619b62d332af5ecf4ef79e5dbb4556c8bc169a9d4f4b0926a50b8e7ce76c",
    ),
    "es6_utf16_vector": (
        {"！": [5.0, 1e-7], "\U0001F600": True},
        "f48a5233c6f0b3a64995e1f665991c6982dfe9b2b5d7d7414cce084aa16fdc7e",
    ),
}


def test_jcs_sha256_matches_pre_extraction_goldens():
    for name, (payload, digest) in _PRE_EXTRACTION_GOLDENS.items():
        assert jcs_sha256(payload) == digest, name


def test_jcs_sha256_accepts_tuples_and_mapping_views_like_the_old_hasher():
    from types import MappingProxyType

    payload = {"t": (1, 2), "m": MappingProxyType({"k": "v"}), "s": "x"}
    # Captured from the pre-change implementation.
    assert jcs_sha256(payload) == (
        "c901be73875a5d12916befa69903eb1eefad71338e143818f7b6cb68d6f9ffe1")


def test_materialize_hash_delegates_byte_identically():
    """Old callers produce identical output before and after the extraction."""
    for name, (payload, digest) in _PRE_EXTRACTION_GOLDENS.items():
        assert materialize_hash(payload) == digest, name
        assert materialize_hash(payload) == jcs_sha256(payload), name


def test_jcs_sha256_rejects_non_mapping():
    with pytest.raises(TypeError):
        jcs_sha256([1])  # type: ignore[arg-type]


def test_jcs_sha256_refuses_out_of_domain_integer():
    from featuregen.formula._jcs import CanonicalizationError

    with pytest.raises(CanonicalizationError):
        jcs_sha256({"n": 1234567890123456789})


# ── contract_hash_v1: the name/version envelope + registry enforcement ──────────────────────────

_PROBE = "task0s-envelope-probe"
_PROBE_PAYLOAD = {"pin": "envelope", "n": 7}


def _register_probe() -> None:
    register_contract_version(_PROBE, "1", owner="tests.featuregen.test_canonical")
    register_contract_version(_PROBE, "2", owner="tests.featuregen.test_canonical")


def test_contract_hash_v1_unregistered_version_fails_loudly():
    with pytest.raises(ContractVersionError):
        contract_hash_v1("task0s-never-registered", "1", {"a": 1})


def test_contract_hash_v1_pinned_envelope_vector():
    """The envelope is {"contract_name", "contract_version", "payload"} hashed by the SAME
    JCS hasher. Digest captured from the pre-change hasher over that exact envelope: the
    envelope scheme itself is pinned, not merely self-consistent."""
    _register_probe()
    assert contract_hash_v1(_PROBE, "1", _PROBE_PAYLOAD) == (
        "c43b147cc2813a391db3cf2197f56b0aec2eb02c67c578402adec5922380a677")


def test_new_contract_version_is_not_a_byte_alias_of_its_predecessor():
    _register_probe()
    v1 = contract_hash_v1(_PROBE, "1", _PROBE_PAYLOAD)
    v2 = contract_hash_v1(_PROBE, "2", _PROBE_PAYLOAD)
    assert v1 != v2
    # v2 is pinned too: an implementation that ignores the version cannot fake this.
    assert v2 == "2d8e32cf75f4a95157ebdc01c17a7ec42a105bbaf7be6fc9c6bd72ddaf5c2c62"


def test_contract_hash_v1_differs_from_raw_jcs_sha256():
    """A contract hash never collides with a bare payload hash (no envelope-less alias)."""
    _register_probe()
    assert contract_hash_v1(_PROBE, "1", _PROBE_PAYLOAD) != jcs_sha256(_PROBE_PAYLOAD)
    assert jcs_sha256(_PROBE_PAYLOAD) == (
        "845a2622de0e2db54f7e1c4a49e03b4d93008d2ec5db1015ca4db17295b475c7")


def test_contract_hash_v1_distinguishes_contract_names():
    _register_probe()
    register_contract_version("task0s-other-probe", "1", owner="tests.featuregen.test_canonical")
    assert contract_hash_v1(_PROBE, "1", _PROBE_PAYLOAD) != contract_hash_v1(
        "task0s-other-probe", "1", _PROBE_PAYLOAD)
