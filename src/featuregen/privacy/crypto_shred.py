from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from psycopg.types.json import Json

from featuregen.contracts import IdentityEnvelope
from featuregen.privacy.classification import GOVERNANCE_RETAINED
from featuregen.privacy.kms import KeyManager
from featuregen.privacy.legal_hold import is_under_legal_hold

if TYPE_CHECKING:
    from featuregen.contracts import DbConn

# Resolves whether a blob's owning feature_version is currently active/governed (=> retain).
# The concrete blob->feature_version->feature_active_versions mapping + active/governed predicate
# are policy/runtime, owned by Phase 06 / SP-9/10/12; SP-0 only defines the hook (§9).
GovernanceActiveResolver = Callable[["DbConn", str], bool]


def _default_governance_active(conn: "DbConn", blob_id: str) -> bool:
    """Fail-closed default: treat a governance-retained body as belonging to an active/governed
    version (retain). Callers wire a resolver that consults `feature_active_versions` to allow
    erasure of governance-retained bodies whose owning version is no longer active/governed (§9)."""
    return True


@dataclass(frozen=True, slots=True)
class ErasureOutcome:
    blob_id: str
    outcome: str                                   # shredded | retained_governance | retained_legal_hold | not_found
    erasure_id: str


def _record(
    conn: "DbConn",
    *,
    blob_id: str,
    classification: Optional[str],
    kms_key_id: Optional[str],
    reason: str,
    requested_by: IdentityEnvelope,
    outcome: str,
) -> ErasureOutcome:
    erasure_id = "era_" + uuid.uuid4().hex
    conn.execute(
        "INSERT INTO erasure_audit "
        "(erasure_id, blob_id, classification, kms_key_id, reason, requested_by, outcome) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (erasure_id, blob_id, classification, kms_key_id, reason, Json(asdict(requested_by)), outcome),
    )
    return ErasureOutcome(blob_id=blob_id, outcome=outcome, erasure_id=erasure_id)


def crypto_shred(
    conn: "DbConn",
    blob_ids: Iterable[str],
    *,
    reason: str,
    requested_by: IdentityEnvelope,
    key_manager: KeyManager,
    governance_active: GovernanceActiveResolver = _default_governance_active,
) -> list[ErasureOutcome]:
    """Crypto-shred pii-erasable bodies (§9): destroy the per-body key + mark status='shredded'.
    A governance-retained body is auto-retained ONLY while its owning feature_version is
    active/governed (decided by `governance_active(conn, blob_id)`); once the owning version is no
    longer active/governed it becomes erasable and is shredded. Legal-held bodies are always exempt.
    Operates ONLY over blob_index — the security stream and attempt-memory are exempt."""
    outcomes: list[ErasureOutcome] = []
    for blob_id in blob_ids:
        row = conn.execute(
            "SELECT classification, kms_key_id FROM blob_index WHERE blob_id = %s", (blob_id,)
        ).fetchone()
        if row is None:
            outcomes.append(_record(conn, blob_id=blob_id, classification=None, kms_key_id=None,
                                    reason=reason, requested_by=requested_by, outcome="not_found"))
            continue
        classification, kms_key_id = row
        if classification == GOVERNANCE_RETAINED and governance_active(conn, blob_id):
            outcomes.append(_record(conn, blob_id=blob_id, classification=classification, kms_key_id=kms_key_id,
                                    reason=reason, requested_by=requested_by, outcome="retained_governance"))
            continue
        if is_under_legal_hold(conn, "blob", blob_id):
            outcomes.append(_record(conn, blob_id=blob_id, classification=classification, kms_key_id=kms_key_id,
                                    reason=reason, requested_by=requested_by, outcome="retained_legal_hold"))
            continue
        # pii-erasable, OR governance-retained whose owning version is no longer active/governed.
        if kms_key_id is not None:
            key_manager.destroy(kms_key_id)
        conn.execute("UPDATE blob_index SET status = 'shredded', swept_at = now() WHERE blob_id = %s", (blob_id,))
        outcomes.append(_record(conn, blob_id=blob_id, classification=classification, kms_key_id=kms_key_id,
                                reason=reason, requested_by=requested_by, outcome="shredded"))
    return outcomes


class BlobNotFoundError(Exception):
    """Raised when a referenced blob_id is absent from blob_index."""


def rotate_blob_key(conn: "DbConn", blob_id: str, *, key_manager: KeyManager) -> str:
    """Rotate a body's per-body KMS key WITHOUT rewriting any events (§9)."""
    row = conn.execute(
        "SELECT object_key, kms_key_id FROM blob_index WHERE blob_id = %s", (blob_id,)
    ).fetchone()
    if row is None:
        raise BlobNotFoundError(blob_id)
    object_key, old_key = row
    new_key = key_manager.rotate(old_key, object_key)
    conn.execute("UPDATE blob_index SET kms_key_id = %s WHERE blob_id = %s", (new_key, blob_id))
    return new_key
