"""Task 0S — the serialized-contract version registry (brief bullet 5).

Every new serialized contract version is registered with one owner module; an unregistered
version fails LOUDLY (``ContractVersionError``) instead of yielding an empty stage, and a
second module claiming an already-owned (name, version) fails at import time — the
owner/version-mismatch guard of brief bullet 6.
"""
from __future__ import annotations

import pytest

from featuregen.contracts.contract_versions import (
    ContractVersionError,
    assert_contract_version,
    contract_owner,
    register_contract_version,
    registered_contract_versions,
)

_OWNER = "tests.featuregen.contracts.test_contract_versions"


def test_registered_version_passes_and_reports_owner():
    register_contract_version("task0s-registry-probe", "1", owner=_OWNER)
    assert_contract_version("task0s-registry-probe", "1")
    assert contract_owner("task0s-registry-probe", "1") == _OWNER
    assert registered_contract_versions()[("task0s-registry-probe", "1")] == _OWNER


def test_unregistered_version_fails_loudly_with_specifics():
    with pytest.raises(ContractVersionError) as exc:
        assert_contract_version("task0s-registry-missing", "9")
    assert "task0s-registry-missing" in str(exc.value)
    assert "9" in str(exc.value)


def test_unregistered_owner_lookup_fails_loudly():
    with pytest.raises(ContractVersionError):
        contract_owner("task0s-registry-missing", "9")


def test_reregistration_by_the_same_owner_is_idempotent():
    register_contract_version("task0s-registry-idem", "1", owner=_OWNER)
    register_contract_version("task0s-registry-idem", "1", owner=_OWNER)
    assert_contract_version("task0s-registry-idem", "1")


def test_second_owner_for_the_same_version_fails_at_registration():
    register_contract_version("task0s-registry-owned", "1", owner=_OWNER)
    with pytest.raises(ContractVersionError) as exc:
        register_contract_version("task0s-registry-owned", "1", owner="somewhere.else")
    assert _OWNER in str(exc.value)  # the loud message names the actual owner


@pytest.mark.parametrize(
    "name,version,owner",
    [
        ("", "1", _OWNER),
        ("task0s-shape", "", _OWNER),
        ("task0s shape", "1", _OWNER),  # whitespace in the name
        ("task0s-shape", "1 ", _OWNER),  # whitespace in the version
        ("task0s-shape", "1", ""),
    ],
)
def test_malformed_registrations_are_rejected(name, version, owner):
    with pytest.raises(ContractVersionError):
        register_contract_version(name, version, owner=owner)


def test_registered_contract_versions_returns_a_copy():
    register_contract_version("task0s-registry-copy", "1", owner=_OWNER)
    snapshot = registered_contract_versions()
    snapshot[("task0s-registry-copy", "1")] = "tampered"
    assert contract_owner("task0s-registry-copy", "1") == _OWNER
