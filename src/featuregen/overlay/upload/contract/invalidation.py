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

# H2 C-2 fail-closed POISON. A check-clearing dependency that is UNRESOLVABLE *at confirm* (its state
# signature is MISSING) cannot have legitimately cleared anything — its lineage is defective. We store a
# hash over THIS sentinel, which ``_catalog_state_signature`` NEVER returns (it only ever yields a live
# state dict or ``_MISSING``), so a read-time recompute can never reproduce it: the item reads as
# permanently drifted and the contract can never be served with a promoted stamp. This is what closes the
# self-matching ``MISSING == MISSING`` hole (a confirm-time MISSING no longer equals a read-time MISSING).
_UNRESOLVED_AT_CONFIRM = "UNRESOLVED_AT_CONFIRM"

# H2 C-1 — a JOIN EDGE dependency (the ``graph_edge`` that clears JOIN_CONNECTIVITY) is not a graph_node,
# so it is recorded under a marker ``logical_ref`` that ``_catalog_state_signature`` routes to the edge
# state reader. The marker encodes the UNORDERED endpoint pair so either authoring orientation lands on
# one deterministic item, mirroring how the projector touches an edge in both orientations.
_EDGE_PREFIX = "joinedge:"

# H3 fix — a GOVERNED-SEGMENT dependency (a governed cross-catalog plan's bridge fact / realization) is
# ALSO not a graph_node: its ``ordered_path`` ref is a bridge fact KEY (``bfk_*``, the PK of
# ``entity_bridge_edge``) or a realization ID (``{catalog}:{from_key}->{to_key}``). Recording those raw as
# graph_node deps made ``_catalog_state_signature`` return ``MISSING`` at confirm → the ``confirm_dependency_hash``
# poison → the contract read as permanently drifted (no governed contract could ever serve a promoted stamp).
# They are recorded under these markers instead, which ``_catalog_state_signature`` routes to the RIGHT
# authoritative state (the bridge's VERIFIED sanction / the realization's cardinality) — mirroring the
# ``joinedge:`` pattern: RESOLVES at confirm (no poison, promotable) and CHANGES on revocation/drift.
_BRIDGEFACT_PREFIX = "bridgefact:"
_REALIZATION_PREFIX = "realization:"


def join_edge_marker(from_ref: str, to_ref: str) -> str:
    """The ``logical_ref`` marker for the join EDGE between two column endpoints (C-1). Endpoints are
    lower-cased and SORTED so the same physical edge yields one marker regardless of which side the draft
    step names — the read gate then hashes the edge's CURRENT state (see ``_join_edge_signature``)."""
    a, b = sorted((from_ref.lower(), to_ref.lower()))
    return f"{_EDGE_PREFIX}{a}|{b}"


def bridge_fact_marker(fact_key: str) -> str:
    """H3 fix — the ``logical_ref`` marker for a governed cross-catalog plan's BRIDGE-FACT segment. The
    read gate hashes the bridge's CURRENT VERIFIED sanction in ``entity_bridge_edge`` (see
    :func:`_bridge_fact_signature`): a dropped / revoked bridge (its row DELETEd) ⟶ ``MISSING`` ⟶ drift.
    ``fact_key`` is the bridge fact's key (the ``entity_bridge_edge`` PK), an opaque id — matched EXACTLY."""
    return f"{_BRIDGEFACT_PREFIX}{fact_key}"


def realization_marker(realization_id: str) -> str:
    """H3 fix — the ``logical_ref`` marker for a governed plan's intra-catalog REALIZATION segment. The
    read gate hashes the realization's CURRENT cardinality + governed authority from ``graph_edge`` (see
    :func:`_realization_signature`): a cardinality retype, an approval revocation, or the edge's drop ⟶ a
    changed / ``MISSING`` signature ⟶ drift. ``realization_id`` is ``{catalog}:{from_key}->{to_key}`` (the
    deriver's id); it is hashed verbatim as the item's identity and parsed for the state lookup."""
    return f"{_REALIZATION_PREFIX}{realization_id}"

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
    ``object_ref`` (mirrors ``field_resolution._graph_key``'s lower-cased projection key).

    C-1: an ``object_ref`` carrying the ``joinedge:`` marker is a JOIN EDGE dependency — its state comes
    from ``graph_edge`` (existence + approved-join status/authority/cardinality), not a graph_node.
    H3 fix: a ``bridgefact:`` marker is a governed cross-catalog BRIDGE segment (state from the VERIFIED
    ``entity_bridge_edge`` sanction) and a ``realization:`` marker is a governed intra-catalog REALIZATION
    segment (cardinality + governed authority from ``graph_edge``) — neither is a graph_node.
    M-d: a deterministic ``ORDER BY`` guards the case-insensitive match against nondeterministic picks
    when two case-variant sibling nodes coexist for one ``lower(object_ref)``."""
    if not object_ref:
        return _MISSING
    if object_ref.startswith(_EDGE_PREFIX):
        return _join_edge_signature(conn, catalog_source, object_ref)
    if object_ref.startswith(_BRIDGEFACT_PREFIX):
        return _bridge_fact_signature(conn, object_ref)
    if object_ref.startswith(_REALIZATION_PREFIX):
        return _realization_signature(conn, catalog_source, object_ref)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, data_type, declared_type, definition, is_grain, is_as_of, concept, domain, "
            "additivity, sensitivity, unit, currency, entity, effective_restriction, "
            "classification_status FROM graph_node "
            "WHERE catalog_source = %s AND lower(object_ref) = lower(%s) "
            "ORDER BY object_ref LIMIT 1",
            (catalog_source, object_ref))
        row = cur.fetchone()
    if row is None:
        return _MISSING
    return {col: row[col] for col in _STATE_COLUMNS}


def _join_edge_signature(conn: DbConn, catalog_source: str, marker: str):
    """C-1 — the CURRENT state of the join ``graph_edge`` a JOIN_CONNECTIVITY disposition cleared, or
    ``MISSING`` when the edge is gone. Matches the marker's unordered endpoint pair in EITHER stored
    orientation (an edge is written once; the traversal may name it reversed). Captures existence +
    ``authority`` + ``approved_join_status`` + fact-link presence + ``cardinality``: dropping the edge
    ⟶ MISSING, and losing the VERIFIED approval (async demote flips authority to ``display_only`` and
    restamps the status) ⟶ a changed signature — either way the read gate downgrades. Deterministic
    ``ORDER BY`` so a duplicate-orientation edge can't pick nondeterministically."""
    body = marker[len(_EDGE_PREFIX):]
    a, _, b = body.partition("|")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT cardinality, authority, approved_join_status, "
            "(approved_join_fact_key IS NOT NULL) AS has_fact FROM graph_edge "
            "WHERE catalog_source = %s AND kind = 'joins' AND "
            "((lower(from_ref) = %s AND lower(to_ref) = %s) OR "
            " (lower(from_ref) = %s AND lower(to_ref) = %s)) "
            "ORDER BY from_ref, to_ref LIMIT 1",
            (catalog_source, a, b, b, a))
        row = cur.fetchone()
    if row is None:
        return _MISSING
    return {"edge": True, "cardinality": row["cardinality"], "authority": row["authority"],
            "approved_join_status": row["approved_join_status"], "has_approved_fact": row["has_fact"]}


def _bridge_fact_signature(conn: DbConn, marker: str):
    """H3 fix (I-3) — the CURRENT state of the governed cross-catalog BRIDGE a plan segment crossed, keyed
    by its bridge fact key (the ``bridgefact:`` marker body). The bridge's source of truth is the overlay
    fact stream projected into ``entity_bridge_edge`` (VERIFIED only); a reject / expire / stale DELETEs
    the row (``bridge_projection.demote_bridge_edges``). Captures existence + the VERIFIED ``status`` +
    the projected endpoints + entity: a REVOKED bridge ⟶ the row is gone ⟶ ``MISSING`` (the recomputed
    hash can no longer match the live hash stored at confirm) ⟶ the read gate downgrades. A live VERIFIED
    bridge RESOLVES to a real dict at confirm, so the item hash is NOT poisoned — the governed contract is
    PROMOTABLE. ``fact_key`` is the ``entity_bridge_edge`` PK (global, not catalog-scoped) — matched exactly."""
    fact_key = marker[len(_BRIDGEFACT_PREFIX):]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status, entity_id, left_catalog_source, left_object_ref, "
            "right_catalog_source, right_object_ref FROM entity_bridge_edge WHERE fact_key = %s",
            (fact_key,))
        row = cur.fetchone()
    if row is None:
        return _MISSING
    return {"bridge": True, "status": row["status"], "entity_id": row["entity_id"],
            "left": f"{row['left_catalog_source']}.{row['left_object_ref']}",
            "right": f"{row['right_catalog_source']}.{row['right_object_ref']}"}


def _realization_signature(conn: DbConn, catalog_source: str, marker: str):
    """H3 fix (I-1) — the CURRENT cardinality + governed authority of an intra-catalog REALIZATION a plan
    segment used, keyed by its realization id (``realization:{catalog}:{from_key}->{to_key}``). A
    realization is DERIVED from a declared ``graph_edge`` join (``catalog_realizations.derive_catalog_
    realizations``), so the read gate re-reads that edge's CURRENT cardinality / authority / approved-join
    status: a cardinality retype, an approval revocation (VERIFIED→display_only), or the edge's drop ⟶ a
    changed / ``MISSING`` signature ⟶ drift. It RESOLVES to a real dict at confirm (no poison), so the
    governed contract is PROMOTABLE. The realization IDENTITY is preserved because the marker (``logical_ref``)
    is itself part of the item hash; this reads only the load-bearing cardinality/authority state.
    Case-insensitive match with a deterministic ``ORDER BY`` (mirrors ``_join_edge_signature``)."""
    rid = marker[len(_REALIZATION_PREFIX):]
    _catalog, _, keypair = rid.partition(":")          # strip the leading catalog (== catalog_source)
    from_key, _, to_key = keypair.partition("->")
    if not from_key or not to_key:
        return _MISSING                                # malformed / legacy id — fail closed (poisonable)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT cardinality, authority, approved_join_status, "
            "(approved_join_fact_key IS NOT NULL) AS has_fact FROM graph_edge "
            "WHERE catalog_source = %s AND kind = 'joins' "
            "AND lower(from_ref) = lower(%s) AND lower(to_ref) = lower(%s) "
            "ORDER BY from_ref, to_ref LIMIT 1",
            (catalog_source, from_key, to_key))
        row = cur.fetchone()
    if row is None:
        return _MISSING
    return {"realization": True, "cardinality": row["cardinality"], "authority": row["authority"],
            "approved_join_status": row["approved_join_status"], "has_approved_fact": row["has_fact"]}


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
    recomputes a comparable value over the item's then-current state.

    C-2 fail-closed: if the item is UNRESOLVABLE at confirm (its state is ``MISSING`` — a mis-attributed
    cross-catalog ref, a display-string join step, an entity/bridge segment), we hash the
    ``_UNRESOLVED_AT_CONFIRM`` poison instead of ``MISSING``. A check-clearing dependency that cannot be
    resolved cannot have legitimately cleared anything, and a poison baseline can never be reproduced by
    a read-time recompute (which only ever yields a live dict or ``MISSING``), so the read gate treats
    the item as permanently drifted — the contract never serves a promoted stamp (no self-matching
    ``MISSING == MISSING``)."""
    state = _catalog_state_signature(conn, catalog_source, logical_ref)
    if state == _MISSING:
        state = _UNRESOLVED_AT_CONFIRM
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


def has_dependency_rows(conn: DbConn, contract_id: str) -> bool:
    """I-1fc — whether a contract has ANY recorded ``contract_metadata_dependency`` row. A PROMOTED
    contract with ZERO rows is gate-BLIND (``dependencies_drifted`` returns False on no rows), so the
    read gate must fail closed on it (a promoted stamp with no recorded lineage cannot be trusted
    drift-free). A non-promoted contract never reaches that gate branch, so a legacy_unassessed contract
    is unaffected."""
    return conn.execute(
        "SELECT 1 FROM contract_metadata_dependency WHERE contract_id = %s LIMIT 1",
        (contract_id,)).fetchone() is not None


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
    (reason, catalog_source, object_ref, decision_id, fact_id) already been appended SINCE THE LAST
    re-clearing event? Reads committed + same-transaction rows, so a re-call (or a duplicate ref within
    one call) never stacks a duplicate.

    M-c: the dedup is scoped to the CURRENT TAIL — a matching INVALIDATED with ``seq`` GREATER than the
    last re-clearing (ASSESSED / EXTERNAL_PASSED) event. An all-time dedup would wrongly suppress a
    GENUINELY RECURRED drift: after INVALIDATED → re-clear (a new pass/assessment moves the epoch) →
    the SAME drift recurs, that recurrence must re-invalidate (defense-in-depth for the read gate)."""
    epoch = conn.execute(
        "SELECT coalesce(max(seq), 0) FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type IN ('ASSESSED', 'EXTERNAL_PASSED')",
        (contract_id,)).fetchone()[0]
    return conn.execute(
        "SELECT 1 FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type = 'INVALIDATED' AND seq > %s "
        "AND payload->>'reason' = %s "
        "AND coalesce(payload->>'catalog_source', '') = coalesce(%s, '') "
        "AND coalesce(payload->>'object_ref', '') = coalesce(%s, '') "
        "AND coalesce(payload->>'decision_id', '') = coalesce(%s, '') "
        "AND coalesce(payload->>'fact_id', '') = coalesce(%s, '') LIMIT 1",
        (contract_id, epoch, ref.reason, ref.catalog_source, ref.object_ref, ref.decision_id,
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
