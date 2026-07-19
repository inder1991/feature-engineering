"""Delivery H2c — drift-invalidation of confirmed contracts + the read-time SECOND fail-closed gate.

A confirmed contract's validation stamp is now DRIFT-AWARE. The reverse-dependency ROW WRITES live in
``govern.py`` (next to the H2b input-row writes); the drift MACHINERY lives here:

  * ``invalidate_contracts_for`` — EAGER (defense-in-depth) invalidation: when a catalog item a
    confirmed contract depends on drifts, append an ``INVALIDATED`` validation event for every affected
    contract version and fold it. The C4 projection ALREADY demotes a prior DATA-CHECKED when it folds
    an ``INVALIDATED`` (``feature_validation_projection._fold_effective_state``) — we EMIT events, we do
    NOT touch the fold. Append-only + idempotent (never stacks duplicate events for one drift).

  * ``dependencies_drifted`` / ``current_dependency_hash`` — the engine of the READ-TIME SECOND gate:
    recompute each ``contract_metadata_dependency`` item's CURRENT content hash and compare it to the
    hash stored AT CONFIRM. Any missing / drifted item means the contract can no longer be served with
    its promoted stamp — this catches drift even when NO ``INVALIDATED`` was ever folded (projection
    lag, a missed eager wire, a seamed drift source). ``contract_read_status`` (govern.py) layers it on
    the projection's effective stamp as a HARD, fail-closed downgrade.

  * ``dependency_item_hash`` — the ONE deterministic content hash used BOTH to store a dependency's
    at-confirm hash and to recompute its current hash, so the two are comparable by construction. It
    reuses ``field_evidence.canonical_hash`` (the same convention H2b's ``contract_input_column`` item
    hash uses) over the item's identity refs + its CURRENT catalog state signature (the value/type that
    made it load-bearing).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import DbConn
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload import feature_validation_projection

# INVALIDATED reason codes (the payload ``reason`` — the WHY a dependency invalidated a contract).
REASON_CATALOG_DROPPED = "CATALOG_DROPPED"
REASON_CATALOG_RETYPED = "CATALOG_RETYPED"
REASON_FIELD_DECISION_RETIRED = "FIELD_DECISION_RETIRED"
REASON_FIELD_DECISION_CONFLICT = "FIELD_DECISION_CONFLICT"
REASON_FACT_STALE = "FACT_STALE"
REASON_FACT_EXPIRED = "FACT_EXPIRED"
REASON_FACT_REJECTED = "FACT_REJECTED"
# H2c SEAM (not wired): POLICY_VERSION_CHANGED. ``field_resolution.FIELD_POLICY_VERSION`` is a
# COMPILE-TIME constant — there is no runtime "policy bumped" event/call site to hook, so a bump ships
# as a code change + migration/replay, at which point a batch invalidate_contracts_for over the affected
# scope belongs in that release. Reason code reserved here for that wiring; the read gate does not fold
# policy version into the item signature (a policy bump is a deploy-time concern, not per-read drift).
REASON_POLICY_VERSION_CHANGED = "POLICY_VERSION_CHANGED"

# Sentinel CURRENT-state for a dropped/absent dependency item — a plain string that can NEVER equal a
# live item's state dict, so a dropped item's recomputed hash never matches its stored (live) hash.
_MISSING = "MISSING"

# The graph_node VALUE columns that make an item load-bearing. Deliberately EXCLUDES the volatile
# ``*_decision_id`` link columns (a benign re-upload re-mints them, which would be false drift) — only
# the resolved VALUES/types are hashed, so an unchanged re-upload keeps the SAME signature.
_STATE_COLUMNS = (
    "kind", "data_type", "declared_type", "definition", "is_grain", "is_as_of", "concept", "domain",
    "additivity", "sensitivity", "unit", "currency", "entity", "effective_restriction",
    "classification_status",
)


@dataclass(frozen=True, slots=True)
class ChangedRef:
    """A drifted catalog reference to fan out over ``contract_metadata_dependency``. Matches a
    dependency row by ``catalog_source`` AND (``object_ref`` against ``logical_ref``/``graph_ref``)
    and/or ``decision_id``/``fact_id``. ``reason`` is the INVALIDATED payload reason code."""

    catalog_source: str
    reason: str
    object_ref: str | None = None
    decision_id: str | None = None
    fact_id: str | None = None


def _catalog_state_signature(conn: DbConn, catalog_source: str, object_ref: str | None):
    """The CURRENT load-bearing state of a catalog item (its ``graph_node`` VALUE columns), or the
    ``MISSING`` sentinel when the node is absent (a dropped column/table). Read case-insensitively on
    ``object_ref`` (mirrors ``field_resolution._graph_key``'s lower-cased projection key)."""
    if not object_ref:
        return _MISSING
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, data_type, declared_type, definition, is_grain, is_as_of, concept, domain, "
            "additivity, sensitivity, unit, currency, entity, effective_restriction, "
            "classification_status FROM graph_node "
            "WHERE catalog_source = %s AND lower(object_ref) = lower(%s)",
            (catalog_source, object_ref))
        row = cur.fetchone()
    if row is None:
        return _MISSING
    return {col: row[col] for col in _STATE_COLUMNS}


def dependency_item_hash(*, contract_id: str, catalog_source: str, graph_ref: str | None,
                         logical_ref: str | None, decision_id: str | None, fact_id: str | None,
                         event_id: str | None, state) -> str:
    """The ONE deterministic content hash for a reverse-dep item — used to STORE the at-confirm hash
    AND to RECOMPUTE the current hash (both call this), so the two are comparable by construction.
    Reuses ``canonical_hash`` (H2b's convention) over the item's PERSISTED identity refs + its
    ``state`` signature (the value/type that made it load-bearing). ``role`` is deliberately NOT hashed
    — it is not a persisted column, so a read-time recompute could not reproduce it; two roles over the
    SAME item then collapse to one dependency row (harmless — one drift signal per item suffices)."""
    return canonical_hash({
        "contract_id": contract_id, "catalog_source": catalog_source, "graph_ref": graph_ref,
        "logical_ref": logical_ref, "decision_id": decision_id, "fact_id": fact_id,
        "event_id": event_id, "state": state})


def confirm_dependency_hash(conn: DbConn, *, contract_id: str, catalog_source: str,
                            graph_ref: str | None, logical_ref: str | None, decision_id: str | None,
                            fact_id: str | None, event_id: str | None) -> str:
    """Compute a dependency item's content hash over its state AT CONFIRM (called when the reverse-dep
    row is first written). Same function family as :func:`current_dependency_hash`, so a later read
    recomputes a comparable value over the item's then-current state."""
    state = _catalog_state_signature(conn, catalog_source, logical_ref)
    return dependency_item_hash(
        contract_id=contract_id, catalog_source=catalog_source, graph_ref=graph_ref,
        logical_ref=logical_ref, decision_id=decision_id, fact_id=fact_id, event_id=event_id,
        state=state)


def current_dependency_hash(conn: DbConn, dep_row: Mapping) -> str:
    """Recompute a stored dependency row's hash over the item's CURRENT catalog state — comparable to
    the stored ``item_hash`` (both via :func:`dependency_item_hash`). A dropped/missing item hashes the
    ``MISSING`` sentinel, which can never match a live item's hash → the read gate downgrades."""
    state = _catalog_state_signature(conn, dep_row["catalog_source"], dep_row["logical_ref"])
    return dependency_item_hash(
        contract_id=dep_row["contract_id"], catalog_source=dep_row["catalog_source"],
        graph_ref=dep_row["graph_ref"], logical_ref=dep_row["logical_ref"],
        decision_id=dep_row["decision_id"], fact_id=dep_row["fact_id"],
        event_id=dep_row["event_id"], state=state)


def dependencies_drifted(conn: DbConn, contract_id: str) -> bool:
    """True iff ANY of the contract's ``contract_metadata_dependency`` items has drifted since confirm
    — its recomputed current hash differs from the stored ``item_hash`` (item missing / retyped /
    cleared / retired). A contract with NO dependency rows (legacy / pre-H2c) never drifts here, so the
    gate is additive and never falsely downgrades a contract it has no lineage for."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT contract_id, catalog_source, graph_ref, logical_ref, decision_id, fact_id, "
            "event_id, item_hash FROM contract_metadata_dependency WHERE contract_id = %s",
            (contract_id,))
        deps = cur.fetchall()
    return any(current_dependency_hash(conn, dep) != dep["item_hash"] for dep in deps)


def _affected_contract_ids(conn: DbConn, ref: ChangedRef) -> list[str]:
    """Every contract version with a ``contract_metadata_dependency`` row matching ``ref`` — by
    ``catalog_source`` plus a graph object_ref (case-insensitive on either ``logical_ref`` or
    ``graph_ref``) and/or a decision/fact id. Only the supplied match keys participate (a NULL key
    matches nothing)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT contract_id FROM contract_metadata_dependency "
            "WHERE catalog_source = %s AND ("
            "  (%s::text IS NOT NULL AND (lower(logical_ref) = lower(%s) "
            "                             OR lower(graph_ref) = lower(%s))) "
            "  OR (%s::text IS NOT NULL AND decision_id = %s) "
            "  OR (%s::text IS NOT NULL AND fact_id = %s))",
            (ref.catalog_source, ref.object_ref, ref.object_ref, ref.object_ref,
             ref.decision_id, ref.decision_id, ref.fact_id, ref.fact_id))
        return [r[0] for r in cur.fetchall()]


def _already_invalidated(conn: DbConn, contract_id: str, ref: ChangedRef) -> bool:
    """Idempotency guard: has an ``INVALIDATED`` event for this contract with the SAME
    (reason, catalog_source, object_ref, decision_id, fact_id) already been appended? Reads committed +
    same-transaction rows, so a re-call (or a duplicate ref within one call) never stacks a duplicate."""
    return conn.execute(
        "SELECT 1 FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type = 'INVALIDATED' "
        "AND payload->>'reason' = %s "
        "AND coalesce(payload->>'catalog_source', '') = coalesce(%s, '') "
        "AND coalesce(payload->>'object_ref', '') = coalesce(%s, '') "
        "AND coalesce(payload->>'decision_id', '') = coalesce(%s, '') "
        "AND coalesce(payload->>'fact_id', '') = coalesce(%s, '') LIMIT 1",
        (contract_id, ref.reason, ref.catalog_source, ref.object_ref, ref.decision_id,
         ref.fact_id)).fetchone() is not None


def invalidate_contracts_for(conn: DbConn, *, changed: Iterable[ChangedRef]) -> int:
    """EAGER drift invalidation (defense-in-depth). For every drifted ``ChangedRef``, append an
    ``INVALIDATED`` ``feature_contract_validation_event`` to each affected contract version and fold it
    (the projection demotes any prior DATA-CHECKED). Returns the number of INVALIDATED events appended.

    Append-only + idempotent: re-invalidating an already-invalidated contract for the SAME
    (reason, changed-ref) is a no-op (no duplicate event). Historical attestations stay immutable — only
    the append-only stream grows. Takes the projection checkpoint lock (``lock_checkpoint``) BEFORE
    emitting, mirroring ``_seed_validation_lifecycle`` (MF-1): concurrent confirms/invalidations
    serialize their seq-assignment WITH the fold, and the idempotency check re-reads committed state
    under the lock so two racers cannot both append."""
    targets = [(cid, ref) for ref in changed for cid in _affected_contract_ids(conn, ref)]
    if not targets:
        return 0
    feature_validation_projection.lock_checkpoint(conn)
    appended = 0
    for contract_id, ref in targets:
        if _already_invalidated(conn, contract_id, ref):
            continue
        payload = {"reason": ref.reason, "catalog_source": ref.catalog_source}
        if ref.object_ref is not None:
            payload["object_ref"] = ref.object_ref
        if ref.decision_id is not None:
            payload["decision_id"] = ref.decision_id
        if ref.fact_id is not None:
            payload["fact_id"] = ref.fact_id
        conn.execute(
            "INSERT INTO feature_contract_validation_event "
            "(event_id, contract_id, event_type, payload) VALUES (%s, %s, 'INVALIDATED', %s)",
            (mint_id("fcve"), contract_id, Jsonb(payload)))
        appended += 1
    if appended:
        feature_validation_projection.catch_up(conn)   # fold the INVALIDATED(s) — demotes DATA-CHECKED
    return appended
