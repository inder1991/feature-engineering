"""Entity-bridge governance read model — the READ side of the confirmation surface.

Nine cross-catalog links sit PROPOSED on the live catalog and, before this, there was no way to
review them: no listing, no routes, no screen. This module supplies the listing.

**Why it cannot copy its siblings.** Every other governance listing enumerates from
``overlay_proposal``. ``projection.py:50`` deliberately short-circuits ``entity_bridge`` before that
insert — *"an entity_bridge fact is two-source; the single-catalog_source overlay read models don't
model it"* — so a bridge listing built the same way returns EMPTY, and an empty list reads as "no
bridges pending" when nine are. The exclusion is correct and is NOT to be "fixed"; the listing is
what has to change.

**What it enumerates, and why from two places.** ``cross_catalog_links`` already unions the VERIFIED
edge table with the candidate-evidence ledger and computes strength, ``type_basis`` and the grain
flags — this module WRAPS it rather than re-querying. But the ledger is not the governed population:

    W4 — ``bridge_propose`` returns early on an already-governed fact WITHOUT writing the evidence
    row (*"bridge propose no-op (already governed)"*). A bridge whose ledger write was skipped is
    absent from the ledger while being perfectly governed.

Enumerating the ledger alone would therefore hide a pending decision — precisely the failure this
surface exists to remove. So the key set is the UNION of the ledger and the **event stream**, which
is the authority for what is governed, and every field a reviewer needs to act (endpoints, proposer,
CAS target, status) is read from the fact's own payload. The ledger contributes ENRICHMENT only —
strength, type family, ``type_basis``, grain — and a bridge missing that enrichment is surfaced with
``evidence_present=False`` rather than dropped, so the discrepancy is reported instead of hidden.

**No drain (W3).** The sibling listings drain the overlay projection to head because their resolver
reads a read model that lags an in-transaction confirm. This one folds ``load_fact`` — the event
stream — so it already sees an uncommitted CONFIRMED. Adding a drain "for consistency" would
reintroduce the lag it is designed to avoid.

**Read-scoping is mandatory and lives HERE.** ``cross_catalog_links`` takes no ``roles``. Exposed
unchanged through a governance route it becomes an EXISTENCE ORACLE: a caller without ``pii_reader``
would learn the count, the ids and the existence of bridges on columns the asset surfaces already
404. A bridge names TWO columns, so BOTH endpoints are scoped and a hidden endpoint drops the WHOLE
row — absence, never a redacted row (the ``semantic_binding_governance`` rule).

The scope predicate is migration 1032's ``visible_requires <@ allowed``, not the raw ``sensitivity``
column: a business glossary attests no sensitivity, so a ``sensitivity``-only check leaves every
concept-derived restriction open — the exact defect 1032 closed on the deployed FTR catalog.

**``available_actions`` is server-decided.** The UI never computes four-eyes. It is the status's
sanctioned set minus what THIS caller may not do, using the same ``proposer_ne_confirmer`` predicate
the command layer enforces — a read-model projection of the write-side rule, never a second copy of
it.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from featuregen.contracts import DbConn
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.authority import proposer_ne_confirmer
from featuregen.overlay.facts import ENTITY_BRIDGE
from featuregen.overlay.identity import CatalogObjectRef, EntityBridgeRef, _norm, _ref_from_payload
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.cross_catalog_links import (
    LedgerEvidenceV1,
    cross_catalog_links,
    ledger_evidence_by_fact_key,
)
from featuregen.overlay.upload.read_scope import allowed_classes, visibility_predicate
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

_LIMIT_MAX = 500

#: Folded statuses this surface lists. REJECTED is terminal — there is no decision left to make —
#: and a ``None`` status means an empty stream. A folded DRAFT displays as "PROPOSED"; every other
#: listed status renders verbatim (mirrors the semantic-binding peer).
_PENDING_STATUSES = frozenset({"DRAFT", "PARTIALLY_CONFIRMED", "REVERIFY", "STALE"})
_DISPLAY_STATUS = {"DRAFT": "PROPOSED"}

#: The server-sanctioned actions per status — a PROJECTION of the write-side gate, never a second
#: copy of it. ``_lifecycle._AWAITING_CONFIRMATION`` is the set from which ``confirm_fact`` and
#: ``reject_fact`` accept a decision: DRAFT, PARTIALLY_CONFIRMED, REVERIFY and STALE. So a drifted
#: (STALE) or re-verifying bridge is genuinely still decidable and correctly offers both — the queue
#: is not inventing an action, it is reflecting one the commands already take.
_ACTIONS_PENDING = ("confirm", "reject")

#: VERIFIED offers NOTHING, and that is load-bearing rather than a gap.
#:
#: ``_AWAITING_CONFIRMATION`` deliberately EXCLUDES VERIFIED (*"it is replaced via its own re-verify
#: flow"*), so ``reject_fact`` DENIES a VERIFIED fact — ``confirmation_commands.py:245``. Offering
#: ``reject`` here would put a button in the queue that the server answers with a denial, which is
#: precisely what "the UI may not advertise a command the server does not sanction" forbids.
#:
#: The semantic-binding peer CAN offer reverify/withdraw/correct on VERIFIED because it first runs
#: the sanctioned expiry transition (``expiry._apply_expiry``, VERIFIED -> REVERIFY) and only then
#: dispatches the real command. Bridges have no such surface yet, and inventing one is a governance
#: decision with its own demote and blast-radius semantics — not something a read model may imply.
_ACTIONS_VERIFIED: tuple[str, ...] = ()


def _endpoint_hidden(conn: DbConn, ref: CatalogObjectRef, allowed: list[str],
                     memo: dict) -> bool:
    """READ-SCOPE: True iff this endpoint column's ``graph_node`` row EXISTS and requires a
    visibility class these roles do not hold (migration 1032's ``visible_requires <@ %s``, which
    folds BOTH the raw file tag and the governed concept-derived floor).

    A column with NO graph_node row is NOT hidden — the same KEEP-if-absent rule the peer read
    models apply: the visibility requirement lives only on ``graph_node``, so a hidden column ALWAYS
    has a row carrying it, while a column with no row carries nothing to leak. Over-dropping here
    would hide a bridge whose endpoint has not been (re)ingested, which is not a sensitivity fact.
    """
    source = _norm(ref.catalog_source)
    if not source or not ref.table or not ref.column:
        return False
    object_ref = f"{_norm(ref.schema) or 'public'}.{_norm(ref.table)}.{_norm(ref.column)}"
    cached = memo.get((source, object_ref))
    if cached is not None:
        return cached
    row = conn.execute(
        f"SELECT ({visibility_predicate()}) FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (allowed, source, object_ref)).fetchone()
    hidden = row is not None and not row[0]
    memo[(source, object_ref)] = hidden
    return hidden


def _bridge_ref(stream) -> EntityBridgeRef:
    """The typed bridge ref off the PROPOSED event, or raise. The ref — not the ledger — is the
    governed identity: it is what ``fact_key`` canonicalizes and what the confirm command replays."""
    ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
    if not isinstance(ref, EntityBridgeRef):
        raise TypeError(f"not an entity bridge ref: {type(ref).__name__}")
    return ref


def _stream_bridge_keys(conn: DbConn) -> list[str]:
    """Every governed ``entity_bridge`` fact_key, oldest proposal first.

    This is the W4 half of the enumeration: the event stream is what "governed" MEANS, and it does
    not depend on the ledger write that ``bridge_propose``'s early return skips."""
    return [r[0] for r in conn.execute(
        "SELECT overlay_fact_id, min(global_seq) AS seq FROM events "
        "WHERE aggregate = 'overlay_fact' AND type = 'OVERLAY_FACT_PROPOSED' "
        "  AND payload->>'fact_type' = %s AND overlay_fact_id IS NOT NULL "
        "GROUP BY overlay_fact_id ORDER BY seq", (ENTITY_BRIDGE,)).fetchall()]


def _ledger_by_fact_key(conn: DbConn) -> dict[str, LedgerEvidenceV1]:
    """The candidate ledger keyed by ``fact_key`` — the SECOND evidence source, and the reason
    ``evidence_present`` can be trusted.

    ``cross_catalog_links`` deliberately SUPPRESSES a link whose fact is not in an AVAILABLE
    lifecycle state — REJECTED, STALE or REVERIFY (*"a human's NO must still count"*, plus drift and
    a lapsed confirmation). REJECTED is terminal here and never listed, but STALE and REVERIFY still
    await a decision and ARE listed — and reading enrichment only through ``cross_catalog_links``
    would report those bridges as ``evidence_present=False`` when their ledger row is sitting right
    there. That would make the W4 discrepancy signal lie about perfectly ordinary drift/expiry
    states. It is also why this queue enumerates from the EVENT STREAM and not from the ranked
    links: what is DECIDABLE is a different question from what is AVAILABLE.

    Shared with ``cross_catalog_links`` rather than re-queried. The ledger's primary key is the
    ORDERED endpoint tuple while a bridge's identity is its orientation-free ``fact_key``, so one
    bridge can own two rows — and this used to key a dict comprehension straight off an unordered
    query, silently keeping whichever contradictory row came back last. A confirmer would then be
    shown evidence the ranked read model did not agree with, and shown it differently on the next
    read. :func:`ledger_evidence_by_fact_key` merges instead of picking."""
    return ledger_evidence_by_fact_key(conn)


def _evidence(link, ledger_row: LedgerEvidenceV1 | None) -> dict | None:
    """The derivation evidence for one bridge, from the ranked link when available and from the raw
    ledger row otherwise, or None when neither exists (the W4 case).

    ``strength`` is None on the ledger fallback rather than 0: 0 is a REAL rank (a link with no
    grain, no attestation and no confirmation scores exactly 0), so reporting it for an unranked
    bridge would be indistinguishable from the weakest genuine candidate."""
    if link is not None:
        return {"data_type_family": link.data_type_family, "type_basis": link.type_basis,
                "left_is_grain": bool(link.left_is_grain),
                "right_is_grain": bool(link.right_is_grain), "strength": link.strength}
    if ledger_row is not None:
        return {"data_type_family": ledger_row.data_type_family,
                "type_basis": ledger_row.type_basis,
                "left_is_grain": ledger_row.left_is_grain,
                "right_is_grain": ledger_row.right_is_grain, "strength": None}
    return None


def _ref_dict(ref: CatalogObjectRef) -> dict:
    return {"catalog_source": ref.catalog_source, "schema": ref.schema,
            "table": ref.table, "column": ref.column}


def _actions(status: str, stream, actor: IdentityEnvelope | None) -> list[str]:
    """The actions the SERVER sanctions for this caller. Four-eyes is projected from the write-side
    predicate (``proposer_ne_confirmer``), never recomputed: a proposer may not confirm their own
    fact, so ``confirm`` is withheld from them. ``actor=None`` is the unscoped internal path — the
    command layer still enforces four-eyes for real, this only decides what the UI may offer."""
    sanctioned = _ACTIONS_VERIFIED if status == "VERIFIED" else _ACTIONS_PENDING
    if actor is not None and not proposer_ne_confirmer(stream, actor):
        return [a for a in sanctioned if a != "confirm"]
    return list(sanctioned)


def _view(conn: DbConn, key: str, evidence: dict | None, allowed: list[str] | None, memo: dict,
          actor: IdentityEnvelope | None) -> dict | None:
    """ONE bridge view, or None when the bridge is filtered (terminal/empty status, or a
    read-scoped endpoint). Raises on structural corruption — the caller skips that row."""
    stream = load_fact(conn, key)
    if not stream:
        counters.incr("overlay.bridge_governance.stream_empty")
        return None
    payload0 = stream[0].payload
    if payload0.get("fact_type") != ENTITY_BRIDGE:
        return None  # another fact type sharing the enumeration — filtered, not an error
    ref = _bridge_ref(stream)
    state = fold_overlay_state(stream)
    status = state.status
    if status not in _PENDING_STATUSES and status != "VERIFIED":
        return None  # REJECTED / None — terminal or empty, no decision left
    # READ-SCOPE: a bridge naming a hidden column on EITHER side is DROPPED — absent from the list
    # with no count/id/existence leak, indistinguishable from a bridge that is not there.
    if allowed is not None and (_endpoint_hidden(conn, ref.left_ref, allowed, memo)
                                or _endpoint_hidden(conn, ref.right_ref, allowed, memo)):
        return None
    display = _DISPLAY_STATUS.get(status, status)
    return {
        "fact_key": key,
        "kind": ENTITY_BRIDGE,
        "status": display,
        "entity_id": ref.entity_id,
        "catalogs": [ref.left_ref.catalog_source, ref.right_ref.catalog_source],
        "left": _ref_dict(ref.left_ref),
        "right": _ref_dict(ref.right_ref),
        # Derivation evidence — enrichment, absent when W4's early return skipped the ledger row.
        "evidence_present": evidence is not None,
        "data_type_family": evidence["data_type_family"] if evidence else "",
        # `declared` means two glossary spreadsheets agreed the types match, NOT a read of the
        # physical schema. The confirmer is approving a materially different claim, so it is shown.
        "type_basis": evidence["type_basis"] if evidence else "",
        "left_is_grain": evidence["left_is_grain"] if evidence else False,
        "right_is_grain": evidence["right_is_grain"] if evidence else False,
        "strength": evidence["strength"] if evidence else None,
        "proposed_by": payload0.get("proposed_by"),
        "proposed_at": stream[0].occurred_at.isoformat() if stream[0].occurred_at else None,
        "target_event_id": _cas_target(state),
        "available_actions": _actions(status, stream, actor),
    }


def list_bridge_proposals(conn: DbConn, *, source: str | None = None, limit: int = 100,
                          roles: Iterable[str] | None = None,
                          actor: IdentityEnvelope | None = None) -> list[dict]:
    """Every governed entity bridge awaiting or holding a decision — PROPOSED / PARTIALLY_CONFIRMED
    / REVERIFY / STALE and VERIFIED — one view per ``fact_key``, oldest proposal first. See the
    module docstring for the shape and for why this cannot enumerate ``overlay_proposal``.

    ``source=None`` (the default) means EVERY catalog — the cross-catalog case the existing
    ``/sources/{source}/…`` routes cannot express. When a source IS given a bridge matches if
    EITHER endpoint is that source: ``EntityBridgeRef`` identity is unordered, so opening the FTR
    side must find the same bridge the CIB side does.

    ``roles`` are the caller's role claims. When passed (the route always does), a bridge either of
    whose endpoint columns is sensitivity-hidden for those roles is DROPPED — no count, id or
    existence leak. ``roles=None`` is the UNSCOPED internal path, reached only by tests and internal
    callers; ``roles=()`` is a REAL caller holding nothing and DOES scope.

    ``actor`` decides ``available_actions``: the proposer of a bridge is not offered ``confirm``
    (four-eyes). ``actor=None`` offers the status's full sanctioned set — the command layer enforces
    four-eyes regardless, this only governs what the UI may advertise.

    ``limit`` is clamped to 1..500. Bad data on one bridge is skipped — it never aborts the list."""
    limit = max(1, min(int(limit), _LIMIT_MAX))
    want = _norm(source) if source is not None else None
    allowed = allowed_classes(roles) if roles is not None else None
    # The RANKED links, wrapped rather than re-queried: strength, type family, type_basis and the
    # CURRENT-graph grain flags all come from here. Keyed by fact_key; a candidate never proposed as
    # a fact has nothing to govern and no CAS target, so it is not a governance row.
    try:
        links = {link.fact_key: link for link in cross_catalog_links(conn) if link.fact_key}
    except Exception:  # noqa: BLE001 — evidence is ENRICHMENT; losing it must not blank the queue
        counters.incr("overlay.bridge_governance.links_unreadable")
        logger.warning("bridge governance: cross_catalog_links unreadable — listing without "
                       "ranked derivation evidence", exc_info=True)
        links = {}
    try:
        ledger = _ledger_by_fact_key(conn)
    except Exception:  # noqa: BLE001 — same rule: no evidence is a degraded view, not a blank queue
        counters.incr("overlay.bridge_governance.ledger_unreadable")
        logger.warning("bridge governance: candidate ledger unreadable", exc_info=True)
        ledger = {}
    # W4: the governed population is the EVENT STREAM, not the ledger. Union so a bridge whose
    # ledger row was skipped by `bridge_propose`'s early return is still reviewable, and so a
    # ledger/edge row whose stream is missing is still ATTEMPTED (and counted when it is not).
    keys = list(_stream_bridge_keys(conn))
    seen = set(keys)
    keys += [k for k in (links | ledger) if k not in seen]

    memo: dict = {}
    views: list[dict] = []
    for key in keys:
        try:
            view = _view(conn, key, _evidence(links.get(key), ledger.get(key)), allowed, memo,
                         actor)
        except Exception:  # noqa: BLE001 — ONE corrupt bridge must not abort the whole queue
            counters.incr("overlay.bridge_governance.view_skipped")
            logger.warning("bridge governance: view for fact %s unreadable — skipped", key,
                           exc_info=True)
            continue
        if view is None:
            continue
        if want is not None and want not in {_norm(c) for c in view["catalogs"]}:
            continue  # neither endpoint is this catalog — filtered, not an error
        views.append(view)
        if len(views) >= limit:
            break
    return views


def load_bridge_context(conn: DbConn, fact_key: str,
                        roles: Iterable[str] | None = None, *,
                        include_settled: bool = False) -> dict | None:
    """The typed confirm/reject command args for one bridge ``fact_key``, or None.

    Returns ``{"ref": EntityBridgeRef, "fact_type", "use_case", "target_event_id", "status"}`` —
    everything the command layer needs, with ``target_event_id`` the EXACT event id confirm/reject
    CAS against (``_cas_target``), so a superseded decision is denied as stale rather than applied.

    None when the key names nothing, names a fact of ANOTHER type (the generic-approval hole: a
    fact_key for some other governed fact must not be confirmable through the bridge route), holds
    no live decision, is structurally corrupt, or — when ``roles`` is passed — names an endpoint
    this caller may not see. That last case matters: if the loader answered for a hidden endpoint,
    the listing's drop would be cosmetic, because a caller could confirm what they cannot see.

    ``include_settled`` additionally admits the terminal REJECTED state, for a caller that must tell
    "already settled" apart from "no such proposal". The BULK reject needs exactly that distinction:
    a replayed batch is idempotent, not eight failures, and folding it into the not-found answer
    would report a reviewer's double-click as a fleet of missing bridges. It relaxes ONLY the status
    gate — the fact_type check and read-scoping are applied identically below — so a settled bridge
    on a hidden endpoint stays invisible and this cannot become an existence oracle.
    """
    stream = load_fact(conn, fact_key)
    if not stream:
        return None
    payload0 = stream[0].payload
    if payload0.get("fact_type") != ENTITY_BRIDGE:
        return None
    try:
        ref = _bridge_ref(stream)
    except Exception:  # noqa: BLE001 — a corrupt payload is not a confirmable decision
        counters.incr("overlay.bridge_governance.ref_undecodable")
        logger.warning("bridge governance: fact %s ref undecodable", fact_key, exc_info=True)
        return None
    state = fold_overlay_state(stream)
    admitted = {*_PENDING_STATUSES, "VERIFIED", *(("REJECTED",) if include_settled else ())}
    if state.status not in admitted:
        return None
    if roles is not None:
        allowed = allowed_classes(roles)
        memo: dict = {}
        if (_endpoint_hidden(conn, ref.left_ref, allowed, memo)
                or _endpoint_hidden(conn, ref.right_ref, allowed, memo)):
            return None
    return {"ref": ref, "fact_type": ENTITY_BRIDGE, "use_case": payload0.get("use_case"),
            "target_event_id": _cas_target(state), "status": state.status}
