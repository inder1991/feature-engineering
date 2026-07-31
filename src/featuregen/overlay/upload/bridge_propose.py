"""Phase-3B.2B — propose a governed entity bridge onto the overlay_fact spine.

`propose_bridge` sends raw structural evidence through ``propose_fact`` so duplicate denials mint
nothing, then records every new assessment revision even when the generic lifecycle proposal already
exists. The legacy ledger remains a refreshed compatibility projection."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import cast

from featuregen.contracts.envelopes import Command
from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.tasks import open_task
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.identity import (
    CatalogObjectRef,
    EntityBridgeRef,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_candidates import (
    BRIDGE_DERIVATION_VERSION,
    BridgeCandidateV1,
    bridge_candidate_write_error,
    derive_bridge_candidate_for_refs,
)
from featuregen.overlay.upload.bridge_store import (
    demote_realizations_for_bridge,
    load_candidate_pointer_versions,
    load_current_candidate_assessments,
    record_candidate_assessment,
)
from featuregen.overlay.upload.object_ref import parse_ref

logger = logging.getLogger(__name__)


class BridgeProposalError(ValueError):
    """A caller supplied a candidate that no longer matches the governed catalog."""


def _ensure_current_review_task(conn, ref: EntityBridgeRef, key: str, state, actor) -> None:
    """Restore optional review when a withdrawn candidate becomes current again.

    A first proposal already has an open task, making the common path an indexed no-op. Withdrawal
    supersedes the obsolete task; a later re-derivation reopens accountability without creating a
    second proposal event or making human review an availability/execution gate.
    """
    if state.status not in {"DRAFT", "STALE", "REVERIFY"}:
        return
    if conn.execute(
        "SELECT 1 FROM human_tasks WHERE fact_key=%s AND status='open' LIMIT 1",
        (key,),
    ).fetchone() is not None:
        return
    authority = resolve_authority(conn, current_catalog_adapter(), ref, "entity_bridge")
    if state.status in {"STALE", "REVERIFY"}:
        if state.confirmed_event_id is None:
            return
        from featuregen.overlay.reverify_tasks import open_reverify_task

        open_reverify_task(
            conn,
            fact_key=key,
            fact_type="entity_bridge",
            target_confirmed_event_id=state.confirmed_event_id,
            authority=authority,
            actor=actor,
        )
        return
    if state.draft_event_id is None:
        return
    for eligible in authority.task_assignees:
        assignees = cast("dict[str, str]", dict(eligible))
        open_task(
            conn,
            GateTaskSpec(
                gate=authority.gate,
                required_inputs=("proposed_value",),
                eligible_assignees=assignees,
                allowed_responses=("confirm", "reject"),
                fact_key=key,
                draft_event_id=state.draft_event_id,
                target_event_id=state.draft_event_id,
                evidence_ref=state.evidence_ref,
            ),
            actor,
        )


def _object_ref_str(ref) -> str:
    return f"{ref.schema}.{ref.table}.{ref.column}"


def propose_bridge(conn, candidate: BridgeCandidateV1, *, actor, now=None,
                   ingestion_run_id: str | None = None,
                   expected_candidate_pointer_version: int | None = None) -> str:
    """Propose one bridge candidate as an entity_bridge fact. Returns the fact_key. Deterministic +
    append-only. Human confirmation is optional review/ranking and does not gate availability.

    ``ingestion_run_id`` is the durable run whose upload triggered this derivation, stamped onto the
    evidence row's existing ``producer_item_ref``. It is what keeps the audit trail whole once the
    PROPOSER is a service actor rather than the uploading human: ``ingestion_run.actor_subject``
    resolves the run back to the person, so the trail answers WHICH upload as well as WHO — strictly
    more than a human ``proposed_by`` ever said. ``None`` (a direct caller with no run) leaves the
    column NULL, exactly as before."""
    ts = now if now is not None else datetime.now(UTC)
    write_error = bridge_candidate_write_error(conn, candidate)
    if write_error is not None:
        raise BridgeProposalError(write_error)
    ref = EntityBridgeRef(entity_id=candidate.entity_id, left_ref=candidate.left_ref,
                          right_ref=candidate.right_ref)
    key = fact_key(ref, "entity_bridge")
    # `type_basis` travels with the evidence so the CONFIRMER can see how the type match was
    # established: `declared` means a glossary file's own answer (someone's spreadsheet entry), not a
    # structural read of the physical schema. It is advisory context for the human gate, never an
    # authority input — the fact still requires the same confirmation either way.
    evidence = {"entity_id": candidate.entity_id, "candidate_id": candidate.candidate_id,
                "data_type_family": candidate.data_type_family, "left_is_grain": candidate.left_is_grain,
                "right_is_grain": candidate.right_is_grain, "type_basis": candidate.type_basis,
                "left_concept_authority": candidate.left_concept_authority,
                "right_concept_authority": candidate.right_concept_authority,
                "derivation_version": BRIDGE_DERIVATION_VERSION}
    raw_evidence = {
        "table_snapshot_at": ts,
        "row_count": 0,
        "sample_size": 0,
        "profile_version": BRIDGE_DERIVATION_VERSION,
        "thresholds": {},
        "metric_values": evidence,
        "producer": "structural_connector",
        "strength": "proposed",
        "lifecycle": "active",
        "producer_configuration_hash": canonical_hash({
            "derivation_version": BRIDGE_DERIVATION_VERSION}),
        "producer_item_ref": ingestion_run_id,
        "evidence_spans": (
            "entity_id",
            "data_type_family",
            "type_basis",
            "concept_authority",
            "grain_membership",
        ),
    }
    value = {"entity_id": candidate.entity_id, "left_ref": asdict(candidate.left_ref),
             "right_ref": asdict(candidate.right_ref)}
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "proposed_value": value,
         "evidence": raw_evidence},
        actor, proposal_fingerprint(value)))
    if not res.accepted:
        # Expected benign denial: the overlay spine dedups on the deterministic fact_key/fingerprint,
        # so a re-derived / re-uploaded candidate whose fact already exists (pending or sticky-rejected)
        # is denied. This is a no-op, NOT an error — never raise out of a batch derivation cycle
        # (mirrors passc/propose.py::_propose_one). The ledger row was stamped on the first propose.
        expected_denial = any(
            marker in (res.denied_reason or "")
            for marker in (
                "duplicate of a pending proposal",
                "a non-terminal fact already exists",
                "fingerprint previously rejected",
            )
        )
        if not expected_denial:
            raise BridgeProposalError(res.denied_reason or "bridge proposal denied")
        logger.debug("bridge propose no-op (already governed): %s (%s)", key, res.denied_reason)

    assessment = candidate.assessment
    if assessment is None:
        raise BridgeProposalError("candidate has no typed identifier-link assessment")
    record_candidate_assessment(
        conn,
        replace(assessment, bridge_fact_key=key),
        expected_pointer_version=expected_candidate_pointer_version,
    )

    state = fold_overlay_state(load_fact(conn, key))
    _ensure_current_review_task(conn, ref, key, state, actor)
    proposed_event_id = state.draft_event_id
    conn.execute(
        "INSERT INTO entity_bridge_candidate_evidence ("
        "  entity_id, left_catalog_source, left_object_ref, right_catalog_source, right_object_ref,"
        "  candidate_id, fact_key, proposed_event_id, data_type_family, evidence_json, derivation_version,"
        "  updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (entity_id, left_catalog_source, left_object_ref, right_catalog_source,"
        "  right_object_ref) DO UPDATE SET candidate_id = EXCLUDED.candidate_id,"
        "  fact_key = EXCLUDED.fact_key, proposed_event_id = EXCLUDED.proposed_event_id,"
        "  data_type_family = EXCLUDED.data_type_family,"
        "  evidence_json = EXCLUDED.evidence_json,"
        "  derivation_version = EXCLUDED.derivation_version,"
        "  updated_at = EXCLUDED.updated_at",
        (candidate.entity_id, candidate.left_ref.catalog_source, _object_ref_str(candidate.left_ref),
         candidate.right_ref.catalog_source, _object_ref_str(candidate.right_ref), candidate.candidate_id,
         key, proposed_event_id, candidate.data_type_family,
         json.dumps(evidence), BRIDGE_DERIVATION_VERSION, ts))
    return key


def _column_ref(endpoint) -> CatalogObjectRef:
    source, schema, table, column = parse_ref(
        endpoint.members[0].logical_column_ref)
    assert column is not None
    return CatalogObjectRef(
        catalog_source=source,
        object_kind="column",
        schema=schema,
        table=table,
        column=column,
    )


def reassess_bridge_candidates_for_table(
    conn,
    *,
    catalog_source: str,
    table: str,
    now=None,
) -> tuple[int, int]:
    """Refresh existing candidates that touch one newly projected table.

    Returns ``(refreshed, withdrawn_realizations)``. A pair that current evidence can no longer
    assess keeps its immutable history, but all executable realizations backed by it are withdrawn.
    The generic overlay lifecycle remains the authority for whether the semantic link itself is
    available.
    """
    from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR

    source = catalog_source.strip().lower()
    target_table = table.strip().lower()
    pointer_versions = load_candidate_pointer_versions(conn)
    assessments = load_current_candidate_assessments(conn)
    refreshed = 0
    withdrawn = 0
    for prior in assessments:
        endpoint_tables = {
            parse_ref(endpoint.logical_table_ref)[0:3]
            for endpoint in (prior.left_endpoint, prior.right_endpoint)
        }
        if not any(
            endpoint_source == source and endpoint_table == target_table
            for endpoint_source, _schema, endpoint_table in endpoint_tables
        ):
            continue
        candidate = derive_bridge_candidate_for_refs(
            conn,
            _column_ref(prior.left_endpoint),
            _column_ref(prior.right_endpoint),
        )
        if candidate is None:
            if prior.bridge_fact_key:
                withdrawn += demote_realizations_for_bridge(
                    conn, prior.bridge_fact_key, lifecycle="stale")
            continue
        propose_bridge(
            conn,
            candidate,
            actor=_ENRICH_ACTOR,
            now=now,
            expected_candidate_pointer_version=pointer_versions.get(
                prior.candidate_id, 0),
        )
        refreshed += 1
    return refreshed, withdrawn
