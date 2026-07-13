"""Pass C — candidate fingerprint + dedupe/conflict lifecycle (Phase 3A Task 6).

`decide_action` adjudicates a scored candidate against (a) the `approved_join` fact stream for its
OWN `fact_key` and (b) the durable `pass_c_candidate_evidence` ledger row for its UNORDERED
**COLUMN**-ref pair (migration 0988; Task 10 writes rows, this module only reads them).

The conflict grain is the unordered COLUMN pair, NOT the table pair: two joins on DIFFERENT
columns between the same tables are legitimate (a second join → PROPOSE). Only a DIFFERENT
`fact_key` (different direction/cardinality) that is ACTIVE **for the same column pair** is a
CONFLICT. Folded statuses come from `state.py` — note EXPIRED folds to `REVERIFY`; there is no
"EXPIRED" folded status.
"""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.passc.types import JoinCandidateEvidenceV1

# Folded statuses under which the fact is LIVE or pending a decision — a same-key candidate
# dedupes (SKIP_ACTIVE) and a different-key same-pair candidate conflicts (CONFLICT).
_ACTIVE: frozenset[str] = frozenset({"DRAFT", "PARTIALLY_CONFIRMED", "VERIFIED"})

# Same-key statuses that admit re-adjudication when the evidence materially changed. REVERIFY is
# what an EXPIRED event folds to (state.py); STALE is the drift demotion; REJECTED is terminal.
_REPROPOSABLE: frozenset[str] = frozenset({"REJECTED", "STALE", "REVERIFY"})


class Action(StrEnum):
    PROPOSE = "PROPOSE"            # no prior claim on the key or the column pair
    SKIP_ACTIVE = "SKIP_ACTIVE"    # the SAME fact_key is already active — dedupe
    CONFLICT = "CONFLICT"          # a DIFFERENT fact_key is active for the SAME column pair
    REPROPOSE = "REPROPOSE"        # same key, terminal, and the evidence materially changed


def unordered_pair(evidence: JoinCandidateEvidenceV1) -> tuple[str, str]:
    """The candidate's UNORDERED column-ref pair, sorted — the ledger's identity (its PK stores
    `from_ref`/`to_ref` sorted so both directions of the same pair land on one row)."""
    return tuple(sorted((evidence.from_ref, evidence.to_ref)))  # type: ignore[return-value]


def candidate_fingerprint(evidence: JoinCandidateEvidenceV1) -> str:
    """Stable sha256 over the candidate's MATERIAL content — what it proposes (endpoints, column
    pairs, direction, cardinality) and how it was adjudicated (bucket, namespace verdict) under
    which config/algorithm. Volatile fields (source_snapshot_id, score, signal evidence,
    explanations) are EXCLUDED so a re-ingest of the same content dedupes to the same fingerprint;
    only a materially different candidate yields a new one (mirrors `proposal_fingerprint`)."""
    canonical = {
        "from_ref": evidence.from_ref,
        "to_ref": evidence.to_ref,
        "column_pairs": [list(p) for p in evidence.column_pairs],
        "proposed_direction": evidence.proposed_direction,
        "proposed_cardinality": evidence.proposed_cardinality,
        "bucket": evidence.bucket,
        "namespace_compatibility": evidence.namespace_compatibility.value,
        "config_version": evidence.config_version,
        "candidate_algorithm_version": evidence.candidate_algorithm_version,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _column_ref(object_ref: str, source: str) -> CatalogObjectRef:
    """Parse a Pass-C column `object_ref` (``source::schema.table.column`` as built by the
    ingest metadata loader, or a bare ``schema.table.column`` / ``table.column``) into a typed
    column ref under `source`."""
    bare = object_ref.split("::")[-1]
    parts = [p.strip() for p in bare.split(".")]
    if len(parts) == 3:
        schema, table, column = parts
    elif len(parts) == 2:
        schema, (table, column) = "public", parts
    else:
        raise ValueError(f"unparseable column object_ref {object_ref!r}: "
                         "expected 'schema.table.column' or 'table.column'")
    if not (schema and table and column):
        raise ValueError(f"column object_ref {object_ref!r} has an empty component")
    return CatalogObjectRef(source, "column", schema, table, column)


def build_join_ref(evidence: JoinCandidateEvidenceV1, source: str) -> ApprovedJoinRef:
    """The governed `ApprovedJoinRef` this candidate proposes. Requires an inferred cardinality —
    only a grain-inferred candidate is ever proposable (candidates.py never defaults one for a
    neither-grain pair), so a cardinality-less candidate here is a caller bug, not a default."""
    if evidence.proposed_cardinality is None:
        raise ValueError(
            f"candidate {evidence.candidate_id} has no inferred cardinality; only a "
            "grain-inferred candidate can build an ApprovedJoinRef")
    return ApprovedJoinRef(
        from_ref=_column_ref(evidence.from_ref, source),
        to_ref=_column_ref(evidence.to_ref, source),
        column_pairs=tuple(ColumnPair(f, t) for f, t in evidence.column_pairs),
        cardinality=evidence.proposed_cardinality)


def _ledger_row(conn, catalog_source: str, evidence: JoinCandidateEvidenceV1):
    """The prior ledger row for the candidate's unordered column pair, or None (Task 10 writes)."""
    lo, hi = unordered_pair(evidence)
    return conn.execute(
        "SELECT candidate_fingerprint, fact_key, bucket, namespace_compatibility, lifecycle "
        "FROM pass_c_candidate_evidence "
        "WHERE catalog_source=%s AND from_ref=%s AND to_ref=%s",
        (catalog_source, lo, hi)).fetchone()


def decide_action(conn, ref: ApprovedJoinRef, evidence: JoinCandidateEvidenceV1) -> Action:
    """Adjudicate one scored candidate against the overlay + the candidate ledger.

    * same-`fact_key` ACTIVE (DRAFT/PARTIALLY_CONFIRMED/VERIFIED) → SKIP_ACTIVE (dedupe; a DRAFT
      with the same fingerprint would also be sticky-denied downstream by `propose_fact`).
    * a DIFFERENT-`fact_key` ACTIVE fact for the SAME unordered column pair (the ledger row's
      recorded key — a different direction/cardinality hashes differently) → CONFLICT. A fact on a
      DIFFERENT column pair between the same tables is NOT a conflict.
    * same key in a terminal/demoted state (REJECTED / STALE / REVERIFY — EXPIRED folds to
      REVERIFY) whose prior ledger `bucket` or `namespace_compatibility` materially changed →
      REPROPOSE (fresh evidence warrants a new adjudication).
    * otherwise → PROPOSE. Dispatch-level guards stay authoritative: `propose_fact` still
      sticky-denies a previously rejected fingerprint and denies a non-terminal re-propose.
    """
    key = fact_key(ref, "approved_join")
    state = fold_overlay_state(load_fact(conn, key))
    if state.status in _ACTIVE:
        return Action.SKIP_ACTIVE
    row = _ledger_row(conn, ref.from_ref.catalog_source, evidence)
    if row is not None:
        prior_fp, prior_key, prior_bucket, prior_namespace, _lifecycle = row
        if prior_key is not None and prior_key != key:
            # The pair is already claimed under ANOTHER direction/cardinality — a conflict only
            # while that claim is live; a rejected/demoted rival does not block a new proposal.
            rival = fold_overlay_state(load_fact(conn, prior_key))
            if rival.status in _ACTIVE:
                return Action.CONFLICT
        elif state.status in _REPROPOSABLE and (
                prior_bucket != evidence.bucket
                or prior_namespace != evidence.namespace_compatibility.value):
            return Action.REPROPOSE
    return Action.PROPOSE
