"""THE hasher for the materialization package — RFC 8785 JCS bytes, sha256 hex.

``materialize_hash(payload)`` DELEGATES byte-identically to the neutral
``featuregen.canonical.jcs_sha256`` (Task 0S extracted the implementation there,
verbatim — shared ledger §3). Same canonicalization, same digests: existing
materialization payloads gain no envelope and every previously minted hash is
reproduced exactly (golden-pinned in ``tests/featuregen/test_canonical.py`` and
the absolute RFC 8785 vectors below in ``tests/featuregen/materialize/test_canonical.py``).

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

from collections.abc import Mapping
from typing import Any

from featuregen.canonical import jcs_sha256

__all__ = ["materialize_hash"]


def materialize_hash(payload: Mapping[str, Any]) -> str:
    """``sha256`` hex digest of ``payload``'s RFC 8785 canonical JSON bytes.

    Pure delegation to :func:`featuregen.canonical.jcs_sha256` — see its docstring
    for the accepted shapes and the ``TypeError`` / ``CanonicalizationError`` contract.
    """
    return jcs_sha256(payload)
