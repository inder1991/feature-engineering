"""Table-fact governance read model + confirmation bridge (Pass B confirm surface, Task 1).

The single-confirmer sibling of `join_governance.py` for the governed `grain` /
`availability_time` facts Pass B proposes (`table_synth._propose_table_facts`).
`list_open_table_fact_proposals_governance` is a READ MODEL over the existing `human_tasks` gate
rows and the `overlay_fact` event stream (not a new queue): one view per `fact_key` of a source's
open (folded-DRAFT, displayed "PROPOSED") table-fact proposals. The table comes from the DRAFT
event's typed `CatalogObjectRef` (structural truth, never the display string); the origin is
stamped honestly ("llm_proposed_not_profiled" — Pass B is LLM synthesis, never profiler proof);
and the table's advisory fields (table_role/primary_entity/event_or_snapshot — LLM field
evidence, never governed facts) are read BEST-EFFORT for display, null on absence or any error.

FAILURE ISOLATION IS LOAD-BEARING (mirrors join_governance): one task whose proposal is
unreadable, whose ref will not decode, or is not a sourced table ref is SKIPPED with a warning +
counter — never raised. A single poisoned row must not take down the whole governance queue.

`load_table_fact_confirmation_context` turns a fact_key back into the typed command args a
confirm/reject route dispatches (fact_type-VALIDATED — a non-grain/availability fact_key raises
`TableFactGovernanceNotFound`, closing the generic-approval hole), and
`project_verified_table_fact` makes a just-VERIFIED grain/as-of operational SYNCHRONOUSLY
(drain-then-project onto graph_node on the request conn, fail-soft) instead of waiting for the
next re-upload's projector run.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime

from featuregen.contracts import DbConn
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.identity import CatalogObjectRef, _ref_from_payload
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.table_fact_projection import (
    _TABLE_FACT_TYPES,
    _WORKLIST_READER,
    project_table_facts_for_ref,
)
from featuregen.projections.runner import (
    projection_lag,
    run_projection,
    try_lock_checkpoint_nowait,
)
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)


class TableFactGovernanceNotFound(Exception):
    """`fact_key` does not name a loadable grain/availability_time proposal: its stream is empty,
    its DRAFT ref will not decode to a typed table `CatalogObjectRef`, or its `fact_type` is not a
    table fact. The confirm/reject routes (Task 2) map this to 404 — critically, BEFORE any event
    is written, so the table-fact surface can never be used to approve an arbitrary
    (join/policy) fact."""


_LIMIT_MAX = 500

# The honest provenance stamp: a Pass B grain/availability proposal is LLM synthesis over the
# uploaded schema — never a profiled/proven claim (table_fact_projection.uniqueness_basis).
_ORIGIN = "llm_proposed_not_profiled"

# The advisory table-level fields Pass B records as LLM field evidence
# (table_synth._ADVISORY_TABLE_FIELDS) — display-only context for the reviewer, NOT load-bearing.
_ADVISORY_FIELDS = ("table_role", "primary_entity", "event_or_snapshot")

# graph_node columns the projection verification reads, per fact type: (flag, provenance id).
# Closed internal map — never caller-supplied — so interpolating the column names is safe.
_PROJECTION_COLUMNS = {
    "grain": ("is_grain", "grain_fact_event_id"),
    "availability_time": ("is_as_of", "availability_fact_event_id"),
}


def _normalize_source(s: str) -> str:
    """Source comparison must match `identity._norm` / `object_ref.normalize_ref` lowercasing."""
    return s.strip().lower()


def _advisory(conn: DbConn, ref: CatalogObjectRef) -> dict:
    """The table's advisory fields for the review surface — BEST-EFFORT, display-only.

    Read from the ACTIVE field evidence on the table's logical_ref (latest active row wins,
    matching `read_active_field_evidence`'s created_at ordering). The ref's schema is the
    always-public `table_ref` schema, matching where technical uploads write the advisory
    evidence; a glossary table whose advisory evidence is keyed under a non-public schema simply
    shows nulls. ANY error yields all-null — advisory context must never break the queue."""
    empty = dict.fromkeys(_ADVISORY_FIELDS)
    try:
        from featuregen.overlay.field_evidence import read_active_field_evidence

        logical_ref = normalize_ref(ref.catalog_source, ref.schema, ref.table)
        out = dict(empty)
        for field_name in _ADVISORY_FIELDS:
            rows = read_active_field_evidence(conn, logical_ref, field_name)
            if rows:
                out[field_name] = rows[-1].proposed_value
        return out
    except Exception:  # noqa: BLE001 — advisory display data must never break the queue
        counters.incr("overlay.table_fact_governance.advisory_unreadable")
        logger.warning("table-fact governance: advisory fields unreadable for %s.%s — nulled",
                       ref.catalog_source, ref.table, exc_info=True)
        return empty


def _build_view(conn: DbConn, key: str, task_id: str, proposal: Mapping,
                want_source: str) -> dict | None:
    """ONE proposal view for `key`, or None when it is filtered (another source, not an open
    folded-DRAFT) or structurally corrupt (undecodable / non-table / source-less ref — those are
    counted + logged; a filter is silent)."""
    stream = load_fact(conn, key)
    if not stream:
        counters.incr("overlay.table_fact_governance.ref_undecodable")
        logger.warning("table-fact governance: fact %s has no event stream — skipped", key)
        return None
    try:
        ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
    except Exception:  # noqa: BLE001 — a corrupt DRAFT payload skips this fact, not the queue
        counters.incr("overlay.table_fact_governance.ref_undecodable")
        logger.warning("table-fact governance: fact %s ref undecodable — skipped", key,
                       exc_info=True)
        return None
    if not isinstance(ref, CatalogObjectRef) or not ref.catalog_source:
        counters.incr("overlay.table_fact_governance.ref_not_table")
        logger.warning("table-fact governance: fact %s is not a sourced table ref — skipped", key)
        return None
    if _normalize_source(ref.catalog_source) != want_source:
        return None  # another catalog's fact — filtered, not an error
    if fold_overlay_state(stream).status != "DRAFT":
        return None  # VERIFIED/REJECTED/REVERIFY/STALE — not an open proposal
    proposed_value = proposal["proposed_value"]
    return {
        "fact_key": key,
        "task_id": task_id,
        "target_event_id": proposal["target_event_id"],
        "fact_type": proposal["fact_type"],
        "table": ref.table,
        "proposed_value": proposed_value,
        "status": "PROPOSED",  # the surface vocabulary for a folded DRAFT
        "origin": _ORIGIN,
        "advisory": _advisory(conn, ref),
        "evidence_parse_status": "parsed" if isinstance(proposed_value, Mapping) else "missing",
    }


def list_open_table_fact_proposals_governance(conn: DbConn, source: str, *,
                                              limit: int = 100) -> list[dict]:
    """A source's open grain/availability_time proposals, ONE view per `fact_key`, newest first.

    Each view: ``fact_key``, ``task_id``, ``target_event_id``, ``fact_type``
    ("grain" | "availability_time"), ``table``, ``proposed_value``, ``status`` ("PROPOSED" — only
    folded-DRAFT proposals are listed), ``origin`` ("llm_proposed_not_profiled"), ``advisory``
    (``{table_role, primary_entity, event_or_snapshot}``, see :func:`_advisory`),
    ``evidence_parse_status`` ("parsed" | "missing"). ``limit`` is clamped to 1..500. Bad data on
    one task is skipped — it never aborts the list."""
    from featuregen.overlay._lifecycle import OverlayCommandError
    from featuregen.overlay.task_read import get_task_proposal  # mirrors table_fact_projection

    limit = max(1, min(limit, _LIMIT_MAX))
    want = _normalize_source(source)
    rows = conn.execute(
        "SELECT task_id, fact_key FROM human_tasks "
        "WHERE status = 'open' ORDER BY created_at DESC").fetchall()
    views: dict[str, dict] = {}  # fact_key -> view (insertion-ordered: newest task first)
    skipped: set[str] = set()    # fact_keys already adjudicated not-listable (filtered/corrupt)
    for task_id, key in rows:
        if key is None or key in skipped or key in views:
            continue
        try:
            proposal = get_task_proposal(conn, task_id, _WORKLIST_READER)
        except OverlayCommandError as exc:
            # get_task_proposal's authz denial (task_read.py: a subject-scoped data-owner task
            # the subject-less governance reader is not bound to) is a NORMAL "not my task" in
            # a mixed-catalog DB — a benign skip, NOT corruption: debug, no counter.
            if "not authorized" in str(exc):
                logger.debug("table-fact governance: task %s not readable by the governance "
                             "reader — skipped", task_id)
                continue
            counters.incr("overlay.table_fact_governance.task_unreadable")
            logger.warning("table-fact governance: task %s unreadable — skipped", task_id,
                           exc_info=True)
            continue
        except Exception:  # noqa: BLE001 — a task the reader can't read is skipped, never fatal
            counters.incr("overlay.table_fact_governance.task_unreadable")
            logger.warning("table-fact governance: task %s unreadable — skipped", task_id,
                           exc_info=True)
            continue
        if proposal["fact_type"] not in _TABLE_FACT_TYPES:
            continue
        try:
            view = _build_view(conn, key, task_id, proposal, want)
        except Exception:  # noqa: BLE001 — ONE corrupt fact must not abort the whole queue
            counters.incr("overlay.table_fact_governance.proposal_skipped")
            logger.warning("table-fact governance: proposal for fact %s unreadable — skipped",
                           key, exc_info=True)
            skipped.add(key)
            continue
        if view is None:
            skipped.add(key)
            continue
        views[key] = view
    return list(views.values())[:limit]


def load_table_fact_confirmation_context(conn: DbConn, fact_key: str) -> dict:
    """The typed confirm/reject command args for `fact_key`'s grain/availability_time proposal:
    ``{ref, fact_type, use_case, target_event_id}`` (``use_case`` is always None — grain and
    availability_time are data facts). Raises :class:`TableFactGovernanceNotFound` when the
    stream is empty, the fact is not a table fact, or the DRAFT ref will not decode to a table
    `CatalogObjectRef`.

    ``target_event_id`` is `_cas_target(state)` — the EXACT id `confirm_fact`/`reject_fact` CAS
    against (confirmation_commands.py) — never a raw stream head: under re-verification the CAS
    target is the prior `confirmed_event_id`, not the latest event."""
    stream = load_fact(conn, fact_key)
    if not stream:
        raise TableFactGovernanceNotFound(f"no fact stream for {fact_key!r}")
    payload = stream[0].payload
    fact_type = payload.get("fact_type")
    if fact_type not in _TABLE_FACT_TYPES:
        raise TableFactGovernanceNotFound(f"fact {fact_key!r} is not a grain/availability fact")
    try:
        ref = _ref_from_payload(payload["catalog_object_ref"])
    except Exception as exc:  # noqa: BLE001 — a corrupt DRAFT payload is a 404, never a 500
        raise TableFactGovernanceNotFound(f"fact {fact_key!r} ref undecodable") from exc
    if not isinstance(ref, CatalogObjectRef):
        raise TableFactGovernanceNotFound(f"fact {fact_key!r} ref is not a typed table ref")
    return {
        "ref": ref,
        "fact_type": fact_type,
        "use_case": None,
        "target_event_id": _cas_target(fold_overlay_state(stream)),
    }


def project_verified_table_fact(conn: DbConn, source: str, ref, fact_type: str, *,
                                now: datetime | None) -> str:
    """SYNCHRONOUSLY project a just-VERIFIED grain/availability_time onto `graph_node` — the
    confirm route's no-re-upload-needed step. Returns ``"projected"`` (the flag actually landed)
    or ``"pending"`` (deferred to the next caught-up ingest re-projection). NEVER raises — the
    fact stream stays VERIFIED regardless.

    DRAIN-then-project, mirroring `join_governance.project_verified_join`: the caller's
    `confirm_fact` has JUST appended OVERLAY_FACT_CONFIRMED in the SAME uncommitted request
    transaction, so the async projector's checkpoint is behind head here and `resolve_fact`'s
    `overlay_fact_state` read model lacks the just-VERIFIED row. Draining on THIS conn brings the
    read model to head inside the request transaction; the residual lag check then only fires when
    the drain poison-HALTED short of head, where projecting could serve a stale status — defer
    instead of projecting a lie. Everything runs inside a savepoint so a fault cannot poison the
    caller's transaction, and any exception is fail-soft.

    The declared sets are EMPTY by design: a table with a Pass B grain/availability proposal has
    no file-declared grain/as-of for that key (Pass B skips when a VERIFIED claim governs it), so
    there is nothing for the clear-then-set projection to spare.

    HONEST REPORTING: "projected" is claimed ONLY when a `graph_node` column of ``(source,
    table)`` actually carries the flag (`is_grain` / `is_as_of`) WITH its fact-event provenance id
    after the projector ran. `project_table_facts_for_ref` -> `resolve_fact` can CORRECTLY refuse
    to serve the just-VERIFIED fact — most commonly the drift-freshness guard on a stale source
    watermark (an admin approving hours after the upload) — in which case no flag lands. That
    refusal must stand (never launder the watermark); the return value reports the deferral
    honestly: the next fresh-watermark ingest re-projection makes the fact operational."""
    columns = _PROJECTION_COLUMNS.get(fact_type)
    if columns is None:
        counters.incr("overlay.table_fact_governance.projection_unknown_fact_type")
        logger.warning("table-fact governance: cannot project fact_type %r — returning pending",
                       fact_type)
        return "pending"
    flag_col, event_col = columns
    try:
        with conn.transaction():   # savepoint: a projection fault must not roll back the confirm
            if not try_lock_checkpoint_nowait(conn, "overlay"):
                # A concurrent ingest holds the 'overlay' checkpoint row to commit (its in-tx drain
                # across the D4/Pass-B LLM stages) — draining here would BLOCK the confirm behind the
                # whole multi-minute ingest tx (audit finding [9]). Defer to the same fail-closed
                # projection-lag path: the fact stays VERIFIED and the next caught-up ingest
                # reproject makes the grain/as-of operational.
                counters.incr("overlay.table_fact_governance.projection_skipped_lock")
                logger.warning("table-fact governance: overlay checkpoint lock held by an in-flight "
                               "ingest — deferring projection of a verified %s in %r to the next "
                               "caught-up ingest", fact_type, source)
                return "pending"
            while run_projection(conn, OverlayProjection()) >= 500:
                pass               # one pass caps at 500 events — loop until caught up
            if projection_lag(conn, "overlay") != 0:
                # The drain reached a poison-HALT, not head: the read model may still be stale.
                counters.incr("overlay.table_fact_governance.projection_skipped_lag")
                logger.warning("table-fact governance: overlay projection lags after drain — "
                               "deferring projection of a verified %s in %r to the next "
                               "caught-up ingest", fact_type, source)
                return "pending"
            # only_fact_type scopes the clear-then-set to the JUST-CONFIRMED type (whole-branch
            # review FIX 1): confirming a grain must never clear a file-declared as_of (or vice
            # versa) — under a stale drift watermark resolve_fact refuses to re-serve the other
            # fact, so an unscoped clear would silently wipe it until the next re-upload.
            project_table_facts_for_ref(conn, source=source, table=ref.table,
                                        declared_grain=set(), declared_as_of=set(),
                                        only_fact_type=fact_type, now=now)
            # flag_col/event_col come from the closed _PROJECTION_COLUMNS map above, never input.
            row = conn.execute(
                f"SELECT 1 FROM graph_node WHERE catalog_source = %s AND table_name = %s"
                f" AND kind = 'column' AND {flag_col} = true AND {event_col} IS NOT NULL",
                (source, ref.table)).fetchone()
        if row is None:
            # resolve_fact refused to serve the fact (stale drift watermark, demotion, expiry) or
            # the column nodes are absent: no flag landed, so the fact is NOT operational.
            counters.incr("overlay.table_fact_governance.projection_deferred_unserved")
            logger.warning("table-fact governance: verified %s on %r.%s not servable at "
                           "projection time (no flag written) — returning pending",
                           fact_type, source, ref.table)
            return "pending"
        return "projected"
    except Exception:  # noqa: BLE001 — fail-soft: the fact stays VERIFIED; ingest re-projects
        counters.incr("overlay.table_fact_governance.projection_error")
        logger.warning("table-fact governance: synchronous verified-%s projection failed for "
                       "%r.%s — fact intact, returning pending", fact_type, source, ref.table,
                       exc_info=True)
        return "pending"
