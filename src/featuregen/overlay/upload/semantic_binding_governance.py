"""Semantic-binding governance read model + review bridge (Delivery E, Task E2).

The owner-or-admin sibling of ``join_governance.py`` / ``table_fact_governance.py`` for the
governed ``entity_assignment`` / ``currency_binding`` facts D2 proposes and E3 projects. It gives
the human review surfaces that move a DRAFT semantic binding to VERIFIED (or reject it), plus the
reverify / withdraw / correct actions for an ALREADY-VERIFIED binding.

``list_semantic_binding_proposals`` is a READ MODEL over the ``overlay_proposal`` read model and
the ``overlay_fact`` event stream (not a new queue): it lists BOTH the open (pending) proposals AND
the VERIFIED bindings for a source — the VERIFIED ones so the asset UI can offer reverify /
withdraw / correct. Each view carries the candidate evidence + candidate-set / ingestion-run
provenance (``…_candidate_proposal`` → ``…_candidate`` → ``…_set``), the reason codes, the prior
value, the CAS target event id, the latest reviewer note, and the ``available_actions`` the server
sanctions for the binding's status — the asset UI may NOT advertise an edge as editable unless the
server returns one of these commands.

``load_semantic_binding_confirmation_context`` turns a ``fact_key`` back into the typed
confirm/reject command args a route dispatches (fact_type-VALIDATED — a non-semantic-binding
fact_key raises :class:`SemanticBindingGovernanceNotFound`, closing the generic-approval hole),
with ``target_event_id = _cas_target(state)`` — the EXACT id confirm/reject CAS against.

The reverify / withdraw / correct actions on a VERIFIED binding NEVER hand-write fact state: they
reuse the SANCTIONED expiry/reverify transition (``expiry._apply_expiry`` — VERIFIED → REVERIFY,
demote the projection, open the re-verify task) and then dispatch the REAL overlay commands
(``reject_fact`` for withdraw, ``reject_fact`` + ``propose_fact`` for correct). Every action first
re-checks the E1 owner-or-admin authority (``resolve_authority`` + ``_actor_is_authority``) and
writes a tamper-evident ``COMMAND_DENIED`` on a non-authority (mirroring ``_deny_audited``);
four-eyes is preserved because ``correct`` records the correcting human as the NEW proposer, so a
DIFFERENT authorized human must confirm the corrected value (a human may never propose AND approve
one value).

FAILURE ISOLATION IS LOAD-BEARING (mirrors the peer read models): one binding whose stream / ref /
candidate row is unreadable is SKIPPED with a warning + counter — never raised. A single poisoned
row must not take down the whole governance queue.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from featuregen.contracts import Command, DbConn
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.overlay._lifecycle import _cas_target, _latest_proposed
from featuregen.overlay.authority import (
    _actor_is_authority,
    resolve_authority,
)
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.facts import CURRENCY_BINDING, ENTITY_ASSIGNMENT
from featuregen.overlay.identity import (
    CatalogObjectRef,
    _norm,
    _ref_from_payload,
    join_write_error,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.runtime.observability import counters
from featuregen.security.audit import record_denial

logger = logging.getLogger(__name__)

_SEMANTIC_FACT_TYPES = (ENTITY_ASSIGNMENT, CURRENCY_BINDING)
_LIMIT_MAX = 500

# Folded statuses this surface lists (REJECTED is terminal — excluded). A folded DRAFT displays as
# "PROPOSED"; every other listed status renders verbatim.
_PENDING_STATUSES = frozenset({"DRAFT", "PARTIALLY_CONFIRMED", "REVERIFY", "STALE"})
_DISPLAY_STATUS = {"DRAFT": "PROPOSED"}

# The server-sanctioned actions per status. A VERIFIED binding is editable (reverify/withdraw/
# correct); a pending one awaits a confirm/reject decision. The asset UI keys "is this edge
# editable?" off THIS.
_ACTIONS_VERIFIED = ("reverify", "withdraw", "correct")
_ACTIONS_PENDING = ("confirm", "reject")


class SemanticBindingGovernanceNotFound(Exception):
    """``fact_key`` does not name a loadable ``entity_assignment`` / ``currency_binding`` proposal:
    its stream is empty, its DRAFT ref will not decode to a typed ``CatalogObjectRef``, or its
    ``fact_type`` is not a governed semantic binding. The confirm/reject/reverify/withdraw/correct
    routes map this to 404 — critically, BEFORE any event is written, so this surface can never
    approve a join/policy/table fact."""


def _read_candidate_provenance(conn: DbConn, fact_key: str) -> dict:
    """The D1 candidate evidence + candidate-set / ingestion-run provenance + reason codes for a
    ``fact_key``, via the ``semantic_binding_candidate_proposal`` link (D1/D2). Returns ``{}`` when
    the binding has no candidate link (e.g. a human-corrected binding proposed off no shortlist) or
    on ANY error — provenance is display context and must never break the queue. The newest linked
    candidate set wins (a re-shortlist re-links)."""
    try:
        # SAVEPOINT: a query fault (e.g. a schema drift) must roll back to here, never abort the
        # OUTER read transaction — the queue must still render every OTHER binding (fail-soft).
        with conn.transaction():
            row = conn.execute(
                "SELECT c.candidate_id, c.candidate_set_id, c.disposition, c.reason_codes, "
                "       c.evidence_json, s.ingestion_run_id, s.attempt_no, "
                "       s.metadata_input_fingerprint, s.task_version, c.model_version, "
                "       p.proposed_event_id "
                "FROM semantic_binding_candidate_proposal p "
                "JOIN semantic_binding_candidate c ON c.candidate_id = p.candidate_id "
                "JOIN semantic_binding_candidate_set s ON s.candidate_set_id = c.candidate_set_id "
                "WHERE p.fact_key = %s "
                "ORDER BY s.created_at DESC, c.created_at DESC LIMIT 1",
                (fact_key,)).fetchone()
    except Exception:  # noqa: BLE001 — provenance is display data; never break the queue
        counters.incr("overlay.semantic_binding_governance.provenance_unreadable")
        logger.warning("semantic-binding governance: provenance for fact %s unreadable — nulled",
                       fact_key, exc_info=True)
        return {}
    if row is None:
        return {}
    (candidate_id, candidate_set_id, disposition, reason_codes, evidence_json,
     ingestion_run_id, attempt_no, fingerprint, task_version, model_version,
     proposed_event_id) = row
    return {
        "candidate_id": candidate_id,
        "candidate_set_id": candidate_set_id,
        "disposition": disposition,
        "reason_codes": reason_codes if isinstance(reason_codes, list) else [],
        "evidence": evidence_json if isinstance(evidence_json, Mapping) else {},
        "ingestion_run_id": ingestion_run_id,
        "attempt_no": attempt_no,
        "metadata_input_fingerprint": fingerprint,
        "task_version": task_version,
        "model_version": model_version,
        "proposed_event_id": proposed_event_id,
    }


def _latest_reviewer_note(stream) -> str | None:
    """The most recent reviewer note folded off the stream — the ``note`` on the last
    OVERLAY_FACT_CONFIRMED / OVERLAY_FACT_PARTIALLY_CONFIRMED, or the ``reason`` on the last
    OVERLAY_FACT_REJECTED — whichever is most recent. Best-effort (None on absence)."""
    for event in reversed(list(stream)):
        payload = getattr(event, "payload", {}) or {}
        if event.type in ("OVERLAY_FACT_CONFIRMED", "OVERLAY_FACT_PARTIALLY_CONFIRMED"):
            note = payload.get("note")
            if note:
                return note
        elif event.type == "OVERLAY_FACT_REJECTED":
            reason = payload.get("reason")
            if reason:
                return reason
    return None


def _effective_value(state, stream) -> object | None:
    """The value the reviewer should see: the CURRENT confirmed value (VERIFIED), else the value
    being re-verified (REVERIFY/STALE prior_value), else the proposed value (a fresh DRAFT)."""
    if state.value is not None:
        return state.value
    if state.prior_value is not None:
        return state.prior_value
    proposed = _latest_proposed(stream)
    return proposed.payload.get("proposed_value") if proposed else None


def _column_hidden(conn: DbConn, source: str, table: str | None, column: str | None,
                   allowed: list[str]) -> bool:
    """READ-SCOPE (audit finding [1], the E2 existence-oracle + write-bypass fix): True iff the
    subject/target column's ``graph_node`` row EXISTS and carries a sensitivity these roles can't see
    (:func:`allowed_sensitivities`) — the SAME fail-closed scope the F0 asset-detail + F3
    field-correction peers apply, so this surface can never list or mutate a governed binding on a
    column those peers 404. The public-flattened ``public.<table>.<column>`` object_ref is the form
    ``build_graph`` writes (mirrors ``semantic_bindings.projection._col_endpoint``).

    A column with NO graph_node row is NOT hidden: the sensitivity tag lives ONLY on graph_node, so a
    hidden PII column ALWAYS has a row (with its 'pii'/'restricted' tag) and is dropped — while a
    column with no row carries nothing sensitivity-restricted to leak (mirrors
    ``readiness._read_scoped_refs``'s KEEP-if-absent rule), so a binding proposed before/without its
    subject being ingested stays listable rather than being over-dropped."""
    if not table or not column:
        return False
    row = conn.execute(
        "SELECT sensitivity FROM graph_node WHERE catalog_source = %s AND object_ref = %s "
        "AND kind = 'column'",
        (_norm(source), f"public.{_norm(table)}.{_norm(column)}")).fetchone()
    if row is None:
        return False
    sensitivity = row[0]
    return sensitivity is not None and sensitivity not in allowed


def _build_view(conn: DbConn, key: str, want_source: str,
                allowed: list[str] | None) -> dict | None:
    """ONE binding view for ``key``, or None when it is filtered (another source, terminal REJECTED,
    or — when read-scoped: ``allowed`` is a (possibly empty) sensitivity list, not ``None`` — a
    subject/currency-target column that fails the fail-closed scope) or structurally corrupt (empty
    stream / undecodable / non-semantic ref). ``allowed`` is ``None`` for the UNSCOPED legacy path."""
    stream = load_fact(conn, key)
    if not stream:
        counters.incr("overlay.semantic_binding_governance.stream_empty")
        return None
    payload0 = stream[0].payload
    if payload0.get("fact_type") not in _SEMANTIC_FACT_TYPES:
        return None
    try:
        ref = _ref_from_payload(payload0["catalog_object_ref"])
    except Exception:  # noqa: BLE001 — a corrupt DRAFT payload skips this fact, not the queue
        counters.incr("overlay.semantic_binding_governance.ref_undecodable")
        logger.warning("semantic-binding governance: fact %s ref undecodable — skipped", key,
                       exc_info=True)
        return None
    if not isinstance(ref, CatalogObjectRef) or not ref.catalog_source:
        counters.incr("overlay.semantic_binding_governance.ref_not_semantic")
        logger.warning("semantic-binding governance: fact %s is not a sourced column ref — skipped",
                       key)
        return None
    if _norm(ref.catalog_source) != want_source:
        return None  # another catalog's binding — filtered, not an error
    state = fold_overlay_state(stream)
    status = state.status
    if status not in _PENDING_STATUSES and status != "VERIFIED":
        return None  # REJECTED / None — not listable (terminal / empty)
    # READ-SCOPE (finding [1]): a binding on a sensitivity-hidden SUBJECT column is DROPPED — absent
    # from the list with no count/id/existence leak, indistinguishable from a binding that isn't there.
    if allowed is not None and _column_hidden(conn, want_source, ref.table, ref.column, allowed):
        return None
    fact_type = payload0["fact_type"]
    effective = _effective_value(state, stream)
    subject = {"schema": ref.schema, "table": ref.table, "column": ref.column}
    target = None
    entity_id = None
    if fact_type == CURRENCY_BINDING and isinstance(effective, Mapping):
        cc = effective.get("currency_column") or {}
        # A currency binding also NAMES its currency-target column — drop the whole binding if that
        # target column is hidden, so no hidden column leaks via the target ref either.
        if allowed is not None and _column_hidden(conn, want_source, cc.get("table"),
                                                  cc.get("column"), allowed):
            return None
        target = {"schema": cc.get("schema"), "table": cc.get("table"), "column": cc.get("column")}
    elif fact_type == ENTITY_ASSIGNMENT and isinstance(effective, Mapping):
        entity_id = effective.get("entity_id")
    provenance = _read_candidate_provenance(conn, key)
    return {
        "fact_key": key,
        "binding_kind": fact_type,
        "status": _DISPLAY_STATUS.get(status, status),
        "subject": subject,
        "target": target,               # currency column (currency_binding) or None
        "entity_id": entity_id,          # governed entity (entity_assignment) or None
        "value": effective,
        "prior_value": state.prior_value,
        "target_event_id": _cas_target(state),
        "reason_codes": provenance.get("reason_codes", []),
        "evidence": provenance.get("evidence", {}),
        "disposition": provenance.get("disposition"),
        "candidate_id": provenance.get("candidate_id"),
        "candidate_set_id": provenance.get("candidate_set_id"),
        "ingestion_run_id": provenance.get("ingestion_run_id"),
        "attempt_no": provenance.get("attempt_no"),
        "reviewer_note": _latest_reviewer_note(stream),
        "available_actions": list(
            _ACTIONS_VERIFIED if status == "VERIFIED" else _ACTIONS_PENDING),
    }


def list_semantic_binding_proposals(conn: DbConn, source: str, *, limit: int = 100,
                                    roles: Iterable[str] | None = None) -> list[dict]:
    """A source's governed semantic bindings — pending (DRAFT/PARTIALLY_CONFIRMED/REVERIFY/STALE)
    AND VERIFIED — ONE view per ``fact_key``, newest first. See the module docstring for the view
    shape; ``available_actions`` tells the asset UI which of confirm/reject (pending) or reverify/
    withdraw/correct (VERIFIED) the server sanctions for the binding. ``limit`` is clamped to
    1..500. Bad data on one binding is skipped — it never aborts the list.

    READ-SCOPED (audit finding [1]): ``roles`` are the caller's role claims. When passed (the route
    always does), a binding whose SUBJECT (or currency-TARGET) column is sensitivity-hidden — its
    ``graph_node`` row absent or carrying a sensitivity these roles can't see (:func:`_column_hidden`,
    fail-closed like asset_detail's M-5) — is DROPPED, so a platform-admin without pii_reader never
    sees a governed binding on a PII column the F0/F3 peers already 404 (no count/id/existence leak).
    ``roles=None`` (the default) is the UNSCOPED legacy path (no filtering) — only internal callers /
    tests reach it; the route surface is always scoped.

    Enumerates from the ``overlay_proposal`` read model (one row per fact_key, carrying
    ``catalog_source`` + ``fact_type``); the projection is DRAINED to head first so a just-proposed
    / just-confirmed binding is not missed to projection lag (the confirm path appends on an
    uncommitted conn — mirrors the sibling projectors' drain). The drain first probes the 'overlay'
    checkpoint row NOWAIT (audit finding [9], gap (b)): an in-flight ingest holds that row FOR
    UPDATE to commit (across the D4/Pass-B LLM stages), and ``run_projection``'s plain FOR UPDATE
    would BLOCK this live governance GET behind the whole multi-minute ingest tx — a block, not an
    exception, so the best-effort except below never fired. On lock-unavailable the drain is
    SKIPPED and the list serves the documented possibly-stale read instead."""
    from featuregen.overlay.projection import OverlayProjection
    from featuregen.projections.runner import run_projection, try_lock_checkpoint_nowait

    limit = max(1, min(limit, _LIMIT_MAX))
    want = _norm(source)
    allowed = allowed_sensitivities(roles) if roles is not None else None
    try:  # bring the read model to head; a poison-halt just stops advancing (best-effort current)
        if try_lock_checkpoint_nowait(conn, "overlay"):
            while run_projection(conn, OverlayProjection()) >= 500:
                pass
        else:
            counters.incr("overlay.semantic_binding_governance.drain_skipped_lock")
            logger.warning("semantic-binding governance: overlay checkpoint lock held by an "
                           "in-flight ingest — listing without a drain (possibly-stale "
                           "overlay_proposal)")
    except Exception:  # noqa: BLE001 — the list is best-effort; a drain fault must not 500 the read
        counters.incr("overlay.semantic_binding_governance.drain_error")
        logger.warning("semantic-binding governance: overlay drain failed before list — reading "
                       "possibly-stale overlay_proposal", exc_info=True)
    rows = conn.execute(
        "SELECT fact_key, catalog_source FROM overlay_proposal "
        "WHERE fact_type IN ('entity_assignment', 'currency_binding') "
        "ORDER BY updated_seq DESC").fetchall()
    views: list[dict] = []
    for key, csource in rows:
        if key is None or _norm(csource) != want:
            continue
        try:
            view = _build_view(conn, key, want, allowed)
        except Exception:  # noqa: BLE001 — ONE corrupt binding must not abort the whole queue
            counters.incr("overlay.semantic_binding_governance.view_skipped")
            logger.warning("semantic-binding governance: view for fact %s unreadable — skipped",
                           key, exc_info=True)
            continue
        if view is not None:
            views.append(view)
        if len(views) >= limit:
            break
    return views


def caller_binding_actions(
    conn: DbConn, *, fact_key: str, actor: IdentityEnvelope | None
) -> dict:
    """The display status + the subset of the status-sanctioned actions THIS caller may execute for
    a semantic-binding ``fact_key`` — the READ-MODEL projection of the SAME owner-or-admin authz the
    execute paths enforce. Returns ``{"status": <display status or None>, "actions": [...]}``.

    Reuses the E2 status→actions mapping (:data:`_ACTIONS_VERIFIED` / :data:`_ACTIONS_PENDING`, the
    same ones :func:`_build_view` returns) gated on the E1 authority predicate
    (:func:`resolve_authority` + :func:`_actor_is_authority`, owner-or-admin) — it does NOT reinvent
    authz. A non-authority caller, a ``None`` actor (a caller who did not authenticate as a
    principal), a terminal/unlistable status, or an unloadable / non-semantic fact all yield
    ``actions=[]`` — so the asset UI can never advertise an edge as editable unless the server
    sanctions the command here.

    FAIL-SOFT (mirrors the peer read models): a missing catalog adapter or an undecodable ref is
    caught and degraded to ``actions=[]`` (a read-only view), never raised — the asset read must
    render the edge even when authority can't be resolved."""
    stream = load_fact(conn, fact_key)
    if not stream:
        return {"status": None, "actions": []}
    payload0 = stream[0].payload
    fact_type = payload0.get("fact_type")
    if fact_type not in _SEMANTIC_FACT_TYPES:
        return {"status": None, "actions": []}
    status = fold_overlay_state(stream).status
    display = _DISPLAY_STATUS.get(status, status)
    if status == "VERIFIED":
        sanctioned = _ACTIONS_VERIFIED
    elif status in _PENDING_STATUSES:
        sanctioned = _ACTIONS_PENDING
    else:
        return {"status": display, "actions": []}  # REJECTED / terminal — no editable actions
    if actor is None:
        return {"status": display, "actions": []}
    try:
        ref = _ref_from_payload(payload0["catalog_object_ref"])
        if not isinstance(ref, CatalogObjectRef):
            return {"status": display, "actions": []}
        authority = resolve_authority(conn, current_catalog_adapter(), ref, fact_type)
        authorized = _actor_is_authority(authority, actor)
    except Exception:  # noqa: BLE001 — no adapter / undecodable ref: fail-closed to no actions
        counters.incr("overlay.semantic_binding_governance.actions_unresolved")
        logger.warning("semantic-binding governance: caller actions for fact %s unresolved — "
                       "returning no actions", fact_key, exc_info=True)
        return {"status": display, "actions": []}
    return {"status": display, "actions": list(sanctioned) if authorized else []}


def load_semantic_binding_confirmation_context(conn: DbConn, fact_key: str, *,
                                                roles: Iterable[str] | None = None) -> dict:
    """The typed confirm/reject command args for ``fact_key``'s semantic-binding proposal:
    ``{ref, fact_type, use_case, target_event_id}`` (``use_case`` is always None — both types are
    data facts). Raises :class:`SemanticBindingGovernanceNotFound` when the stream is empty, the
    fact is not a governed semantic binding, or the DRAFT ref will not decode to a typed
    ``CatalogObjectRef``.

    READ-SCOPE (audit finding [1], WRITE half): when ``roles`` is passed (the route always does), a
    binding whose SUBJECT (or currency-TARGET) column is sensitivity-hidden from those roles raises
    :class:`SemanticBindingGovernanceNotFound` too — so confirm/correct/withdraw/reject/reverify 404
    BEFORE any command dispatch, mirroring field_correction's I-1 gate. A hidden column is then
    indistinguishable from a missing one on WRITE as well as read: no existence oracle, no
    governed-write bypass. ``roles=None`` (the internal reverify/withdraw/correct callers, reached
    only AFTER the route already scoped) preserves the unscoped bridge.

    ``target_event_id`` is ``_cas_target(state)`` — the EXACT id confirm/reject CAS against — never
    a raw stream head: under a re-verify cycle the CAS target is the cycle-stable prior
    ``confirmed_event_id``, so guessing ``stream[-1].event_id`` would 409 the second re-confirm."""
    stream = load_fact(conn, fact_key)
    if not stream:
        raise SemanticBindingGovernanceNotFound(f"no fact stream for {fact_key!r}")
    payload = stream[0].payload
    fact_type = payload.get("fact_type")
    if fact_type not in _SEMANTIC_FACT_TYPES:
        raise SemanticBindingGovernanceNotFound(
            f"fact {fact_key!r} is not an entity_assignment/currency_binding")
    try:
        ref = _ref_from_payload(payload["catalog_object_ref"])
    except Exception as exc:  # noqa: BLE001 — a corrupt DRAFT payload is a 404, never a 500
        raise SemanticBindingGovernanceNotFound(f"fact {fact_key!r} ref undecodable") from exc
    if not isinstance(ref, CatalogObjectRef):
        raise SemanticBindingGovernanceNotFound(f"fact {fact_key!r} ref is not a typed column ref")
    state = fold_overlay_state(stream)
    if roles is not None:
        allowed = allowed_sensitivities(roles)
        hidden = _column_hidden(conn, ref.catalog_source, ref.table, ref.column, allowed)
        if not hidden and fact_type == CURRENCY_BINDING:
            # A currency binding also WRITES a currency edge onto its target column — refuse if that
            # target is hidden, so a confirm can't project onto a column the caller can't see.
            value = _effective_value(state, stream)
            if isinstance(value, Mapping):
                cc = value.get("currency_column") or {}
                hidden = _column_hidden(conn, ref.catalog_source, cc.get("table"),
                                        cc.get("column"), allowed)
        if hidden:
            raise SemanticBindingGovernanceNotFound(
                f"fact {fact_key!r} is not visible to these roles")
    return {
        "ref": ref,
        "fact_type": fact_type,
        "use_case": None,
        "target_event_id": _cas_target(state),
    }


# ═════════════════ reverify / withdraw / correct on a VERIFIED binding ═══════════════════════════
# Never hand-write fact state: reuse the sanctioned expiry/reverify transition + the REAL overlay
# commands. Each action re-checks the E1 owner-or-admin authority + writes an audited deny for a
# non-authority (mirrors _deny_audited); four-eyes is preserved by `correct` re-proposing as the
# correcting human.


def _authority_denial(conn: DbConn, adapter, ref, fact_type: str, key: str,
                      actor: IdentityEnvelope, action: str) -> str | None:
    """None when ``actor`` is the E1 owner-or-admin authority for the fact; otherwise a denial
    reason — and a tamper-evident ``COMMAND_DENIED`` row is written on THIS connection (mirrors
    ``_deny_audited``: the caller RETURNS the denial so ``get_conn`` commits the audit trace)."""
    authority = resolve_authority(conn, adapter, ref, fact_type)
    if _actor_is_authority(authority, actor):
        return None
    reason = "actor is not the resolved authority for this fact"
    record_denial(
        conn,
        Command(action=action, aggregate="overlay_fact", aggregate_id=key, args={},
                actor=actor, idempotency_key=f"{action}:{key}:{actor.subject}"),
        reason)
    return reason


def _verified_or_denied(conn: DbConn, key: str, action: str) -> tuple[object | None, str | None]:
    """Fold ``key`` and require VERIFIED (the only state reverify/withdraw/correct apply to).
    Returns ``(state, None)`` when VERIFIED, else ``(None, reason)``."""
    state = fold_overlay_state(load_fact(conn, key))
    if state.status != "VERIFIED":
        return None, (f"binding is not VERIFIED (status={state.status}); {action} applies to a "
                      "VERIFIED binding")
    return state, None


def request_reverify(conn: DbConn, *, fact_key: str, actor: IdentityEnvelope) -> dict:
    """REVERIFY a VERIFIED binding: reopen a fresh re-verification cycle and demote the operational
    projection until an authorized human re-confirms. Reuses the sanctioned ``expiry._apply_expiry``
    transition (VERIFIED → REVERIFY, demote, open the re-verify task) — never hand-writes fact state.
    Fail-closed on authority + wrong-state.

    FOUR-EYES GUARANTEE (D+E review M-4 — precise wording): the re-confirmation is guarded by the
    platform's ``proposer ≠ confirmer`` rule ONLY. Reverify does NOT re-open a proposal, so it does
    not bind the requester as the new proposer — meaning the same admin who requested this reverify,
    or the human who originally confirmed, MAY re-confirm alone (only the ORIGINAL proposer is barred).
    To force a genuinely DIFFERENT human, route the change through ``correct_binding``, which
    re-proposes with the correcting human as proposer (``requires_distinct_confirmer``)."""
    ctx = load_semantic_binding_confirmation_context(conn, fact_key)
    adapter = current_catalog_adapter()
    denial = _authority_denial(conn, adapter, ctx["ref"], ctx["fact_type"], fact_key, actor,
                               "reverify_fact")
    if denial is not None:
        return {"accepted": False, "denied_reason": denial}
    state, wrong = _verified_or_denied(conn, fact_key, "reverify")
    if wrong is not None:
        return {"accepted": False, "denied_reason": wrong}
    from featuregen.overlay.expiry import _apply_expiry
    applied = _apply_expiry(conn, adapter, fact_key=fact_key,
                            confirmed_event_id=state.confirmed_event_id, actor=actor)
    if not applied:
        return {"accepted": False, "denied_reason": "reverify superseded: the binding advanced"}
    return {"accepted": True, "governance_status": "REVERIFY", "operational_projection": "demoted"}


def withdraw_binding(conn: DbConn, *, fact_key: str, actor: IdentityEnvelope, category: str,
                     note: str | None = None) -> dict:
    """WITHDRAW a VERIFIED binding: retire it → REJECTED and demote its operational projection
    (restore the file entity / demote the currency edge). Reopens the re-verify cycle
    (``_apply_expiry``) then dispatches the REAL ``reject_fact`` (which re-checks authority +
    demotes + retires the value's sticky fingerprint). Fail-closed on authority + wrong-state."""
    ctx = load_semantic_binding_confirmation_context(conn, fact_key)
    adapter = current_catalog_adapter()
    denial = _authority_denial(conn, adapter, ctx["ref"], ctx["fact_type"], fact_key, actor,
                               "reject_fact")
    if denial is not None:
        return {"accepted": False, "denied_reason": denial}
    state, wrong = _verified_or_denied(conn, fact_key, "withdraw")
    if wrong is not None:
        return {"accepted": False, "denied_reason": wrong}
    from featuregen.overlay.confirmation_commands import reject_fact
    from featuregen.overlay.expiry import _apply_expiry
    if not _apply_expiry(conn, adapter, fact_key=fact_key,
                         confirmed_event_id=state.confirmed_event_id, actor=actor):
        return {"accepted": False, "denied_reason": "withdraw superseded: the binding advanced"}
    reopened = fold_overlay_state(load_fact(conn, fact_key))   # REVERIFY
    result = reject_fact(conn, Command(
        action="reject_fact", aggregate="overlay_fact", aggregate_id=fact_key,
        args={"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
              "target_event_id": _cas_target(reopened), "reason": note, "category": category},
        actor=actor, idempotency_key=f"withdraw:{fact_key}:{actor.subject}", expected_version=None))
    if not result.accepted:
        return {"accepted": False, "denied_reason": result.denied_reason}
    return {"accepted": True, "governance_status": "REJECTED", "category": category,
            "operational_projection": "demoted"}


def correct_binding(conn: DbConn, *, fact_key: str, actor: IdentityEnvelope, value: dict,
                    note: str | None = None) -> dict:
    """CORRECT a VERIFIED binding: retire the prior value and open a NEW proposal for the corrected
    value — one requiring a DIFFERENT authorized human to confirm (four-eyes). The corrected value
    is VALIDATED against the E1 write gate BEFORE anything is retired, so a bad correction is atomic
    (nothing touched). Then: reopen the re-verify cycle (``_apply_expiry``), ``reject_fact``
    (→ REJECTED, demote, retire the old value's fingerprint), and ``propose_fact`` the corrected
    value with the CORRECTING human as the proposer — so ``proposer ≠ confirmer`` forces a distinct
    confirmer, and one principal can never propose AND approve one value. Fail-closed on authority +
    wrong-state + bad value."""
    ctx = load_semantic_binding_confirmation_context(conn, fact_key)
    adapter = current_catalog_adapter()
    ref, fact_type = ctx["ref"], ctx["fact_type"]
    denial = _authority_denial(conn, adapter, ref, fact_type, fact_key, actor, "propose_fact")
    if denial is not None:
        return {"accepted": False, "denied_reason": denial}
    state, wrong = _verified_or_denied(conn, fact_key, "correct")
    if wrong is not None:
        return {"accepted": False, "denied_reason": wrong}
    # Validate the corrected value up front (schema + E1 write gate) so a bad correction is atomic.
    from featuregen.overlay.facts import FactValidationError, validate_fact_value
    try:
        validate_fact_value(fact_type, value, use_case=None)
    except FactValidationError as exc:
        return {"accepted": False, "denied_reason": f"corrected value invalid: {exc}"}
    gate = join_write_error(ref, fact_type, value, None)
    if gate is not None:
        return {"accepted": False, "denied_reason": f"corrected value rejected: {gate}"}
    from featuregen.overlay.confirmation_commands import reject_fact
    from featuregen.overlay.expiry import _apply_expiry
    from featuregen.overlay.proposal_commands import propose_fact
    if not _apply_expiry(conn, adapter, fact_key=fact_key,
                         confirmed_event_id=state.confirmed_event_id, actor=actor):
        return {"accepted": False, "denied_reason": "correct superseded: the binding advanced"}
    reopened = fold_overlay_state(load_fact(conn, fact_key))   # REVERIFY
    retired = reject_fact(conn, Command(
        action="reject_fact", aggregate="overlay_fact", aggregate_id=fact_key,
        args={"ref": ref, "fact_type": fact_type, "use_case": ctx["use_case"],
              "target_event_id": _cas_target(reopened), "reason": note,
              "category": "superseded_by_correction"},
        actor=actor, idempotency_key=f"correct-retire:{fact_key}:{actor.subject}",
        expected_version=None))
    if not retired.accepted:
        return {"accepted": False,
                "denied_reason": f"correct could not retire the prior value: "
                                 f"{retired.denied_reason}"}
    proposed = propose_fact(conn, Command(
        action="propose_fact", aggregate="overlay_fact", aggregate_id=None,
        args={"ref": ref, "fact_type": fact_type, "proposed_value": value},
        actor=actor, idempotency_key=f"correct-propose:{fact_key}:{actor.subject}",
        expected_version=None))
    if not proposed.accepted:
        # The value passed the write gate above, so a denial here is exceptional (e.g. a sticky
        # fingerprint) — surface it; the request tx rolls back the retire so the binding is intact.
        return {"accepted": False, "rollback_required": True,
                "denied_reason": f"corrected proposal denied: {proposed.denied_reason}"}
    return {"accepted": True, "governance_status": "PROPOSED", "fact_key": proposed.aggregate_id,
            "proposed_event_id": proposed.produced_event_ids[0],
            "requires_distinct_confirmer": True, "operational_projection": "demoted"}
