"""Join-governance read model + approval-stream reader (confirmation surface, Tasks 3+4).

`list_open_approved_join_proposals` is a READ MODEL over the existing `human_tasks` gate rows and
the `overlay_fact` event stream (not a new queue) — it lists one view PER `fact_key` of a source's
open discovered-join proposals. A dual join opens TWO side-labelled platform-admin tasks
(`propose_fact` via `authority.task_assignees`); they collapse into ONE proposal accumulating both
task rows. from/to/cardinality come from the DRAFT event's typed `ApprovedJoinRef`
(`payload["catalog_object_ref"]` — structural truth, never the display string), the reviewer
evidence is shaped TOLERANTLY out of the pre-minted Pass C evidence row
(`metric_values = asdict(JoinCandidateEvidenceV1)`, passc/propose.py), and the approval state
(who confirmed, with which Task-1 `note`) is folded off the PARTIALLY_CONFIRMED/CONFIRMED events.

FAILURE ISOLATION IS LOAD-BEARING: one task whose proposal is unreadable, whose ref will not
decode, is not an `ApprovedJoinRef`, or has no source is SKIPPED with a warning + counter —
never raised. A single poisoned row must not take down the whole governance queue.

Task 4 adds the confirm/reject bridge: `load_join_confirmation_context` turns a fact_key back into
the typed command args a confirm/reject route dispatches (fact_type-VALIDATED — a non-join
fact_key raises `JoinGovernanceNotFound`, closing the generic-approval hole), and
`project_verified_join` makes a just-VERIFIED join operational SYNCHRONOUSLY (lag-guarded,
fail-soft) instead of waiting for the next re-upload's projector run.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay import facts
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.identity import ApprovedJoinRef, _ref_from_payload
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.passc.projection import project_confirmed_joins
from featuregen.projections.runner import projection_lag
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)


class JoinGovernanceNotFound(Exception):
    """`fact_key` does not name a loadable approved_join proposal: its stream is empty, its DRAFT
    ref will not decode to a typed `ApprovedJoinRef`, or its `fact_type` is not "approved_join".
    The confirm/reject routes (Task 5) map this to 404 — critically, BEFORE any event is written,
    so the join surface can never be used to approve an arbitrary (grain/as-of/policy) fact."""

# Mirrors table_fact_projection._WORKLIST_READER: both sides of an upload-context join route to
# the platform-admin governance queue (UploadContextAdapter.owner_of -> None), and
# get_task_proposal authorizes on role_claims — the reader MUST hold platform-admin or every
# read is denied. Subject-less system reader.
_READER = IdentityEnvelope(
    subject="system:join-governance", actor_kind="service", authenticated=True,
    auth_method="internal", role_claims=("platform-admin",))

# Folded stream statuses the queue lists, mapped to the surface vocabulary: a folded DRAFT is
# displayed as "PROPOSED". Everything else (VERIFIED/REJECTED/REVERIFY/STALE) is not an open
# proposal and is excluded.
_DISPLAY_STATUS = {"DRAFT": "PROPOSED", "PARTIALLY_CONFIRMED": "PARTIALLY_CONFIRMED"}

_LIMIT_MAX = 500

# Reviewer-evidence fields shaped out of Evidence.metric_values (= asdict(JoinCandidateEvidenceV1)):
# (output key, metric_values key, expected type, default factory). A missing/None field is
# defaulted + warned ("partial"); a present field of the WRONG type marks the evidence "invalid".
_EVIDENCE_FIELDS = (
    ("score", "score", int, lambda: None),
    ("positive_signals", "positive_signals", list, list),
    ("negative_signals", "negative_signals", list, list),
    ("namespace_compatibility", "namespace_compatibility", str, lambda: None),
    ("namespace_reason_codes", "namespace_reason_codes", list, list),
    ("grain_status", "cardinality_status", str, lambda: None),
    ("grain_evidence", "grain_evidence", list, list),
    ("explanation", "explanation", str, str),
)


def _normalize_source(s: str) -> str:
    """Source comparison must match `identity._norm` / `object_ref.normalize_ref` lowercasing."""
    return s.strip().lower()


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _shape_evidence(evidence) -> tuple[dict, str | None, str]:
    """Shape a TaskProposal's `evidence` (an `Evidence` record or None) for the review surface.

    Returns ``(evidence_dict, evidence_version, parse_status)`` and NEVER raises — the queue must
    render a proposal whose evidence is absent or malformed. ``parse_status``: "parsed" (every
    field present), "partial" (some defaulted — each noted in ``evidence_dict["warnings"]``),
    "missing" (no evidence record / no metric_values), "invalid" (wrong-typed / unreadable shape).
    ``evidence_version`` is the record's ``profile_version`` (Pass C stamps ALGORITHM_VERSION)."""
    if evidence is None:
        return {}, None, "missing"
    try:
        version = getattr(evidence, "profile_version", None)
        metric_values = getattr(evidence, "metric_values", None)
        if not metric_values:
            return {}, version, "missing"
        if not isinstance(metric_values, Mapping):
            return {}, version, "invalid"
        shaped: dict = {}
        warnings: list[str] = []
        for out_key, mv_key, expected, default in _EVIDENCE_FIELDS:
            value = metric_values.get(mv_key)
            if value is None:
                shaped[out_key] = default()
                warnings.append(f"evidence field {mv_key!r} missing — defaulted")
            elif not isinstance(value, expected):
                return {}, version, "invalid"
            else:
                shaped[out_key] = value
        shaped["warnings"] = warnings
        return shaped, version, ("parsed" if not warnings else "partial")
    except Exception:  # noqa: BLE001 — evidence shape must never break the queue
        counters.incr("overlay.join_governance.evidence_shape_error")
        logger.warning("join governance: unreadable evidence record — marked invalid",
                       exc_info=True)
        return {}, None, "invalid"


def _approvals_from_stream(stream) -> list[dict]:
    """The CURRENT confirmation cycle's approvals folded off one fact stream.

    One entry per PARTIALLY_CONFIRMED (`by_owner`/`role`/Task-1 `note`); the CONFIRMED event adds
    any confirmer not already recorded (its `note` belongs to the event's ACTOR — the second
    confirmer; the first keeps the note from their own partial event). Mirrors the fold's cycle
    resets: a re-proposal after REJECTED and an EXPIRED/STALED demotion both start a fresh cycle,
    so prior-cycle approvals never leak onto a new decision (state.py resets partial_confirmers
    the same way)."""
    approvals: list[dict] = []
    open_for_proposal = True  # a PROPOSED (re)opens a cycle only on an empty/REJECTED stream
    for event in stream:
        payload = event.payload
        if event.type == facts.OVERLAY_FACT_PROPOSED:
            if open_for_proposal:
                approvals = []
                open_for_proposal = False
        elif event.type == facts.OVERLAY_FACT_PARTIALLY_CONFIRMED:
            approvals.append({
                "subject": payload.get("by_owner"),
                "display_name": None,      # no identity directory yet — UI falls back to subject
                "role": payload.get("role"),
                "note": payload.get("note"),
                "confirmed_at": _iso(getattr(event, "occurred_at", None)),
            })
        elif event.type == facts.OVERLAY_FACT_CONFIRMED:
            seen = {a["subject"] for a in approvals}
            actor_subject = getattr(event.actor, "subject", None)
            for confirmer in payload.get("confirmers") or ():
                subject = confirmer.get("subject")
                if subject in seen:
                    continue  # already recorded from their own PARTIALLY_CONFIRMED (keeps note)
                approvals.append({
                    "subject": subject,
                    "display_name": None,
                    "role": confirmer.get("role"),
                    "note": payload.get("note") if subject == actor_subject else None,
                    "confirmed_at": _iso(getattr(event, "occurred_at", None)),
                })
        elif event.type == facts.OVERLAY_FACT_REJECTED:
            open_for_proposal = True
        elif event.type in (facts.OVERLAY_FACT_EXPIRED, facts.OVERLAY_FACT_STALED):
            approvals = []  # demotion: the next confirmation cycle starts clean
    return approvals


def read_join_approvals(conn: DbConn, fact_key: str) -> list[dict]:
    """Approvals recorded on `fact_key`'s stream (current cycle): a list of
    ``{subject, display_name, role, note, confirmed_at}``. Used by the open-proposal list and by
    the confirm routes for their response bodies (Task 5). Called STANDALONE by the API layer
    (outside the list's per-task guard), so it shares the module's fail-soft posture: a
    malformed/unexpected event yields a best-effort ``[]`` (warned + counted), never a raise."""
    try:
        return _approvals_from_stream(load_fact(conn, fact_key))
    except Exception:  # noqa: BLE001 — approvals are display data; never break a confirm response
        counters.incr("overlay.join_governance.approvals_unreadable")
        logger.warning("join governance: approvals for fact %s unreadable — returning []",
                       fact_key, exc_info=True)
        return []


def _build_view(conn, key: str, proposal: Mapping, want_source: str) -> dict | None:
    """ONE proposal view for `key`, or None when it is filtered (another source, not an open
    lifecycle status) or structurally corrupt (undecodable / non-join / source-less ref — those
    are counted + logged; a filter is silent)."""
    stream = load_fact(conn, key)
    if not stream:
        counters.incr("overlay.join_governance.ref_undecodable")
        logger.warning("join governance: fact %s has no event stream — skipped", key)
        return None
    try:
        ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
    except Exception:  # noqa: BLE001 — a corrupt DRAFT payload skips this fact, not the queue
        counters.incr("overlay.join_governance.ref_undecodable")
        logger.warning("join governance: fact %s ref undecodable — skipped", key, exc_info=True)
        return None
    if not isinstance(ref, ApprovedJoinRef) or not ref.from_ref.catalog_source:
        counters.incr("overlay.join_governance.ref_not_join")
        logger.warning("join governance: fact %s is not a sourced approved_join ref — skipped",
                       key)
        return None
    if _normalize_source(ref.from_ref.catalog_source) != want_source:
        return None  # another catalog's join — filtered, not an error
    display = _DISPLAY_STATUS.get(fold_overlay_state(stream).status)
    if display is None:
        return None  # VERIFIED/REJECTED/REVERIFY/STALE — not an open proposal
    evidence_dict, evidence_version, parse_status = _shape_evidence(proposal.get("evidence"))
    return {
        "fact_key": key,
        "tasks": [],  # the caller accumulates every side-task row for this fact_key
        "from": {"table": ref.from_ref.table, "column": ref.from_ref.column},
        "to": {"table": ref.to_ref.table, "column": ref.to_ref.column},
        "cardinality": ref.cardinality,
        # Derived from the DECODED ref (from -> to), never the evidence display string — the
        # direction can therefore never disagree with the from/to the approvers see.
        "proposed_direction": (f"{ref.from_ref.table}.{ref.from_ref.column}"
                               f" -> {ref.to_ref.table}.{ref.to_ref.column}"),
        "status": display,
        "approvals": _approvals_from_stream(stream),
        "evidence": evidence_dict,
        "evidence_version": evidence_version,
        "evidence_parse_status": parse_status,
    }


def list_open_approved_join_proposals(conn: DbConn, source: str, *, limit: int = 100) -> list[dict]:
    """A source's open discovered-join proposals, ONE view per `fact_key`, newest task first.

    Each view: ``fact_key``, ``tasks`` (every open side task: ``{task_id, side, status}``),
    ``from``/``to`` (``{table, column}``), ``cardinality``, ``proposed_direction``, ``status``
    ("PROPOSED" | "PARTIALLY_CONFIRMED"), ``approvals`` (see :func:`read_join_approvals`),
    ``evidence``/``evidence_version``/``evidence_parse_status`` (see :func:`_shape_evidence`).
    ``limit`` bounds the number of PROPOSALS (clamped to 1..500). Bad data on one task is
    skipped — it never aborts the list."""
    from featuregen.overlay._lifecycle import OverlayCommandError
    from featuregen.overlay.task_read import get_task_proposal  # mirrors table_fact_projection

    limit = max(1, min(limit, _LIMIT_MAX))
    want = _normalize_source(source)
    rows = conn.execute(
        "SELECT task_id, fact_key, eligible_assignees, status FROM human_tasks "
        "WHERE status = 'open' ORDER BY created_at DESC").fetchall()
    views: dict[str, dict] = {}  # fact_key -> view (insertion-ordered: newest task first)
    skipped: set[str] = set()    # fact_keys already adjudicated not-listable (filtered/corrupt)
    for task_id, key, eligible, task_status in rows:
        if key is None or key in skipped:
            continue
        # A non-dict eligible_assignees JSONB (a list/string) must corrupt ONE task, not the
        # whole loop — this read sits OUTSIDE the per-task guard, so coerce before access.
        eligible = eligible if isinstance(eligible, Mapping) else {}
        task_row = {"task_id": task_id, "side": eligible.get("side"),
                    "status": task_status}
        if key in views:
            views[key]["tasks"].append(task_row)  # the dual join's OTHER side task — dedup
            continue
        try:
            proposal = get_task_proposal(conn, task_id, _READER)
        except OverlayCommandError as exc:
            # get_task_proposal's authz denial (task_read.py: a subject-scoped data-owner task
            # the subject-less governance reader is not bound to) is a NORMAL "not my task" in
            # a mixed-catalog DB — a benign skip, NOT corruption: debug, no counter.
            if "not authorized" in str(exc):
                logger.debug("join governance: task %s not readable by the governance reader "
                             "— skipped", task_id)
                continue
            counters.incr("overlay.join_governance.task_unreadable")
            logger.warning("join governance: task %s unreadable — skipped", task_id,
                           exc_info=True)
            continue
        except Exception:  # noqa: BLE001 — a task the reader can't read is skipped, never fatal
            counters.incr("overlay.join_governance.task_unreadable")
            logger.warning("join governance: task %s unreadable — skipped", task_id,
                           exc_info=True)
            continue
        if proposal["fact_type"] != "approved_join":
            continue
        try:
            view = _build_view(conn, key, proposal, want)
        except Exception:  # noqa: BLE001 — ONE corrupt fact must not abort the whole queue
            counters.incr("overlay.join_governance.proposal_skipped")
            logger.warning("join governance: proposal for fact %s unreadable — skipped", key,
                           exc_info=True)
            skipped.add(key)
            continue
        if view is None:
            skipped.add(key)
            continue
        view["tasks"].append(task_row)
        views[key] = view
    return list(views.values())[:limit]


def load_join_confirmation_context(conn: DbConn, fact_key: str) -> dict:
    """The typed confirm/reject command args for `fact_key`'s approved_join proposal:
    ``{ref, fact_type, use_case, target_event_id}`` (``use_case`` is always None — approved_join
    is a data fact). Raises :class:`JoinGovernanceNotFound` when the stream is empty, the DRAFT
    ref will not decode to an `ApprovedJoinRef`, or the fact is not an approved_join.

    ``target_event_id`` is `_cas_target(state)` — the EXACT id `confirm_fact`/`reject_fact` CAS
    against (confirmation_commands.py) — never a raw stream head: for a re-verify cycle's second
    confirmer the CAS target is the cycle-stable prior `confirmed_event_id`, not the latest event,
    so guessing `stream[-1].event_id` would 409 every second re-confirm."""
    stream = load_fact(conn, fact_key)
    if not stream:
        raise JoinGovernanceNotFound(f"no fact stream for {fact_key!r}")
    payload = stream[0].payload
    if payload.get("fact_type") != "approved_join":
        raise JoinGovernanceNotFound(f"fact {fact_key!r} is not an approved_join")
    try:
        ref = _ref_from_payload(payload["catalog_object_ref"])
    except Exception as exc:  # noqa: BLE001 — a corrupt DRAFT payload is a 404, never a 500
        raise JoinGovernanceNotFound(f"fact {fact_key!r} ref undecodable") from exc
    if not isinstance(ref, ApprovedJoinRef):
        raise JoinGovernanceNotFound(f"fact {fact_key!r} ref is not a typed join ref")
    return {
        "ref": ref,
        "fact_type": "approved_join",
        "use_case": None,
        "target_event_id": _cas_target(fold_overlay_state(stream)),
    }


def project_verified_join(conn: DbConn, source: str, ref, *, now: datetime | None) -> str:
    """SYNCHRONOUSLY project a just-VERIFIED join onto `graph_edge` — the confirm route's
    no-re-upload-needed step. Returns ``"projected"`` (ran) or ``"pending"`` (deferred to the next
    caught-up ingest re-projection). NEVER raises — the fact stream stays VERIFIED regardless.

    Mirrors the ingest re-projection guards (ingest.py): `resolve_fact` reads the
    `overlay_fact_state` read model, so a lagging overlay projection could serve a stale status —
    defer instead of projecting a lie. The projector runs inside a savepoint so a fault cannot
    poison the caller's transaction, and any exception (lag check included) is fail-soft."""
    try:
        if projection_lag(conn, "overlay") != 0:
            counters.incr("overlay.join_governance.projection_skipped_lag")
            logger.warning("join governance: overlay projection lags — deferring projection of a "
                           "verified join in %r to the next caught-up ingest", source)
            return "pending"
        with conn.transaction():   # savepoint: a projection fault must not roll back the confirm
            project_confirmed_joins(conn, source=source, pairs=[ref], now=now)
        return "projected"
    except Exception:  # noqa: BLE001 — fail-soft: the fact stays VERIFIED; ingest re-projects
        counters.incr("overlay.join_governance.projection_error")
        logger.warning("join governance: synchronous verified-join projection failed for %r — "
                       "fact intact, returning pending", source, exc_info=True)
        return "pending"
