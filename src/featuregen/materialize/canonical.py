"""THE hasher for the materialization package — RFC 8785 JCS bytes, sha256 hex.

``materialize_hash(payload)`` canonicalizes a plain JSON-able mapping with the
vendored RFC 8785 implementation (``featuregen.formula._jcs.dumps``, verified
interface) and returns the sha256 hex digest of those canonical UTF-8 bytes.

**One scheme, package-wide.** Every identity in Spec A — contract hashes, IR
hashes, schema hashes, project hashes, manifest hashes — is produced by this
function. A second canonicalization would fork identity: two components would
compute different digests for the same object and every equality gate between
them (``IR_HASH_MISMATCH``, ``SCHEMA_HASH_MISMATCH``, ``PROJECT_HASH_MISMATCH``)
would compare hashes that were never comparable. No second scheme may appear.

**Identity fields only.** Callers put identity into the payload and nothing
else — no provenance, no timestamps, no run-time observations — otherwise a
recompilation of the same governed inputs yields a different hash.

Note on the deliberate ``TypeError``: a non-mapping payload is a *programming*
error at a call site, not a governed refusal, so it is not one of the §14
codes. Governed refusals raise ``MaterializationRefused`` with a typed code.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from featuregen.formula._jcs import dumps as _jcs_dumps

__all__ = ["materialize_hash"]


def _plain(value: Any) -> Any:
    """Deep-convert Mappings to dicts and sequences to lists; everything else passes through.

    Scalars are untouched, so a plain-dict payload canonicalizes to the exact
    bytes it always did — this changes what is ACCEPTED, never what is hashed.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def materialize_hash(payload: Mapping[str, Any]) -> str:
    """``sha256`` hex digest of ``payload``'s RFC 8785 canonical JSON bytes.

    Key order is irrelevant (JCS sorts object keys by their UTF-16 encoding),
    so two mappings that differ only in insertion order hash identically.

    Raises:
        TypeError: ``payload`` is not a mapping.
        featuregen.formula._jcs.CanonicalizationError: a value inside
            ``payload`` is not JSON-canonicalizable (non-string key, NaN /
            infinity, an out-of-domain integer, or an unsupported type).
    """
    if not isinstance(payload, Mapping):
        raise TypeError(
            f"materialize_hash requires a mapping, got {type(payload).__name__}"
        )
    # `_jcs.dumps` dispatches on `isinstance(obj, dict)` / `isinstance(obj, list)`
    # at EVERY level, so a non-dict Mapping (or a tuple) anywhere in the tree —
    # not just at the top — would fall through to its "unsupported type" branch.
    # `_plain` deep-converts the whole payload first; the vendored `_jcs` stays
    # untouched.
    return hashlib.sha256(_jcs_dumps(_plain(dict(payload)))).hexdigest()
