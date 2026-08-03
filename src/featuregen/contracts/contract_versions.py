"""The serialized-contract version registry (Task 0S, suggestion-discovery freeze 0F-4).

Every NEW serialized contract version — anything hashed by
``featuregen.canonical.contract_hash_v1`` or emitted as a versioned wire dataclass — is
registered here by its one owner module at import time. The registry enforces the shared
ledger §2 one-owner rule mechanically:

- ``contract_hash_v1`` refuses an UNREGISTERED (name, version): a forgotten registration
  fails loudly at the first hash, in tests and at startup, instead of yielding an empty
  suggestion stage;
- a second module claiming an already-owned (name, version) raises at ITS import — an
  owner/version mismatch can never silently fork a contract.

This registry is deliberately not the event-body :class:`~featuregen.events.registry.
EventSchemaRegistry` (DB event schemas), not the LLM output-schema store in
``overlay/upload/enrich_llm.py`` and not ``overlay/upload/taxonomy/versions.py`` (taxonomy
version pins): none of those govern JCS contract-hash identities. Versions here are opaque
strings (the 0F freeze uses ``"1"``/``"2"``); byte-alias protection across versions is the
envelope's job (see ``contract_hash_v1``) and is test-pinned, not re-checked here.
"""
from __future__ import annotations

__all__ = [
    "ContractVersionError",
    "assert_contract_version",
    "contract_owner",
    "register_contract_version",
    "registered_contract_versions",
]


class ContractVersionError(RuntimeError):
    """A serialized-contract version was unregistered, malformed, or claimed by two owners."""


_REGISTRY: dict[tuple[str, str], str] = {}


def _require_token(value: str, what: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or any(
            ch.isspace() for ch in value):
        raise ContractVersionError(f"{what} must be a non-empty string without whitespace, "
                                   f"got {value!r}")


def register_contract_version(contract_name: str, contract_version: str, *, owner: str) -> None:
    """Register (contract_name, contract_version) under exactly one owner module.

    Idempotent for the SAME owner (owner modules register at import, which may re-run in
    tests). A different owner for an already-registered version raises — the loud
    owner/version-mismatch failure the Task 0S brief requires — so the conflicting module
    fails at import rather than shipping a competing definition.
    """
    _require_token(contract_name, "contract_name")
    _require_token(contract_version, "contract_version")
    _require_token(owner, "owner")
    key = (contract_name, contract_version)
    existing = _REGISTRY.get(key)
    if existing is not None and existing != owner:
        raise ContractVersionError(
            f"contract {contract_name!r} version {contract_version!r} is already owned by "
            f"{existing!r}; refusing the competing registration from {owner!r}")
    _REGISTRY[key] = owner


def assert_contract_version(contract_name: str, contract_version: str) -> None:
    """Raise :class:`ContractVersionError` unless (contract_name, contract_version) is registered."""
    if (contract_name, contract_version) not in _REGISTRY:
        known = sorted(v for (n, v) in _REGISTRY if n == contract_name)
        raise ContractVersionError(
            f"serialized contract {contract_name!r} version {contract_version!r} is not "
            f"registered (registered versions of this contract: {known or 'none'}); register "
            f"it with featuregen.contracts.contract_versions.register_contract_version in the "
            f"owner module before hashing/serializing")


def contract_owner(contract_name: str, contract_version: str) -> str:
    """The registered owner module path. Raises :class:`ContractVersionError` if unregistered."""
    assert_contract_version(contract_name, contract_version)
    return _REGISTRY[(contract_name, contract_version)]


def registered_contract_versions() -> dict[tuple[str, str], str]:
    """A copy of the {(contract_name, contract_version): owner} registry for tests/startup checks."""
    return dict(_REGISTRY)
