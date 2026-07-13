"""Pass C — the PROPOSE wiring (Phase 3A Task 7).

`propose_join_candidates` turns each STRONG, grain-inferred candidate into a governed
`approved_join` proposal through the UNCHANGED `propose_fact` gate (service actor -> DRAFT ->
the dual human confirmation; never VERIFIED here). Two invariants a two-round review baked in:

* GRAIN-GATE — only a candidate with `cardinality_status == INFERRED_FROM_CONFIRMED_GRAIN`
  AND a non-None `proposed_cardinality` is ever proposed. The approved_join value schema
  requires `1:1|1:N|N:1`, so an `ApprovedJoinRef(cardinality=None)` would schema-deny; it must
  never be built. Weak / cardinality-less / both-grain candidates are ledger diagnostics
  (Tasks 9/10), not proposals. Belt-and-suspenders: a strong+grain-inferred candidate WITHOUT
  a cardinality (a scorer-contract violation) is skipped LOUD (counter + warning).

* REVIEWER EVIDENCE RIDES `evidence_ref`, NOT the payload — `propose_fact` persists a FIXED
  `proposed_value` and the join schema is `additionalProperties:false`, so candidate evidence
  cannot ride the value or an extra Command arg. We PRE-MINT the immutable evidence row
  (`write_evidence`, producer=STRUCTURAL_CONNECTOR / strength=PROPOSED, `metric_values` =
  asdict(JoinCandidateEvidenceV1): score / reason codes / explanation / signals) and pass its
  id as the Command's `evidence_ref`; `propose_fact` stamps it onto the DRAFT payload + gate
  task, and `get_task_proposal` round-trips it to the reviewer via `read_evidence`.

Dedupe/conflict come from `decide_action` (Task 6): SKIP_ACTIVE dedupes a re-ingest, CONFLICT
(a DIFFERENT fact_key active on the SAME unordered column pair) is counted + logged with NO
second governed proposal, and PROPOSE/REPROPOSE dispatch — then stamp `fact_key` /
`proposed_event_id` back onto the `pass_c_candidate_evidence` ledger row (a no-op UPDATE until
Task 10 writes the row). Dispatch-level guards stay authoritative: a `propose_fact` deny (e.g.
a sticky-rejected fingerprint on REPROPOSE) is counted, never raised.

Fail-soft + adapter-gated (mirrors `_propose_governed_joins`): no adapter -> skip-loud and
return; each candidate runs inside its own EXPLICIT savepoint (`SAVEPOINT`/`ROLLBACK TO` SQL,
NOT `conn.transaction()` — at the outermost level that block COMMITS, which would break the
caller's/test-harness's rollback semantics), so an error rolls back ONLY that candidate's
writes (no orphan pre-minted evidence row — `propose_fact` relies on tx rollback for the same
invariant) and the loop continues — nothing ever raises out.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime

from featuregen.contracts.envelopes import Command
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer, write_evidence
from featuregen.overlay.identity import ApprovedJoinRef, fact_key, proposal_fingerprint
from featuregen.overlay.upload.passc.lifecycle import (
    Action,
    build_join_ref,
    decide_action,
    unordered_pair,
)
from featuregen.overlay.upload.passc.types import (
    ALGORITHM_VERSION,
    CardinalityInferenceStatus,
    JoinCandidateEvidenceV1,
)
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

_INFERRED = CardinalityInferenceStatus.INFERRED_FROM_CONFIRMED_GRAIN


def _join_value(ref: ApprovedJoinRef) -> dict:
    """The FIXED approved_join payload (must agree with `ref` — `join_write_error` enforces it).
    Candidate evidence NEVER rides here: the schema is `additionalProperties:false`."""
    return {
        "from_ref": asdict(ref.from_ref),
        "to_ref": asdict(ref.to_ref),
        "column_pairs": [{"from_col": p.from_col, "to_col": p.to_col} for p in ref.column_pairs],
        "cardinality": ref.cardinality,
    }


def propose_join_candidates(
    conn, source: str, evidences: Iterable[JoinCandidateEvidenceV1], *, actor,
) -> None:
    """Propose every STRONG, grain-inferred candidate as a governed approved_join DRAFT.

    `actor` is the service proposer (`_ENRICH_ACTOR`) — four-eyes holds against the two human
    confirmers. Advisory/fail-soft: this NEVER raises out of the loop."""
    try:
        current_catalog_adapter()
    except RuntimeError:
        counters.incr("overlay.passc.propose.skipped_no_adapter")
        logger.warning("Pass C propose: no catalog adapter registered — skipping %s "
                       "join-candidate proposals for %r", "all", source)
        return
    for ev in evidences:
        # THE GRAIN-GATE: only a strong, grain-inferred candidate is proposable. Everything else
        # (weak, both-grain, neither-grain) is a ledger diagnostic — never a governed proposal.
        if ev.bucket != "strong" or ev.cardinality_status is not _INFERRED:
            continue
        if ev.proposed_cardinality is None:
            # Scorer rule 1 forces every non-inferred candidate weak, so this shape is a
            # contract violation — skip LOUD; never build ApprovedJoinRef(cardinality=None).
            counters.incr("overlay.passc.propose.skipped_no_cardinality")
            logger.warning("Pass C candidate %s is strong+grain-inferred but carries no "
                           "cardinality — skipping (schema would deny a None cardinality)",
                           ev.candidate_id)
            continue
        try:
            # A savepoint per candidate: a failure rolls back ONLY this candidate's writes
            # (including its pre-minted evidence row) and cannot poison the caller's tx.
            conn.execute("SAVEPOINT passc_propose")
            _propose_one(conn, source, ev, actor=actor)
            conn.execute("RELEASE SAVEPOINT passc_propose")
        except Exception:  # noqa: BLE001 — advisory: a propose failure never fails the ingest
            counters.incr("overlay.passc.propose.error")
            logger.warning("Pass C propose raised for candidate %s (%s -> %s)",
                           ev.candidate_id, ev.from_ref, ev.to_ref, exc_info=True)
            try:
                conn.execute("ROLLBACK TO SAVEPOINT passc_propose")
            except Exception:  # noqa: BLE001 — the tx/connection itself is unusable
                logger.warning("Pass C propose: savepoint rollback failed — abandoning the "
                               "candidate loop (connection unusable)", exc_info=True)
                return


def _propose_one(conn, source: str, ev: JoinCandidateEvidenceV1, *, actor) -> None:
    """Adjudicate + dispatch ONE gated candidate; stamp the ledger row on an accepted propose."""
    ref = build_join_ref(ev, source)
    action = decide_action(conn, ref, ev)
    if action is Action.SKIP_ACTIVE:
        counters.incr("overlay.passc.propose.skipped_active")
        return
    if action is Action.CONFLICT:
        counters.incr("overlay.passc.propose.conflict")
        logger.warning("Pass C candidate %s conflicts with an ACTIVE approved_join on the same "
                       "column pair (%s <-> %s) — not proposing", ev.candidate_id,
                       ev.from_ref, ev.to_ref)
        return
    # PROPOSE / REPROPOSE: pre-mint the reviewer evidence, then dispatch with its evidence_ref.
    key = fact_key(ref, "approved_join")
    evidence_ref = write_evidence(
        conn,
        fact_key=key,
        table_snapshot_at=datetime.now(UTC),   # mint time — Pass C has no table snapshot
        row_count=0,
        sample_size=0,                          # metadata-only: no rows/samples were profiled
        profile_version=ALGORITHM_VERSION,
        thresholds_used={},
        metric_values=asdict(ev),               # score / reason_codes / explanation / signals …
        created_by=identity_to_jsonb(actor),
        producer=EvidenceProducer.STRUCTURAL_CONNECTOR,
        strength=AssertionStrength.PROPOSED,
    )
    value = _join_value(ref)
    result = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "proposed_value": value,
         "evidence_ref": evidence_ref},
        actor, proposal_fingerprint(value)))
    if not result.accepted:
        # Expected on races / a sticky-rejected fingerprint — advisory, counted, never raised.
        counters.incr("overlay.passc.propose.denied")
        logger.info("Pass C proposal for candidate %s not accepted: %s",
                    ev.candidate_id, result.denied_reason)
        return
    counters.incr("overlay.passc.propose.proposed")
    lo, hi = unordered_pair(ev)
    conn.execute(
        "UPDATE pass_c_candidate_evidence SET fact_key=%s, proposed_event_id=%s, updated_at=now()"
        " WHERE catalog_source=%s AND from_ref=%s AND to_ref=%s",
        (key, result.produced_event_ids[0], source, lo, hi))
