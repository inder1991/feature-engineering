from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class KeyManager(Protocol):
    """Per-body KMS abstraction (§9). Destroying a key crypto-shreds its body; rotate re-wraps."""

    def destroy(self, kms_key_id: str) -> None: ...

    def rotate(self, old_kms_key_id: str, object_key: str) -> str:
        """Re-encrypt the body under a fresh key; return the new kms_key_id."""
