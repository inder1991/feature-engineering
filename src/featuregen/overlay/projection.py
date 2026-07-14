from __future__ import annotations

from collections.abc import Mapping

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn, EventEnvelope
from featuregen.overlay import facts
from featuregen.overlay.dependencies import fact_dependencies


def _catalog_source(payload: Mapping) -> str:
    """The catalog_source of a fact's referenced object(s), read from the STRUCTURED
    catalog_object_ref on the event (SP-1.5 Task 2). Single ref -> its catalog_source; an
    approved_join -> from_ref's — SP-1.5 disallows cross-catalog joins (F4), so from == to source."""
    cor = payload["catalog_object_ref"]
    if "from_ref" in cor:
        return cor["from_ref"]["catalog_source"]
    return cor["catalog_source"]


class OverlayProjection:
    """Fail-closed projection (Projection Protocol) maintaining three read models from the
    overlay_fact stream: overlay_fact_state (hot/VERIFIED merged-view), overlay_proposal (in-flight
    workflow detail), overlay_fact_dependency (general dependency index). Every write is guarded by
    `updated_seq` so out-of-order/replayed events never regress newer state (§3.6)."""

    name = "overlay"
    is_analytics = False

    def reset(self, conn: DbConn) -> None:
        conn.execute("TRUNCATE overlay_fact_state")
        conn.execute("TRUNCATE overlay_proposal")
        conn.execute("TRUNCATE overlay_fact_dependency")

    def apply(self, conn: DbConn, event: EventEnvelope) -> None:
        if event.aggregate != "overlay_fact":
            return
        seq = event.global_seq
        fk = event.aggregate_id
        payload = event.payload

        if event.type == facts.OVERLAY_FACT_PROPOSED:
            # 3B.2B: an entity_bridge fact is two-source; the single-catalog_source overlay read models
            # (overlay_proposal/_state/_dependency) don't model it and _catalog_source would KeyError on
            # its {entity_id,left_ref,right_ref} ref. The bridge lifecycle uses a direct fold
            # (bridge_projection), not this projection; later bridge events (CONFIRMED/EXPIRED/…) are
            # inherently no-ops here (no proposal/state row is ever created). Bridge drift/expire/stale
            # integration is deferred to 3B.3.
            if payload.get("fact_type") == "entity_bridge":
                return
            conn.execute(
                """
                INSERT INTO overlay_proposal
                    (fact_key, status, proposed_value, proposal_fingerprint, draft_event_id,
                     object_ref, catalog_source, fact_type, use_case, evidence_ref, updated_seq)
                VALUES (%s, 'DRAFT', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fact_key) DO UPDATE SET
                    status = 'DRAFT',
                    proposed_value = EXCLUDED.proposed_value,
                    proposal_fingerprint = EXCLUDED.proposal_fingerprint,
                    draft_event_id = EXCLUDED.draft_event_id,
                    object_ref = EXCLUDED.object_ref,
                    catalog_source = EXCLUDED.catalog_source,
                    fact_type = EXCLUDED.fact_type,
                    use_case = EXCLUDED.use_case,
                    evidence_ref = EXCLUDED.evidence_ref,
                    partial_confirmers = '[]',
                    target_event_id = NULL,
                    prior_value = NULL,
                    updated_seq = EXCLUDED.updated_seq
                WHERE overlay_proposal.updated_seq < EXCLUDED.updated_seq
                """,
                (
                    fk, Jsonb(payload["proposed_value"]), payload["proposal_fingerprint"],
                    event.event_id, payload["object_ref"], _catalog_source(payload),
                    payload["fact_type"], payload.get("use_case"), payload.get("evidence_ref"), seq,
                ),
            )
            # A fresh PROPOSED supersedes a terminal read-model state (M-9): propose_fact only
            # admits a re-propose over an empty stream or a REJECTED terminal, so when a once-
            # confirmed fact (overlay_fact_state row exists) is re-proposed, reset the row to the
            # new DRAFT — otherwise resolve_fact keeps reporting the stale REJECTED for a fact
            # whose workflow status is DRAFT. Fail-closed unchanged (a DRAFT never serves a value);
            # seq-guarded like every write here so replays never regress newer state. The next
            # CONFIRM's DO UPDATE restores VERIFIED with fresh value/confirmers.
            conn.execute(
                """
                UPDATE overlay_fact_state
                SET status = 'DRAFT', value = NULL, prior_value = NULL, expires_at = NULL,
                    updated_seq = %s
                WHERE fact_key = %s AND updated_seq < %s
                """,
                (seq, fk, seq),
            )
            # Refresh the dependency set on every (re)proposal: DELETE the fact's existing
            # rows first so a re-proposal after REJECTED — which may reference DIFFERENT columns —
            # never leaves stale dependency rows behind. Then insert the fresh set.
            csource = _catalog_source(payload)
            conn.execute(
                "DELETE FROM overlay_fact_dependency WHERE fact_key = %s", (fk,)
            )
            for dep_source, ref_object in fact_dependencies(
                payload["object_ref"], payload["fact_type"], payload["proposed_value"], csource
            ):
                conn.execute(
                    "INSERT INTO overlay_fact_dependency (fact_key, catalog_source, ref_object) "
                    "VALUES (%s, %s, %s) ON CONFLICT (fact_key, catalog_source, ref_object) "
                    "DO NOTHING",
                    (fk, dep_source, ref_object),
                )

        elif event.type == facts.OVERLAY_FACT_PARTIALLY_CONFIRMED:
            conn.execute(
                """
                UPDATE overlay_proposal
                SET status = 'PARTIALLY_CONFIRMED',
                    partial_confirmers = partial_confirmers || %s::jsonb,
                    updated_seq = %s
                WHERE fact_key = %s AND updated_seq < %s
                """,
                (Jsonb([{"subject": payload["by_owner"], "role": payload["role"]}]), seq, fk, seq),
            )

        elif event.type == facts.OVERLAY_FACT_CONFIRMED:
            conn.execute(
                """
                INSERT INTO overlay_fact_state
                    (fact_key, object_ref, catalog_source, fact_type, use_case, status, value,
                     confirmers, confirmed_at, expires_at, prior_value, confirmed_event_id,
                     updated_seq)
                SELECT %s, prop.object_ref, prop.catalog_source, prop.fact_type, prop.use_case,
                       'VERIFIED', %s, %s, %s, %s, NULL, %s, %s
                FROM overlay_proposal prop WHERE prop.fact_key = %s
                ON CONFLICT (fact_key) DO UPDATE SET
                    status = 'VERIFIED',
                    value = EXCLUDED.value,
                    confirmers = EXCLUDED.confirmers,
                    confirmed_at = EXCLUDED.confirmed_at,
                    expires_at = EXCLUDED.expires_at,
                    prior_value = NULL,
                    confirmed_event_id = EXCLUDED.confirmed_event_id,
                    updated_seq = EXCLUDED.updated_seq
                WHERE overlay_fact_state.updated_seq < EXCLUDED.updated_seq
                """,
                (
                    fk, Jsonb(payload["value"]), Jsonb(payload["confirmers"]),
                    event.occurred_at, payload.get("expires_at"), event.event_id, seq, fk,
                ),
            )
            conn.execute(
                "UPDATE overlay_proposal SET status = 'VERIFIED', target_event_id = %s, "
                "prior_value = NULL, updated_seq = %s WHERE fact_key = %s AND updated_seq < %s",
                (event.event_id, seq, fk, seq),
            )
            # Re-derive the dependency set from the AUTHORITATIVE (confirmed) value, not the
            # proposal: a human override can change the referenced columns away from the
            # proposed set, so the index must follow the confirmed value or catalog-change detection
            # watches the wrong columns. Idempotent for no-override/approved_join (reproduces the
            # PROPOSED set) and self-heals re-verify (EXPIRED/STALED carry no new PROPOSED).
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT object_ref, catalog_source, fact_type FROM overlay_proposal "
                    "WHERE fact_key = %s",
                    (fk,),
                )
                prop = cur.fetchone()
            if prop is not None:
                conn.execute(
                    "DELETE FROM overlay_fact_dependency WHERE fact_key = %s", (fk,)
                )
                for dep_source, ref_object in fact_dependencies(
                    prop["object_ref"], prop["fact_type"], payload["value"], prop["catalog_source"]
                ):
                    conn.execute(
                        "INSERT INTO overlay_fact_dependency (fact_key, catalog_source, ref_object) "
                        "VALUES (%s, %s, %s) ON CONFLICT (fact_key, catalog_source, ref_object) "
                        "DO NOTHING",
                        (fk, dep_source, ref_object),
                    )

        elif event.type == facts.OVERLAY_FACT_EXPIRED:
            conn.execute(
                "UPDATE overlay_fact_state SET status = 'REVERIFY', prior_value = value, "
                "value = NULL, expires_at = NULL, updated_seq = %s "
                "WHERE fact_key = %s AND updated_seq < %s",
                (seq, fk, seq),
            )
            # Carry the just-retired VERIFIED value into the in-flight proposal row (now read from
            # overlay_fact_state.prior_value, set above) and bind target_event_id to the confirmed
            # event being re-verified so the re-verify task / get_task_proposal can show prior_value
            # and CAS the re-confirm.
            conn.execute(
                "UPDATE overlay_proposal p SET status = 'REVERIFY', prior_value = s.prior_value, "
                "target_event_id = %s, updated_seq = %s "
                "FROM overlay_fact_state s "
                "WHERE p.fact_key = s.fact_key AND p.fact_key = %s AND p.updated_seq < %s",
                (payload["expires_confirmed_event_id"], seq, fk, seq),
            )

        elif event.type == facts.OVERLAY_FACT_STALED:
            conn.execute(
                "UPDATE overlay_fact_state SET status = 'STALE', prior_value = value, "
                "value = NULL, updated_seq = %s WHERE fact_key = %s AND updated_seq < %s",
                (seq, fk, seq),
            )
            conn.execute(
                "UPDATE overlay_proposal p SET status = 'STALE', prior_value = s.prior_value, "
                "target_event_id = %s, updated_seq = %s "
                "FROM overlay_fact_state s "
                "WHERE p.fact_key = s.fact_key AND p.fact_key = %s AND p.updated_seq < %s",
                (payload["stales_confirmed_event_id"], seq, fk, seq),
            )

        elif event.type == facts.OVERLAY_FACT_REJECTED:
            conn.execute(
                "UPDATE overlay_fact_state SET status = 'REJECTED', value = NULL, updated_seq = %s "
                "WHERE fact_key = %s AND updated_seq < %s",
                (seq, fk, seq),
            )
            conn.execute(
                "UPDATE overlay_proposal SET status = 'REJECTED', updated_seq = %s "
                "WHERE fact_key = %s AND updated_seq < %s",
                (seq, fk, seq),
            )


def current_fact(conn: DbConn, fact_key: str) -> dict | None:
    """The hot overlay_fact_state row for `fact_key` (VERIFIED / REVERIFY / STALE / REJECTED, or
    DRAFT after a once-confirmed fact is re-proposed), or None if the fact never reached a
    confirmed state. Drives the merged-view read path."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM overlay_fact_state WHERE fact_key = %s", (fact_key,))
        return cur.fetchone()


def read_proposal(conn: DbConn, fact_key: str) -> dict | None:
    """The in-flight overlay_proposal row (workflow/task detail) for `fact_key`, or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM overlay_proposal WHERE fact_key = %s", (fact_key,))
        return cur.fetchone()


def dependents_of(conn: DbConn, catalog_source: str, object_ref: str) -> list[str]:
    """fact_keys whose value references `(catalog_source, object_ref)` (reverse-reference index,
    §8). Source-qualified (SP-1.5 Task 2): drift in one catalog never stales another catalog's
    facts even when their objects share a `schema.table.column` name."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fact_key FROM overlay_fact_dependency "
            "WHERE catalog_source = %s AND ref_object = %s ORDER BY fact_key",
            (catalog_source, object_ref),
        )
        return [row[0] for row in cur.fetchall()]
