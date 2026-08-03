"""THE neutral canonical hasher — RFC 8785 JCS bytes, sha256 hex (shared ledger §3, freeze 0F-4).

``jcs_sha256(payload)`` is the exact hasher that lived in
``featuregen.materialize.canonical.materialize_hash`` (extracted, not rewritten):
it canonicalizes a plain JSON-able mapping with the vendored RFC 8785 implementation
(``featuregen.formula._jcs.dumps``) and returns the sha256 hex digest of those canonical
UTF-8 bytes. ``materialize_hash`` now DELEGATES here byte-identically — existing
materialization payloads gain no new envelope, and existing ``field_evidence.canonical_hash``
values are untouched. Byte-identity is pinned by golden vectors captured from the
pre-extraction implementation (``tests/featuregen/test_canonical.py``).

``contract_hash_v1(contract_name, contract_version, payload)`` injects the required
name/version envelope before hashing::

    {"contract_name": <name>, "contract_version": <version>, "payload": <payload>}

All new semantic/profile/source/temporal/crosswalk/suggestion contracts use
``contract_hash_v1`` — never an inline ``json.dumps`` variant, never bare ``jcs_sha256``.
The envelope guarantees that a new contract version is never a byte alias of its
predecessor and that no contract hash collides with a bare payload hash. Every
(contract_name, contract_version) must be registered by its owner module with
``featuregen.contracts.contract_versions`` — hashing an unregistered version raises
``ContractVersionError`` loudly instead of minting an ungoverned identity.

**Identity fields only.** Callers put identity into the payload and nothing else — no
provenance, no timestamps, no run-time observations — otherwise re-deriving the same
governed content yields a different hash (freeze 0F-10's exclusion list).
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from featuregen.contracts.contract_versions import assert_contract_version
from featuregen.formula._jcs import dumps as _jcs_dumps

__all__ = ["contract_hash_v1", "jcs_sha256"]


def _plain(value: Any) -> Any:
    """Deep-convert Mappings to dicts and sequences to lists; everything else passes through.

    Scalars are untouched, so a plain-dict payload canonicalizes to the exact bytes it
    always did — this changes what is ACCEPTED, never what is hashed.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def jcs_sha256(payload: Mapping[str, Any]) -> str:
    """``sha256`` hex digest of ``payload``'s RFC 8785 canonical JSON bytes.

    Key order is irrelevant (JCS sorts object keys by their UTF-16 encoding), so two
    mappings that differ only in insertion order hash identically.

    Raises:
        TypeError: ``payload`` is not a mapping (a *programming* error at a call site,
            never a governed refusal).
        featuregen.formula._jcs.CanonicalizationError: a value inside ``payload`` is not
            JSON-canonicalizable (non-string key, NaN / infinity, an out-of-domain
            integer, or an unsupported type).
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"jcs_sha256 requires a mapping, got {type(payload).__name__}")
    # `_jcs.dumps` dispatches on `isinstance(obj, dict)` / `isinstance(obj, list)` at EVERY
    # level, so a non-dict Mapping (or a tuple) anywhere in the tree — not just at the top —
    # would fall through to its "unsupported type" branch. `_plain` deep-converts the whole
    # payload first; the vendored `_jcs` stays untouched.
    return hashlib.sha256(_jcs_dumps(_plain(dict(payload)))).hexdigest()


def contract_hash_v1(contract_name: str, contract_version: str, payload: Mapping[str, Any]) -> str:
    """``jcs_sha256`` over the frozen name/version envelope around ``payload``.

    Raises:
        featuregen.contracts.contract_versions.ContractVersionError: the
            (contract_name, contract_version) pair is not registered by an owner module —
            the loud failure the Task 0S brief requires for unregistered versions.
        TypeError / CanonicalizationError: as :func:`jcs_sha256`.
    """
    assert_contract_version(contract_name, contract_version)
    return jcs_sha256({
        "contract_name": contract_name,
        "contract_version": contract_version,
        "payload": payload,
    })
