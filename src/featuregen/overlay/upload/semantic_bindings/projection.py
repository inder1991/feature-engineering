"""E3 — the OPERATIONAL projection of a VERIFIED governed semantic fact.

E1 registered the fact types (entity_assignment / currency_binding); D1/D2 propose DRAFT candidates.
This module makes a CONFIRMED (VERIFIED) semantic fact operational and keeps it operational:

* ``entity_assignment`` -> ``graph_node.entity`` (the effective business entity), the source file's
  entity PRESERVED in ``declared_entity`` (labelled context), plus provenance links
  (``entity_fact_key`` / ``entity_fact_event_id`` / ``entity_status='VERIFIED'``). The node's
  ``search_doc`` is rebuilt in the SAME transaction (the governed tag is full-text searchable).
* ``currency_binding`` -> a ``semantic_binding_edge`` row (measure column -> currency column,
  ``status='VERIFIED'``).

The load-bearing truth is the fact stream; these are its operational projections. A VERIFIED value
WINS: a conflicting re-upload records a DIVERGENCE signal (``declared_entity <> entity``), never an
overwrite. A non-VERIFIED transition DEMOTES immediately (restore the file entity / demote the edge)
— never data loss. Operational readers require ``entity_status='VERIFIED'`` / edge
``status='VERIFIED'`` as a SECOND fail-closed gate.

Three drivers, ONE idempotent projector (``project_semantic_binding_fact``) so they can never
disagree:

1. **Synchronous** — the confirm path calls :func:`project_verified_semantic_binding` (drain-then-
   project, fail-soft), so a binding is operational with no re-upload.
2. **build_graph reproject** — :func:`reproject_semantic_bindings` re-applies every VERIFIED binding
   after a rebuild wiped the graph (a re-ingest MUST NOT erase a governed binding).
3. **Registered projection** — :class:`SemanticBindingProjection` rebuilds the SAME state from the
   event stream (replay parity), with checkpoint / reset / sequence guard / poison-degraded.

The async demotion hooks (reject / expiry / drift) live in ``overlay.expiry`` and call
:func:`demote_semantic_binding` directly — the ingest-latency closer, mirroring
``demote_projected_join_edges``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from psycopg.rows import dict_row

from featuregen.contracts import DbConn
from featuregen.contracts.errors import ProjectionApplyError
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.facts import CURRENCY_BINDING, ENTITY_ASSIGNMENT
from featuregen.overlay.identity import _norm, _ref_from_payload, fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.contract.invalidation import (
    REASON_ENTITY_BINDING_CHANGED,
    ChangedRef,
    invalidate_contracts_for,
)
from featuregen.overlay.upload.graph import rebuild_search_doc
from featuregen.projections.runner import (
    projection_lag,
    rebuild_projection,
    run_projection,
    try_lock_checkpoint_nowait,
)
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

PROJECTION_NAME = "semantic_binding"
_SEMANTIC_FACT_TYPES = (ENTITY_ASSIGNMENT, CURRENCY_BINDING)
# The edge-status vocabulary (mirrors the 1015 CHECK): the folded FactStatus values.
_STATUS_VOCAB = frozenset(
    {"DRAFT", "PARTIALLY_CONFIRMED", "VERIFIED", "REJECTED", "STALE", "REVERIFY"})


def _col_endpoint(table: str, column: str) -> str:
    """A column endpoint in PUBLIC graph scope — the ``graph_node.object_ref`` rendering (matches the
    ``public.<table>.<column>`` form ``build_graph`` writes, NOT the ``src::public.…`` evidence form)."""
    return f"public.{table}.{column}"


def _clamp_status(status: str) -> str:
    """Map a resolved status to the edge-status vocabulary. A non-vocab sentinel (``'missing'``) can
    only accompany a NEVER-projected fact (whose edge row does not exist), so a safe non-VERIFIED
    marker keeps the CHECK satisfied for the harmless no-op UPDATE."""
    return status if status in _STATUS_VOCAB else "STALE"


# ==================================================================================================
# The one idempotent projector — every driver routes through here.
# ==================================================================================================
def project_semantic_binding_fact(conn: DbConn, *, source: str, ref, fact_type: str,
                                  now: datetime | None = None,
                                  emit_invalidation: bool = True) -> None:
    """Clear-then-set ONE semantic-binding fact onto its operational surface, IDEMPOTENTLY.

    ``resolve_fact`` serves VERIFIED-only (the first gate — expiry + drift-freshness read-time guards
    fold in); anything else DEMOTES (fail-closed). Mirrors ``project_confirmed_joins`` /
    ``project_table_facts_for_ref``. ``now`` is threaded so ingest keeps ONE clock basis for
    resolve_fact's read-time guards (resolving on the real clock would drift-stale a fact whose
    watermark was attested under an injected ingest clock).

    [5] ``emit_invalidation`` (default True) — an entity apply/demote that CHANGES ``graph_node.entity``
    (a value H2c hashes as contract-dependency state) eagerly invalidates dependent contracts (durable +
    audited). The GENUINE-change callers (sync confirm, async reject/expire/drift demote, a direct apply)
    keep the default; the benign build_graph reproject + the replay fold pass ``False`` — they reproduce
    identical committed state, so they must NEVER spuriously invalidate a contract that isn't drifted."""
    now = now or datetime.now(UTC)
    adapter = current_catalog_adapter()
    key = fact_key(ref, fact_type)
    resolved = resolve_fact(conn, adapter, ref, fact_type, now=now)
    verified = resolved.status == "VERIFIED" and resolved.value is not None
    if fact_type == ENTITY_ASSIGNMENT:
        if verified:
            _apply_verified_entity(conn, source, ref, resolved, key,
                                   emit_invalidation=emit_invalidation)
        else:
            _demote_entity(conn, key, emit_invalidation=emit_invalidation)
    elif fact_type == CURRENCY_BINDING:
        if verified:
            _apply_verified_currency(conn, source, ref, resolved, key, now)
        else:
            _demote_currency(conn, key, _clamp_status(resolved.status), now)


def _invalidate_entity_change(conn: DbConn, source: str, obj_ref: str) -> None:
    """[5] EAGERLY invalidate every confirmed contract that depends on ``graph_node.entity`` for this
    column node — the value just changed (a genuine confirm/demote), and ``entity`` is in H2c's
    ``_STATE_COLUMNS``. Makes the read-gate downgrade DURABLE + AUDITED + round-trip-proof, consistent
    with the ingest dropped-field wire. A no-op (returns 0) when no confirmed contract depends on it."""
    invalidate_contracts_for(conn, changed=[ChangedRef(
        catalog_source=source, reason=REASON_ENTITY_BINDING_CHANGED, object_ref=obj_ref)])


def _apply_verified_entity(conn: DbConn, source: str, ref, resolved, key: str,
                           *, emit_invalidation: bool = True) -> None:
    """Set the effective governed entity on the subject column node + provenance links; PRESERVE the
    file's declared entity; rebuild search_doc. VERIFIED WINS — a conflicting file declaration is a
    recorded divergence (``declared_entity <> entity``), never an overwrite.

    [5] ``emit_invalidation`` (default True) — when this apply actually CHANGES the effective ``entity``
    value (``current_entity != governed``), eagerly invalidate dependent contracts. The benign
    reproject/replay pass ``False`` (they reproduce identical state and must not spuriously invalidate)."""
    obj_ref = _col_endpoint(ref.table, ref.column)
    governed = resolved.value["entity_id"]
    confirmed_event_id = (resolved.provenance or {}).get("confirmed_event_id")
    row = conn.execute(
        "SELECT entity, declared_entity, entity_status FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s",
        (source, obj_ref)).fetchone()
    if row is None:
        # The subject column node isn't in the graph (not yet ingested / foreign scope). Nothing to
        # project onto — defer to the next build_graph reproject. NEVER raise (fact stays VERIFIED).
        counters.incr("overlay.semantic_binding.entity_node_absent")
        return
    current_entity, declared_entity, entity_status = row
    # Preserve the FILE's display entity. On a FRESH projection (entity_status not yet VERIFIED) the
    # current graph_node.entity IS the file-declared value (build_graph just wrote it) — capture it.
    # On a RE-projection of an already-governed node keep the STORED declared_entity (never overwrite
    # it with the governed value the prior projection already set into `entity`). Idempotent.
    declared = declared_entity if entity_status == "VERIFIED" else current_entity
    conn.execute(
        "UPDATE graph_node SET entity = %s, declared_entity = %s, entity_fact_key = %s, "
        "entity_fact_event_id = %s, entity_status = 'VERIFIED' "
        "WHERE catalog_source = %s AND object_ref = %s",
        (governed, declared, key, confirmed_event_id, source, obj_ref))
    rebuild_search_doc(conn, source, obj_ref)
    if emit_invalidation and current_entity != governed:   # the H2c-hashed entity value actually moved
        _invalidate_entity_change(conn, source, obj_ref)
    if declared is not None and _norm(declared) != _norm(governed):
        # DIVERGENCE: the file declared a DIFFERENT entity than the governed one. The governed value
        # stands in `entity` (VERIFIED wins); the file value stays in `declared_entity` — the two
        # differing IS the durable divergence/re-review signal, never an overwrite.
        counters.incr("overlay.semantic_binding.entity_divergence")
        logger.warning("entity_assignment divergence in %s on %s: file declared %r, governed %r "
                       "(governed wins; file preserved as declared_entity)",
                       source, obj_ref, declared, governed)


def _demote_entity(conn: DbConn, key: str, *, emit_invalidation: bool = True) -> None:
    """RESTORE the file's declared entity as the effective display context, CLEAR governed provenance,
    rebuild search_doc — never data loss. Located by ``entity_fact_key`` (no ref needed); a fact that
    never projected matches no node (no-op).

    [5] ``emit_invalidation`` (default True) — when this demote (reject/expire/drift/withdraw) actually
    CHANGES the effective ``entity`` (the governed value differed from the restored file value), eagerly
    invalidate dependent contracts. The benign reproject/replay pass ``False``."""
    # Capture the pre-image (only when we may need it) so we can tell whether the effective entity value
    # actually moves — restoring an identical declared value is NOT a drift the read gate would see.
    before = conn.execute(
        "SELECT catalog_source, object_ref, entity, declared_entity FROM graph_node "
        "WHERE entity_fact_key = %s", (key,)).fetchall() if emit_invalidation else []
    rows = conn.execute(
        "UPDATE graph_node SET entity = declared_entity, declared_entity = NULL, "
        "entity_fact_key = NULL, entity_fact_event_id = NULL, entity_status = NULL "
        "WHERE entity_fact_key = %s RETURNING catalog_source, object_ref",
        (key,)).fetchall()
    for src, obj_ref in rows:
        rebuild_search_doc(conn, src, obj_ref)
    if emit_invalidation:
        for src, obj_ref, old_entity, declared in before:
            if old_entity != declared:                 # the H2c-hashed entity value actually moved
                _invalidate_entity_change(conn, src, obj_ref)


def _apply_verified_currency(conn: DbConn, source: str, ref, resolved, key: str,
                             now: datetime) -> None:
    """Upsert the VERIFIED currency-binding edge (measure column -> currency column), keyed by
    fact_key. The write gate forced the currency column into the SAME source/schema/table, so both
    endpoints render in the source's public graph scope."""
    cc = resolved.value["currency_column"]
    from_ref = _col_endpoint(ref.table, ref.column)
    to_ref = _col_endpoint(cc["table"], cc["column"])
    confirmed_event_id = (resolved.provenance or {}).get("confirmed_event_id")
    conn.execute(
        "INSERT INTO semantic_binding_edge (fact_key, catalog_source, kind, from_ref, to_ref, "
        "confirmed_event_id, status, projected_at) "
        "VALUES (%s, %s, 'currency_binding', %s, %s, %s, 'VERIFIED', %s) "
        "ON CONFLICT (fact_key) DO UPDATE SET "
        "catalog_source = EXCLUDED.catalog_source, from_ref = EXCLUDED.from_ref, "
        "to_ref = EXCLUDED.to_ref, confirmed_event_id = EXCLUDED.confirmed_event_id, "
        "status = 'VERIFIED', projected_at = EXCLUDED.projected_at",
        (key, source, from_ref, to_ref, confirmed_event_id, now))


def _demote_currency(conn: DbConn, key: str, status: str, now: datetime) -> None:
    """Demote the edge: stamp the non-VERIFIED folded status, KEEP the row (audit trail + the
    status='VERIFIED' 2nd gate excludes it). A never-projected fact has no row — a harmless no-op."""
    conn.execute(
        "UPDATE semantic_binding_edge SET status = %s, projected_at = %s WHERE fact_key = %s",
        (status, now, key))


# ==================================================================================================
# Async demotion hook target (reject / expiry / drift) — located by fact_key, no resolve needed.
# ==================================================================================================
def demote_semantic_binding(conn: DbConn, *, fact_key: str, fact_type: str, status: str,
                            now: datetime | None = None) -> None:
    """ASYNC demotion (the ingest-latency closer): the moment a governed semantic fact leaves VERIFIED
    (reject / expiry / drift-stale) demote its operational projection NOW — restore the file entity /
    demote the edge — without waiting for the next build_graph reproject. Located by fact_key. Shared
    by ``overlay.expiry.demote_projected_semantic_binding`` (which savepoints + fail-softs it)."""
    now = now or datetime.now(UTC)
    if fact_type == ENTITY_ASSIGNMENT:
        # [5] a GENUINE demote (reject/expire/drift) — invalidate dependent contracts on a real change.
        _demote_entity(conn, fact_key, emit_invalidation=True)
    elif fact_type == CURRENCY_BINDING:
        _demote_currency(conn, fact_key, _clamp_status(status), now)


# ==================================================================================================
# build_graph reproject (survive re-upload) — enumerate off the FACTS, re-apply.
# ==================================================================================================
def list_semantic_binding_refs(conn: DbConn, source: str) -> list[tuple[object, str]]:
    """Every ``entity_assignment`` / ``currency_binding`` ``(ref, fact_type)`` ever proposed for
    ``source``, rebuilt from the FACTS — the ``overlay_proposal`` read model gives the fact_keys, the
    proposal event gives the subject ``catalog_object_ref`` (NEVER graph_node / semantic_binding_edge:
    build_graph just wiped the graph and the edge table is what a reproject rebuilds). Deterministic
    (sorted by fact_key). Passing EVERY ref — VERIFIED or not — is safe: the projector resolves each
    and demotes/no-ops a non-VERIFIED one."""
    norm_source = _norm(source)
    out: list[tuple[object, str]] = []
    rows = conn.execute(
        "SELECT fact_key, catalog_source, fact_type FROM overlay_proposal "
        "WHERE fact_type IN ('entity_assignment', 'currency_binding') ORDER BY fact_key").fetchall()
    for key, csource, fact_type in rows:
        if _norm(csource) != norm_source:
            continue
        stream = load_fact(conn, key)
        if not stream:
            continue
        ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
        out.append((ref, fact_type))
    return out


def reproject_semantic_bindings(conn: DbConn, *, source: str, now: datetime | None = None) -> None:
    """Re-project EVERY governed semantic binding for ``source`` after a ``build_graph`` rebuild — a
    re-ingest MUST NOT erase a governed binding (mirrors the table-fact / approved-join reapply).
    build_graph wiped ``graph_node`` (entity + governed columns) and left ``semantic_binding_edge``
    intact; this restores the operational projection from the FACTS. Idempotent + declared-spare: a
    non-VERIFIED fact demotes/no-ops, a pure-declared catalog enumerates nothing and writes nothing."""
    now = now or datetime.now(UTC)
    for ref, fact_type in list_semantic_binding_refs(conn, source):
        # [5] emit_invalidation=False — a build_graph reproject RESTORES the SAME governed state from the
        # facts (VERIFIED wins); it must never spuriously invalidate a contract that isn't drifted.
        project_semantic_binding_fact(conn, source=source, ref=ref, fact_type=fact_type, now=now,
                                      emit_invalidation=False)


# ==================================================================================================
# Synchronous confirm-time projection (mirror project_verified_join) — drain-then-project, fail-soft.
# ==================================================================================================
def _entity_is_operational(conn: DbConn, source: str, ref, key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM graph_node WHERE catalog_source = %s AND object_ref = %s "
        "AND entity_fact_key = %s AND entity_status = 'VERIFIED'",
        (source, _col_endpoint(ref.table, ref.column), key)).fetchone() is not None


def _currency_is_operational(conn: DbConn, key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM semantic_binding_edge WHERE fact_key = %s AND status = 'VERIFIED'",
        (key,)).fetchone() is not None


def project_verified_semantic_binding(conn: DbConn, source: str, ref, fact_type: str, *,
                                      now: datetime | None = None) -> str:
    """SYNCHRONOUSLY project a just-VERIFIED semantic binding onto its operational surface — the
    confirm path's no-re-upload-needed step. Returns ``"projected"`` (operational row exists) or
    ``"pending"`` (deferred to the next caught-up reproject). NEVER raises — the fact stays VERIFIED.

    DRAIN-then-project (mirrors ``project_verified_join``): the caller's ``confirm_fact`` JUST appended
    OVERLAY_FACT_CONFIRMED in the SAME uncommitted transaction, so the async projector's checkpoint is
    behind head and ``resolve_fact``'s ``overlay_fact_state`` lacks the just-VERIFIED row — draining
    THIS conn brings the read model to head. A residual lag (drain poison-HALTED) defers honestly, and
    ``"projected"`` is claimed ONLY when an operational row actually exists (``resolve_fact`` can
    correctly refuse a stale-watermark fact — that refusal must stand, reported as a deferral)."""
    key = fact_key(ref, fact_type)
    try:
        with conn.transaction():  # savepoint: a projection fault must not roll back the confirm
            if not try_lock_checkpoint_nowait(conn, "overlay"):
                # A concurrent ingest holds the 'overlay' checkpoint row to commit (its in-tx drain
                # across the D4/Pass-B LLM stages) — draining here would BLOCK the confirm behind the
                # whole multi-minute ingest tx (audit finding [9]). Defer to the same fail-closed
                # projection-lag path: the fact stays VERIFIED and the next caught-up ingest
                # reproject makes the binding operational.
                counters.incr("overlay.semantic_binding.projection_skipped_lock")
                logger.warning("semantic binding: overlay checkpoint lock held by an in-flight "
                               "ingest — deferring projection of a verified %s in %s", fact_type,
                               source)
                return "pending"
            while run_projection(conn, OverlayProjection()) >= 500:
                pass
            if projection_lag(conn, "overlay") != 0:
                counters.incr("overlay.semantic_binding.projection_skipped_lag")
                logger.warning("semantic binding: overlay projection lags after drain — deferring "
                               "projection of a verified %s in %s", fact_type, source)
                return "pending"
            # [5] the SYNC confirm path is a GENUINE entity change — invalidate dependent contracts on a
            # real value move (the default; only the reproject/replay rebuild paths suppress it).
            project_semantic_binding_fact(conn, source=source, ref=ref, fact_type=fact_type, now=now,
                                          emit_invalidation=True)
        operational = (_entity_is_operational(conn, source, ref, key)
                       if fact_type == ENTITY_ASSIGNMENT
                       else _currency_is_operational(conn, key))
        if not operational:
            counters.incr("overlay.semantic_binding.projection_deferred_unserved")
            return "pending"
        return "projected"
    except Exception:  # noqa: BLE001 — fail-soft: the fact stays VERIFIED; reproject makes it operational
        counters.incr("overlay.semantic_binding.projection_error")
        logger.warning("synchronous verified semantic-binding projection failed for %s in %s — fact "
                       "intact, returning pending", fact_type, source, exc_info=True)
        return "pending"


# ==================================================================================================
# Operational reads — the SECOND fail-closed gate (status='VERIFIED').
# ==================================================================================================
def verified_entity_of(conn: DbConn, catalog_source: str, object_ref: str) -> str | None:
    """The GOVERNED business entity for a column node, gated on ``entity_status='VERIFIED'`` (the
    SECOND fail-closed gate: a stale/demoted projection can never serve a non-VERIFIED entity). None
    when the node carries no VERIFIED governed entity_assignment."""
    row = conn.execute(
        "SELECT entity FROM graph_node WHERE catalog_source = %s AND object_ref = %s "
        "AND entity_status = 'VERIFIED'",
        (catalog_source, object_ref)).fetchone()
    return row[0] if row else None


def verified_currency_binding(conn: DbConn, key: str) -> dict | None:
    """The VERIFIED currency-binding edge for a fact_key, gated on ``status='VERIFIED'`` (2nd gate).
    None when demoted/absent."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM semantic_binding_edge WHERE fact_key = %s AND status = 'VERIFIED'",
            (key,))
        return cur.fetchone()


# ==================================================================================================
# Registered projection (replay parity) — mirror feature_validation_projection.
# ==================================================================================================
class SemanticBindingProjection:
    """The E3 semantic read model as a REGISTERED projection over the ``overlay_fact`` event stream.

    The synchronous confirm-time projector + the build_graph reproject are the OPTIMIZATION; this
    projection is the from-the-event-stream REBUILD whose full replay reproduces the IDENTICAL
    ``graph_node.entity`` + ``semantic_binding_edge`` state (the load-bearing projection invariant).
    Consumes the GLOBAL ``events`` stream via the shared runner (like ``OverlayProjection``), so it
    inherits its checkpoint / sequence-ordering / poison-degraded (``is_analytics=False`` -> a poison
    event HALTS, marks the aggregate degraded, never advances past it).

    Each ``apply`` re-projects the affected fact to its CURRENT resolved state via
    ``project_semantic_binding_fact`` — the SAME function the synchronous path calls — so sync ==
    replay by construction, and the fold is idempotent (a replayed event re-projects the same current
    state, never regresses)."""

    name = PROJECTION_NAME
    is_analytics = False

    def apply(self, conn: DbConn, event) -> None:
        if event.aggregate != "overlay_fact":
            return
        stream = load_fact(conn, event.aggregate_id)
        if not stream:
            return
        proposed = stream[0].payload
        fact_type = proposed.get("fact_type")
        if fact_type not in _SEMANTIC_FACT_TYPES:
            return
        try:
            ref = _ref_from_payload(proposed["catalog_object_ref"])
        except Exception as exc:  # noqa: BLE001 — a corrupt ref payload is poison (fail-closed)
            raise ProjectionApplyError(
                "overlay_fact", event.aggregate_id,
                f"undecodable semantic-binding ref: {exc}") from exc
        # [5] emit_invalidation=False — this fold re-projects the fact's CURRENT state (a live catch-up
        # re-applies what the sync path already emitted; a from-zero rebuild reproduces identical state).
        # The sync confirm + async demote paths own the genuine-change invalidation; the fold must not.
        project_semantic_binding_fact(conn, source=ref.catalog_source, ref=ref, fact_type=fact_type,
                                      emit_invalidation=False)

    def reset(self, conn: DbConn) -> None:
        """Restore EVERY governed entity node to its file display context + truncate the edge table so
        a from-zero rebuild reproduces state identically. The event log (the authority) is untouched."""
        rows = conn.execute(
            "UPDATE graph_node SET entity = declared_entity, declared_entity = NULL, "
            "entity_fact_key = NULL, entity_fact_event_id = NULL, entity_status = NULL "
            "WHERE entity_fact_key IS NOT NULL RETURNING catalog_source, object_ref").fetchall()
        for src, obj_ref in rows:
            rebuild_search_doc(conn, src, obj_ref)
        conn.execute("TRUNCATE semantic_binding_edge")

    def catch_up(self, conn: DbConn, *, batch: int = 500) -> int:
        """Fold every ``overlay_fact`` event > checkpoint into the semantic read model. DRAIN
        ``OverlayProjection`` FIRST (``project_semantic_binding_fact`` reads ``overlay_fact_state`` via
        ``resolve_fact`` — it must be at head), then advance THIS projection over the same stream."""
        while run_projection(conn, OverlayProjection()) >= 500:
            pass
        return run_projection(conn, self, batch=batch)

    def rebuild(self, conn: DbConn) -> None:
        """``reset()`` then deterministically replay ALL events from ``global_seq``=0 — reproducing the
        exact state the synchronous path produced. Drains the overlay read model to head first so every
        fact resolves to its final state during the replay."""
        while run_projection(conn, OverlayProjection()) >= 500:
            pass
        rebuild_projection(conn, self)
