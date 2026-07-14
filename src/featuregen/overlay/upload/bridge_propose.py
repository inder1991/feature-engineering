"""Phase-3B.2B — propose a governed entity bridge onto the overlay_fact spine.

`propose_bridge` mirrors passc/propose.py::_propose_one: pre-mint an immutable evidence record, then
dispatch the generic `propose_fact` command with fact_type='entity_bridge'. The bridge lifecycle is
thereafter the standard overlay_fact stream (DRAFT -> ... -> VERIFIED). Also stamps the durable candidate
ledger with the resolved fact_key + proposed event id."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

from featuregen.contracts.envelopes import Command
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer, write_evidence
from featuregen.overlay.identity import EntityBridgeRef, fact_key, proposal_fingerprint
from featuregen.overlay.upload.bridge_candidates import BRIDGE_DERIVATION_VERSION, BridgeCandidateV1


def _object_ref_str(ref) -> str:
    return f"{ref.schema}.{ref.table}.{ref.column}"


def propose_bridge(conn, candidate: BridgeCandidateV1, *, actor, now=None) -> str:
    """Propose one bridge candidate as an entity_bridge fact. Returns the fact_key. Deterministic +
    append-only. The four-eyes gate holds because a human (not this service actor) later confirms."""
    ts = now if now is not None else datetime.now(UTC)
    ref = EntityBridgeRef(entity_id=candidate.entity_id, left_ref=candidate.left_ref,
                          right_ref=candidate.right_ref)
    key = fact_key(ref, "entity_bridge")
    evidence = {"entity_id": candidate.entity_id, "candidate_id": candidate.candidate_id,
                "data_type_family": candidate.data_type_family, "left_is_grain": candidate.left_is_grain,
                "right_is_grain": candidate.right_is_grain, "derivation_version": BRIDGE_DERIVATION_VERSION}
    evidence_ref = write_evidence(
        conn, fact_key=key, table_snapshot_at=ts, row_count=0, sample_size=0,
        profile_version=BRIDGE_DERIVATION_VERSION, thresholds_used={}, metric_values=evidence,
        created_by=identity_to_jsonb(actor),
        producer=EvidenceProducer.STRUCTURAL_CONNECTOR, strength=AssertionStrength.PROPOSED)
    value = {"entity_id": candidate.entity_id, "left_ref": asdict(candidate.left_ref),
             "right_ref": asdict(candidate.right_ref)}
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "proposed_value": value,
         "evidence_ref": evidence_ref},
        actor, proposal_fingerprint(value)))
    if not res.accepted:
        raise RuntimeError(f"bridge proposal denied: {res.denied_reason}")
    proposed_event_id = res.produced_event_ids[0] if res.produced_event_ids else None
    conn.execute(
        "INSERT INTO entity_bridge_candidate_evidence ("
        "  entity_id, left_catalog_source, left_object_ref, right_catalog_source, right_object_ref,"
        "  candidate_id, fact_key, proposed_event_id, data_type_family, evidence_json, derivation_version,"
        "  updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (entity_id, left_catalog_source, left_object_ref, right_catalog_source,"
        "  right_object_ref) DO UPDATE SET fact_key = EXCLUDED.fact_key,"
        "  proposed_event_id = EXCLUDED.proposed_event_id, updated_at = EXCLUDED.updated_at",
        (candidate.entity_id, candidate.left_ref.catalog_source, _object_ref_str(candidate.left_ref),
         candidate.right_ref.catalog_source, _object_ref_str(candidate.right_ref), candidate.candidate_id,
         key, proposed_event_id, candidate.data_type_family,
         json.dumps(evidence), BRIDGE_DERIVATION_VERSION, ts))
    return key
