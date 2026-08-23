"""The run's immutable identity row, written when the creation chain completes (spec §6.1).

The hash payload is EXPLICIT and never self-referential: exactly the thirteen fields below, no
timestamps. A run whose creation path lacks any chain link gets NO row — it renders PRE_SPINE, and
no identity is ever fabricated over absent inputs."""
from __future__ import annotations

from featuregen.canonical import jcs_sha256
from featuregen.contracts.envelopes import IdentityEnvelope

WORKFLOW_DEFINITION_V1 = "V1"


def record_run_identity(conn, generation_run_id: str, actor: IdentityEnvelope) -> str | None:
    gi = conn.execute(
        "SELECT intent_id, confirmed_scope_id, generation_input_content_hash "
        "FROM contract_generation_input WHERE generation_run_id = %s",
        (generation_run_id,)).fetchone()
    # contract_generation_input keys on generation_run_id (1024), so `gi` is at most one row. The
    # considered revision has no such key, so read ALL of them and refuse an ambiguous answer
    # instead of silently picking one: 1021's UNIQUE (intent_id, generation_run_id) plus 1027's
    # lineage trigger (which pins the revision's intent to the run's own) make a second row
    # unreachable today, and this keeps the writer deterministic if either ever relaxes.
    ccrs = conn.execute(
        "SELECT considered_revision_id, considered_content_hash, metadata_snapshot_id, "
        "metadata_snapshot_content_hash FROM contract_considered_revision "
        "WHERE generation_run_id = %s", (generation_run_id,)).fetchall()
    if gi is None or len(ccrs) != 1:
        # honest absence: the chain is incomplete (or ambiguous), so there is no identity
        return None
    ccr = ccrs[0]
    if ccr[2] is None or ccr[3] is None:
        return None  # 1021 leaves the snapshot pin NULLABLE; a half-absent chain is not an identity
    intent_id, scope_id, input_hash = gi
    considered_id, considered_hash, snapshot_id, snapshot_hash = ccr
    payload = {
        "workflow_definition_version": WORKFLOW_DEFINITION_V1,
        "generation_run_id": generation_run_id,
        "intent_id": intent_id,
        "confirmed_scope_id": scope_id,
        "generation_input_content_hash": input_hash,
        "considered_revision_id": considered_id,
        "considered_content_hash": considered_hash,
        "metadata_snapshot_id": snapshot_id,
        "metadata_snapshot_content_hash": snapshot_hash,
        "owner_subject": actor.subject,
        "owner_tenant": actor.tenant,
        "root_generation_run_id": generation_run_id,   # foundation: every run is a root
        "parent_generation_run_id": None,
    }
    run_identity_hash = jcs_sha256(payload)
    conn.execute(
        "INSERT INTO feature_run_identity (generation_run_id, workflow_definition_version, "
        "intent_id, confirmed_scope_id, generation_input_content_hash, considered_revision_id, "
        "considered_content_hash, metadata_snapshot_id, metadata_snapshot_content_hash, "
        "owner_subject, owner_tenant, root_generation_run_id, parent_generation_run_id, "
        "run_identity_hash, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s) "
        "ON CONFLICT (generation_run_id) DO NOTHING",
        (generation_run_id, WORKFLOW_DEFINITION_V1, intent_id, scope_id, input_hash,
         considered_id, considered_hash, snapshot_id, snapshot_hash,
         actor.subject, actor.tenant, generation_run_id, run_identity_hash, actor.subject))
    row = conn.execute("SELECT run_identity_hash FROM feature_run_identity "
                       "WHERE generation_run_id = %s", (generation_run_id,)).fetchone()
    return row[0]
